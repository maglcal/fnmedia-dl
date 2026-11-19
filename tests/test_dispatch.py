#!/usr/bin/env python3
"""派发调度测试：自动降级、显式通道、统一任务视图、记录持久化。"""
from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import downloader                              # noqa: E402
import links                                    # noqa: E402
import magnetmeta                               # noqa: E402
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
        self.deleted_with_files = []
        self.renamed = []

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
    def delete(self, tid, *, with_files=False):
        self.deleted.append(tid)
        self.deleted_with_files.append(with_files)

    def rename_task(self, tid, name):
        self.renamed.append((tid, name))
        return True


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

    def test_true_name_record_beats_fallback_live_name(self):
        """真名显示回归钉（0.1.45.6）：真名回填只写本地记录，合并必须让
        记录真名盖过引擎侧兜底名——旧合并只认引擎名，回填永远上不了屏。"""
        url = "magnet:?xt=urn:btih:" + "a" * 40
        fallback = "magnet:aaaaaaaa…"
        live = [TaskView(backend="aria2", id="g1", name=fallback,
                         state="running", progress=10, url=url)]
        mgr = self.manager({"aria2": FakeBackend("aria2", "aria2", tasks=live)})
        mgr._record("aria2", "g1", url, fallback, "")
        self.assertTrue(
            mgr._rename_record("aria2", "g1", fallback, "真名.S01E01"))
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(tasks[0]["name"], "真名.S01E01")

    def test_engine_real_name_wins_over_record(self):
        """引擎侧真实改名（非兜底名）始终优先于本地记录。"""
        url = "magnet:?xt=urn:btih:" + "b" * 40
        fallback = "magnet:bbbbbbbb…"
        live = [TaskView(backend="aria2", id="g1", name="引擎改的名",
                         state="running", progress=10, url=url)]
        mgr = self.manager({"aria2": FakeBackend("aria2", "aria2", tasks=live)})
        mgr._record("aria2", "g1", url, fallback, "")
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(tasks[0]["name"], "引擎改的名")

    def test_both_fallback_keeps_live_name(self):
        """真名还没解析出来（双方都是兜底名）→ 维持显示不误换。"""
        url = "magnet:?xt=urn:btih:" + "c" * 40
        fallback = "magnet:cccccccc…"
        live = [TaskView(backend="aria2", id="g1", name=fallback,
                         state="running", progress=10, url=url)]
        mgr = self.manager({"aria2": FakeBackend("aria2", "aria2", tasks=live)})
        mgr._record("aria2", "g1", url, fallback, "")
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(tasks[0]["name"], fallback)

    def test_is_fallback_name_shapes(self):
        """兜底名两形态：磁力短名 / 截断链接名；其余为真名。"""
        url = "https://x/" + "d" * 100
        self.assertTrue(downloader.DownloadManager._is_fallback_name(
            "magnet:300529cf…", "magnet:?xt=urn:btih:300529cf" + "0" * 32))
        self.assertTrue(downloader.DownloadManager._is_fallback_name(
            url[:80], url))
        self.assertFalse(downloader.DownloadManager._is_fallback_name(
            "真名.S01E01.1080p", url))

    def test_same_task_on_two_backends_single_row(self):
        """同一底层任务被两个通道各列一行时合并为一行（回归）。

        同一迅雷实例从远程通道与本地直连面板都能看到同一任务，
        删一条另一条也跟着消失——确认是同一任务，不得出两行。
        归属取有本地记录的通道。"""
        tv = TaskView(backend="thunder_cloud", id="t1", name="a.mkv",
                      state="running", progress=10, url="https://x/a.mkv")
        tv2 = TaskView(backend="thunder_engine", id="t1", name="a.mkv",
                       state="running", progress=10, url="https://x/a.mkv")
        cloud = FakeBackend("thunder_cloud", "cloud", tasks=[tv])
        engine = FakeBackend("thunder_engine", "engine", tasks=[tv2])
        mgr = self.manager({"thunder_cloud": cloud, "thunder_engine": engine})
        mgr._record("thunder_cloud", "t1", "https://x/a.mkv", "a.mkv", "")
        tasks, errors = mgr.unified_tasks()
        self.assertEqual(errors, {})
        self.assertEqual(len(tasks), 1, tasks)
        self.assertEqual(tasks[0]["backend"], "thunder_cloud")

    def test_same_task_two_backends_without_record_first_wins(self):
        """无本地记录时同任务跨通道也只出一行（按通道顺序归属）。"""
        tv = TaskView(backend="thunder_cloud", id="t1", name="a.mkv",
                      state="running", progress=10, url="https://x/a.mkv")
        tv2 = TaskView(backend="thunder_engine", id="t1", name="a.mkv",
                       state="running", progress=10, url="https://x/a.mkv")
        cloud = FakeBackend("thunder_cloud", "cloud", tasks=[tv])
        engine = FakeBackend("thunder_engine", "engine", tasks=[tv2])
        mgr = self.manager({"thunder_cloud": cloud, "thunder_engine": engine})
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(len(tasks), 1, tasks)

    def test_different_ids_same_url_not_merged(self):
        """不同引擎的两条同名同链接任务（不同 id）是两条真实下载，不合并。"""
        tv = TaskView(backend="thunder_cloud", id="c1", name="a.mkv",
                      state="running", progress=10, url="https://x/a.mkv")
        tv2 = TaskView(backend="aria2", id="g1", name="a.mkv",
                       state="running", progress=10, url="https://x/a.mkv")
        cloud = FakeBackend("thunder_cloud", "cloud", tasks=[tv])
        aria = FakeBackend("aria2", "aria2", tasks=[tv2])
        mgr = self.manager({"thunder_cloud": cloud, "aria2": aria})
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(len(tasks), 2, tasks)

    def test_hidden_backend_tasks_not_listed(self):
        """隐藏通道不参与任务列表（通道管理：界面只保留启用通道）。"""
        tv = TaskView(backend="aria2", id="g1", name="live.bin",
                      state="running", progress=10, url="https://x/live.bin")
        hidden = FakeBackend("aria2", "aria2", tasks=[tv])
        hidden.list_tasks = lambda: (_ for _ in ()).throw(
            AssertionError("隐藏通道不应被查询"))
        cloud = FakeBackend("thunder_cloud", "cloud")
        mgr = self.manager({"thunder_cloud": cloud, "aria2": hidden},
                           {"engine_order": ["thunder_cloud", "aria2"],
                            "disabled_backends": ["aria2"]})
        tasks, errors = mgr.unified_tasks()
        self.assertEqual(tasks, [])
        self.assertEqual(errors, {})

    def test_duplicate_records_single_row(self):
        """同一 (通道, id) 落了两条记录也只出一行（防重复行回归）。"""
        live = [TaskView(backend="aria2", id="g1", name="live.bin",
                         state="running", progress=10, url="https://x/live.bin")]
        good = FakeBackend("aria2", "aria2", tasks=live)
        mgr = self.manager({"aria2": good})
        mgr._record("aria2", "g1", "https://x/live.bin", "live.bin", "")
        mgr._record("aria2", "g1", "https://x/live.bin", "live.bin", "")
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(len(tasks), 1, tasks)

    def test_gone_record_suppressed_when_live_on_other_backend(self):
        """记录所在通道查不到、但其它通道有同任务实时行：只出实时行，
        不再补「已清理」幽灵行。"""
        tv = TaskView(backend="thunder_engine", id="t1", name="a.mkv",
                      state="running", progress=10, url="https://x/a.mkv")
        cloud = FakeBackend("thunder_cloud", "cloud")
        engine = FakeBackend("thunder_engine", "engine", tasks=[tv])
        mgr = self.manager({"thunder_cloud": cloud, "thunder_engine": engine})
        mgr._record("thunder_cloud", "t1", "https://x/a.mkv", "a.mkv", "")
        tasks, _ = mgr.unified_tasks()
        self.assertEqual(len(tasks), 1, tasks)
        self.assertEqual(tasks[0]["state"], "running")
        self.assertEqual(tasks[0]["backend"], "thunder_engine")

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


