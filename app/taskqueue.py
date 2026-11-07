#!/usr/bin/env python3
"""任务队列：链接先入队，每天定时自动派发。

用途：迅雷下载引擎（套件端入口）非会员每日任务创建次数有限。把不急的
链接放进队列，开启「每日定时派发」后每天到点自动派发（auto 降级链），
配额重置后自动吃掉额度，不用手动挤配额；也可随时「立即派发」手动放行。

状态文件 state/queue.json：
  {"items": [{"id","url","name","directory","added_at","status","error","backend"}],
   "settings": {"enabled": false, "time": "00:05", "last_run": ""}}

注意：文件名不能叫 queue.py——app 目录在 sys.path 首位，会遮蔽标准库
queue 模块把 threading/anyio 等依赖带崩。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

import config as cfgmod
import links as linksmod

_FILE_LOCK = threading.Lock()
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_TIME = "00:05"
PENDING = ("queued", "failed")


class TaskQueue:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path(cfgmod.STATE_DIR) / "queue.json"

    # ------------------------------------------------------------ 内部

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        settings = data.get("settings") or {}
        data["items"] = [i for i in (data.get("items") or []) if isinstance(i, dict)]
        data["settings"] = {
            "enabled": bool(settings.get("enabled")),
            "time": settings.get("time") or DEFAULT_TIME,
            "last_run": settings.get("last_run") or "",
        }
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ 队列操作

    def add(self, urls: list[str], *, directory: str | None = None) -> list[dict]:
        """链接入队（状态 queued），返回新增项。"""
        added: list[dict] = []
        with _FILE_LOCK:
            data = self._load()
            for url in urls:
                url = (url or "").strip()
                if not url:
                    continue
                info = linksmod.parse_line(url)
                item = {
                    "id": uuid.uuid4().hex[:10],
                    "url": url,
                    "name": getattr(info, "display", "") or url[:80],
                    "directory": directory or "",
                    "added_at": int(time.time()),
                    "status": "queued",
                }
                data["items"].append(item)
                added.append(item)
            self._save(data)
        return added

    def remove(self, item_id: str) -> bool:
        """移除队列项，返回是否命中。"""
        with _FILE_LOCK:
            data = self._load()
            before = len(data["items"])
            data["items"] = [i for i in data["items"] if i.get("id") != item_id]
            self._save(data)
            return len(data["items"]) != before

    def set_settings(self, *, enabled: bool, time_str: str) -> dict:
        """设置每日定时派发（时间格式 HH:MM）。返回新 settings。"""
        t = (time_str or "").strip() or DEFAULT_TIME
        if not _TIME_RE.match(t):
            raise ValueError("时间格式应为 HH:MM（如 00:05）")
        with _FILE_LOCK:
            data = self._load()
            data["settings"]["enabled"] = bool(enabled)
            data["settings"]["time"] = t
            self._save(data)
            return dict(data["settings"])

    def snapshot(self) -> dict:
        with _FILE_LOCK:
            data = self._load()
        return {"items": list(data["items"]), "settings": dict(data["settings"])}

    # ------------------------------------------------------------ 派发

    def run_now(self, dispatch_fn) -> list[dict]:
        """立即派发所有待发项（queued/failed），返回逐项结果。

        dispatch_fn(url, directory) → 派发结果 dict（至少含 ok 字段）。
        已派发成功（done）的项跳过；失败项保留在队列可再次派发/手动移除。"""
        results: list[dict] = []
        with _FILE_LOCK:
            data = self._load()
            for item in data["items"]:
                if item.get("status") not in PENDING:
                    continue
                try:
                    res = dispatch_fn(item.get("url", ""),
                                      item.get("directory") or None) or {}
                except Exception as exc:  # noqa: BLE001 - 单项失败不阻断整批
                    res = {"ok": False, "error": str(exc)}
                if res.get("ok"):
                    item["status"] = "done"
                    item["backend"] = res.get("backend", "")
                    item["error"] = ""
                else:
                    item["status"] = "failed"
                    item["error"] = str(res.get("error") or "派发失败")
                results.append({
                    "id": item.get("id", ""), "url": item.get("url", ""),
                    "ok": bool(res.get("ok")),
                    "backend": res.get("backend", ""),
                    "error": item.get("error", ""),
                })
            self._save(data)
        return results

    def should_run(self, now: str | None = None) -> bool:
        """是否到了今天定时派发的点（启用 + 有待发项 + 到点 + 当天未跑过）。"""
        with _FILE_LOCK:
            data = self._load()
        st = data["settings"]
        if not st["enabled"]:
            return False
        if st["last_run"] == time.strftime("%Y-%m-%d"):
            return False
        if not any(i.get("status") in PENDING for i in data["items"]):
            return False
        hhmm = now or time.strftime("%H:%M")
        return hhmm >= st["time"]

    def tick(self, dispatch_fn) -> list[dict] | None:
        """定时检查：到点则派发待发项并记录今日已跑；未到点返回 None。"""
        if not self.should_run():
            return None
        results = self.run_now(dispatch_fn)
        with _FILE_LOCK:
            data = self._load()
            data["settings"]["last_run"] = time.strftime("%Y-%m-%d")
            self._save(data)
        return results
