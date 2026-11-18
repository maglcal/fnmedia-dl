#!/usr/bin/env python3
"""filelock 测试：锁冲突可重试、永久性错误立即抛不掩盖真因。"""
from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import filelock  # noqa: E402


class TestLockConflictClassification(unittest.TestCase):
    def test_blocking_io_is_conflict(self):
        self.assertTrue(filelock._is_lock_conflict(BlockingIOError()))

    def test_permanent_error_not_conflict(self):
        self.assertFalse(
            filelock._is_lock_conflict(OSError(errno.EBADF, "bad fd")))
        self.assertFalse(
            filelock._is_lock_conflict(OSError(errno.ENOMEM, "no memory")))

    def test_acquire_reraises_permanent_error_immediately(self):
        # 非法 fd → EBADF：不重试满 10s，立即上抛真因
        with self.assertRaises(OSError) as cm:
            filelock._acquire(-1, timeout=5)
        self.assertEqual(cm.exception.errno, errno.EBADF)

    def test_conflict_retries_until_timeout(self):
        # 持续锁冲突（可重试错误）：重试到超时才放弃
        import unittest.mock as mock

        target = ("msvcrt.locking" if sys.platform == "win32"
                  else "fcntl.flock")
        with tempfile.TemporaryDirectory() as td:
            fd = os.open(str(Path(td) / "x.lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with mock.patch(target, side_effect=BlockingIOError()):
                    with self.assertRaises(TimeoutError):
                        filelock._acquire(fd, timeout=0.2)
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
