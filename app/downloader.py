#!/usr/bin/env python3
"""下载调度：链接派发（自动降级）、统一任务视图、任务记录持久化。

派发策略（engine_order 为降级链，默认迅雷远程 → 迅雷引擎 → aria2）：
  1) 指定通道：仅用该通道；失败即报错（用户显式选择不静默换通道）；
  2) auto：按降级链逐个尝试，通道不可用/创建失败自动落到下一个，
     全部失败才报错。ed2k 等不支持的链接直接拒绝派发。

配额感知（迅雷引擎非会员每日任务创建次数有限，撞限返回 HTTP 402）：
  - 撞满后当天 auto 派发跳过引擎（quota.py 记录，跨天自动恢复）；
  - 重试撞配额自动换 auto 降级链重派（迅雷远程通道不占引擎配额）。
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
import links as linksmod
import quota as quotamod

RECORDS_PATH = STATE_DIR / "tasks.json"

# 批量派发软预算（0.1.31）：网关中继响应预算 180s，同步派发链必须远小于它。
# 超预算的尾部链接直接返回「预算耗尽」错误（信息可见），而不是继续同步派发
# 把整个请求拖过中继超时（裸 502，用户什么都看不到）。
DISPATCH_BUDGET = 50.0


class DownloadManager:
    def __init__(self, cfg: dict, client: httpx.Client | None = None):
        self.cfg = cfg
        # trust_env=False：忽略 HTTP(S)_PROXY 环境变量，直连国内迅雷接口。
        # NAS 上残留的代理变量会让登录请求绕道境外出口触发风控。
        self.client = client or httpx.Client(trust_env=False)
        self._lock = threading.Lock()
        self.records: list[dict] = self._load_records()
        # 引擎每日配额跟踪（路径跟 RECORDS_PATH 同目录，测试可整体换目录）
        self.quota = quotamod.QuotaTracker(STATE_DIR / "quota.json")

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
            # id 统一字符串化：引擎可能返回整型 id，与 live 视图（str）不一致
            # 会导致 unified_tasks 匹配失败（同任务出「已清理」+重复行）。
            "backend": backend, "id": str(task_id), "url": url,
            "name": name, "dir": directory, "created_at": int(time.time()),
            "identity": linksmod.identity(url),
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
        # 派发前的下载历史（不含本次新建记录）：命中时结果带重复提示
        prior = self.history_check(url)

        errors: list[str] = []
        for name_key in order:
            impl = pool[name_key]
            # 今日引擎配额已满：auto 派发直接跳过（别再撞 402 报错）；
            # 用户显式指定引擎通道时仍尝试（尊重显式选择）。
            if (name_key == "thunder_engine" and backend == "auto"
                    and self.quota.engine_blocked()):
                errors.append(f"{impl.label}: 今日任务创建次数已满，已跳过（明日自动恢复）")
                continue
            try:
                # 显式指定通道时跳过 status 预检直达 add（0.1.29）：预检要多打
                # 2~3 个接口，上游卡顿时白白吃掉网关响应预算（用户实测派发
                # 提示 502 的帮凶）；auto 降级链仍预检——那是通道探活的依据。
                if backend == "auto":
                    st = impl.status()
                    if not st.get("reachable"):
                        errors.append(f"{impl.label}: {st.get('detail') or '不可用'}")
                        continue
                task_id = impl.add(url, name=name or None, directory=directory)
                if name_key == "thunder_engine":
                    self.quota.note_engine_create()
                self._record(name_key, task_id, url, name, directory or "")
                result = {"ok": True, "url": url, "backend": name_key,
                          "id": task_id, "label": impl.label}
                if prior["downloaded"]:
                    result["duplicate"] = prior["matches"][0]
                return result
            except BackendError as exc:
                if name_key == "thunder_engine" and quotamod.is_quota_error(str(exc)):
                    self.quota.note_engine_quota_hit()
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
        """批量派发（保持串行：records/quota 写盘无锁，并行会竞态）。
        DISPATCH_BUDGET 软预算：超预算的尾部链接不再派发，返回明确错误。"""
        links = list(links)
        t0 = time.monotonic()
        out: list[dict] = []
        for i, link in enumerate(links):
            if out and time.monotonic() - t0 > DISPATCH_BUDGET:
                url = getattr(link, "url", None) or str(link)
                out.append({
                    "ok": False, "url": url,
                    "error": (f"派发预算耗尽（{DISPATCH_BUDGET:.0f}s）："
                              f"前 {len(out)} 条已处理，本条及后续"
                              f"共 {len(links) - i} 条未派发，请分批重试"),
                })
                continue
            out.append(self.dispatch(link, backend=backend, directory=directory))
        return out

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
            k = (rec.get("backend"), str(rec.get("id")))
            if k in live:
                item = dict(live[k])
                item["url"] = item.get("url") or rec.get("url", "")
                item["created_at"] = rec.get("created_at")
                seen.add(k)
            else:
                item = {"backend": rec.get("backend"), "id": str(rec.get("id")),
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
                            if not (r.get("backend") == backend
                                    and str(r.get("id")) == str(task_id))]
            self._save_records()
        else:
            raise BackendError(f"未知操作：{action}")

    # ------------------------------------------------------------ 重试

    def retry(self, backend: str, task_id: str) -> dict:
        """失败/已清理任务重试：取回原始链接后删旧任务、原通道重新派发。

        迅雷 drive/v1 没有原生重试接口（接口本身只支持暂停/恢复/删除），
        aria2 失败任务同样只能移除重建——统一「删除重建」语义，
        新任务 id 与原任务不同。

        配额换通道：原通道撞每日配额（HTTP 402）时自动改用 auto 降级链
        重派（迅雷远程/aria2 不占引擎配额），结果带 switched/message 标注，
        避免「重试 = 原通道再撞一次配额」死循环。"""
        pool = self.backends()
        impl = pool.get(backend)
        if not impl:
            raise BackendError(f"未知通道：{backend}")
        rec = next((r for r in reversed(self.records)
                    if r.get("backend") == backend
                    and str(r.get("id")) == str(task_id)), None)
        url = (rec or {}).get("url", "")
        directory = (rec or {}).get("dir", "")
        if not url:
            # 无本地记录（别处创建的任务）：从引擎实时任务里取原始链接
            try:
                for tv in impl.list_tasks():
                    if tv.id == task_id and tv.url:
                        url = tv.url
                        break
            except BackendError:
                pass
        if not url:
            raise BackendError("找不到该任务的原始链接，无法重试（请复制链接重新派发）")
        # 删旧任务与旧记录（任务可能已不在引擎，删除失败不阻断重试）
        try:
            impl.delete(task_id)
        except BackendError:
            pass
        self.records = [r for r in self.records
                        if not (r.get("backend") == backend
                                and str(r.get("id")) == str(task_id))]
        self._save_records()
        info = linksmod.parse_line(url)
        directory = directory or self.cfg.get("default_dir") or None
        res = self.dispatch(info, backend=backend, directory=directory)
        if res.get("ok"):
            return res
        err = res.get("error") or "重试派发失败"
        if backend != "auto" and quotamod.is_quota_error(err):
            # 原通道今日配额已满：换 auto 降级链重派（引擎撞满会被跳过）
            res2 = self.dispatch(info, backend="auto", directory=directory)
            if res2.get("ok"):
                res2["switched"] = True
                res2["message"] = (f"原通道今日配额已满，已换 "
                                   f"{res2.get('label')} 通道重新派发")
                return res2
            raise BackendError(f"原通道配额已满；换通道重试也失败："
                               f"{res2.get('error')}")
        raise BackendError(err)

    # ------------------------------------------------------------ 历史查重

    def history_check(self, url: str) -> dict:
        """下载历史查重：同一资源是否派发过。

        链接身份按 links.identity 归一——magnet 按信息哈希匹配，
        不同 tracker / dn 的同一资源命中同一条记录。"""
        ident = linksmod.identity(url)
        matches: list[dict] = []
        for r in reversed(self.records):          # 新 → 旧
            r_ident = r.get("identity") or linksmod.identity(r.get("url", ""))
            if ident and r_ident == ident:
                matches.append({
                    "backend": r.get("backend", ""),
                    "id": r.get("id", ""),
                    "name": r.get("name") or r.get("url", "")[:80],
                    "dir": r.get("dir", ""),
                    "url": r.get("url", ""),
                    "created_at": r.get("created_at"),
                })
        return {"url": url, "identity": ident,
                "downloaded": bool(matches), "count": len(matches),
                "matches": matches}
