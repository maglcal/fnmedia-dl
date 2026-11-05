#!/usr/bin/env python3
"""下载调度：链接派发（自动降级）、统一任务视图、任务记录持久化。

派发策略（engine_order 为降级链，默认迅雷远程 → 迅雷引擎 → aria2）：
  1) 指定通道：仅用该通道；失败即报错（用户显式选择不静默换通道）；
  2) auto：按降级链逐个尝试，通道不可用/创建失败自动落到下一个，
     全部失败才报错。ed2k 等不支持的链接直接拒绝派发。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx

from backends import Backend, BackendError, build_backends
from backends.base import TaskView
from config import STATE_DIR
import config as cfgmod

RECORDS_PATH = STATE_DIR / "tasks.json"


class DownloadManager:
    def __init__(self, cfg: dict, client: httpx.Client | None = None):
        self.cfg = cfg
        # trust_env=False：忽略 HTTP(S)_PROXY 环境变量，直连国内迅雷接口。
        # NAS 上残留的代理变量会让登录请求绕道境外出口触发风控。
        self.client = client or httpx.Client(trust_env=False)
        self._lock = threading.Lock()
        self.records: list[dict] = self._load_records()

    # ------------------------------------------------------------ 通道

    def backends(self) -> dict[str, Backend]:
        def persist_cloud(conf: dict) -> None:
            self.cfg.setdefault("thunder_cloud", {}).update(conf)
            # 必须落盘：每个请求都会新建 DownloadManager，只改内存的话
            # device_id / 登录态 / 风控验证状态换个请求就全丢了。
            cfgmod.save(self.cfg)

        return build_backends(self.cfg, self.client, persist_cloud)

    # ------------------------------------------------------------ 记录

    def _load_records(self) -> list[dict]:
        try:
            return json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save_records(self) -> None:
        with self._lock:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = RECORDS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.records, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(RECORDS_PATH)

    def _record(self, backend: str, task_id: str, url: str, name: str,
                directory: str) -> None:
        self.records.append({
            "backend": backend, "id": task_id, "url": url,
            "name": name, "dir": directory, "created_at": int(time.time()),
        })
        self._save_records()

    # ------------------------------------------------------------ 派发

    def dispatch(self, link, *, backend: str = "auto",
                 directory: str | None = None) -> dict:
        """单条链接派发。link 为 links.LinkInfo 或裸 URL 字符串。"""
        url = getattr(link, "url", None) or str(link)
        name = getattr(link, "display", "") or ""
        dispatchable = getattr(link, "dispatchable", True)
        if not dispatchable:
            return {"ok": False, "url": url,
                    "error": getattr(link, "reason", "") or "该链接类型暂不支持派发"}

        pool = self.backends()
        order = self._order(backend, pool)
        if not order:
            return {"ok": False, "url": url, "error": "没有可用的下载通道"}

        errors: list[str] = []
        for name_key in order:
            impl = pool[name_key]
            try:
                st = impl.status()
                if not st.get("reachable"):
                    errors.append(f"{impl.label}: {st.get('detail') or '不可用'}")
                    continue
                task_id = impl.add(url, name=name or None, directory=directory)
                self._record(name_key, task_id, url, name, directory or "")
                return {"ok": True, "url": url, "backend": name_key,
                        "id": task_id, "label": impl.label}
            except BackendError as exc:
                errors.append(f"{impl.label}: {exc}")
                if backend != "auto":
                    break
        return {"ok": False, "url": url,
                "error": "；".join(errors) or "全部通道派发失败"}

    def _order(self, backend: str, pool: dict[str, Backend]) -> list[str]:
        if backend and backend != "auto":
            return [backend] if backend in pool else []
        order = [b for b in self.cfg.get("engine_order", []) if b in pool]
        for b in pool:                       # 兜底补全未配置顺序
            if b not in order:
                order.append(b)
        return order

    def dispatch_many(self, links, *, backend: str = "auto",
                      directory: str | None = None) -> list[dict]:
        return [self.dispatch(link, backend=backend, directory=directory)
                for link in links]

    # ------------------------------------------------------------ 任务视图

    def unified_tasks(self) -> tuple[list[dict], dict]:
        """合并各通道实时任务 + 本地记录（补 URL/时间；通道查不到的标注 gone）。"""
        pool = self.backends()
        live: dict[tuple[str, str], dict] = {}
        errors: dict[str, str] = {}
        for key, impl in pool.items():
            try:
                for tv in impl.list_tasks():
                    live[(key, tv.id)] = tv.to_dict()
            except BackendError as exc:
                errors[key] = str(exc)

        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for rec in reversed(self.records):
            k = (rec.get("backend"), rec.get("id"))
            if k in live:
                item = dict(live[k])
                item["url"] = item.get("url") or rec.get("url", "")
                item["created_at"] = rec.get("created_at")
                seen.add(k)
            else:
                item = {"backend": rec.get("backend"), "id": rec.get("id"),
                        "name": rec.get("name") or rec.get("url", "")[:80],
                        "state": "gone", "progress": 0, "speed": None,
                        "size": None, "url": rec.get("url", ""),
                        "message": "任务已不在引擎列表中（可能已完成清理）",
                        "created_at": rec.get("created_at")}
            out.append(item)
        # 通道里存在但本地无记录的（别处创建的任务），也纳入统一视图
        for k, item in live.items():
            if k not in seen:
                item["created_at"] = None
                out.append(item)
        return out, errors

    # ------------------------------------------------------------ 操作

    def control(self, action: str, backend: str, task_id: str) -> None:
        pool = self.backends()
        impl = pool.get(backend)
        if not impl:
            raise BackendError(f"未知通道：{backend}")
        if action == "pause":
            impl.pause(task_id)
        elif action == "resume":
            impl.resume(task_id)
        elif action == "delete":
            impl.delete(task_id)
            self.records = [r for r in self.records
                            if not (r.get("backend") == backend and r.get("id") == task_id)]
            self._save_records()
        else:
            raise BackendError(f"未知操作：{action}")
