#!/usr/bin/env python3
"""派发调度测试：自动降级、显式通道、统一任务视图、记录持久化。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import downloader                              # noqa: E402
import links                                    # noqa: E402
from backends.base import Backend, BackendError, ParsedResource, TaskView  # noqa: E402


class FakeBackend(Backend):
    def __init__(self, name, label, *, reachable=True, add_ok=True, tasks=None):
        self.name = name
        self.label = label
        self._reachable = reachable
        self._add_ok = add_ok
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
            raise BackendError("add failed")
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


if __name__ == "__main__":
    unittest.main()
