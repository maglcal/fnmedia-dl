#!/usr/bin/env python3
"""下载后端统一接口与任务视图。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


def is_fallback_name(name: str, url: str) -> bool:
    """任务名是否为派发兜底形态（magnet:xxxxxxxx… 或截断链接）。

    兜底显示名不是真名——不得覆盖引擎 resource/list 解析出的真名
    （0.1.45.9 用户实测：真名被兜底名盖掉后只能指望 DHT 回填，弱网
    下永远停在 magnet:xxxxxxxx…）。派发命名、记录回填、unified_tasks
    合并显示三处共用同一判定。"""
    name = str(name or "")
    return name.startswith("magnet:") or name == str(url or "")[:80]


def apply_display_name(resource: ParsedResource, url: str,
                       name: str | None) -> None:
    """把派发显示名写入解析资源：兜底形态不覆盖引擎解析真名（0.1.45.9），
    显式真名（真名缓存命中/用户命名）才覆盖。"""
    if name and not is_fallback_name(name, url):
        resource.name = name


class BackendError(Exception):
    """后端不可用/请求失败（派发时用于触发降级）。"""


@dataclass
class ParsedResource:
    name: str = ""
    size: int = 0
    file_count: int = 1
    mime_type: str = ""
    file_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class TaskView:
    backend: str
    id: str
    name: str
    state: str                     # waiting|running|paused|completed|error|removed
    progress: float = 0.0          # 0..100
    speed: int | None = None       # B/s
    size: int | None = None        # B
    url: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "progress": round(self.progress, 1),
            "speed": self.speed,
            "size": self.size,
            "url": self.url,
            "message": self.message,
        }


class Backend(ABC):
    """下载通道适配器：状态自检 / 解析 / 建任务 / 列表 / 启停删除。"""

    name: str = ""
    label: str = ""
    # 「删除任务并删除文件」是否真实可行（迅雷 drive/v1 走 phase:"delete"
    # 删任务+本地文件；aria2 移除不碰文件，只能删记录）——UI 按此渲染选项。
    can_delete_files: bool = False

    @abstractmethod
    def status(self) -> dict:
        """返回 {configured, reachable, detail}；reachable=False 时派发跳过。"""

    @abstractmethod
    def parse(self, url: str) -> ParsedResource:
        """解析链接返回资源元数据（建任务前置步骤）。"""

    @abstractmethod
    def add(self, url: str, *, name: str | None = None,
            directory: str | None = None) -> str:
        """创建下载任务，返回后端任务 id。失败抛 BackendError。"""

    @abstractmethod
    def list_tasks(self) -> list[TaskView]:
        ...

    @abstractmethod
    def pause(self, task_id: str) -> None:
        ...

    @abstractmethod
    def resume(self, task_id: str) -> None:
        ...

    @abstractmethod
    def delete(self, task_id: str, *, with_files: bool = False) -> None:
        """删除任务。with_files=True 时连已下载文件一起删（引擎同款
        「同时删除本地文件」语义）；通道不支持时仅删任务记录、文件保留。"""

    def rename_task(self, task_id: str, name: str) -> bool:
        """引擎侧任务改名（任务名+磁盘文件/文件夹名随真名）。
        通道不支持（如 aria2）返回 False 什么都不做。"""
        return False
