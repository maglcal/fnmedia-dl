#!/usr/bin/env python3
"""aria2 通道（兜底）：JSON-RPC over HTTP（aria2.addUri / tellActive 等）。

无任务数限制；磁力/BT/HTTP/FTP/种子均可。速度取决于 BT 网络，
作为迅雷通道不可用时的自动降级兜底。
"""
from __future__ import annotations

import itertools

import httpx

from .base import Backend, BackendError, ParsedResource, TaskView

_STATE_MAP = {
    "active": "running",
    "waiting": "waiting",
    "paused": "paused",
    "complete": "completed",
    "error": "error",
    "removed": "removed",
}

_gid_counter = itertools.count(1)


class Aria2(Backend):
    name = "aria2"
    label = "aria2（通用兜底）"

    def __init__(self, client: httpx.Client, conf: dict):
        self.client = client
        self.rpc_url = conf.get("rpc_url") or "http://127.0.0.1:6800/jsonrpc"
        self.secret = conf.get("secret") or ""
        self.dir = conf.get("dir") or ""

    # ------------------------------------------------------------ RPC

    def _rpc(self, method: str, params: list) -> dict:
        if self.secret:
            params = [f"token:{self.secret}"] + params
        payload = {"jsonrpc": "2.0", "id": str(next(_gid_counter)),
                   "method": method, "params": params}
        try:
            resp = self.client.post(self.rpc_url, json=payload, timeout=15)
        except httpx.HTTPError as exc:
            raise BackendError(f"aria2 连接失败：{exc}") from exc
        if resp.status_code >= 400:
            raise BackendError(f"aria2 HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise BackendError(f"aria2 错误 {err.get('code')}: {err.get('message')}")
        return data.get("result")

    # ------------------------------------------------------------ 接口

    def status(self) -> dict:
        try:
            ver = self._rpc("aria2.getVersion", [])
            return {"configured": True, "reachable": True,
                    "detail": f"aria2 {ver.get('version', '?')} @ {self.rpc_url}"}
        except (BackendError, ValueError) as exc:
            return {"configured": True, "reachable": False, "detail": str(exc)}

    def parse(self, url: str) -> ParsedResource:
        # aria2 无需前置解析；直链文件名从 URL 推断
        name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or url[:80]
        return ParsedResource(name=name)

    def add(self, url: str, *, name: str | None = None,
            directory: str | None = None) -> str:
        options: dict = {}
        d = directory or self.dir
        if d:
            options["dir"] = d
        if name and url.startswith("magnet:"):
            options["bt-save-metadata"] = "true"
        try:
            gid = self._rpc("aria2.addUri", [[url], options])
        except BackendError as exc:
            raise BackendError(f"aria2 创建任务失败：{exc}") from exc
        if not gid:
            raise BackendError("aria2 创建任务失败：未返回 GID")
        return gid

    def list_tasks(self) -> list[TaskView]:
        active = self._rpc("aria2.tellActive", []) or []
        waiting = self._rpc("aria2.tellWaiting", [0, 1000]) or []
        stopped = self._rpc("aria2.tellStopped", [0, 1000]) or []
        views: list[TaskView] = []
        for t in active + waiting + stopped:
            views.append(self._to_view(t))
        return views

    def _to_view(self, t: dict) -> TaskView:
        files = t.get("files") or []
        path = files[0].get("path", "") if files else ""
        name = path.rsplit("/", 1)[-1] if path else (t.get("gid") or "")[:12]
        if not name:
            uris = files[0].get("uris") if files else None
            name = (uris[0].get("uri", "") if uris else "")[:80]
        total = int(t.get("totalLength") or 0)
        done = int(t.get("completedLength") or 0)
        pct = done * 100.0 / total if total else (100.0 if t.get("status") == "complete" else 0.0)
        state = _STATE_MAP.get(t.get("status", ""), "waiting")
        return TaskView(
            backend=self.name,
            id=t.get("gid", ""),
            name=name or "(未知文件)",
            state=state,
            progress=pct,
            speed=int(t.get("downloadSpeed") or 0) or None,
            size=total or None,
            message=t.get("errorMessage") or "",
        )

    def pause(self, task_id: str) -> None:
        self._rpc("aria2.pause", [task_id])

    def resume(self, task_id: str) -> None:
        self._rpc("aria2.unpause", [task_id])

    def delete(self, task_id: str) -> None:
        try:
            self._rpc("aria2.remove", [task_id])
        except BackendError:
            self._rpc("aria2.forceRemove", [task_id])
