#!/usr/bin/env python3
"""网关 socket 落点回归测试：锁死 FPK 安装形态的行为（502 事故回归）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import webui_gateway  # noqa: E402


class TestGatewaySocketPath(unittest.TestCase):
    def test_fpk_layout_socket_at_app_root(self):
        """FPK 形态（repo 与 ui 同级）→ <应用根>/fnmedia-dl.sock。"""
        with tempfile.TemporaryDirectory() as td:
            appdest = Path(td)
            repo = appdest / "repo"
            repo.mkdir()
            (appdest / "ui").mkdir()
            got = webui_gateway.gateway_socket_path(repo)
            self.assertEqual(got, appdest / "fnmedia-dl.sock")

    def test_manual_deploy_fallback(self):
        """手动部署（无 ui 同级）→ /run/fnmedia-dl/fnmedia-dl.sock。"""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "fnmedia-dl"
            repo.mkdir()
            got = webui_gateway.gateway_socket_path(repo)
            self.assertEqual(got, Path("/run/fnmedia-dl/fnmedia-dl.sock"))

    def test_repo_must_be_named_repo(self):
        """入参不是 repo 目录会误判——这正是 502 事故的原因，行为必须保持明确。"""
        with tempfile.TemporaryDirectory() as td:
            appdest = Path(td)
            wrong = appdest          # 传了应用根目录而不是 repo
            (appdest / "ui").mkdir()
            got = webui_gateway.gateway_socket_path(wrong)
            self.assertEqual(got, Path("/run/fnmedia-dl/fnmedia-dl.sock"))


if __name__ == "__main__":
    unittest.main()
