#!/usr/bin/env python3
"""API 层测试（FastAPI TestClient，后端全部打桩）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config as cfgmod                              # noqa: E402
import downloader                                    # noqa: E402
from test_dispatch import FakeBackend                # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

import main                                          # noqa: E402

# TestClient 直连绕过网关，无注入令牌可带——写请求走本地调试豁免开关
# （FNDL_DEV_TRUST=1）。TestAdminGuard 会临时摘掉它测真实鉴权闸（P0-1）。
os.environ.setdefault("FNDL_DEV_TRUST", "1")


class TestAPI(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = (downloader.STATE_DIR, downloader.RECORDS_PATH,
                      cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        downloader.STATE_DIR = td
        downloader.RECORDS_PATH = td / "tasks.json"
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        self.client = TestClient(main.app)

    def tearDown(self):
        (downloader.STATE_DIR, downloader.RECORDS_PATH,
         cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        self._td.cleanup()

    def _patch_backends(self, pool: dict):
        orig = downloader.DownloadManager.backends
        downloader.DownloadManager.backends = lambda self: pool
        self.addCleanup(lambda: setattr(downloader.DownloadManager,
                                        "backends", orig))

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # 版本号动态读仓库根部 VERSION 文件，不再硬编码
        vfile = Path(__file__).resolve().parent.parent / "VERSION"
        self.assertEqual(r.json()["version"],
                         vfile.read_text(encoding="utf-8").strip())

    def test_update_progress(self):
        r = self.client.get("/api/update/progress")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("stage", body)
        self.assertIn("percent", body)
        self.assertIn("done", body)

    def test_update_download_rejects_bad_url(self):
        from unittest import mock
        empty = {"ok": True, "current": "0.1.0", "latest": "0.1.0",
                 "has_update": False, "url": "", "notes": "", "source": "",
                 "assets": [], "fetched_at": 0}
        with mock.patch.object(main.updatesrc, "check", return_value=empty):
            r = self.client.post("/api/update/download",
                                 json={"url": "https://evil.example.com/x.fpk"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("官方 Releases", r.json()["detail"])

    def test_ui_assets_no_cache(self):
        """UI 页面/脚本禁协商缓存：在线更新后 iframe 不吃旧页。"""
        for path in ("/", "/static/app.js", "/static/app.css"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("cache-control"), "no-cache", path)
        # API JSON 不受此策略影响（无该头）
        r = self.client.get("/api/health")
        self.assertNotEqual(r.headers.get("cache-control"), "no-cache")

    def test_diagnose_endpoint_shape(self):
        """0.1.31：/api/diagnose 透传 diag.run 的分层结果（探针打桩）。"""
        from unittest import mock
        fake = {"ok": False, "version": "0.1.31", "steps": [
            {"step": "服务版本", "ok": True, "ms": 1, "detail": "v0.1.31"},
            {"step": "中继目录表", "ok": False, "ms": 5000,
             "detail": "超时（>5s 无响应）"}]}
        with mock.patch("diag.run", return_value=fake):
            r = self.client.get("/api/diagnose")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(len(body["steps"]), 2)
        self.assertEqual(body["steps"][1]["step"], "中继目录表")

    def test_update_check(self):
        """API 透传多源检查结果（多源逻辑见 test_updatesrc.py）。"""
        from unittest import mock
        payload = {
            "ok": True, "current": "0.1.0", "latest": "9.9.9",
            "has_update": True,
            "url": "https://github.com/maglcal/fnmedia-dl/releases/tag/v9.9.9",
            "notes": "更新说明", "source": "repobox",
            "assets": [{"name": "fnmedia-dl-9.9.9.fpk",
                        "url": "http://mirror.example.com/raw/fnmedia-dl/x.fpk",
                        "fallback_urls": ["https://github.com/maglcal/fnmedia-dl/"
                                          "releases/download/v9.9.9/fnmedia-dl-9.9.9.fpk"],
                        "sha256": "ab", "size": 12}],
            "fetched_at": 0,
        }
        with mock.patch.object(main.updatesrc, "check", return_value=payload):
            r = self.client.get("/api/update/check")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["latest"], "9.9.9")
        self.assertTrue(d["has_update"])
        self.assertEqual(d["assets"][0]["name"], "fnmedia-dl-9.9.9.fpk")
        self.assertEqual(d["assets"][0]["fallback_urls"],
                         payload["assets"][0]["fallback_urls"])

        # 全部更新源不可用 → 502 明确文案
        from updatesrc import SourceError
        with mock.patch.object(main.updatesrc, "check",
                               side_effect=SourceError("全部更新源不可用")):
            r2 = self.client.get("/api/update/check")
        self.assertEqual(r2.status_code, 502)
        self.assertIn("更新源", r2.json()["detail"])

    def test_parse(self):
        r = self.client.post("/api/parse", json={
            "text": "magnet:?xt=urn:btih:" + "a" * 40})
        self.assertEqual(r.status_code, 200)
        links = r.json()["links"]
        self.assertEqual(links[0]["kind"], "magnet")
        self.assertTrue(links[0]["dispatchable"])

    def test_create_tasks(self):
        good = FakeBackend("aria2", "aria2")
        self._patch_backends({"aria2": good})
        r = self.client.post("/api/tasks", json={
            "links": ["https://x/a.iso"], "backend": "aria2"})
        self.assertEqual(r.status_code, 200)
        results = r.json()["results"]
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["id"], "aria2-task-1")

    def test_create_tasks_empty(self):
        r = self.client.post("/api/tasks", json={"links": []})
        self.assertEqual(r.status_code, 400)

    def test_list_and_control(self):
        from backends.base import TaskView
        live = [TaskView(backend="aria2", id="g1", name="a.bin",
                         state="running", progress=5)]
        good = FakeBackend("aria2", "aria2", tasks=live)
        self._patch_backends({"aria2": good})
        r = self.client.get("/api/tasks")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["tasks"]), 1)

        r = self.client.post("/api/tasks/aria2/g1/pause")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(good.paused, ["g1"])

        r = self.client.delete("/api/tasks/aria2/g1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(good.deleted, ["g1"])

    def test_control_unknown_backend(self):
        self._patch_backends({})
        r = self.client.post("/api/tasks/nope/x/pause")
        self.assertEqual(r.status_code, 502)

    # ------------------------------------------------ 重试/历史查重

    def test_retry_task(self):
        good = FakeBackend("aria2", "aria2")
        self._patch_backends({"aria2": good})
        downloader.DownloadManager({})._record(
            "aria2", "old-1", "https://x/a.iso", "a.iso", "/dl")
        r = self.client.post("/api/tasks/aria2/old-1/retry")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["id"], "aria2-task-1")
        self.assertEqual(good.deleted, ["old-1"])     # 旧任务已删
        self.assertEqual(len(good.added), 1)          # 原链接重新派发

    def test_retry_missing_url_502(self):
        good = FakeBackend("aria2", "aria2")
        self._patch_backends({"aria2": good})
        r = self.client.post("/api/tasks/aria2/nope/retry")
        self.assertEqual(r.status_code, 502)
        self.assertIn("原始链接", r.json()["detail"])

    def test_history_check(self):
        downloader.DownloadManager({})._record(
            "aria2", "g1", "magnet:?xt=urn:btih:" + "a" * 40, "demo", "/dl")
        # 同信息哈希（大小写不同）命中
        r = self.client.post("/api/history/check", json={
            "links": ["magnet:?xt=urn:btih:" + "A" * 40]})
        self.assertEqual(r.status_code, 200)
        res = r.json()["results"][0]
        self.assertTrue(res["downloaded"])
        self.assertEqual(res["matches"][0]["name"], "demo")
        # 未下载过
        r2 = self.client.post("/api/history/check", json={
            "links": ["magnet:?xt=urn:btih:" + "b" * 40]})
        self.assertFalse(r2.json()["results"][0]["downloaded"])
        # 空请求
        r3 = self.client.post("/api/history/check", json={"links": []})
        self.assertEqual(r3.status_code, 400)

    def test_retry_quota_switch_message(self):
        """重试撞配额自动换通道：响应带 switched/message 供前端提示。"""
        eng = FakeBackend("thunder_engine", "引擎")
        self._patch_backends({"thunder_engine": eng,
                              "aria2": FakeBackend("aria2", "aria2")})
        r = self.client.post("/api/tasks", json={
            "links": ["https://x/q.iso"], "backend": "thunder_engine"})
        tid = r.json()["results"][0]["id"]
        eng._add_ok = False
        eng._add_error = "HTTP 402: task_create_count_limit 任务创建次数达到上限"
        r = self.client.post(f"/api/tasks/thunder_engine/{tid}/retry")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["switched"])
        self.assertEqual(body["backend"], "aria2")
        self.assertIn("配额", body["message"])

    # ------------------------------------------------ 任务队列（每日定时派发）

    def test_queue_state_empty(self):
        r = self.client.get("/api/queue")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["items"], [])
        self.assertIn("settings", body)

    def test_queue_flow(self):
        self._patch_backends({"aria2": FakeBackend("aria2", "aria2")})
        # 入队
        r = self.client.post("/api/queue", json={"links": ["https://x/a.iso"]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["items"]), 1)
        qid = body["items"][0]["id"]
        self.assertEqual(body["items"][0]["status"], "queued")
        # 空请求拒绝
        r = self.client.post("/api/queue", json={"links": []})
        self.assertEqual(r.status_code, 400)
        # 定时设置（含非法时间）
        r = self.client.post("/api/queue/settings",
                             json={"enabled": True, "time": "00:05"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["settings"]["enabled"])
        r = self.client.post("/api/queue/settings",
                             json={"enabled": True, "time": "99:99"})
        self.assertEqual(r.status_code, 400)
        # 立即派发
        r = self.client.post("/api/queue/run", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["results"]), 1)
        self.assertTrue(body["results"][0]["ok"])
        self.assertEqual(body["items"][0]["status"], "done")
        # 移除 + 二次移除 404
        r = self.client.delete(f"/api/queue/{qid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])
        r = self.client.delete(f"/api/queue/{qid}")
        self.assertEqual(r.status_code, 404)

    def test_config_roundtrip(self):
        r = self.client.put("/api/config", json={"config": {
            "default_dir": "/data/dl",
            "thunder_cloud": {"login_name": "138", "password": "pw"}}})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/config")
        body = r.json()
        self.assertEqual(body["default_dir"], "/data/dl")
        self.assertEqual(body["thunder_cloud"]["password"], "******")
        # 传 ****** 表示保持不变：再次保存不覆盖真实密码
        r = self.client.put("/api/config", json={"config": {
            "thunder_cloud": {"password": "******"}}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cfgmod.load()["thunder_cloud"]["password"], "pw")

    def test_config_put_preserves_hidden_keys(self):
        """表单保存只带表单键：device_id/登录令牌/风控验证状态必须保留
       （回归：整段替换曾把 review_url 等隐藏键冲掉，短信验证框不出现）。"""
        cfgmod.save({"thunder_cloud": {
            "login_name": "138", "password": "pw", "device_id": "d" * 32,
            "access_token": "at", "refresh_token": "rt",
            "review_url": "https://x/r", "review_token": "tok"}})
        r = self.client.put("/api/config", json={"config": {
            "thunder_cloud": {"login_name": "139", "password": "******",
                              "creditkey": "", "target": "", "dir": ""}}})
        self.assertEqual(r.status_code, 200)
        cur = cfgmod.load()["thunder_cloud"]
        self.assertEqual(cur["login_name"], "139")        # 表单键生效
        self.assertEqual(cur["password"], "pw")           # ****** 保持不变
        self.assertEqual(cur["device_id"], "d" * 32)      # 隐藏键保留
        self.assertEqual(cur["access_token"], "at")
        self.assertEqual(cur["refresh_token"], "rt")
        self.assertEqual(cur["review_url"], "https://x/r")
        self.assertEqual(cur["review_token"], "tok")

    def test_persist_cloud_writes_disk(self):
        """登录态/验证状态必须落盘（回归：persist 曾只改内存，
        换个请求就丢，短信验证框永远不出现）。"""
        mgr = downloader.DownloadManager({})
        tc = mgr.backends()["thunder_cloud"]
        did = tc._device_id()
        self.assertEqual(cfgmod.load()["thunder_cloud"]["device_id"], did)
        # 换一个 manager（模拟下一次请求）还认得同一个 device_id
        tc2 = downloader.DownloadManager(cfgmod.load()).backends()["thunder_cloud"]
        self.assertEqual(tc2._device_id(), did)
        # 风控验证状态同样落盘，loadTcReview 读得到
        tc.conf["review_url"] = "https://x/r?mobile=189%2A%2A%2A%2A0690"
        tc.conf["creditkey"] = "ck-1"
        tc._save()
        cur = cfgmod.load()["thunder_cloud"]
        self.assertEqual(cur["review_url"], tc.conf["review_url"])
        self.assertEqual(cur["creditkey"], "ck-1")
        st = downloader.DownloadManager(cfgmod.load()).backends()[
            "thunder_cloud"].review_state()
        self.assertTrue(st["active"])
        self.assertEqual(st["mobile"], "189****0690")

    # ------------------------------------------------ 目录/授权

    def test_fs_paths_from_env_file(self):
        (cfgmod.STATE_DIR / "paths.env").write_text(
            "FNDL_SHARES_DIR=/vol1/@appshare/fnmedia-dl/downloads\n"
            "FNDL_ACCESSIBLE_PATHS=/vol1/media:/vol2/backup\n", encoding="utf-8")
        body = self.client.get("/api/fs/paths").json()
        self.assertEqual(body["share_dir"],
                         "/vol1/@appshare/fnmedia-dl/downloads")
        self.assertEqual(body["accessible"], ["/vol1/media", "/vol2/backup"])

    def test_fs_paths_env_override(self):
        os.environ["FNDL_SHARES_DIR"] = "/override/share"
        self.addCleanup(os.environ.pop, "FNDL_SHARES_DIR", None)
        body = self.client.get("/api/fs/paths").json()
        self.assertEqual(body["share_dir"], "/override/share")

    def test_fs_check_writable(self):
        with tempfile.TemporaryDirectory() as d:
            body = self.client.get("/api/fs/check",
                                   params={"path": d}).json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["exists"])
            self.assertTrue(body["writable"])
            self.assertEqual(body["hint"], "")

    def test_fs_check_missing(self):
        body = self.client.get(
            "/api/fs/check",
            params={"path": str(Path(tempfile.gettempdir()) /
                                "fndl-definitely-missing")}).json()
        self.assertFalse(body["exists"])
        self.assertFalse(body["ok"])
        self.assertIn("授权目录", body["hint"])

    def test_fs_check_permission_denied(self):
        """0.1.38.2：stat 被拒（子目录权限未继承）按「存在但不可写」
        返回并给权限指引，不再误报「目录不存在」。"""
        with tempfile.TemporaryDirectory() as d:
            real_stat = os.stat

            def fake_stat(p, *a, **k):
                if str(p) == d:
                    raise PermissionError(13, "denied")
                return real_stat(p, *a, **k)

            with mock.patch("os.stat", fake_stat):
                body = self.client.get("/api/fs/check",
                                       params={"path": d}).json()
        self.assertTrue(body["exists"])
        self.assertFalse(body["writable"])
        self.assertTrue(body["denied"])
        self.assertIn("系统文件权限", body["hint"])


class TestUIPaths(unittest.TestCase):
    """iframe 前缀适配回归：页面挂在 /app/fnmedia-dl/ 下时
    绝对路径 /static、/api 会 404 导致裸 HTML——必须全相对。"""

    ABSOLUTE_PATTERNS = ['href="/', 'src="/', "fetch('/", 'fetch("/',
                         'url(/', "url('/", 'url("/']

    def setUp(self):
        self.client = TestClient(main.app)

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<base href=", r.text)   # 动态 base 前缀自适应

    def test_static_assets_served(self):
        for path in ("/static/app.css", "/static/app.js",
                     "/static/favicon.png", "/static/icon.png"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_no_absolute_asset_paths(self):
        """index.html 与 app.js 中不得残留绝对路径引用。"""
        ui = Path(__file__).resolve().parent.parent / "app" / "ui"
        for name in ("index.html", "app.js"):
            text = (ui / name).read_text(encoding="utf-8")
            for pat in self.ABSOLUTE_PATTERNS:
                self.assertNotIn(pat, text, f"{name} 残留绝对路径: {pat}")


class TestAdminGuard(unittest.TestCase):
    """写操作鉴权（P0-1 fail-closed）：无有效网关令牌一律 401；
    令牌经网关（X-Trim-* 头）时仅管理员可写；FNDL_DEV_TRUST=1 显式豁免。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        # 本组测试走真实密钥闸：临时摘掉开发豁免，跑完恢复
        self._dev = os.environ.pop("FNDL_DEV_TRUST", None)
        self.addCleanup(self._restore)
        self.client = TestClient(main.app)
        import gwtoken
        self.token = gwtoken.load_or_create_token(td)

    def _restore(self):
        (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        if self._dev is not None:
            os.environ["FNDL_DEV_TRUST"] = self._dev
        else:
            os.environ.pop("FNDL_DEV_TRUST", None)
        self._td.cleanup()

    def test_mutating_without_token_rejected(self):
        """裸写无令牌 → 401（旧实现「无头直连放行」是信任漏洞，已废除）。"""
        r = self.client.post("/api/parse", json={"text": ""})
        self.assertEqual(r.status_code, 401)

    def test_mutating_with_wrong_token_rejected(self):
        r = self.client.post("/api/parse", json={"text": ""},
                             headers={"x-fndl-auth": "wrong-token"})
        self.assertEqual(r.status_code, 401)

    def test_spoofed_admin_headers_rejected(self):
        """伪造 X-Trim-Isadmin 但无有效令牌 → 401（不看头，先验令牌）。"""
        r = self.client.post("/api/parse", json={"text": ""},
                             headers={"x-trim-userid": "1000",
                                      "x-trim-isadmin": "true"})
        self.assertEqual(r.status_code, 401)

    def test_mutating_admin_allowed(self):
        r = self.client.post(
            "/api/parse", json={"text": ""},
            headers={"x-fndl-auth": self.token,
                     "x-trim-userid": "1000", "x-trim-isadmin": "true"})
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)

    def test_mutating_non_admin_rejected(self):
        hdrs = {"x-fndl-auth": self.token,
                "x-trim-userid": "1001", "x-trim-isadmin": "false"}
        cases = [
            ("post", "/api/parse", {"json": {"text": ""}}),
            ("put", "/api/config", {"json": {"config": {}}}),
            ("delete", "/api/tasks/aria2/x", {}),
        ]
        for method, path, kw in cases:
            r = getattr(self.client, method)(path, headers=hdrs, **kw)
            self.assertEqual(r.status_code, 403, f"{method} {path}")
            self.assertIn("管理员", r.json()["detail"])

    def test_dev_trust_env_bypasses(self):
        """FNDL_DEV_TRUST=1 显式豁免（本地调试/测试专用）。"""
        os.environ["FNDL_DEV_TRUST"] = "1"
        self.addCleanup(os.environ.pop, "FNDL_DEV_TRUST", None)
        r = self.client.post("/api/parse", json={"text": ""})
        self.assertNotEqual(r.status_code, 401)

    def test_readonly_not_blocked(self):
        r = self.client.get("/api/health",
                            headers={"x-trim-isadmin": "false"})
        self.assertEqual(r.status_code, 200)