class TestMagnetTrueName(DispatchTestBase):
    """裸磁力任务真名回填（0.1.45.3）：解析成功回写任务记录名。"""

    IH = "80ae772175e56bcbf9e9a4e1689fa023aae68f9e"

    def _bare_link(self):
        return links.LinkInfo(
            kind="magnet", url=f"magnet:?xt=urn:btih:{self.IH}",
            display=f"magnet:{self.IH[:8]}…",
            meta={"infohash": self.IH, "dn": ""})

    def test_rename_record_only_when_fallback_matches(self):
        mgr = self.manager({})
        mgr._record("aria2", "t1", f"magnet:?xt=urn:btih:{self.IH}",
                    f"magnet:{self.IH[:8]}…", "")
        self.assertFalse(mgr._rename_record("aria2", "t1", "别的名", "真名"))
        self.assertTrue(mgr._rename_record("aria2", "t1",
                                           f"magnet:{self.IH[:8]}…", "真名"))
        self.assertEqual(mgr._load_records()[0]["name"], "真名")

    def test_resolve_and_rename_writes_true_name(self):
        mgr = self.manager({})
        mgr._record("thunder_engine", "t2", f"magnet:?xt=urn:btih:{self.IH}",
                    f"magnet:{self.IH[:8]}…", "")
        orig = magnetmeta.resolve_name
        magnetmeta.resolve_name = \
            lambda ih, timeout=0, trackers=None: "嫁金钗[全30集]"
        try:
            mgr._resolve_and_rename(self._bare_link(), "thunder_engine", "t2",
                                    f"magnet:{self.IH[:8]}…", attempts=1)
        finally:
            magnetmeta.resolve_name = orig
        self.assertEqual(mgr._load_records()[0]["name"], "嫁金钗[全30集]")

    def test_resolve_failure_keeps_fallback(self):
        mgr = self.manager({})
        mgr._record("aria2", "t3", f"magnet:?xt=urn:btih:{self.IH}",
                    f"magnet:{self.IH[:8]}…", "")
        orig = magnetmeta.resolve_name
        magnetmeta.resolve_name = lambda ih, timeout=0, trackers=None: None
        try:
            mgr._resolve_and_rename(self._bare_link(), "aria2", "t3",
                                    f"magnet:{self.IH[:8]}…", attempts=1)
        finally:
            magnetmeta.resolve_name = orig
        self.assertEqual(mgr._load_records()[0]["name"], f"magnet:{self.IH[:8]}…")

    def test_backfill_candidates_only_fallback_magnets(self):
        recs = [
            {"backend": "aria2", "id": "1",
             "url": f"magnet:?xt=urn:btih:{self.IH}",
             "name": f"magnet:{self.IH[:8]}…"},
            {"backend": "aria2", "id": "2",
             "url": f"magnet:?xt=urn:btih:{self.IH}", "name": "已有真名"},
            {"backend": "aria2", "id": "3", "url": "https://x/a.bin",
             "name": "a.bin"},
            {"backend": "aria2", "id": "4",
             "url": f"magnet:?xt=urn:btih:{self.IH}",
             "name": f"magnet:?xt=urn:btih:{self.IH}"[:80]},
        ]
        out = downloader.DownloadManager._backfill_candidates(recs)
        self.assertEqual([c[1] for c in out], ["1", "4"])

    # ---- 引擎侧同步改名（0.1.45.7，修迅雷里任务/文件夹名残留兜底名）----

    def test_resolve_and_rename_renames_engine(self):
        """真名解析成功后不只回写记录——同步改引擎侧任务名。"""
        good = FakeBackend("thunder_engine", "引擎")
        mgr = self.manager({"thunder_engine": good})
        mgr._record("thunder_engine", "t2", f"magnet:?xt=urn:btih:{self.IH}",
                    f"magnet:{self.IH[:8]}…", "")
        orig = magnetmeta.resolve_name
        magnetmeta.resolve_name = \
            lambda ih, timeout=0, trackers=None: "嫁金钗[全30集]"
        try:
            mgr._resolve_and_rename(self._bare_link(), "thunder_engine", "t2",
                                    f"magnet:{self.IH[:8]}…", attempts=1)
        finally:
            magnetmeta.resolve_name = orig
        self.assertEqual(good.renamed, [("t2", "嫁金钗[全30集]")])

    def test_rename_engine_task_silent_on_error(self):
        """引擎改名失败（通道缺实现/抛异常）静默，不阻断主流程。"""
        mgr = self.manager({})
        self.assertIsNone(
            mgr._rename_engine_task("thunder_engine", "t1", "真名"))

        class Boom(FakeBackend):
            def rename_task(self, tid, name):
                raise RuntimeError("boom")

        mgr2 = self.manager({"thunder_engine": Boom("thunder_engine", "引擎")})
        self.assertIsNone(
            mgr2._rename_engine_task("thunder_engine", "t1", "真名"))

    def test_engine_rename_candidates_only_true_name_magnets(self):
        """清尾候选只选「记录已是真名」的磁力任务（引擎侧可能残留兜底名）；
        还没真名的走解析回填路径，非磁力不碰。"""
        recs = [
            {"backend": "thunder_engine", "id": "1",
             "url": f"magnet:?xt=urn:btih:{self.IH}", "name": "已有真名"},
            {"backend": "thunder_engine", "id": "2",
             "url": f"magnet:?xt=urn:btih:{self.IH}",
             "name": f"magnet:{self.IH[:8]}…"},
            {"backend": "thunder_engine", "id": "3",
             "url": "https://x/a.bin", "name": "a.bin"},
        ]
        out = downloader.DownloadManager._engine_rename_candidates(recs)
        self.assertEqual([c[1] for c in out], ["1"])
        self.assertEqual(out[0][2], "已有真名")

    def test_bootstrap_clear_tail_renames_engine(self):
        """启动清尾：记录已是真名的磁力任务直接触发引擎改名
        （升级重启后迅雷里的存量兜底文件夹名自动随真名）。"""
        good = FakeBackend("thunder_engine", "引擎")
        mgr = self.manager({"thunder_engine": good})
        mgr._record("thunder_engine", "t9", f"magnet:?xt=urn:btih:{self.IH}",
                    "嫁金钗[全30集]", "")

        class _Sync:
            """线程同步替身：清尾线程内联执行，测试可确定断言。"""
            def __init__(self, target=None, args=(), **_kw):
                self._t, self._a = target, args

            def start(self):
                self._t(*self._a)

        orig = downloader.threading
        downloader.threading = types.SimpleNamespace(Thread=_Sync)
        try:
            n = mgr.bootstrap_magnet_names()
        finally:
            downloader.threading = orig
        self.assertEqual(n, 1)
        self.assertEqual(good.renamed, [("t9", "嫁金钗[全30集]")])


if __name__ == "__main__":
    unittest.main()
