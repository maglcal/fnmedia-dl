#!/usr/bin/env python3
"""迅雷下载引擎通道（备选）：直连本地/局域网「群晖套件系」迅雷引擎 web 面板。

支持范围：cnk3x/xunlei 的 Docker 版、群晖迅雷套件等 webman 面板系引擎。
应用中心官方原生迅雷（飞牛社区版）**不支持本地直连**（无此面板接口），
请改用迅雷远程通道（原生迅雷会把 NAS 注册为迅雷账号的远程设备）。

认证：GET 面板首页，正则提取 uiauth(value){return "..."} 作为 pan-auth JWT。
请求前缀：/webman/3rdparty/pan-xunlei-com/index.cgi/drive/v1/...
注意：此通道走套件端入口，任务数可能受套件端每日配额限制。
"""
from __future__ import annotations

import re

import httpx

from .base import Backend, BackendError, ParsedResource, TaskView
from .drive_core import DriveCore, task_to_view

UIAUTH_RE = re.compile(r'uiauth\(value\)\{\s*return\s*"([^"]+)"')
PATH_PREFIX = "/webman/3rdparty/pan-xunlei-com/index.cgi"
SCOPE_HINT = ("（本通道仅支持 Docker/群晖套件版迅雷面板；应用中心官方原生迅雷"
              "「社区版」请改用迅雷远程通道）")


class ThunderEngine(Backend):
    name = "thunder_engine"
    label = "迅雷下载引擎（本地直连）"

    def __init__(self, client: httpx.Client, conf: dict):
        self.client = client
        self.base_url = (conf.get("base_url") or "http://127.0.0.1:2345").rstrip("/")
        self.dir = conf.get("dir") or ""
        self._token = ""
        self._space = ""
        self.core = DriveCore(client, self.base_url + PATH_PREFIX + "/drive/v1",
                              lambda: self._headers())

    # ------------------------------------------------------------ 认证

    def _headers(self) -> dict:
        return {"pan-auth": self._token, "device-space": ""}

    def _login(self) -> None:
        try:
            resp = self.client.get(self.base_url + PATH_PREFIX + "/",
                                   timeout=15)
        except httpx.HTTPError as exc:
            raise BackendError(f"引擎面板连接失败：{exc}{SCOPE_HINT}") from exc
        if resp.status_code >= 400:
            raise BackendError(f"引擎面板返回 HTTP {resp.status_code}{SCOPE_HINT}")
        m = UIAUTH_RE.search(resp.text)
        if not m:
            raise BackendError(f"引擎面板未返回 pan-auth"
                               f"（请确认面板地址与版本）{SCOPE_HINT}")
        self._token = m.group(1)

    def _space_of(self) -> str:
        if not self._space:
            targets = self.core.device_targets()
            if not targets:
                raise BackendError("未发现引擎设备空间（user#runner 为空）")
            self._space = targets[0]["target"]
        return self._space

    # ------------------------------------------------------------ 接口

    def status(self) -> dict:
        try:
            self._login()
            self._space_of()
            return {"configured": True, "reachable": True,
                    "detail": f"引擎 {self.base_url}，设备空间 {self._space[:12]}…"}
        except BackendError as exc:
            return {"configured": True, "reachable": False, "detail": str(exc)}

    def parse(self, url: str) -> ParsedResource:
        self._login()
        return self.core.parse_resource(url)

    def add(self, url: str, *, name: str | None = None,
            directory: str | None = None) -> str:
        self._login()
        space = self._space_of()
        try:
            resource = self.core.parse_resource(url)
        except BackendError:
            raise
        if name:
            resource.name = name
        try:
            # on_unresolvable="direct"：目录表查询彻底失败时回退 0.1.22 直传
            # 形状（id 空 + 路径原样）——引擎按路径自行落盘（用户环境实测可用），
            # 本地引擎的最后兜底；云端不用（云端 id 空任务永不启动）。
            return self.core.create_task(url, resource, space,
                                         directory=directory or self.dir,
                                         on_unresolvable="direct")
        except BackendError as exc:
            raise BackendError(f"创建任务失败：{exc}") from exc

    def list_tasks(self) -> list[TaskView]:
        self._login()
        space = self._space_of()
        return [task_to_view(self.name, t) for t in self.core.list_tasks(space)]

    def pause(self, task_id: str) -> None:
        self._login()
        self.core.set_phase(self._space_of(), "user#download-url", task_id, "pause")

    def resume(self, task_id: str) -> None:
        self._login()
        self.core.set_phase(self._space_of(), "user#download-url", task_id, "running")

    def delete(self, task_id: str) -> None:
        self._login()
        self.core.delete_task(self._space_of(), task_id)
