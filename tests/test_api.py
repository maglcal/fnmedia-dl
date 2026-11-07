#!/usr/bin/env python3
"""API 层测试（FastAPI TestClient，后端全部打桩）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config as cfgmod                              # noqa: E402
import downloader                                    # noqa: E402
from test_dispatch import FakeBackend                # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

import main                                          # noqa: E402


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
    """写操作鉴权：网关转发（X-Trim-* 头）时仅管理员可写；无头直连放行兼容。"""

    def setUp(self):
        self.client = TestClient(main.app)

    def test_mutating_without_gateway_headers_allowed(self):
        r = self.client.post("/api/parse", json={"text": ""})
        self.assertNotEqual(r.status_code, 403)

    def test_mutating_admin_allowed(self):
        r = self.client.post("/api/parse", json={"text": ""},
                             headers={"x-trim-userid": "1000",
                                      "x-trim-isadmin": "true"})
        self.assertNotEqual(r.status_code, 403)

    def test_mutating_non_admin_rejected(self):
        hdrs = {"x-trim-userid": "1001", "x-trim-isadmin": "false"}
        cases = [
            ("post", "/api/parse", {"json": {"text": ""}}),
            ("put", "/api/config", {"json": {"config": {}}}),
            ("delete", "/api/tasks/aria2/x", {}),
        ]
        for method, path, kw in cases:
            r = getattr(self.client, method)(path, headers=hdrs, **kw)
            self.assertEqual(r.status_code, 403, f"{method} {path}")
            self.assertIn("管理员", r.json()["detail"])

    def test_readonly_not_blocked(self):
        r = self.client.get("/api/health",
                            headers={"x-trim-isadmin": "false"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
