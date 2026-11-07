#!/usr/bin/env python3
"""Docker 版迅雷一键部署测试（docker/网络全部打桩，全程无副作用）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config as cfgmod                              # noqa: E402
import xunlei_docker as xd                           # noqa: E402


class XdBase(unittest.TestCase):
    """公共夹具：状态/配置落临时目录，共享目录用环境变量注入。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._orig = (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        cfgmod.STATE_DIR = self.tmp / "state"
        cfgmod.CONFIG_PATH = self.tmp / "state" / "config.json"
        (self.tmp / "data").mkdir()
        (self.tmp / "downloads").mkdir()
        os.environ["FNDL_DATA_DIR"] = str(self.tmp / "data")
        os.environ["FNDL_SHARES_DIR"] = str(self.tmp / "downloads")
        self.addCleanup(os.environ.pop, "FNDL_DATA_DIR", None)
        self.addCleanup(os.environ.pop, "FNDL_SHARES_DIR", None)

    def tearDown(self):
        (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        self._td.cleanup()

    # 打桩工具：docker 命令序列记录
    def _patch_docker(self, handler, docker_found: bool = True):
        calls: list[tuple] = []

        def fake(*args, timeout=60):
            calls.append(args)
            return handler(args)

        self.addCleanup(setattr, xd, "_docker", xd._docker)
        xd._docker = fake
        self.addCleanup(setattr, xd, "docker_bin", xd.docker_bin)
        xd.docker_bin = lambda: "/usr/bin/docker" if docker_found else None
        return calls


class TestDeploy(XdBase):
    def test_deploy_fresh_container(self):
        """全新部署：拉镜像 → 起容器 → 等面板 → 自动写配置。"""
        def handler(args):
            if args[0] == "inspect":
                return 1, "no such container"
            return 0, "ok"
        calls = self._patch_docker(handler)
        self.addCleanup(setattr, xd, "_wait_panel", xd._wait_panel)
        xd._wait_panel = lambda **kw: True
        self.addCleanup(setattr, xd, "_dir_owner", xd._dir_owner)
        xd._dir_owner = lambda p: (1000, 1000)

        st = xd.deploy()

        self.assertTrue(st["done"])
        self.assertEqual(st["stage"], "done")
        pulls = [c for c in calls if c[0] == "pull"]
        runs = [c for c in calls if c[0] == "run"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertIn("--network", run)
        self.assertIn("host", run)
        self.assertIn("SYS_ADMIN", run)
        self.assertIn("XL_UID=1000", run)
        self.assertIn(f"{self.tmp / 'data'}:/xunlei/data", run)
        self.assertIn(f"{self.tmp / 'downloads'}:/xunlei/downloads", run)
        self.assertEqual(run[-1], xd.IMAGE)
        # 配置自动回填
        cfg = cfgmod.load()
        self.assertEqual(cfg["thunder_engine"]["base_url"], xd.PANEL_URL)
        self.assertEqual(cfg["thunder_engine"]["dir"], "/xunlei/downloads")

    def test_deploy_keeps_user_config(self):
        """用户自定义面板地址/目录不被覆盖。"""
        cfgmod.save({"thunder_engine": {"base_url": "http://192.168.1.5:2345",
                                        "dir": "/vol1/dl"}})
        def handler(args):
            return (1, "x") if args[0] == "inspect" else (0, "ok")
        self._patch_docker(handler)
        self.addCleanup(setattr, xd, "_wait_panel", xd._wait_panel)
        xd._wait_panel = lambda **kw: True

        xd.deploy()

        cfg = cfgmod.load()
        self.assertEqual(cfg["thunder_engine"]["base_url"], "http://192.168.1.5:2345")
        self.assertEqual(cfg["thunder_engine"]["dir"], "/vol1/dl")

    def test_existing_running_container_reused(self):
        """容器已在跑：不拉镜像不起容器，直接复用并写配置。"""
        def handler(args):
            return (0, "true") if args[0] == "inspect" else (0, "ok")
        calls = self._patch_docker(handler)

        st = xd.deploy()

        self.assertTrue(st["done"])
        self.assertFalse([c for c in calls if c[0] in ("pull", "run")])

    def test_stopped_container_started(self):
        """容器存在但停着：start 而不是重新 run。"""
        def handler(args):
            if args[0] == "inspect":
                return 0, "false"
            return 0, "ok"
        calls = self._patch_docker(handler)
        self.addCleanup(setattr, xd, "_wait_panel", xd._wait_panel)
        xd._wait_panel = lambda **kw: True

        xd.deploy()

        self.assertTrue([c for c in calls if c[0] == "start"])
        self.assertFalse([c for c in calls if c[0] in ("pull", "run")])


class TestDeployErrors(XdBase):
    def test_docker_missing(self):
        st = self._run_deploy(None, docker_found=False)
        self.assertEqual(st["stage"], "error")
        self.assertIn("Docker", st["error"])

    def test_daemon_not_running(self):
        def handler(args):
            return (1, "Cannot connect") if args[0] == "version" else (0, "ok")
        st = self._run_deploy(handler)
        self.assertEqual(st["stage"], "error")
        self.assertIn("Docker 服务未运行", st["error"])

    def test_pull_failure(self):
        def handler(args):
            if args[0] == "inspect":
                return 1, "x"
            if args[0] == "pull":
                return 1, "denied"
            return 0, "ok"
        st = self._run_deploy(handler)
        self.assertEqual(st["stage"], "error")
        self.assertIn("镜像拉取失败", st["error"])

    def test_panel_not_ready(self):
        def handler(args):
            return (1, "x") if args[0] == "inspect" else (0, "ok")
        st = self._run_deploy(handler, panel_ready=False)
        self.assertEqual(st["stage"], "error")
        self.assertIn("面板", st["error"])

    def _run_deploy(self, handler, docker_found: bool = True,
                    panel_ready: bool = True):
        """经 run() 走完整错误兜底，返回落盘状态。"""
        if handler is not None:
            self._patch_docker(handler)
        else:
            self._patch_docker(lambda args: (0, "ok"), docker_found=False)
        self.addCleanup(setattr, xd, "_wait_panel", xd._wait_panel)
        xd._wait_panel = lambda **kw: panel_ready
        rc = xd.run()
        self.assertEqual(rc, 1)
        return xd.progress()


class TestState(XdBase):
    def test_progress_roundtrip(self):
        st = xd._set("pulling", 30, "拉取镜像…", pid=os.getpid())
        self.assertEqual(st["stage"], "pulling")
        self.assertEqual(xd.progress()["percent"], 30)

    def test_running_pid_alive(self):
        xd._set("pulling", 30, "…", pid=os.getpid())
        self.assertTrue(xd.running())
        xd._set("done", 100, "完", done=True)
        self.assertFalse(xd.running())

    def test_container_state_unknown_without_docker(self):
        # docker 不存在时 _docker 返回 127，状态应为 unknown
        self._patch_docker(lambda args: (127, "docker 不存在"), docker_found=False)
        self.assertEqual(xd.container_state(), "unknown")

    def test_container_state_absent(self):
        def handler(args):
            return (1, "no such container") if args[0] == "inspect" else (0, "")
        self._patch_docker(handler)
        self.assertEqual(xd.container_state(), "absent")


class TestAPI(unittest.TestCase):
    """API 冒烟：状态查询与部署触发（部署进程打桩）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        from fastapi.testclient import TestClient
        import main
        self.main = main
        self.client = TestClient(main.app)

    def tearDown(self):
        (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        self._td.cleanup()

    def test_status(self):
        r = self.client.get("/api/xunlei_docker/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("deploy", body)
        self.assertIn("container", body)
        self.assertIn("docker", body)
        self.assertIn("panel_url", body)
        self.assertIn("stage", body["deploy"])

    def test_deploy_spawns_background(self):
        with mock.patch.object(self.main.subprocess, "Popen") as popen, \
             mock.patch.object(self.main.xdmod, "running", return_value=False):
            r = self.client.post("/api/xunlei_docker/deploy")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertTrue(popen.called)

    def test_deploy_rejects_concurrent(self):
        with mock.patch.object(self.main.xdmod, "running", return_value=True):
            r = self.client.post("/api/xunlei_docker/deploy")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
