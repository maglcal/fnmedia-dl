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

    def _patch_panel(self, ready: bool = True, died: bool = False):
        """打桩面板探测：快路径用 _panel_once，等待用 _wait_panel_watchdog。"""
        self.addCleanup(setattr, xd, "_panel_once", xd._panel_once)
        xd._panel_once = lambda url=xd.PANEL_URL: ready
        self.addCleanup(setattr, xd, "_wait_panel_watchdog", xd._wait_panel_watchdog)
        xd._wait_panel_watchdog = lambda **kw: (ready, died)

    @staticmethod
    def _inspect_handler(running: str = "", image: str = ""):
        """按 inspect 格式串区分「运行状态」与「镜像名」两种查询。"""
        def handler(args):
            if args[0] == "inspect":
                if len(args) > 2 and args[2] == "{{.Config.Image}}":
                    return (0, image) if image else (1, "no such container")
                return (0, running) if running else (1, "no such container")
            return 0, "ok"
        return handler


class TestDeploy(XdBase):
    def test_deploy_fresh_container(self):
        """全新部署：拉镜像 → 起容器 → 等面板 → 自动写配置（一级挂载配方）。"""
        calls = self._patch_docker(self._inspect_handler())
        self._patch_panel(ready=True)
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
        # 一级配方：cap + apparmor 放行（cnk3x/xunlei#108，过容器内 mount 拦截）
        self.assertIn("SYS_ADMIN", run)
        self.assertIn("SYS_CHROOT", run)
        self.assertIn("apparmor=unconfined", run)
        self.assertNotIn("--privileged", run)
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
        self._patch_docker(self._inspect_handler())
        self._patch_panel(ready=True)

        xd.deploy()

        cfg = cfgmod.load()
        self.assertEqual(cfg["thunder_engine"]["base_url"], "http://192.168.1.5:2345")
        self.assertEqual(cfg["thunder_engine"]["dir"], "/vol1/dl")

    def test_existing_running_container_reused(self):
        """容器已在跑且面板可用：不拉镜像不起容器，直接复用并写配置。"""
        calls = self._patch_docker(self._inspect_handler(running="true",
                                                         image="cnk3x/xunlei:latest"))
        self._patch_panel(ready=True)

        st = xd.deploy()

        self.assertTrue(st["done"])
        self.assertFalse([c for c in calls if c[0] in ("pull", "run", "rm")])

    def test_stopped_container_recreated(self):
        """容器存在但停着（如旧参数起不来的）：清理重建，而不是 start 旧容器。"""
        calls = self._patch_docker(self._inspect_handler(running="false",
                                                         image="cnk3x/xunlei:latest"))
        self._patch_panel(ready=True)

        xd.deploy()

        self.assertTrue([c for c in calls if c[0] == "rm"])
        runs = [c for c in calls if c[0] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertFalse([c for c in calls if c[0] == "start"])

    def test_foreign_image_same_name_refused(self):
        """同名容器但不是 cnk3x/xunlei 镜像：不碰它，明确报错。"""
        self._patch_docker(self._inspect_handler(running="false",
                                                 image="someone/else:1"))
        self._patch_panel(ready=True)

        with self.assertRaises(xd.DeployError) as ctx:
            xd.deploy()
        self.assertIn("同名容器", str(ctx.exception))

    def test_tier2_fallback_on_crash(self):
        """一级配方容器秒崩（宿主安全策略差异）→ 自动切兼容模式重建再试。"""
        runs: list[tuple] = []
        state = {"n": 0}

        def handler(args):
            if args[0] == "run":
                runs.append(args)
            return self._inspect_handler(running="false",
                                         image="cnk3x/xunlei:latest")(args)
        self._patch_docker(handler)
        # 第一次等待：面板不就绪且容器死了；第二次：就绪
        waits = [(False, True), (True, False)]
        self.addCleanup(setattr, xd, "_panel_once", xd._panel_once)
        xd._panel_once = lambda url=xd.PANEL_URL: False
        self.addCleanup(setattr, xd, "_wait_panel_watchdog", xd._wait_panel_watchdog)

        def fake_wait(**kw):
            i = min(state["n"], len(waits) - 1)
            state["n"] += 1
            return waits[i]
        xd._wait_panel_watchdog = fake_wait

        st = xd.deploy()

        self.assertTrue(st["done"])
        self.assertEqual(len(runs), 2)
        self.assertIn("apparmor=unconfined", runs[0])
        self.assertIn("--privileged", runs[1])

    def test_custom_dirs_override(self):
        """自定义配置文件/下载存放位置：卷挂载用自定义路径，目录自动创建。"""
        data = self.tmp / "xd-custom-data"
        dl = self.tmp / "xd-custom-dl"
        calls = self._patch_docker(self._inspect_handler())
        self._patch_panel(ready=True)

        st = xd.deploy(str(data), str(dl))

        self.assertTrue(st["done"])
        self.assertTrue(data.is_dir() and dl.is_dir())
        runs = [c for c in calls if c[0] == "run"]
        self.assertIn(f"{data}:/xunlei/data", runs[0])
        self.assertIn(f"{dl}:/xunlei/downloads", runs[0])

    def test_custom_dirs_reject_relative(self):
        """相对路径不合法（向导/API 传入值=不可信输入，必须绝对路径）。"""
        self._patch_docker(self._inspect_handler())
        self._patch_panel(ready=True)
        with self.assertRaises(xd.DeployError):
            xd.deploy("relative/path", "")


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
        st = self._run_deploy(self._inspect_handler(), panel_ready=False)
        self.assertEqual(st["stage"], "error")
        self.assertIn("面板", st["error"])

    def _run_deploy(self, handler, docker_found: bool = True,
                    panel_ready: bool = True):
        """经 run() 走完整错误兜底，返回落盘状态。"""
        if handler is not None:
            self._patch_docker(handler)
        else:
            self._patch_docker(lambda args: (0, "ok"), docker_found=False)
        self._patch_panel(ready=panel_ready, died=False)
        rc = xd.run([])
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
        self._patch_docker(self._inspect_handler())
        self.assertEqual(xd.container_state(), "absent")

    def test_safe_dir_validation(self):
        # POSIX 绝对路径（NAS 真实场景）放行并原样返回
        self.assertEqual(str(xd._safe_dir("/vol1/1000/x", "t")),
                         str(Path("/vol1/1000/x")))
        for bad in ("", "relative/x", "/../x", "/"):
            with self.assertRaises(xd.DeployError):
                xd._safe_dir(bad, "t")

    def test_default_dirs(self):
        data, dl = xd.default_dirs()
        self.assertTrue(data)
        self.assertTrue(dl)

    def test_run_argv_passthrough(self):
        """run(['run','--data-dir',X,'--dl-dir',Y]) 透传给 deploy。"""
        seen = {}
        self.addCleanup(setattr, xd, "deploy", xd.deploy)

        def fake_deploy(data_dir_raw="", dl_dir_raw=""):
            seen["data"] = data_dir_raw
            seen["dl"] = dl_dir_raw
            return xd._set("done", 100, "完", done=True)
        xd.deploy = fake_deploy
        rc = xd.run(["run", "--data-dir", "/vol1/a", "--dl-dir", "/vol1/b"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"data": "/vol1/a", "dl": "/vol1/b"})

    def test_run_rejects_unknown_arg(self):
        rc = xd.run(["--nope"])
        self.assertEqual(rc, 2)


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
        self.assertIn("default_data_dir", body)
        self.assertIn("default_dl_dir", body)
        self.assertIn("stage", body["deploy"])

    def test_deploy_spawns_background(self):
        with mock.patch.object(self.main.subprocess, "Popen") as popen, \
             mock.patch.object(self.main.xdmod, "running", return_value=False):
            r = self.client.post("/api/xunlei_docker/deploy")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertTrue(popen.called)

    def test_deploy_passes_custom_dirs(self):
        """body 里的 data_dir/dl_dir 转成部署进程命令行参数。"""
        with mock.patch.object(self.main.subprocess, "Popen") as popen, \
             mock.patch.object(self.main.xdmod, "running", return_value=False):
            r = self.client.post("/api/xunlei_docker/deploy",
                                 json={"data_dir": "/vol1/xd/data",
                                       "dl_dir": "/vol1/xd/dl"})
        self.assertEqual(r.status_code, 200)
        argv = popen.call_args[0][0]
        self.assertIn("--data-dir", argv)
        self.assertIn("/vol1/xd/data", argv)
        self.assertIn("--dl-dir", argv)
        self.assertIn("/vol1/xd/dl", argv)

    def test_deploy_rejects_bad_dir(self):
        """非法路径直接 400，不进入部署。"""
        with mock.patch.object(self.main.xdmod, "running", return_value=False):
            r = self.client.post("/api/xunlei_docker/deploy",
                                 json={"dl_dir": "relative/path"})
        self.assertEqual(r.status_code, 400)

    def test_deploy_rejects_concurrent(self):
        with mock.patch.object(self.main.xdmod, "running", return_value=True):
            r = self.client.post("/api/xunlei_docker/deploy")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
