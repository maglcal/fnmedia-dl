#!/usr/bin/env python3
"""迅雷 drive/v1 核心客户端。

本地引擎（webman 前缀 + pan-auth）与云端远程（xluser 登录 + Bearer）共用
同一套 drive/v1 请求体，仅 base URL 与认证头不同：
  设备发现 GET  /drive/v1/tasks?type=user#runner        → tasks[].params.target
  链接解析 POST /drive/v1/resource/list  {"page_size":2000,"urls":url}
  目录表   GET  /drive/v1/files?space=&filters={"kind":{"eq":"drive#folder"}}
  建目录   POST /drive/v1/files          kind=drive#folder
  创建任务 POST /drive/v1/task           type=user#download-url
  任务列表 GET  /drive/v1/tasks?space=&filters={"type":{"in":"..."}}
  暂停恢复 PATCH /drive/v1/task          set_params.spec={"phase":"pause"|"running"}
  删除任务 DELETE /drive/v1/tasks?space=&task_ids=

任务参数铁律（2026-11-08）：
  params.parent_folder_id/path 是任务真正开始下载的必要字段——
  缺省（空）时引擎会把任务建出来但永远停在 PHASE_TYPE_PENDING
  （插件显示「排队中 0.0%」、迅雷面板没有任何下载数据）。
"""
from __future__ import annotations

import json
import time
from typing import Callable

import httpx

from .base import BackendError, ParsedResource

ACTIVE_PHASES = "PHASE_TYPE_PENDING,PHASE_TYPE_RUNNING"
TASK_TYPES = "user#download,user#download-url"
# 下载根目录常见名（无 /device/config 提示时的兜底匹配）
DOWNLOAD_ROOT_NAMES = {"download", "downloads", "下载", "下载目录"}

# 目录解析总预算（秒）：多形状阶梯+逐段建目录是同步链路，上游慢/挂起时
# 无界累计会拖垮网关响应预算（0.1.31「提示502」原因链的一环）——超预算
# 即止损降级/报明确错误，单次解析最长 ~18s。
RESOLVE_BUDGET = 18.0
# 目录浏览预算（0.1.38.2）：浏览是交互场景（用户点目录逐级下钻，慢一点
# 也愿意等），比派发链更宽容；设备中继已知慢（error_code=2 超时史），
# 深路径在 18s 派发预算下必超支（顶层+config+逐段+entries 多次请求），
# 中途耗尽报「目录不存在」是「看不到子文件夹」的来源之一。
BROWSE_BUDGET = 30.0
# 解析结果复用窗口（秒）：批量派发同一目录只解析一次（0.1.31 提速关键）。
# 按 client 身份隔离（每个请求新建 DownloadManager/client），不跨请求串味。
RESOLVE_CACHE_TTL = 120.0
_resolve_cache: dict[tuple, tuple[float, tuple[str, str], object]] = {}
# 解析失败负缓存（0.1.33）：失败记 30s 冷却——设备插件持续超时时批量派发
# 每条都重烧整条失败链（预检+发现+形状×2+反查），响应预算瞬间耗尽；命中
# 负缓存直接短路报错（带「稍后自动重试」提示），解析成功即清除。
NEG_TTL = 30.0
_resolve_neg: dict[tuple, tuple[float, str, object]] = {}

PHASE_STATE = {
    "PHASE_TYPE_PENDING": "waiting",
    "PHASE_TYPE_RUNNING": "running",
    "PHASE_TYPE_COMPLETE": "completed",
    "PHASE_TYPE_ERROR": "error",
    "PHASE_TYPE_STOP": "paused",
    "PHASE_TYPE_PAUSED": "paused",
}


def _phase_state(phase: str) -> str:
    """phase → 统一状态。精确表优先，未知值按关键词兜底
    （引擎各版本可能给 PAUSED/STOP 的变体拼写，兜底防止
    暂停后状态不翻转、按钮卡在「暂停」）。"""
    p = (phase or "").strip().upper()
    if p in PHASE_STATE:
        return PHASE_STATE[p]
    if "PAUSE" in p or "STOP" in p:
        return "paused"
    if "ERROR" in p or "FAIL" in p:
        return "error"
    if "COMPLETE" in p or "DONE" in p:
        return "completed"
    if "PEND" in p or "WAIT" in p or "QUEUE" in p:
        return "waiting"
    return "running" if p else "waiting"


