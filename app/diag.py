#!/usr/bin/env python3
"""分层自诊断（0.1.31）：一键定位「提示502」类问题的故障层。

覆盖三层故障面（每步 {"step","ok","ms","detail"}，单探针 5s 硬超时）：
  A 服务端配置面：服务版本 / 网关 socket / 上游 socket
  B 依赖服务连接面：DNS / 迅雷登录态 / 设备发现 / 设备 API 发现
  C 数据链路面：中继目录表 / 下载根解析 / api-pan 对照

设计原则：只读探测——不登录、不建目录、不建任务（下载根解析用空目录，
只查表不建级）；任何一步异常都不阻断后续步骤（哪层断了一眼可见）。
每步线程硬超时：上游挂起时诊断本身不能挂起（它就是要诊断挂起）。
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


def _probe(step: str, fn) -> dict:
    """跑一个探测步骤：计时 + 异常捕获 + 线程硬超时。
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
    th.join(STEP_TIMEOUT)
    ms = int((time.monotonic() - t0) * 1000)
    if th.is_alive():
        return {"step": step, "ok": False, "ms": ms,
                "detail": f"超时（>{STEP_TIMEOUT:.0f}s 无响应，上游挂起或网络阻塞）"}
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
        nonlocal target
        targets = _get_core().device_targets()
        names = [f"{t.get('name') or '?'}({str(t.get('target'))[:10]}…)"
                 for t in targets]
        if not target and targets:
            target = str(targets[0].get("target") or "")
        return f"发现 {len(targets)} 台：" + ("、".join(names) or "无") \
            + (f"；派发目标 {target[:10]}…" if target else "")

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

    steps.append(_probe("设备 API 发现", probe_relay_api))

    # ---- C 数据链路面
    def probe_folders():
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        deadline = time.monotonic() + 10.0
        files = _get_core()._folders(target, "", deadline=deadline)
        names = [str(f.get("name") or "?") for f in files[:5]]
        return f"顶层目录 {len(files)} 个：" + ("、".join(names) or "空表")

    steps.append(_probe("中继目录表", probe_folders))

    def probe_root():
        if not target:
            raise RuntimeError("无目标设备（先过「设备发现」步）")
        # 空目录只解析下载根（查表不建级，只读安全）
        pid, path = _get_core().resolve_dest_folder(target, "")
        return f"下载根 id={str(pid)[:12]}… 路径={path or '（未提供）'}"

    steps.append(_probe("下载根解析", probe_root))

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
            return (f"HTTP {resp.status_code}（符合预期：api-pan /files 不服务"
                    f"设备空间，0.1.30——目录表须走设备 API）")
        try:
            n = len((resp.json() or {}).get("files") or [])
        except ValueError:
            n = -1
        return (f"HTTP 200（files={n}）：api-pan 竟可服务该空间——"
                f"设备 API 非必需，但当前实现优先走设备 API 不受影响")

    steps.append(_probe("api-pan 对照", probe_apipan))

    # ok 只看关键步骤（最后一步 api-pan 对照为信息性）
    critical = steps[:-1]
    ok = all(s["ok"] for s in critical)
    _dbg(f"自诊断: ok={ok} " + " ".join(f"{s['step']}={'✓' if s['ok'] else '✗'}"
                                       for s in steps))
    return {"ok": ok, "version": version, "steps": steps}


if __name__ == "__main__":                       # 本地手动排查入口
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=1))
