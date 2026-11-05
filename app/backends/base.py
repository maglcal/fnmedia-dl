#!/usr/bin/env python3
"""下载后端统一接口与任务视图。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
    def delete(self, task_id: str) -> None:
        ...
