#!/usr/bin/env python3
"""迅雷引擎每日任务配额感知。

背景：群晖套件系 / cnk3x Docker 版迅雷（thunder_engine 直连面板走套件端入口）
非会员每天只能创建 3 个任务，撞限时接口返回
HTTP 402 {"error":"task_create_count_limit"}（「任务创建次数达到上限」）。

本模块记录当天引擎创建次数与「已撞满」标记（state/quota.json）：
  - 撞满后当天 auto 派发直接跳过引擎（不再无谓报错），第二天自动恢复；
  - 重试撞配额时由 downloader 换通道重派（迅雷远程通道不占该配额）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import config as cfgmod
import filelock

_FILE_LOCK = threading.Lock()

# 撞配额错误的识别标记（引擎报错文案/错误码，任一命中即视为今日配额已满）
_QUOTA_MARKERS = ("task_create_count_limit", "任务创建次数达到上限", "HTTP 402")


def is_quota_error(msg: str) -> bool:
    """判断错误信息是否为「迅雷引擎今日任务创建次数达到上限」。"""
    text = str(msg or "")
    return any(marker in text for marker in _QUOTA_MARKERS)


class QuotaTracker:
    """引擎每日配额状态：当日创建计数 + 撞满标记，跨天自动重置。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path(cfgmod.STATE_DIR) / "quota.json"

    # ------------------------------------------------------------ 内部

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        today = time.strftime("%Y-%m-%d")
        if data.get("date") != today:
            # 跨天自动重置：配额是「每天」的，昨天的计数/标记不再有意义
            data = {"date": today, "engine_creates": 0, "engine_blocked": False}
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ 记录

    def note_engine_create(self) -> None:
        """引擎通道成功创建任务：当日计数 +1。"""
        # P0-2：threading.Lock 只护本进程；配额读-改-写整段进文件锁，
        # 防升级重启窗口两进程并存时互相覆盖计数/标记。
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            data["engine_creates"] = int(data.get("engine_creates", 0)) + 1
            self._save(data)

    def note_engine_quota_hit(self) -> None:
        """引擎通道撞每日配额：标记今日已满。"""
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            data["engine_blocked"] = True
            self._save(data)

    # ------------------------------------------------------------ 查询

    def engine_blocked(self) -> bool:
        """今日引擎配额是否已撞满（跨天自动恢复 False）。"""
        with _FILE_LOCK:
            return bool(self._load().get("engine_blocked"))

    def snapshot(self) -> dict:
        """配额快照（供 UI 展示）：今日已用次数 + 是否已满。"""
        with _FILE_LOCK:
            data = self._load()
        return {
            "date": data.get("date", ""),
            "used": int(data.get("engine_creates", 0)),
            "blocked": bool(data.get("engine_blocked")),
        }
