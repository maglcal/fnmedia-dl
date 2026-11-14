#!/usr/bin/env python3
"""state 读写防护回归（0.1.41.3 事故修复）。

事故背景（0.1.41.2 升级后真机）：root 身份写 state 留下 root 属主 0600
的 config.json / config.json.lock →
  1) 服务读不到配置被静默吞成默认配置 → 登录信息"丢失"假象；
  2) 锁文件打不开 → 所有写配置路径 Permission denied；
  3) 升级备份 || true 吞错、恢复缺失静默跳过 → state 全丢无人知。

契约钉子：
  - 配置「读不到」（OSError）必须大声抛 ConfigError，绝不回退默认值；
  - 「文件不存在」（FileNotFoundError）才是回退默认值的合法理由；
  - 文件损坏（ValueError）回退默认值（沿旧行为）；
  - 读失败后 update() 不得覆盖真实配置文件；
  - 锁文件打开被拒 → 人话 PermissionError（含属主修复提示）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config as cfgmod                           # noqa: E402
import filelock                                   # noqa: E402


class _StateTmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._orig = (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        cfgmod.STATE_DIR = Path(self._td.name)
        cfgmod.CONFIG_PATH = Path(self._td.name) / "config.json"

    def tearDown(self):
        (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        self._td.cleanup()


class TestLoadFailLoud(_StateTmp):
    """读失败必须大声报错，绝不伪装成「没配置过」。"""

    def test_missing_file_falls_back_to_defaults(self):
        cfg = cfgmod.load()
        self.assertEqual(cfg["thunder_cloud"]["login_name"], "")
        self.assertEqual(cfg["update"]["sources_v"], 2)

    def test_unreadable_raises_config_error(self):
        """PermissionError 读失败 → ConfigError（人话含属主提示），不回默认。"""
        cfgmod.CONFIG_PATH.write_text("{}", encoding="utf-8")
        with mock.patch.object(Path, "read_text",
                               side_effect=PermissionError(13, "denied")):
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load()
        self.assertIn("属主", str(ctx.exception))

    def test_unreadable_error_is_not_oserror_for_fs_handlers(self):
        """ConfigError 须是 RuntimeError 系：fs 浏览类 handler 会按 OSError
        转 403 权限指引，配置读取失败不能被那条路吞掉。"""
        self.assertFalse(issubclass(cfgmod.ConfigError, OSError))
        self.assertTrue(issubclass(cfgmod.ConfigError, RuntimeError))

    def test_missing_read_error_falls_back(self):
        """FileNotFoundError 是 OSError 子类，但语义是「不存在」→ 默认值。"""
        with mock.patch.object(Path, "read_text",
                               side_effect=FileNotFoundError(2, "no such")):
            cfg = cfgmod.load()
        self.assertEqual(cfg["default_backend"], "auto")

    def test_corrupt_json_falls_back_to_defaults(self):
        cfgmod.CONFIG_PATH.write_text("not-json{{", encoding="utf-8")
        cfg = cfgmod.load()
        self.assertEqual(cfg["thunder_cloud"]["login_name"], "")

    def test_valid_config_roundtrip(self):
        cfg = cfgmod.load()
        cfg["thunder_cloud"]["login_name"] = "user@example.com"
        cfgmod.save(cfg)
        self.assertEqual(cfgmod.load()["thunder_cloud"]["login_name"],
                         "user@example.com")


class TestUpdateNoClobber(_StateTmp):
    """读失败时 update() 绝不能把默认配置写回去覆盖真实数据。"""

    def test_update_does_not_clobber_on_read_error(self):
        original = json.dumps({"thunder_cloud": {"login_name": "keep-me"}},
                              ensure_ascii=False)
        cfgmod.CONFIG_PATH.write_text(original, encoding="utf-8")
        with mock.patch.object(Path, "read_text",
                               side_effect=PermissionError(13, "denied")):
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.update(lambda c: c)
        self.assertEqual(cfgmod.CONFIG_PATH.read_text(encoding="utf-8"),
                         original)


class TestLockPermMessage(_StateTmp):
    """锁文件打开被拒 → 人话错误（事故时报的是裸 [Errno 13]）。"""

    def test_lock_permission_error_human_message(self):
        target = Path(self._td.name) / "cfg.json"
        real_open = os.open

        def fake_open(*args, **kwargs):
            if str(args[0]).endswith(".lock"):
                raise PermissionError(13, "denied")
            return real_open(*args, **kwargs)

        with mock.patch("os.open", fake_open):
            with self.assertRaises(PermissionError) as ctx:
                with filelock.locked(target):
                    pass
        self.assertIn("属主", str(ctx.exception))

    def test_lock_happy_path(self):
        target = Path(self._td.name) / "cfg.json"
        with filelock.locked(target):
            pass
        self.assertTrue(Path(str(target) + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
