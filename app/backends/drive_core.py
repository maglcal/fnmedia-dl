#!/usr/bin/env python3
"""迅雷 drive/v1 核心客户端。

本地引擎（webman 前缀 + pan-auth）与云端远程（xluser 登录 + Bearer）共用
同一套 drive/v1 请求体，仅 base URL 与认证头不同：
  设备发现 GET  /drive/v1/tasks?type=user#runner        → tasks[].params.target
  链接解析 POST /drive/v1/resource/list  {"page_size":2000,"urls":url}
  创建任务 POST /drive/v1/task           type=user#download-url
  任务列表 GET  /drive/v1/tasks?space=&filters={"phase":{"in":"..."}}
  暂停恢复 PATCH /drive/v1/task          set_params.spec={"phase":"pause"|"running"}
  删除任务 DELETE /drive/v1/tasks?space=&task_ids=
"""
from __future__ import annotations

import json
from typing import Callable

import httpx

from .base import BackendError, ParsedResource

ACTIVE_PHASES = "PHASE_TYPE_PENDING,PHASE_TYPE_RUNNING"
ALL_PHASES = ("PHASE_TYPE_PENDING,PHASE_TYPE_RUNNING,PHASE_TYPE_COMPLETE,"
              "PHASE_TYPE_ERROR,PHASE_TYPE_STOP,PHASE_TYPE_PAUSED")

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
                 json_body: dict | None = None) -> dict:
        url = self.base_url + path
        try:
            resp = self.client.request(method, url, params=params,
                                       json=json_body, headers=self._headers(),
                                       timeout=30)
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
                             params={"type": "user#runner"})
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

    # ------------------------------------------------------------ 建任务

    def create_task(self, url: str, resource: ParsedResource, space: str,
                    *, directory: str = "", parent_folder_id: str = "") -> str:
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
                "parent_folder_path": directory or "",
                "parent_folder_id": parent_folder_id or "",
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
        return tid

    # ------------------------------------------------------------ 列表

    def list_tasks(self, space: str, *, active_only: bool = False) -> list[dict]:
        """任务列表（自动翻页）：drive/v1 /tasks 按 page_token 分页，
        只拉首页会漏任务。"""
        phases = ACTIVE_PHASES if active_only else ALL_PHASES
        filters = json.dumps({"phase": {"in": phases}})
        out: list[dict] = []
        page_token = ""
        for _ in range(20):        # 安全上限，防异常响应导致死循环
            params: dict = {"space": space, "filters": filters}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", "/tasks", params=params)
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
        找不到再逐个查设备空间。"""
        filters = json.dumps({
            "type": {"in": "user#download,user#download-url"},
            "phase": {"in": ALL_PHASES},
        })
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
            for _ in range(20):
                params: dict = {"space": sp, "filters": filters}
                if page_token:
                    params["page_token"] = page_token
                data = self._request("GET", "/tasks", params=params)
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