def _err_payload(data: dict) -> str:
    """drive/v1 错误体检测：迅雷接口常见 HTTP 200 + 错误体，
    只看状态码会把失败当成功（暂停/删除静默失效的原因之一）。
    与 thunder_cloud._is_error 同语义；返回错误文案，无错返回空串。"""
    if not isinstance(data, dict):
        return ""
    if (data.get("error") or "").strip() == "success":
        return ""
    try:
        code = int(data.get("error_code") or data.get("errorCode") or 0)
    except (TypeError, ValueError):
        code = 0
    detail = (data.get("errorDesc") or data.get("error_description")
              or data.get("error") or "").strip()
    if code or detail:
        return f"{detail or '未知错误'}（error_code={code}）"
    return ""


def scalar_int(v, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _dbg(msg: str) -> None:
    """诊断日志（state/dispatch.log，封顶 512KB）：目录解析/建任务关键事件，
    出问题时一眼定位是哪层断的。记录失败静默——诊断只许帮忙不许添乱。"""
    try:
        import time as _time
        from pathlib import Path as _P
        import config as _cfg
        p = _P(_cfg.STATE_DIR) / "dispatch.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 512 * 1024:
            p.write_text("", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(_time.strftime("%m-%d %H:%M:%S") + " "
                    + str(msg)[:300] + "\n")
    except Exception:
        pass


def _clip_timeout(deadline: float | None, per_call: float) -> float:
    """把单次请求超时裁到解析预算剩余时间内（预算外不限制）。"""
    if deadline is None:
        return per_call
    return max(1.0, min(per_call, deadline - time.monotonic()))


def _agg_tries(tries: list[str]) -> str:
    """聚合各形状/各路错误（0.1.34 同因合并）：错误文案带指引后很长，
    逐形状原样重复会把诊断/派发输出淹掉——同因条目（msg 相同）合并为
    「label1、label2（N 处同因）：msg」只展开一次；无冒头条目原样保留。
    tries 项形如「label：msg」（与 _folders 各处 append 形态一致）。"""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    plain: list[str] = []
    for item in tries:
        label, sep, msg = item.partition("：")
        if not sep:
            plain.append(item)
            continue
        if msg not in groups:
            groups[msg] = []
            order.append(msg)
        groups[msg].append(label)
    parts = list(plain)
    for msg in order:
        labels = groups[msg]
        head = "、".join(labels)
        if len(labels) > 1:
            head += f"（{len(labels)} 处同因）"
        parts.append(f"{head}：{msg}")
    return "；".join(parts)


def _norm_dir(path: str) -> str:
    """目录路径归一：clean + 尾斜杠。"""
    p = (path or "").replace("\\", "/").strip()
    if not p:
        return ""
    segs = [s for s in p.split("/") if s]
    if not segs:
        return "/"
    return "/" + "/".join(segs) + "/"


def _folder_path(folder: dict) -> str:
    """drive/v1/files 目录项的真实路径：
    params.RealPath 优先，AliasPath 兜底；键名大小写防御。"""
    params = folder.get("params") or {}
    for key in ("RealPath", "realPath", "real_path",
                "AliasPath", "aliasPath", "alias_path"):
        v = _norm_dir(str(params.get(key) or ""))
        if v:
            return v
    return ""


class DriveCore:
    """参数化的 drive/v1 客户端。base_url 以 /drive/v1 结尾；
    headers_provider() 每次请求取最新认证头。"""

    def __init__(self, client: httpx.Client, base_url: str,
                 headers_provider: Callable[[], dict]):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.headers_provider = headers_provider

    # ------------------------------------------------------------ 基础

    def _headers(self) -> dict:
        h = {"accept": "application/json;charset=UTF-8"}
        h.update(self.headers_provider() or {})
        return h

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, timeout: float = 15) -> dict:
        # path 以 http 开头时视为完整 URL（如 drive/v1 的同级接口 /device/config）
        url = path if path.startswith("http") else self.base_url + path
        try:
            resp = self.client.request(method, url, params=params,
                                       json=json_body, headers=self._headers(),
                                       timeout=timeout)
        except httpx.HTTPError as exc:
            raise BackendError(f"连接失败：{exc}") from exc
        if resp.status_code >= 400:
            raise BackendError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise BackendError(f"响应不是 JSON：{resp.text[:120]}") from exc
        # 迅雷接口常见 HTTP 200 + 错误体：不检测会把暂停/删除的失败当成功
        err = _err_payload(data) if isinstance(data, dict) else ""
        if err:
            raise BackendError(f"{method} {path} 失败：{err}")
        return data

    # ------------------------------------------------------------ 设备

    def device_targets(self) -> list[dict]:
        """runner 任务即设备空间：[{target, name}]。"""
        data = self._request("GET", "/tasks",
                             params={"type": "user#runner"}, timeout=8)
        out = []
        for t in data.get("tasks", []):
            target = (t.get("params") or {}).get("target", "").strip()
            if target:
                out.append({"target": target, "name": t.get("name") or target})
        return out

    # ------------------------------------------------------------ 解析

    def parse_resource(self, url: str) -> ParsedResource:
        data = self._request("POST", "/resource/list",
                             json_body={"page_size": 2000, "urls": url})
        resources = ((data.get("list") or {}).get("resources")) or []
        if not resources:
            raise BackendError("链接解析失败：未返回资源信息")
        r = resources[0]
        name = r.get("name") or r.get("file_name") or ""
        return ParsedResource(
            name=name,
            size=scalar_int(r.get("file_size")),
            file_count=max(1, scalar_int(r.get("file_count"), 1)),
            mime_type=r.get("mime_type") or "",
            file_id=r.get("file_id") or "",
            raw=r,
        )

    # ------------------------------------------------------------ 目录解析

    def _device_config(self, space: str = "",
                       deadline: float | None = None) -> dict:
        """GET /device/config（drive/v1 的同级接口，用于获取
        download_paths）。space/deadline 仅设备专属 API 覆盖版（_CloudDrive）
        使用，基类本地引擎不需要。云端网关不一定代理此接口——失败静默返回 {}，
        由 resolve_dest_folder 的兜底链继续。"""
        root = self.base_url[:-len("/drive/v1")] if self.base_url.endswith("/drive/v1") else self.base_url
        try:
            return self._request("GET", root + "/device/config")
        except BackendError:
            return {}

    def _folder_shapes(self, space: str, parent_id: str) -> list[tuple[str, dict]]:
        """目录表查询参数形状阶梯（0.1.29）：不同网关对参数容忍度不同——
        本地引擎认扩展（with）/空间（device_space）形状优先（目录项带
        params.RealPath，下载根匹配最准），标准/极简形状兜底。

        0.1.30：远程设备（user#runner 空间）的目录表须走设备专属 API。
        _CloudDrive 覆盖 _folders 走设备专属 API，本阶梯降级为回退路径。"""
        filters = json.dumps({"kind": {"eq": "drive#folder"}},
                             separators=(",", ":"))
        standard = {"space": space, "limit": "200",
                    "parent_id": parent_id or "", "filters": filters}
        minimal = {"space": space, "limit": "200", "parent_id": parent_id or ""}
        shape_ext = dict(standard, page_token="",
                     **{"with": ["withCategoryDiskMountPath",
                                 "withCategoryDownloadPath"]})
        shape_space = dict(standard, page_token="", device_space="")
        return [("扩展形状", shape_ext), ("空间形状", shape_space),
                ("标准形状", standard), ("极简形状", minimal)]

    def _folders(self, space: str, parent_id: str = "",
                 deadline: float | None = None) -> list[dict]:
        """目录表：按 _folder_shapes 阶梯逐形状重试，取第一个非空结果；
        只要有任一形状成功（哪怕空表）就不升级报错，全军覆没才抛聚合错误
        （每形状的错误都保留，502/超时这类网关差异一眼可辨）。
        deadline（0.1.31）：解析总预算，超时即止损，防慢上游拖垮响应预算。
        kind 客户端兜底过滤：极简形状无服务端过滤时只信 drive#folder。"""
        saw_ok, tries = False, []
        for label, params in self._folder_shapes(space, parent_id):
            if deadline is not None and time.monotonic() >= deadline:
                tries.append(f"预算耗尽（{RESOLVE_BUDGET:.0f}s）未及尝试")
                break
            try:
                data = self._request("GET", "/files", params=params,
                                     timeout=_clip_timeout(deadline, 8))
            except BackendError as exc:
                tries.append(f"{label}：{str(exc)[:100]}")
                continue
            saw_ok = True
            files = [f for f in (data.get("files") or [])
                     if isinstance(f, dict)
                     and f.get("kind") in (None, "", "drive#folder")]
            if files:
                return files
            tries.append(f"{label}：空表")
        if saw_ok:
            return []
        err = BackendError(
            "引擎目录表查询失败（各形状均报错）：" + _agg_tries(tries))
        err.tries = tries        # 0.1.34：明细供上层跨层合并同因（_CloudDrive）
        raise err

    def _make_folder(self, space: str, parent_id: str, name: str,
                     deadline: float | None = None) -> dict:
        """建子目录：POST /drive/v1/files kind=drive#folder。"""
        data = self._request("POST", "/files", json_body={
            "parent_id": parent_id, "name": name,
            "space": space, "kind": "drive#folder"},
            timeout=_clip_timeout(deadline, 10))
        fid = (data.get("file") or {}).get("id") or ""
        if not fid:
            raise BackendError(f"创建下载子目录 {name} 失败：响应缺少 file.id")
        return {"id": fid, "name": name,
                "params": {"RealPath": ""}}

    def resolve_dest_folder(self, space: str, directory: str = "") -> tuple[str, str]:
        """解析下载目标文件夹 → (parent_folder_id, parent_folder_path)。

        建任务必须带真实 parent_folder_id/path，否则任务永远不开始下载
        （缺省任务不会开始下载）。下载根目录识别按序：
          1. /device/config download_paths[0] 与目录表 realPath/aliasPath 匹配
          2. 顶层只有一个目录 → 即根
          3. 名称/路径命中下载根常见名（downloads/下载/…）
          4. 启发式兜底：首个顶层目录的 parent_id 或 id
        directory 为根下子目录（相对路径，或根目录下的绝对路径），
        中间层级缺失时自动创建（多级逐段建）。
        根外绝对路径按 _anchor_abs 在目录表顶层锚定真实落点后解析；
        锚不到且目录项带路径信息时显式报错（0.1.37.2——静默回落下载根
        会让文件下错位置），目录表不可用/无路径信息时才降级根 id +
        原样路径尽力落盘。

        0.1.31：结果按 (base_url, space, directory) 缓存 120s——批量派发
        同目录只解析一次（此前每条链接重跑整条目录链，是「批量必超时
        →提示502」的放大器）；缓存按 client 身份隔离，不跨请求串味。
        0.1.33：失败也进负缓存（30s 冷却）——设备插件持续超时时每条链接
        重烧整条失败链会耗尽派发预算，命中负缓存直接短路报错。"""
        key = (self.base_url, space, (directory or "").strip())
        hit = _resolve_cache.get(key)
        if (hit and hit[2] is self.client
                and time.monotonic() - hit[0] < RESOLVE_CACHE_TTL):
            return hit[1]
        neg = _resolve_neg.get(key)
        if (neg and neg[2] is self.client
                and time.monotonic() - neg[0] < NEG_TTL):
            raise BackendError(f"{neg[1]}（{int(NEG_TTL)}s 内已失败过，"
                               f"稍后自动重试）")
        try:
            result = self._resolve_uncached(space, directory,
                                            time.monotonic() + RESOLVE_BUDGET)
        except BackendError as exc:
            if len(_resolve_neg) > 256:           # 简单封顶，防长期运行膨胀
                _resolve_neg.clear()
            _resolve_neg[key] = (time.monotonic(), str(exc), self.client)
            raise
        _resolve_neg.pop(key, None)               # 成功即清除失败记录
        if len(_resolve_cache) > 256:             # 简单封顶，防长期运行膨胀
            _resolve_cache.clear()
        _resolve_cache[key] = (time.monotonic(), result, self.client)
        return result

    def _harvest_root(self, space: str,
                      tasks: list[dict] | None = None) -> tuple[str, str] | None:
        """目录表不可用时的兜底（0.1.32）：从历史任务反查 parent_folder_id/path。

        建任务走 api-pan /task 不依赖设备插件（设备插件超时时下载仍正常），历史任务对象的 params 里带着真实可用的 parent_folder_id。设备插件超时时这是拿到真实根 id 的唯一通路。
        tasks 可传入已取到的任务列表（诊断复用，免二次拉取）。
        返回 (id, path) 或 None（无历史任务/反查失败）。"""
        if tasks is None:
            try:
                tasks = self.list_tasks(space)
            except BackendError as exc:
                _dbg(f"历史任务反查失败: {str(exc)[:80]}")
                return None
        best: tuple[str, str] | None = None
        for t in tasks:
            p = t.get("params") if isinstance(t.get("params"), dict) else {}
            pid = str(p.get("parent_folder_id") or "").strip()
            if not pid:
                continue
            ppath = str(p.get("parent_folder_path") or "").strip()
            if ppath:
                return pid, ppath       # 带路径的最理想，直接采用
            best = best or (pid, "")
        return best

    def _root_folder(self, space: str, deadline: float,
                     tops: list[dict] | None = None) -> tuple[str, str]:
        """只读识别下载根目录 → (root_id, root_path)。识别阶梯：
          1. /device/config download_paths[0] 与目录表 realPath/aliasPath 匹配
          2. 顶层只有一个目录 → 即根
          3. 名称/路径命中下载根常见名（downloads/下载/…）
          4. 启发式兜底：首个顶层目录的 parent_id 或 id
        目录表彻底不可用（设备插件超时 error_code=2 等）时历史任务反查兜底。
        **不创建任何目录**——建目录只发生在 _resolve_uncached 的子目录解析里，
        因此本方法可安全复用于目录浏览选择器。
        tops（0.1.38.2）：调用方已拉好的顶层目录表，复用避免重复请求
        （浏览链此前把顶层列表拉两遍，白烧设备 API 预算）；None = 自拉。"""
        try:
            folders = (tops if tops is not None
                       else self._folders(space, "", deadline=deadline))
            if not folders:
                raise BackendError(
                    "引擎目录表为空，无法解析下载根目录"
                    "（parent_folder_id 缺失时任务不会开始下载）")
        except BackendError as exc:
            # 0.1.32：目录表彻底不可用时先历史任务反查再放弃——反查到的
            # (id, path) 即根（设备插件超时时这是拿到真实根 id 的唯一通路）。
            harvested = self._harvest_root(space)
            if harvested is None:
                # 0.1.33：反查无米（设备任务 0 条/无 params）时给可操作指引
                # ——官方 App 建一个任务即带上 parent_folder_id，反查即恢复。
                raise BackendError(
                    f"{exc}（历史任务反查也不可用：该设备没有带 "
                    f"parent_folder_id 的历史任务。可先用官方迅雷 App 对该设备"
                    f"建一个下载任务（选好下载目录），任务 params 会带上 "
                    f"parent_folder_id，反查即恢复；或检查目标设备上的迅雷"
                    f"远程插件是否在运行）") from exc
            _dbg(f"目录表不可用，历史任务反查兜底: space={space[:12]} "
                 f"→ id={harvested[0][:16]} path={harvested[1][:32]}")
            return harvested

        hint = ""
        for p in (self._device_config(space, deadline=deadline)
                  .get("download_paths") or []):
            if str(p).strip():
                hint = _norm_dir(str(p))
                break

        root_id, root_path = "", ""
        if hint:                              # 1) 配置提示精确匹配
            for f in folders:
                if _folder_path(f) == hint:
                    root_id, root_path = str(f.get("id")), hint
                    break
        if not root_id and len(folders) == 1:  # 2) 唯一顶层目录
            root_id = str(folders[0].get("id"))
            root_path = _folder_path(folders[0]) or hint
        if not root_id:                       # 3) 常见下载根名
            for f in folders:
                name = (f.get("name") or "").strip().lower()
                if _folder_path(f) in ("/downloads/", "/") or name in DOWNLOAD_ROOT_NAMES:
                    root_id = str(f.get("id"))
                    root_path = _folder_path(f) or hint
                    break
        if not root_id:                       # 4) 启发式兜底
            pid = str(folders[0].get("parent_id") or "").strip()
            root_id = pid or str(folders[0].get("id"))
            root_path = hint
        if not root_id:
            raise BackendError("无法识别引擎下载根目录（parent_folder_id 解析失败）")
        return root_id, root_path

    def _anchor_abs(self, tops: list[dict],
                    nd: str) -> tuple[str, str, str] | None:
        """根外绝对路径在目录表顶层找锚点（0.1.37.2）→ (id, 锚定路径, 剩余段)。

        优先 realPath 最长前缀（同挂载/同路径树，语义最准）；目录项不带
        params 路径时退化为「名称段匹配」——取段位最靠前的同名顶层目录，
        剩余段交由逐段走树解析（走树按 name/realPath 双条件匹配，能纠偏）。
        只读：不查子级、不建目录。找不到返回 None，由调用方决定报错或降级
        ——绝不再静默回落下载根（0.1.37.1 用户实测：保存目录填了
        /vol1/1000/movie，文件却下到下载根，静默下错位置比报错更糟）。"""
        segs = [s for s in nd.split("/") if s]
        best: tuple[int, str, str, str] | None = None
        for f in tops:
            fp = _folder_path(f)
            if fp and (nd == fp or nd.startswith(fp)):
                cand = (len(fp), str(f.get("id")), fp, nd[len(fp):])
                if best is None or cand[0] > best[0]:
                    best = cand
        if best:
            return best[1], best[2], best[3]
        for i, seg in enumerate(segs):
            for f in tops:
                if (f.get("name") or "").strip() == seg:
                    fp = (_folder_path(f)
                          or _norm_dir("/".join(segs[:i + 1])))
                    return str(f.get("id")), fp, "/".join(segs[i + 1:])
        return None

    def _resolve_uncached(self, space: str, directory: str,
                          deadline: float) -> tuple[str, str]:
        root_id, root_path = self._root_folder(space, deadline)

        dest = (directory or "").strip()
        if not dest:
            return root_id, root_path
        nd = _norm_dir(dest)
        if root_path and nd == root_path:
            return root_id, root_path
        if root_path and nd.startswith(root_path):
            rel = nd[len(root_path):]             # 根下绝对路径 → 相对段
            pid, ppath = root_id, root_path
        elif dest.startswith("/"):
            # 根外/根未知的绝对路径（0.1.37.2 重做）：先在目录表顶层按
            # realPath/名称锚定真实落点再逐段解析。此前直接「根 id +
            # 原样路径」交引擎，而引擎只认 parent_folder_id 会静默下到
            # 根目录——用户实测保存目录整个被无视的原因。
            try:
                tops = self._folders(space, "", deadline=deadline)
            except BackendError:
                # 目录表彻底不可用（0.1.32 反查兜底场景）：可达性无从判断，
                # 保留 0.1.27 语义——根 id + 原样路径让引擎尽力落盘，
                # 任务至少能开始下载。
                _dbg(f"根外路径锚定跳过（目录表不可用）: {nd[:60]}")
                return root_id, dest
            anchor = self._anchor_abs(tops, nd)
            if anchor is None:
                if any(_folder_path(f) for f in tops):
                    # 可达性已判定（目录项带路径信息但无一命中）：
                    # 显式报错——静默下到根目录就是本次要修的 bug。
                    names = "、".join(
                        (f.get("name") or "").strip()
                        for f in tops if (f.get("name") or "").strip())
                    raise BackendError(
                        f"保存目录 {dest} 不在引擎可达的下载目录树内"
                        f"（下载根 {root_path or '未知'}，顶层目录 "
                        f"{names or '无'}），已停止派发以免文件下错位置。"
                        "可改用「选择目录」的设备目录树、或填下载根下的"
                        "相对路径；确需该目录请先在迅雷面板把它加入下载"
                        "目录（Docker 引擎需把该路径挂载进容器）后重试。")
                # 目录项无任何路径信息（极简形状等）：无从判定可达性，
                # 保留 0.1.27 语义降级（根 id + 原样路径尽力落盘）。
                _dbg(f"根外路径锚定跳过（目录项无路径信息）: {nd[:60]}")
                return root_id, dest
            pid, ppath, rel = anchor
        else:
            rel = nd.lstrip("/")                  # 相对路径
            pid, ppath = root_id, root_path

        target = _norm_dir((ppath or "") + rel)
        try:
            for seg in [s for s in rel.split("/") if s]:
                child = None
                for f in self._folders(space, pid, deadline=deadline):
                    if ((f.get("name") or "").strip() == seg
                            or _folder_path(f) == ppath + seg + "/"):
                        child = f
                        break
                if child is None:
                    child = self._make_folder(space, pid, seg, deadline=deadline)
                pid = str(child.get("id"))
                ppath = ppath + seg + "/"
        except BackendError:
            # 子目录解析/创建失败（常见：引擎账号对该目录无读写权）→ 保留
            # 已解析到的目录 id 让任务能开始下载，完整目标路径给引擎尽力落盘
            _dbg(f"子目录解析降级: space={space} 目标={target} → 用 {pid}")
            return pid, target
        return pid, ppath

    def browse(self, space: str, path: str = "",
               deadline: float | None = None) -> dict:
        """只读浏览下载目录树（0.1.38 目录选择器数据源）。

        path 为下载根下的相对路径（或根下的绝对路径），空 = 下载根本身；
        根外绝对路径按 _anchor_abs 锚定到真实落点后浏览（与派发同一套
        落点规则，选择器回填的路径派发时能解析到同一处）。
        返回 {root_id, root, path, entries}；
        entries 只含子目录，path 值可直接回填给 create_task 的 directory。

        **只读不建目录**：路径缺级直接报「目录不存在」，由用户挑已存在的
        目录——选择器语义是「浏览并选定」，不该有隐藏的写副作用。"""
        dl = deadline if deadline is not None else time.monotonic() + BROWSE_BUDGET
        memo: dict[str, list[dict]] = {}

        def folders(pid: str) -> list[dict]:
            """同一 parent 在一次浏览内只拉一次目录表（0.1.38.2）：
            根外锚定场景此前顶层列表会被 _root_folder 与 _anchor_abs
            各拉一遍，白烧设备 API 预算。"""
            if pid not in memo:
                memo[pid] = self._folders(space, pid, deadline=dl)
            return memo[pid]

        try:
            tops = folders("")
        except BackendError:
            tops = None       # 目录表不可用 → 交给 _root_folder 反查兜底
        root_id, root_path = self._root_folder(space, dl, tops=tops)
        nd = _norm_dir((path or "").strip())
        # 归一到「走树起点 + 相对段」：根下绝对路径去根前缀；根外绝对路径
        # 与派发同一套 _anchor_abs 锚定（只读），其余按根下相对段处理
        base = root_path or "/"
        pid, ppath = root_id, root_path
        if root_path and nd == root_path:
            rel = ""
        elif root_path and nd.startswith(root_path):
            rel = nd[len(root_path):]
        elif (path or "").strip().startswith("/"):
            anchor = self._anchor_abs(tops, nd) if tops else None
            if anchor:
                pid, ppath, rel = anchor
                base = ppath
            else:
                rel = nd.lstrip("/")
        else:
            rel = nd.lstrip("/")
        for seg in [s for s in rel.split("/") if s]:
            child = None
            for f in folders(pid):
                if ((f.get("name") or "").strip() == seg
                        or _folder_path(f) == ppath + seg + "/"):
                    child = f
                    break
            if child is None:
                raise BackendError(f"目录不存在：{seg}（浏览为只读，不会自动创建）")
            pid = str(child.get("id"))
            ppath = ppath + seg + "/"
        entries = []
        for f in folders(pid):
            name = (f.get("name") or "").strip()
            if not name:
                continue
            entries.append({"id": str(f.get("id")), "name": name,
                            "path": ppath + name + "/"})
        entries.sort(key=lambda e: e["name"])
        return {"root_id": root_id, "root": base or "/",
                "path": ppath, "entries": entries}

    # ------------------------------------------------------------ 建任务

    def create_task(self, url: str, resource: ParsedResource, space: str,
                    *, directory: str = "", parent_folder_id: str = "",
                    parent_folder_path: str = "",
                    on_unresolvable: str = "raise") -> str:
        """创建任务。parent_folder_id 为空时按 directory 解析真实下载目录——
        这是任务真正开始下载的必要字段（缺省任务永远停在排队中）。

        解析是阶梯式尽力而为（0.1.28）：完整解析 → 子目录失败降级用根 id +
        目标路径 → 根外绝对路径锚定目录表落点解析（0.1.37.2），锚不到且
        可达性可判定时显式报错，无从判定才根 id + 原样路径。

        on_unresolvable（0.1.29）：目录解析彻底失败（目录表查询全挂/空表）时——
        "raise"（默认，云端）：显式报错，云端 id 空的任务永不启动，静默建
        死任务比报错糟；"direct"（本地引擎）：回退 0.1.22 直传形状（id 空 +
        路径原样），引擎按路径自行落盘（用户环境实测可用），仅作最后兜底。"""
        if parent_folder_id:
            pfid = parent_folder_id
            pfpath = parent_folder_path or directory or ""
        else:
            try:
                pfid, pfpath = self.resolve_dest_folder(
                    space, (directory or "").strip())
            except BackendError as exc:
                _dbg(f"目录解析失败: dir={directory[:40]!r} → {str(exc)[:120]}")
                if on_unresolvable != "direct":
                    raise
                pfid, pfpath = "", (directory or "").strip()
                _dbg(f"引擎直传兜底: id=空 path={pfpath[:40]}")
            _dbg(f"目录解析: dir={directory[:40]!r} → id={pfid[:16]} "
                 f"path={pfpath[:40]}")
        name = resource.name or url[:80]
        body = {
            "type": "user#download-url",
            "name": name,
            "file_name": name,
            "file_size": str(resource.size or "0"),
            "space": space,
            "params": {
                "target": space,
                "url": url,
                "total_file_count": str(resource.file_count or "1"),
                "parent_folder_path": pfpath,
                "parent_folder_id": pfid,
                "sub_file_index": "--1,",
                "mime_type": resource.mime_type,
                "file_id": resource.file_id,
            },
        }
        data = self._request("POST", "/task", json_body=body)
        task = data.get("task") or data
        tid = task.get("id") or ""
        if not tid:
            raise BackendError(f"创建任务失败：{json.dumps(data, ensure_ascii=False)[:200]}")
        _dbg(f"建任务成功: id={tid} parent={pfid[:16]}")
        return str(tid)   # 引擎 id 可能是整型，统一字符串防 unified_tasks 匹配失败

    # ------------------------------------------------------------ 列表

    def list_tasks(self, space: str, *, active_only: bool = False) -> list[dict]:
        """任务列表（自动翻页）：drive/v1 /tasks 按 page_token 分页，
        只拉首页会漏任务。

        filters 实现：type 过滤只看
        下载任务（防 user#runner 设备行混进任务列表）；全量查询不枚举 phase
        （引擎各版本 phase 拼写有变体，枚举会漏掉 PHASE_TYPE_STOPPED 等任务）。"""
        filters: dict = {"type": {"in": TASK_TYPES}}
        if active_only:
            filters["phase"] = {"in": ACTIVE_PHASES}
        filters_s = json.dumps(filters, separators=(",", ":"))
        out: list[dict] = []
        page_token = ""
        # 0.1.31：翻页封顶 6 页（1200 条）+ 单页 8s——此前上限 20 页无超时
        # 特化，慢上游下任务列表能把整个响应拖过中继超时（裸 502）。
        for _ in range(6):
            params: dict = {"space": space, "filters": filters_s,
                            "limit": "200"}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", "/tasks", params=params, timeout=8)
            out.extend(data.get("tasks", []))
            page_token = ((data.get("next_page_token")
                           or data.get("nextPageToken") or "").strip())
            if not page_token:
                break
        return out

    # ------------------------------------------------------------ 操作

    def find_task(self, task_id: str, space: str = "") -> dict | None:
        """按 id 查任务（含暂停/失败态），返回任务对象（带真实 space/type）。

        PATCH 前先查任务取 task.Space / task.Type 发请求——硬编码 type/space 与任务不符时
        引擎会静默忽略（暂停卡住不翻转的原因）。先查给定 space，
        找不到再逐个查设备空间。只按 type 过滤（不枚举 phase，
        防 phase 拼写变体漏掉任务）。"""
        filters = json.dumps({"type": {"in": TASK_TYPES}}, separators=(",", ":"))
        spaces: list[str] = []
        if space:
            spaces.append(space)
        try:
            for t in self.device_targets():
                if t["target"] not in spaces:
                    spaces.append(t["target"])
        except BackendError:
            pass                      # 设备发现失败不阻断主 space 的查找
        for sp in spaces:
            page_token = ""
            for _ in range(6):    # 0.1.31：封顶 6 页 + 单页 8s（同 list_tasks）
                params: dict = {"space": sp, "filters": filters}
                if page_token:
                    params["page_token"] = page_token
                data = self._request("GET", "/tasks", params=params, timeout=8)
                for t in data.get("tasks", []):
                    if str(t.get("id", "")) == str(task_id):
                        if not t.get("space"):
                            t["space"] = sp
                        return t
                page_token = ((data.get("next_page_token")
                               or data.get("nextPageToken") or "").strip())
                if not page_token:
                    break
        return None

    def set_phase(self, space: str, task_type: str, task_id: str, phase: str) -> None:
        """phase: "pause" | "running"。

        先查任务真实 space/type 再 PATCH；
        查不到（引擎已清理等）时回退调用方给的值。"""
        task = self.find_task(task_id, space) or {}
        body = {
            "space": task.get("space") or space,
            "type": task.get("type") or task_type,
            "id": task_id,
            "set_params": {"spec": json.dumps({"phase": phase})},
        }
        self._request("PATCH", "/task", json_body=body)

    def delete_task(self, space: str, task_id: str) -> None:
        self._request("DELETE", "/tasks",
                      params={"space": space, "task_ids": task_id})


def task_to_view(backend: str, t: dict):
    """drive/v1 任务对象 → 统一任务视图。"""
    from .base import TaskView
    phase = t.get("phase") or ""
    state = _phase_state(phase)
    size = scalar_int(t.get("file_size"), 0) or None
    done = scalar_int(t.get("status_size"), 0)
    progress = t.get("progress")
    if isinstance(progress, (int, float)):
        pct = float(progress)
        pct = pct if pct <= 100 else pct / 100.0  # 兼容千分比等异常值按 100 封顶
    elif size:
        pct = done * 100.0 / size
    else:
        pct = 0.0
    name = t.get("name") or t.get("file_name") or (t.get("params") or {}).get("url", "")[:80]
    return TaskView(
        backend=backend,
        id=str(t.get("id", "")),
        name=name,
        state=state,
        progress=max(0.0, min(100.0, pct)),
        speed=None,
        size=size,
        url=(t.get("params") or {}).get("url", ""),
        message=t.get("message") or "",
    )
