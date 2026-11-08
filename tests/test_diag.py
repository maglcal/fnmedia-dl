#!/usr/bin/env python3
"""自诊断探针测试（0.1.32）：_probe 三态（正常/异常/线程硬超时）。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import diag  # noqa: E402


class TestDiagProbe(unittest.TestCase):
    def test_probe_ok(self):
        r = diag._probe("步A", lambda: "一切正常")
        self.assertTrue(r["ok"])
        self.assertEqual(r["detail"], "一切正常")
        self.assertEqual(r["step"], "步A")
        self.assertIsInstance(r["ms"], int)

    def test_probe_error_captured(self):
        def boom():
            raise ValueError("炸了")
        r = diag._probe("步B", boom)
        self.assertFalse(r["ok"])
        self.assertIn("ValueError", r["detail"])
        self.assertIn("炸了", r["detail"])

    def test_probe_thread_hard_timeout(self):
        r = diag._probe("步C", lambda: time.sleep(3), timeout=0.2)
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["detail"])


if __name__ == "__main__":
    unittest.main()
