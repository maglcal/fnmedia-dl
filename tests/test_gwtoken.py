#!/usr/bin/env python3
"""网关共享密钥回归（P0-1）：生成收敛、空文件重建、常量时间校验。

密钥是本地 socket 链路的不可伪造鉴权凭据（state/gateway.token 0600），
契约三点：并发创建收敛到同一值、损坏/空文件自动重建、verify 对
缺失/错误/非 ASCII 一律 False。测试全程显式传临时 state_dir，
绝不写真仓库 state/。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import gwtoken                                                # noqa: E402


class TestLoadOrCreate(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_creates_then_stable(self):
        tok = gwtoken.load_or_create_token(self.state)
        self.assertEqual(len(tok), 64)              # token_hex(32)
        self.assertEqual(gwtoken.load_or_create_token(self.state), tok)
        p = self.state / gwtoken.TOKEN_FILE
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_text(encoding="ascii"), tok)

    def test_token_file_mode_0600_on_posix(self):
        import os
        if os.name == "nt":
            self.skipTest("Windows 无 POSIX 权限位语义")
        gwtoken.load_or_create_token(self.state)
        mode = (self.state / gwtoken.TOKEN_FILE).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_concurrent_create_converges(self):
        results: list[str] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(gwtoken.load_or_create_token(self.state))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1, "并发创建必须收敛到同一密钥")

    def test_empty_file_rebuilt(self):
        p = self.state / gwtoken.TOKEN_FILE
        p.write_text("")                            # 模拟创建中断残留
        tok = gwtoken.load_or_create_token(self.state)
        self.assertTrue(tok)
        self.assertEqual(p.read_text(encoding="ascii"), tok)


class TestVerify(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state = Path(self._td.name)
        self.tok = gwtoken.load_or_create_token(self.state)
        self.addCleanup(self._td.cleanup)

    def test_correct_token_passes(self):
        self.assertTrue(gwtoken.verify(self.tok, self.state))

    def test_wrong_token_rejected(self):
        self.assertFalse(gwtoken.verify("0" * 64, self.state))
        self.assertFalse(gwtoken.verify(self.tok[:-1] + "x", self.state))

    def test_missing_token_rejected(self):
        self.assertFalse(gwtoken.verify(None, self.state))
        self.assertFalse(gwtoken.verify("", self.state))

    def test_non_ascii_rejected(self):
        self.assertFalse(gwtoken.verify("密钥令牌", self.state))
        self.assertFalse(gwtoken.verify("toké", self.state))

    def test_header_name_is_lowercase(self):
        self.assertEqual(gwtoken.HEADER, "x-fndl-auth")


if __name__ == "__main__":
    unittest.main(verbosity=2)
