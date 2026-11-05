#!/usr/bin/env python3
"""下载通道注册与构建。"""
from __future__ import annotations

import httpx

from .aria2 import Aria2
from .base import Backend, BackendError, ParsedResource, TaskView
from .thunder_cloud import ThunderCloud
from .thunder_engine import ThunderEngine

__all__ = ["Backend", "BackendError", "ParsedResource", "TaskView",
           "build_backends", "BACKEND_LABELS"]

BACKEND_LABELS = {
    "thunder_cloud": ThunderCloud.label,
    "thunder_engine": ThunderEngine.label,
    "aria2": Aria2.label,
}


def build_backends(cfg: dict, client: httpx.Client,
                   persist_cloud=None) -> dict[str, Backend]:
    """按配置构建全部通道实例（惰性使用，不做联网检查）。"""
    return {
        "thunder_cloud": ThunderCloud(client, cfg.get("thunder_cloud", {}), persist_cloud),
        "thunder_engine": ThunderEngine(client, cfg.get("thunder_engine", {})),
        "aria2": Aria2(client, cfg.get("aria2", {})),
    }
