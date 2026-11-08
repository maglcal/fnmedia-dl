#!/usr/bin/env python3
"""分层自诊断（0.1.31）：一键定位「提示502」类问题的故障层。

覆盖三层故障面（每步 {"step","ok","ms","detail"}，单探针 5~15s 硬超时）：
  A 服务端配置面：服务版本 / 网关 socket / 上游 socket
  B 依赖服务连接面：DNS / 迅雷登录态 / 设备发现（含目标名关联）/
    设备 API 发现 / 设备插件探活（逐台并行，区分单台问题与全账号问题）
  C 数据链路面：中继目录表 / 下载根解析 / 历史任务反查 / api-pan 对照（含错误体）

设计原则：只读探测——不登录、不建目录、不建任务（下载根解析用空目录，
只查表不建级）；任何一步异常都不阻断后续步骤（哪层断了一眼可见）。
每步线程硬超时：上游挂起时诊断本身不能挂起（它就是要诊断挂起）。
0.1.33：探针期关闭设备超时 GET 重试（_device_timeout_retries=0）让真实
错误透出——0.1.32 实测重试链撑爆探针窗，真实错误被「超时」文案掩盖；
探针窗按步放大（发现 8s/目录表 12s/根解析 15s）。
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx

import config as cfgmod
from backends.drive_core import _dbg

STEP_TIMEOUT = 5.0          # 单步硬超时（秒）：线程 join 上限
DNS_HOSTS = ["api-pan.xunlei.com", "xluser-ssl.xunlei.com"]


def _probe(step: str, fn, timeout: float = STEP_TIMEOUT) -> dict:
    """跑一个探测步骤：计时 + 异常捕获 + 线程硬超时（timeout 可按步放大）。
    超时的线程是 daemon，留它自生自灭（探针只读，无副作用）。"""
    out: dict = {}

    def run():
        try:
            out["detail"] = str(fn())
        except Exception as exc:                      # noqa: BLE001 诊断面全捕获
            out["error"] = f"{type(exc).__name__}: {exc}"

    t0 = time.monotonic()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    ms = int((time.monotonic() - t0) * 1000)
    if th.is_alive():
        return {"step": step, "ok": False, "ms": ms,
                "detail": f"超时（>{timeout:.0f}s 无响应，上游挂起或网络阻塞）"}
    if "error" in out:
        return {"step": step, "ok": False, "ms": ms, "detail": out["error"]}
    return {"step": step, "ok": True, "ms": ms, "detail": out.get("detail", "")}


def _probe_dns() -> str:
    addrs = []
    for host in DNS_HOSTS:
        try:
            socket.getaddrinfo(host, 443)
            addrs.append(f"{host} ✓")
        except OSError as exc:
            addrs.append(f"{host} ✗ {exc}")
    return "；".join(addrs)


def _probe_gateway() -> str:
    import webui_gateway
    from config import REPO_ROOT
    gw = webui_gateway.gateway_socket_path(REPO_ROOT)
    app_sock = cfgmod.STATE_DIR / "app.sock"
    return (f"网关 socket {'存在' if gw.exists() else '缺失'}（{gw}）；"
            f"上游 socket {'存在' if app_sock.exists() else '缺失'}（{app_sock}）")


# 设备端插件无响应类错误的识别词（0.1.32 专用文案/错误体原文）
_PLUGIN_DOWN_KEYS = ("设备端响应超时", "远程调用失败", "error_code=2")
# 网络层不可达类错误的识别词（httpx 异常包装文案）
_NET_DOWN_KEYS = ("网络错误", "连接失败", "timed out", "timeout")


def _probe_one_plugin(core, target: str) -> tuple[bool, str]:
    """单台设备的插件通道单次探活（0.1.33，不重试）：
    发现设备 API → 打一发 relay device/config（经迅雷服务端中转到设备
    插件）。返回 (可达, 说明)。有响应但报错算「可达」——通道是活的，
    错误另记；只有设备端超时/网络层不可达才算「不可达」。"""
    try:
        api = core._relay_api(target)
    except Exception as exc:                      # noqa: BLE001 诊断面全捕获
        return False, f"设备API发现失败：{str(exc)[:60]}"
    try:
        core._request("GET", api["base"] + "device/config",
                      params=core._relay_params(api, target), timeout=5)
        return True, "插件响应正常"
    except Exception as exc:                      # noqa: BLE001
        msg = str(exc)
        if any(k in msg for k in _PLUGIN_DOWN_KEYS):
            return False, "插件无响应（设备端超时）"
        if any(k in msg.lower() for k in _NET_DOWN_KEYS):
            return False, f"不可达（{msg[:60]}）"
        return True, f"通道有响应（{msg[:60]}）"


def probe_devices_plugin(core, targets: list[dict],
                         active_target: str = "") -> str:
    """逐台设备插件通道探活（0.1.33）：并行 daemon 线程、单台 6s 窗、
    单次不重试——回答「是目标设备单台问题，还是全账号/网络级问题」
    （0.1.32 实测目标设备插件持续超时，但无从对比其他设备）。"""
    tgs = [t for t in (targets or []) if str(t.get("target") or "").strip()][:10]
    if not tgs:
        raise RuntimeError("无设备可探（先过「设备发现」步）")
    results: dict[str, tuple[bool, str]] = {}
    lock = threading.Lock()

    def work(t: dict) -> None:
        tg = str(t.get("target") or "")
        try:
            r = _probe_one_plugin(core, tg)
        except Exception as exc:                  # noqa: BLE001
            r = (False, f"探测异常：{exc}")
        with lock:
            results[tg] = r

    threads = [threading.Thread(target=work, args=(t,), daemon=True)
               for t in tgs]
    for th in threads:
        th.start()
    for th in threads:
        th.join(6.0)

    up: list[str] = []
    down: list[str] = []
    for t in tgs:
        tg = str(t.get("target") or "")
        name = str(t.get("name") or "?")[:14]
        ok, why = results.get(tg, (False, "超时（>6s 无响应）"))
        if ok:
            up.append(f"{name}✓")
        else:
            down.append(f"{name}✗（{why}）")
    parts = [f"可达 {len(up)}/{len(tgs)}：" + ("、".join(up) or "无")]
    if down:
        parts.append("不可达：" + "、".join(down))
    if active_target:
        r = results.get(active_target)
        mark = "✓ 插件可达" if r and r[0] else (f"✗ {r[1]}" if r
                                                else "✗ 不在探测列表")
        parts.append(f"派发目标（{active_target[:10]}…）{mark}")
    if not up:
        parts.append("结论：全部设备不可达——更可能是账号级/网络级问题，"
                     "或各设备的迅雷远程插件均未运行")
    elif (active_target in results
          and not results.get(active_target, (True,))[0]):
        # 0.1.34：按倒下比例措辞——1/10 可达时报「其他设备正常」是误导
        # （0.1.33 实测 9/10 不可达却被误报为单台问题）
        ups = "、".join(up)
        if len(down) * 2 <= len(tgs):
            parts.append(f"结论：仅 {len(down)}/{len(tgs)} 台不可达"
                         f"（含派发目标）——目标设备单台插件问题，可在设置里"
                         f"换 ✓ 设备（{ups}）为目标")
        else:
            parts.append(f"结论：{len(down)}/{len(tgs)} 台均不可达"
                         f"（含派发目标）——多数设备的迅雷远程插件未运行或"
                         f"链路不通，非单台问题；可达的仅 {ups}，建议换其中"
                         f"一台为派发目标，或到目标设备上启动/更新迅雷远程插件")
    else:
        parts.append("结论：设备插件通道整体可用")
    return "；".join(parts)


def run(version: str = "") -> dict:
    """执行全链路自诊断 → {"ok", "version", "steps": [...]}。
    ok = 关键步骤全绿（api-pan 对照是信息性步骤，不计入 ok）。"""
    cfg = cfgmod.load()
    steps: list[dict] = []

    # ---- A 服务端配置面
    steps.append(_probe("服务版本",
                        lambda: f"fnmedia-dl v{version or '未知'}"
                                f"（配置已加载，状态目录 {cfgmod.STATE_DIR}）"))
    steps.append(_probe("网关转发链", _probe_gateway))

    # ---- B 依赖服务连接面
    steps.append(_probe("DNS 解析", _probe_dns))

    mgr_backends = {}
    core = None
    target = (cfg.get("thunder_cloud", {}).get("target") or "").strip()
    all_targets: list[dict] = []

    def _get_pool():
        nonlocal mgr_backends
        if not mgr_backends:
            from downloader import DownloadManager
            mgr_backends = DownloadManager(cfg).backends()
        return mgr_backends

    def _get_core():
        nonlocal core
        if core is None:
            tc = _get_pool().get("thunder_cloud")
            if tc is None:
                raise RuntimeError("迅雷远程通道未加载")
            core = tc.core
            # 0.1.33：探针期关掉设备超时 GET 重试——重试链（~3s×3+退避）
            # 会撑爆探针窗口，真实错误被「超时」文案掩盖（0.1.32）
            if hasattr(core, "_device_timeout_retries"):
                core._device_timeout_retries = 0
        return core

    def probe_login():
        tc = _get_pool().get("thunder_cloud")
        if tc is None:
            raise RuntimeError("迅雷远程通道未加载")
        st = tc.status()
        if not st.get("reachable"):
            raise RuntimeError(st.get("detail") or "不可用")
        return st.get("detail") or "登录态正常"

    steps.append(_probe("迅雷登录态", probe_login))

    def probe_devices():
        nonlocal target, all_targets
        targets = _get_core().device_targets()
        all_targets = targets
        names = [f"{t.get('name') or '?'}({str(t.get('target'))[:10]}…)"
                 for t in targets]
        if not target and targets:
            target = str(targets[0].get("target") or "")
        # 目标名关联（0.1.32）：让「派发目标」可读——目标是哪台/什么类型
        # 直接决定设备插件是否可用（电脑端常无插件 → error_code=2）
        tname = next((str(t.get("name") or "?") for t in targets
                      if str(t.get("target") or "") == target), "")
        return f"发现 {len(targets)} 台：" + ("、".join(names) or "无") \
            + (f"；派发目标「{tname or '未在在线列表'}」（{target[:10]}…）"
               if target else "")

    steps.append(_probe("设备发现", probe_devices))

    relay_base = ""

    def probe_relay_api():
        nonlocal relay_base
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        api = _get_core()._relay_api(target)
        relay_base = api.get("base") or ""
        from urllib.parse import urlsplit
        host = urlsplit(relay_base).netloc
        return (f"设备 API 已发现：{host}；"
                f"plugin 凭据 {'有' if api.get('plugin_app_token') else '无'}")

    steps.append(_probe("设备 API 发现", probe_relay_api, timeout=8.0))

    # ---- B' 设备插件逐台探活（0.1.33，信息性）：单台问题 vs 全账号问题
    steps.append(_probe(
        "设备插件探活",
        lambda: probe_devices_plugin(_get_core(), all_targets, target),
        timeout=12.0))

    # ---- C 数据链路面
    def probe_folders():
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        # deadline 10s（0.1.33 放大）：重试已关，让降级链在 12s 线程窗内
        # 收口，真实错误不被超时掩盖
        deadline = time.monotonic() + 10.0
        files = _get_core()._folders(target, "", deadline=deadline)
        names = [str(f.get("name") or "?") for f in files[:5]]
        return f"顶层目录 {len(files)} 个：" + ("、".join(names) or "空表")

    steps.append(_probe("中继目录表", probe_folders, timeout=12.0))

    def probe_root():
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        # 空目录只解析下载根（查表不建级，只读安全）。0.1.33：直接走
        # _resolve_uncached 绕开正/负缓存——探针必须跑真实链路，且不污染
        # 负缓存（否则诊断后 30s 内正常派发被误短路）
        pid, path = _get_core()._resolve_uncached(
            target, "", time.monotonic() + 12.0)
        return f"下载根 id={str(pid)[:12]}… 路径={path or '（未提供）'}"

    steps.append(_probe("下载根解析", probe_root, timeout=15.0))

    def probe_harvest():
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        core = _get_core()
        tasks = core.list_tasks(target)
        hv = core._harvest_root(target, tasks)
        if hv:
            res = f"反查可用：id={str(hv[0])[:12]}… 路径={hv[1] or '（无）'}"
        else:
            res = "反查不可用（无历史任务带 parent_folder_id）"
        phases = [f"{str(t.get('name') or '?')[:14]}("
                  f"{str(t.get('phase') or '?').replace('PHASE_TYPE_', '')})"
                  for t in tasks[:3]]
        return (f"{res}；设备任务 {len(tasks)} 条，最近："
                + ("、".join(phases) or "无"))

    steps.append(_probe("历史任务反查", probe_harvest, timeout=8.0))

    # ---- 信息性对照：api-pan 对设备空间的行为（0.1.30 复核点）
    def probe_apipan():
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        tc = _get_pool().get("thunder_cloud")
        if tc is None:
            raise RuntimeError("迅雷远程通道未加载")
        filters = '{"kind":{"eq":"drive#folder"}}'
        try:
            resp = tc.client.request(
                "GET", "https://api-pan.xunlei.com/drive/v1/files",
                params={"space": target, "filters": filters, "limit": "5"},
                headers=tc.core._headers(), timeout=5)
        except httpx.HTTPError as exc:
            return (f"api-pan 不可达（{exc}）——目录表已走设备 API，"
                    f"此项仅作对照，不影响派发")
        if resp.status_code >= 400:
            extra = ""
            try:
                d = resp.json() or {}
                code = d.get("error_code") or d.get("errorCode") or "?"
                desc = (d.get("error_description") or d.get("errorDesc")
                        or d.get("error") or "")
                extra = f"，响应体 error_code={code} {desc}"
                if str(code) == "2" or "远程调用失败" in str(desc):
                    extra += "（与「设备 API 发现」同因：设备端插件响应超时）"
            except ValueError:
                pass
            return (f"HTTP {resp.status_code}（符合预期：api-pan /files 不服务"
                    f"设备空间，0.1.30——目录表须走设备 API）{extra}")
        try:
            n = len((resp.json() or {}).get("files") or [])
        except ValueError:
            n = -1
        return (f"HTTP 200（files={n}）：api-pan 竟可服务该空间——"
                f"设备 API 非必需，但当前实现优先走设备 API 不受影响")

    steps.append(_probe("api-pan 对照", probe_apipan, timeout=8.0))

    # ok 只看关键步骤（设备插件探活/历史任务反查/api-pan 对照是信息性步骤）
    info_steps = {"设备插件探活", "历史任务反查", "api-pan 对照"}
    ok = all(s["ok"] for s in steps if s["step"] not in info_steps)
    _dbg(f"自诊断: ok={ok} " + " ".join(f"{s['step']}={'✓' if s['ok'] else '✗'}"
                                       for s in steps))
    return {"ok": ok, "version": version, "steps": steps}


if __name__ == "__main__":                       # 本地手动排查入口
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=1))
