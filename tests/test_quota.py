#!/usr/bin/env python3
"""配额感知测试：撞配额错误识别、当日计数、撞满标记、跨天重置。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import quota  # noqa: E402


QUOTA_MSG = "迅雷下载引擎（本地直连）：创建任务失败：HTTP 402: task_create_count_limit 任务创建次数达到上限"


class TestIsQuotaError(unittest.TestCase):
    def test_full_engine_message(self):
        self.assertTrue(quota.is_quota_error(QUOTA_MSG))

    def test_error_code_marker(self):
        self.assertTrue(quota.is_quota_error("task_create_count_limit"))

    def test_cn_text_marker(self):
        self.assertTrue(quota.is_quota_error("任务创建次数达到上限"))

    def test_http_402_marker(self):
        self.assertTrue(quota.is_quota_error("HTTP 402"))

    def test_unrelated_error(self):
        self.assertFalse(quota.is_quota_error("连接失败：Connection refused"))

    def test_empty(self):
        self.assertFalse(quota.is_quota_error(""))
        self.assertFalse(quota.is_quota_error(None))


class TestQuotaTracker(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "quota.json"

    def tearDown(self):
        self._td.cleanup()

    def test_initial_state(self):
        t = quota.QuotaTracker(self.path)
        snap = t.snapshot()
        self.assertEqual(snap["used"], 0)
        self.assertFalse(snap["blocked"])
        self.assertFalse(t.engine_blocked())

    def test_create_counter(self):
        t = quota.QuotaTracker(self.path)
        t.note_engine_create()
        t.note_engine_create()
        self.assertEqual(t.snapshot()["used"], 2)
        # 持久化：换个实例读到同一状态
        self.assertEqual(quota.QuotaTracker(self.path).snapshot()["used"], 2)

    def test_blocked_marks_and_persists(self):
        t = quota.QuotaTracker(self.path)
        t.note_engine_quota_hit()
        self.assertTrue(t.engine_blocked())
        self.assertTrue(quota.QuotaTracker(self.path).engine_blocked())
        self.assertTrue(t.snapshot()["blocked"])

    def test_cross_day_reset(self):
        t = quota.QuotaTracker(self.path)
        t.note_engine_create()
        t.note_engine_quota_hit()
        # 手工把状态改成昨天的：下次读取应整体重置（惰性重置，写入时才落盘）
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["date"] = "2000-01-01"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(t.engine_blocked())
        self.assertEqual(t.snapshot()["used"], 0)
        self.assertEqual(t.snapshot()["date"], time.strftime("%Y-%m-%d"))
        # 重置后的写入落盘新日期
        t.note_engine_create()
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["date"],
            time.strftime("%Y-%m-%d"))

    def test_corrupt_file_resets(self):
        self.path.write_text("{ not json", encoding="utf-8")
        t = quota.QuotaTracker(self.path)
        self.assertFalse(t.engine_blocked())
        self.assertEqual(t.snapshot()["used"], 0)


if __name__ == "__main__":
    unittest.main()