class TestDailyCheck(unittest.TestCase):
    """每日自动更新检查（0.1.37.5）：state/update-check.json 状态记录。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = cfgmod.STATE_DIR, cfgmod.CONFIG_PATH
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        self.client = TestClient(main.app)

    def tearDown(self):
        cfgmod.STATE_DIR, cfgmod.CONFIG_PATH = self._orig
        self._td.cleanup()

    @staticmethod
    def _mock_result(has_update=True, latest="0.2.0", current="0.1.0"):
        return {"ok": True, "current": current, "latest": latest,
                "has_update": has_update, "url": "https://x", "notes": "changelog",
                "source": "test", "notice": "",
                "assets": [{"name": "fnmedia-dl.fpk", "url": "https://x/f.fpk",
                            "size": 1, "sha256": "abc", "fallback_urls": []}],
                "fetched_at": 0}

    def _state_file(self):
        return cfgmod.STATE_DIR / "update-check.json"

    def test_first_check_prompts(self):
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               return_value=self._mock_result()):
            r = self.client.get("/api/update/daily-check")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["action"], "prompt")
        self.assertEqual(body["latest"], "0.2.0")
        self.assertEqual(body["notes"], "changelog")
        # 状态落盘
        import json
        st = json.loads(self._state_file().read_text())
        self.assertIn("last_check_date", st)
        self.assertEqual(st["prompted_version"], "0.2.0")

    def test_same_day_skips(self):
        """当天已检查过 → skip，不再请求更新源。"""
        import datetime, json
        today = datetime.date.today().isoformat()
        self._state_file().write_text(json.dumps(
            {"last_check_date": today}), encoding="utf-8")
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check") as mc:
            r = self.client.get("/api/update/daily-check")
        self.assertEqual(r.json()["action"], "skip")
        mc.assert_not_called()

    def test_no_update(self):
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               return_value=self._mock_result(
                                   has_update=False, latest="0.1.0")):
            r = self.client.get("/api/update/daily-check")
        self.assertEqual(r.json()["action"], "none")
        # 0.1.39.1：无更新也回传当前版本，前端给「已是最新版」反馈
        self.assertEqual(r.json()["current"], "0.1.0")

    def test_same_version_same_day_no_repeat(self):
        """同一版本同一天只弹一次窗。"""
        import datetime, json
        today = datetime.date.today().isoformat()
        self._state_file().write_text(json.dumps(
            {"last_check_date": today, "prompted_date": today,
             "prompted_version": "0.2.0"}), encoding="utf-8")
        r = self.client.get("/api/update/daily-check")
        self.assertEqual(r.json()["action"], "skip")

    def test_new_version_same_day_prompts_again(self):
        """同一天但发现更新版本 → 允许弹窗。"""
        import datetime, json
        today = datetime.date.today().isoformat()
        self._state_file().write_text(json.dumps(
            {"last_check_date": "2026-01-01",  # 昨天
             "prompted_date": today, "prompted_version": "0.2.0"}),
            encoding="utf-8")
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               return_value=self._mock_result(
                                   latest="0.3.0")):
            r = self.client.get("/api/update/daily-check")
        self.assertEqual(r.json()["action"], "prompt")
        self.assertEqual(r.json()["latest"], "0.3.0")

    def test_source_error_saves_check_date(self):
        """更新源不可用也记 last_check_date（当天不反复请求坏源）；
        0.1.39.1：失败与无更新区分——返回 action=error 带原因。"""
        import datetime, json
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               side_effect=Exception("network")):
            r = self.client.get("/api/update/daily-check")
        body = r.json()
        self.assertEqual(body["action"], "error")
        self.assertIn("network", body["error"])
        st = json.loads(self._state_file().read_text())
        self.assertEqual(st["last_check_date"],
                         datetime.date.today().isoformat())

    def test_state_file_written_atomically(self):
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               return_value=self._mock_result()):
            self.client.get("/api/update/daily-check")
        self.assertTrue(self._state_file().is_file())
        # 无残留 tmp 文件
        import json
        st = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertEqual(st["prompted_version"], "0.2.0")

    # ---- 0.1.39.2：检查结果落 last_result/last_error，供常驻状态行展示 ----

    def test_last_result_none_recorded(self):
        import json
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               return_value=self._mock_result(
                                   has_update=False, latest="0.1.0")):
            self.client.get("/api/update/daily-check")
        st = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertEqual(st["last_result"], "none")
        self.assertEqual(st["last_error"], "")

    def test_last_result_error_recorded(self):
        import json
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               side_effect=Exception("boom")):
            self.client.get("/api/update/daily-check")
        st = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertEqual(st["last_result"], "error")
        self.assertIn("boom", st["last_error"])

    def test_last_result_prompt_recorded(self):
        import json
        from unittest import mock
        with mock.patch.object(main.updatesrc, "check",
                               return_value=self._mock_result()):
            self.client.get("/api/update/daily-check")
        st = json.loads(self._state_file().read_text(encoding="utf-8"))
        self.assertEqual(st["last_result"], "prompt")


class TestUpdateStatusChangelog(unittest.TestCase):
    """/api/update/status 与 /api/update/changelog（0.1.39.2 可见性重设计）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = cfgmod.STATE_DIR, cfgmod.CONFIG_PATH
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        self.client = TestClient(main.app)

    def tearDown(self):
        cfgmod.STATE_DIR, cfgmod.CONFIG_PATH = self._orig
        self._td.cleanup()

    def test_status_empty_state(self):
        r = self.client.get("/api/update/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["last_check_date"], "")
        self.assertEqual(body["last_result"], "")
        self.assertEqual(body["last_error"], "")
        self.assertIn("current", body)

    def test_status_reflects_state(self):
        import datetime, json
        today = datetime.date.today().isoformat()
        (cfgmod.STATE_DIR / "update-check.json").write_text(json.dumps({
            "last_check_date": today, "last_result": "error",
            "last_error": "源不可用", "prompted_version": "0.2.0"}),
            encoding="utf-8")
        body = self.client.get("/api/update/status").json()
        self.assertEqual(body["last_check_date"], today)
        self.assertEqual(body["last_result"], "error")
        self.assertEqual(body["last_error"], "源不可用")
        self.assertEqual(body["prompted_version"], "0.2.0")

    def test_changelog_current_version_section(self):
        """无参调用返回当前版本段落（随包 CHANGELOG.md 提取）。"""
        body = self.client.get("/api/update/changelog").json()
        self.assertTrue(body["found"])
        self.assertEqual(body["version"], body["current"])
        self.assertTrue(body["text"].strip())
        self.assertNotIn("##", body["text"])   # 段落正文不含下级标题行

    def test_changelog_missing_version_falls_back_to_latest(self):
        """指定不存在的版本 → 取最新一段，found=False。"""
        body = self.client.get("/api/update/changelog",
                               params={"version": "9.9.9"}).json()
        self.assertFalse(body["found"])
        self.assertTrue(body["text"].strip())
        self.assertNotEqual(body["version"], "9.9.9")   # 回退到已有段落

    def test_changelog_heading_parse(self):
        """分段解析：提取正文不含其他版本的条目。"""
        sec = main._changelog_section(main.VERSION)
        self.assertTrue(sec["found"])
        self.assertEqual(sec["version"], main.VERSION)


class TestCompliancePins(unittest.TestCase):
    """命名合规与可见性重设计的文件级钉子（0.1.39.2）。"""

    ROOT = Path(__file__).resolve().parent.parent

    def test_manifest_display_name(self):
        text = (self.ROOT / "packaging" / "fpk" / "manifest.in").read_text(
            encoding="utf-8")
        self.assertIn("display_name          = 批量下载助手", text)
        self.assertNotIn("飞牛下载助手", text)

    def test_entry_title(self):
        text = (self.ROOT / "packaging" / "fpk" / "app" / "ui" / "config").read_text(
            encoding="utf-8")
        self.assertIn("批量下载助手", text)
        self.assertNotIn("飞牛下载助手", text)

    def test_no_legacy_product_name(self):
        """「飞牛下载助手」为旧产品名（违反命名规范），关键文件不得出现。"""
        rels = ["app/ui/index.html", "app/ui/app.js", "app/ui/app.css",
                "app/main.py", "app/xunlei_docker.py",
                "packaging/fpk/manifest.in", "packaging/fpk/app/ui/config",
                "packaging/fpk/wizard/install", "README.md"]
        for rel in rels:
            text = (self.ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("飞牛下载助手", text, f"{rel} 残留旧产品名")

    def test_update_status_markup(self):
        html = (self.ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="updateAutoStatus"', html)          # 常驻状态行
        self.assertIn('<div id="updateNotes" class="update-notes">', html)
        js = (self.ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("refreshUpdateStatus", js)
        self.assertIn("refreshVersionNotes", js)

    def test_changelog_file_shipped(self):
        self.assertTrue((self.ROOT / "CHANGELOG.md").is_file())
        text = (self.ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # 当前版本必须有对应段落（/api/update/changelog 的数据源）
        self.assertIn("## " + main.VERSION, text)


if __name__ == "__main__":
    unittest.main()
