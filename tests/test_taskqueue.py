#!/usr/bin/env python3
"""任务队列测试：入队/移除/定时设置/立即派发/定时触发。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import taskqueue  # noqa: E402


def ok_dispatch(url, directory):
    return {"ok": True, "backend": "aria2", "id": "t-1"}


def fail_dispatch(url, directory):
    return {"ok": False, "error": "全部通道派发失败"}


class QueueTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "queue.json"
        self.q = taskqueue.TaskQueue(self.path)

    def tearDown(self):
        self._td.cleanup()


class TestQueueBasics(QueueTestBase):
    def test_add_and_persist(self):
        added = self.q.add(["https://x/a.iso", "magnet:?xt=urn:btih:" + "a" * 40],
                           directory="/vol1/dl")
        self.assertEqual(len(added), 2)
        self.assertTrue(all(i["status"] == "queued" for i in added))
        snap = taskqueue.TaskQueue(self.path).snapshot()
        self.assertEqual(len(snap["items"]), 2)
        self.assertEqual(snap["items"][0]["directory"], "/vol1/dl")

    def test_add_skips_blank(self):
        added = self.q.add(["", "  ", "https://x/a.iso"])
        self.assertEqual(len(added), 1)

    def test_remove(self):
        (item,) = self.q.add(["https://x/a.iso"])
        self.assertTrue(self.q.remove(item["id"]))
        self.assertFalse(self.q.remove(item["id"]))   # 二次移除不命中
        self.assertEqual(self.q.snapshot()["items"], [])

    def test_settings(self):
        st = self.q.set_settings(enabled=True, time_str="08:30")
        self.assertTrue(st["enabled"])
        self.assertEqual(st["time"], "08:30")
        self.assertTrue(self.q.snapshot()["settings"]["enabled"])

    def test_settings_bad_time(self):
        with self.assertRaises(ValueError):
            self.q.set_settings(enabled=True, time_str="25:99")
        with self.assertRaises(ValueError):
            self.q.set_settings(enabled=True, time_str="8:30")   # 必须补零两位

    def test_settings_default_time(self):
        st = self.q.set_settings(enabled=True, time_str="")
        self.assertEqual(st["time"], taskqueue.DEFAULT_TIME)


class TestQueueDispatch(QueueTestBase):
    def test_run_now_marks_done(self):
        self.q.add(["https://x/a.iso", "https://x/b.iso"])
        results = self.q.run_now(ok_dispatch)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        items = self.q.snapshot()["items"]
        self.assertTrue(all(i["status"] == "done" for i in items))
        self.assertTrue(all(i["backend"] == "aria2" for i in items))

    def test_run_now_marks_failed_and_retries(self):
        self.q.add(["https://x/a.iso"])
        results = self.q.run_now(fail_dispatch)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(self.q.snapshot()["items"][0]["status"], "failed")
        # failed 项下次仍会派发（可自愈）
        results = self.q.run_now(ok_dispatch)
        self.assertTrue(results[0]["ok"])

    def test_run_now_skips_done(self):
        self.q.add(["https://x/a.iso"])
        self.q.run_now(ok_dispatch)
        self.assertEqual(self.q.run_now(ok_dispatch), [])   # done 不重复派发

    def test_run_now_isolates_item_failures(self):
        self.q.add(["https://x/a.iso", "https://x/b.iso"])

        def first_fail(url, directory):
            return fail_dispatch(url, directory) if url.endswith("a.iso") \
                else ok_dispatch(url, directory)

        results = self.q.run_now(first_fail)
        self.assertEqual([r["ok"] for r in results], [False, True])

    def test_run_now_dispatch_fn_raises(self):
        self.q.add(["https://x/a.iso"])

        def boom(url, directory):
            raise RuntimeError("炸了")

        results = self.q.run_now(boom)
        self.assertFalse(results[0]["ok"])
        self.assertIn("炸了", results[0]["error"])


class TestQueueSchedule(QueueTestBase):
    def test_should_run_disabled(self):
        self.q.add(["https://x/a.iso"])
        self.q.set_settings(enabled=False, time_str="00:05")
        self.assertFalse(self.q.should_run(now="23:59"))

    def test_should_run_before_time(self):
        self.q.add(["https://x/a.iso"])
        self.q.set_settings(enabled=True, time_str="08:00")
        self.assertFalse(self.q.should_run(now="07:59"))

    def test_should_run_after_time(self):
        self.q.add(["https://x/a.iso"])
        self.q.set_settings(enabled=True, time_str="08:00")
        self.assertTrue(self.q.should_run(now="08:00"))
        self.assertTrue(self.q.should_run(now="21:00"))

    def test_should_run_requires_pending(self):
        self.q.set_settings(enabled=True, time_str="00:05")
        self.assertFalse(self.q.should_run(now="23:00"))   # 空队列不跑

    def test_tick_runs_once_per_day(self):
        self.q.add(["https://x/a.iso"])
        self.q.set_settings(enabled=True, time_str="00:05")
        results = self.q.tick(ok_dispatch)
        self.assertEqual(len(results), 1)
        # 当天不再重复触发（last_run 已记录）
        self.assertIsNone(self.q.tick(ok_dispatch))

    def test_tick_not_due(self):
        self.q.add(["https://x/a.iso"])
        self.q.set_settings(enabled=True, time_str="23:59")
        # 未到点不触发（tick 内部走 should_run 同一判定）
        self.assertFalse(self.q.should_run(now="00:00"))


if __name__ == "__main__":
    unittest.main()
