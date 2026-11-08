#!/usr/bin/env python3
"""派发调度测试：自动降级、显式通道、统一任务视图、记录持久化。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import downloader                              # noqa: E402
import links                                    # noqa: E402
from backends.base import Backend, BackendError, ParsedResource, TaskView  # noqa: E402


class FakeBackend(Backend):
    def __init__(self, name, label, *, reachable=True, add_ok=True, tasks=None,
                 add_error="add failed"):
        self.name = name
        self.label = label
        self._reachable = reachable
        self._add_ok = add_ok
        self._add_error = add_error
        self._tasks = tasks or []
        self.added = []
        self.paused = []
        self.resumed = []
        self.deleted = []

    def status(self):
        return {"configured": True, "reachable": self._reachable,
                "detail": "ok" if self._reachable else "down"}

    def parse(self, url):
        return ParsedResource(name="f")

    def add(self, url, *, name=None, directory=None):
        if not self._add_ok:
            raise BackendError(self._add_error)
        self.added.append((url, name, directory))
        return f"{self.name}-task-{len(self.added)}"

    def list_tasks(self):
        return self._tasks

    def pause(self, tid): self.paused.append(tid)
    def resume(self, tid): self.resumed.append(tid)
    def delete(self, tid): self.deleted.append(tid)


class DispatchTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = (downloader.STATE_DIR, downloader.RECORDS_PATH)
        downloader.STATE_DIR = td
        downloader.RECORDS_PATH = td / "tasks.json"

    def tearDown(self):
        downloader.STATE_DIR, downloader.RECORDS_PATH = self._orig
        self._td.cleanup()

    def manager(self, pool: dict, cfg: dict | None = None):
        mgr = downloader.DownloadManager(cfg or {
            "engine_order": ["thunder_cloud", "thunder_engine", "aria2"]})
        mgr.backends = lambda: pool
        return mgr


class TestDispatch(DispatchTestBase):
    def test_auto_fallback(self):
        bad = FakeBackend("thunder_cloud", "云", reachable=False)
        also_bad = FakeBackend("thunder_engine", "引擎", add_ok=False)
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"thunder_cloud": bad, "thunder_engine": also_bad,
                            "aria2": good})
        res = mgr.dispatch("https://x/a.iso")
        self.assertTrue(res["ok"])
        self.assertEqual(res["backend"], "aria2")
        self.assertEqual(len(good.added), 1)

    def test_first_channel_wins(self):
        first = FakeBackend("thunder_cloud", "云")
        second = FakeBackend("aria2", "aria2")
        mgr = self.manager({"thunder_cloud": first, "aria2": second})
        res = mgr.dispatch("magnet:?xt=urn:btih:" + "a" * 40)
        self.assertEqual(res["backend"], "thunder_cloud")
        self.assertEqual(second.added, [])

    def test_explicit_backend_no_fallback(self):
        bad = FakeBackend("aria2", "aria2", add_ok=False)
        good = FakeBackend("thunder_cloud", "云")
        mgr = self.manager({"thunder_cloud": good, "aria2": bad})
        res = mgr.dispatch("https://x/a", backend="aria2")
        self.assertFalse(res["ok"])
        self.assertEqual(good.added, [])

    def test_ed2k_rejected(self):
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        info = links.parse_line("ed2k://|file|d.mkv|1|aa|/")
        res = mgr.dispatch(info)
        self.assertFalse(res["ok"])
        self.assertEqual(good.added, [])

    def test_all_fail_reports_errors(self):
        bad = FakeBackend("aria2", "aria2", reachable=False)
        mgr = self.manager({"aria2": bad})
        res = mgr.dispatch("https://x/a")
        self.assertFalse(res["ok"])
        self.assertIn("aria2", res["error"])

    def test_record_saved(self):
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        mgr.dispatch("https://x/a")
        self.assertEqual(len(mgr.records), 1)
        self.assertEqual(mgr.records[0]["backend"], "aria2")
        # 磁盘持久化
        self.assertTrue(downloader.RECORDS_PATH.exists())


class TestUnifiedTasks(DispatchTestBase):
    def test_merge_and_gone(self):
        live = [TaskView(backend="aria2", id="g1", name="live.bin",
                         state="running", progress=10, url="https://x/live.bin")]
        good = FakeBackend("aria2", "aria2", tasks=live)
        mgr = self.manager({"aria2": good})
        mgr._record("aria2", "g1", "https://x/live.bin", "live.bin", "")
        mgr._record("aria2", "gone1", "https://x/old.bin", "old.bin", "")
        tasks, errors = mgr.unified_tasks()
        self.assertEqual(errors, {})
        by_id = {t["id"]: t for t in tasks}
        self.assertEqual(by_id["g1"]["state"], "running")
        self.assertEqual(by_id["gone1"]["state"], "gone")

    def test_backend_error_reported(self):
        bad = FakeBackend("aria2", "aria2", reachable=False)
        bad.list_tasks = lambda: (_ for _ in ()).throw(BackendError("down"))
        mgr = self.manager({"aria2": bad})
        _, errors = mgr.unified_tasks()
        self.assertIn("aria2", errors)

    def test_delete_removes_record(self):
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        mgr._record("aria2", "g9", "https://x/a", "a", "")
        mgr.control("delete", "aria2", "g9")
        self.assertEqual(good.deleted, ["g9"])
        self.assertEqual(mgr.records, [])


class TestRetry(DispatchTestBase):
    def test_retry_redispatches_same_channel(self):
        """重试 = 删旧任务 + 原通道按原链接/目录重新派发（删除重建语义）。"""
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        mgr._record("aria2", "old-1", "https://x/a.iso", "a.iso", "/dl")
        res = mgr.retry("aria2", "old-1")
        self.assertTrue(res["ok"])
        self.assertEqual(good.deleted, ["old-1"])          # 旧任务已删
        self.assertEqual(len(good.added), 1)               # 已重新派发
        self.assertEqual(good.added[0][0], "https://x/a.iso")
        self.assertEqual(good.added[0][2], "/dl")          # 沿用原目录
        # 旧记录换成新任务记录
        self.assertEqual([r["id"] for r in mgr.records], [res["id"]])
        self.assertEqual(mgr.records[0]["url"], "https://x/a.iso")

    def test_retry_uses_live_task_url_without_record(self):
        """无本地记录（别处创建的任务）：从引擎实时任务里取原始链接重试。"""
        live = [TaskView(backend="aria2", id="g5", name="b.iso",
                         state="error", url="https://x/b.iso")]
        good = FakeBackend("aria2", "aria2", tasks=live)
        mgr = self.manager({"aria2": good})
        res = mgr.retry("aria2", "g5")
        self.assertTrue(res["ok"])
        self.assertEqual(good.added[0][0], "https://x/b.iso")

    def test_retry_delete_failure_does_not_block(self):
        """旧任务已不在引擎（删除报错）时重试仍要走通。"""
        good = FakeBackend("aria2", "aria2")
        good.delete = lambda tid: (_ for _ in ()).throw(BackendError("gone"))
        mgr = self.manager({"aria2": good})
        mgr._record("aria2", "old-1", "https://x/a.iso", "a.iso", "")
        res = mgr.retry("aria2", "old-1")
        self.assertTrue(res["ok"])

    def test_retry_without_url_fails(self):
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        with self.assertRaises(BackendError):
            mgr.retry("aria2", "missing")
        self.assertEqual(good.added, [])

    def test_retry_unknown_backend(self):
        mgr = self.manager({})
        with self.assertRaises(BackendError):
            mgr.retry("nope", "x")


class TestHistoryCheck(DispatchTestBase):
    def test_magnet_same_infohash_matches_across_trackers(self):
        """磁力查重按信息哈希归一：不同 tracker/dn 的同资源命中同一条。"""
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        h = "a" * 40
        mgr.dispatch(f"magnet:?xt=urn:btih:{h}&dn=x&tr=http://t1/ann")
        res = mgr.history_check(f"magnet:?xt=urn:btih:{h.upper()}&dn=y&tr=http://t2/ann")
        self.assertTrue(res["downloaded"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["matches"][0]["backend"], "aria2")

    def test_base32_and_hex_same_resource(self):
        import base64
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        hexhash = "a" * 40
        b32 = base64.b32encode(bytes.fromhex(hexhash)).decode()
        mgr.dispatch(f"magnet:?xt=urn:btih:{hexhash}")
        self.assertTrue(mgr.history_check(f"magnet:?xt=urn:btih:{b32}")["downloaded"])

    def test_different_resource_no_match(self):
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        mgr.dispatch("magnet:?xt=urn:btih:" + "a" * 40)
        res = mgr.history_check("magnet:?xt=urn:btih:" + "b" * 40)
        self.assertFalse(res["downloaded"])
        self.assertEqual(res["matches"], [])

    def test_dispatch_result_carries_duplicate(self):
        """重复派发同一链接：结果带 duplicate 提示（避免重复下载）。"""
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"aria2": good})
        first = mgr.dispatch("https://x/a.iso")
        self.assertNotIn("duplicate", first)          # 首次派发无历史
        second = mgr.dispatch("https://x/a.iso")
        self.assertTrue(second["ok"])
        self.assertEqual(second["duplicate"]["id"], first["id"])


class TestQuotaDispatch(DispatchTestBase):
    """配额感知派发：撞满跳过引擎、撞限标记、重试自动换通道。"""

    QUOTA_ERR = ("创建任务失败：HTTP 402: task_create_count_limit "
                 "任务创建次数达到上限")

    def test_blocked_engine_skipped_in_auto(self):
        eng = FakeBackend("thunder_engine", "引擎")
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"thunder_engine": eng, "aria2": good},
                           {"engine_order": ["thunder_engine", "aria2"]})
        mgr.quota.note_engine_quota_hit()
        res = mgr.dispatch("https://x/a.iso")
        self.assertTrue(res["ok"])
        self.assertEqual(res["backend"], "aria2")
        self.assertEqual(eng.added, [])                # 引擎被跳过未尝试

    def test_blocked_engine_still_tried_when_explicit(self):
        eng = FakeBackend("thunder_engine", "引擎")
        mgr = self.manager({"thunder_engine": eng},
                           {"engine_order": ["thunder_engine"]})
        mgr.quota.note_engine_quota_hit()
        res = mgr.dispatch("https://x/b.iso", backend="thunder_engine")
        self.assertTrue(res["ok"])                     # 显式指定：仍尝试引擎
        self.assertEqual(len(eng.added), 1)

    def test_quota_error_marks_blocked_and_falls_back(self):
        eng = FakeBackend("thunder_engine", "引擎", add_ok=False,
                          add_error=self.QUOTA_ERR)
        good = FakeBackend("aria2", "aria2")
        mgr = self.manager({"thunder_engine": eng, "aria2": good},
                           {"engine_order": ["thunder_engine", "aria2"]})
        res = mgr.dispatch("https://x/c.iso")
        self.assertTrue(res["ok"])
        self.assertEqual(res["backend"], "aria2")
        self.assertTrue(mgr.quota.engine_blocked())    # 撞限已记录
        self.assertEqual(mgr.quota.snapshot()["used"], 0)

    def test_engine_create_counted(self):
        eng = FakeBackend("thunder_engine", "引擎")
        mgr = self.manager({"thunder_engine": eng},
                           {"engine_order": ["thunder_engine"]})
        mgr.dispatch("https://x/d.iso")
        self.assertEqual(mgr.quota.snapshot()["used"], 1)

    def test_retry_quota_error_switches_channel(self):
        """重试撞配额自动换通道：不再原通道死循环撞 402。"""
        eng = FakeBackend("thunder_engine", "引擎")
        cloud = FakeBackend("thunder_cloud", "云")
        mgr = self.manager({"thunder_cloud": cloud, "thunder_engine": eng},
                           {"engine_order": ["thunder_cloud", "thunder_engine"]})
        res0 = mgr.dispatch("https://x/e.iso", backend="thunder_engine")
        self.assertTrue(res0["ok"])
        eng._add_ok = False
        eng._add_error = self.QUOTA_ERR
        res = mgr.retry("thunder_engine", res0["id"])
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("switched"))
        self.assertEqual(res["backend"], "thunder_cloud")
        self.assertIn("配额", res.get("message", ""))
        self.assertIn("云", res.get("message", ""))

    def test_retry_non_quota_error_does_not_switch(self):
        eng = FakeBackend("thunder_engine", "引擎")
        cloud = FakeBackend("thunder_cloud", "云")
        mgr = self.manager({"thunder_cloud": cloud, "thunder_engine": eng},
                           {"engine_order": ["thunder_cloud", "thunder_engine"]})
        res0 = mgr.dispatch("https://x/f.iso", backend="thunder_engine")
        eng._add_ok = False
        eng._add_error = "boom"                        # 非配额错误
        with self.assertRaises(BackendError):
            mgr.retry("thunder_engine", res0["id"])
        self.assertEqual(cloud.added, [])              # 未换通道

    def test_retry_switch_all_fail_raises(self):
        eng = FakeBackend("thunder_engine", "引擎")
        cloud = FakeBackend("thunder_cloud", "云", add_ok=False)
        mgr = self.manager({"thunder_cloud": cloud, "thunder_engine": eng},
                           {"engine_order": ["thunder_cloud", "thunder_engine"]})
        res0 = mgr.dispatch("https://x/g.iso", backend="thunder_engine")
        eng._add_ok = False
        eng._add_error = self.QUOTA_ERR
        with self.assertRaises(BackendError) as ctx:
            mgr.retry("thunder_engine", res0["id"])
        self.assertIn("配额已满", str(ctx.exception))


class TestDispatchBudget(DispatchTestBase):
    """0.1.31：批量派发软预算——超预算尾部链接返回明确错误，
    而不是继续同步派发把请求拖过中继超时（裸 502 什么都看不到）。"""

    def _slow_backend(self):
        class Slow(FakeBackend):
            def add(self, url, *, name=None, directory=None):
                time.sleep(0.05)               # 模拟慢上游
                return super().add(url, name=name, directory=directory)
        return Slow("thunder_cloud", "云")

    def test_budget_exhausted_tail_reports_clear_error(self):
        good = self._slow_backend()
        mgr = self.manager({"thunder_cloud": good})
        orig = downloader.DISPATCH_BUDGET
        downloader.DISPATCH_BUDGET = 0.02      # 首条 50ms 即超预算
        try:
            links = [f"https://x/{i}.iso" for i in range(5)]
            res = mgr.dispatch_many(links)
        finally:
            downloader.DISPATCH_BUDGET = orig
        self.assertEqual(len(res), 5)
        self.assertTrue(res[0]["ok"])           # 首条必派
        for r in res[1:]:                       # 尾部必带明确错误
            self.assertFalse(r["ok"])
            self.assertIn("派发预算耗尽", r["error"])
            self.assertIn("未派发", r["error"])
        self.assertEqual(len(good.added), 1)    # 不再空耗上游

    def test_budget_not_hit_when_fast(self):
        good = FakeBackend("thunder_cloud", "云")
        mgr = self.manager({"thunder_cloud": good})
        res = mgr.dispatch_many([f"https://x/{i}.iso" for i in range(3)])
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual(len(good.added), 3)

    def test_directory_passed_through_to_backend(self):
        """目录参数穿透：dispatch_many 的 directory 必须传到每条 add。"""
        good = FakeBackend("thunder_cloud", "云")
        mgr = self.manager({"thunder_cloud": good})
        mgr.dispatch_many(["https://x/1.iso", "https://x/2.iso"],
                          directory="电影/2025")
        self.assertEqual([d for _, _, d in good.added],
                         ["电影/2025", "电影/2025"])


if __name__ == "__main__":
    unittest.main()
