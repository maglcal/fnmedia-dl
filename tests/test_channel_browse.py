#!/usr/bin/env python3
"""通道管理（隐藏下载通道）与下载目录浏览 API 测试。

覆盖：downloader._order 的隐藏过滤、/api/backends 的 disabled 标记、
/api/fs/browse 双来源（本机授权目录越界拒绝 / 设备目录树只读浏览）。"""
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
from backends.base import Backend, BackendError       # noqa: E402
from backends.drive_core import DriveCore             # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

import main                                          # noqa: E402


POOL = {k: object() for k in ("thunder_cloud", "thunder_engine", "aria2")}


class TestOrderFiltersDisabled(unittest.TestCase):
    """_order 是派发通道的唯一入口：隐藏通道必须在这一层被彻底剔除。"""

    def _mgr(self, cfg):
        m = downloader.DownloadManager.__new__(downloader.DownloadManager)
        m.cfg = cfg
        return m

    def test_default_config_has_disabled_backends(self):
        self.assertEqual(cfgmod.DEFAULT_CONFIG.get("disabled_backends"), [])

    def test_auto_skips_disabled(self):
        m = self._mgr({"engine_order": ["thunder_cloud", "thunder_engine", "aria2"],
                       "disabled_backends": ["thunder_engine", "aria2"]})
        self.assertEqual(m._order("auto", POOL), ["thunder_cloud"])

    def test_auto_backfill_skips_disabled(self):
        """engine_order 没写全时的兜底补全也必须跳过隐藏通道。"""
        m = self._mgr({"engine_order": [], "disabled_backends": ["aria2"]})
        self.assertEqual(sorted(m._order("auto", POOL)),
                         ["thunder_cloud", "thunder_engine"])

    def test_explicit_hidden_backend_rejected(self):
        m = self._mgr({"engine_order": [], "disabled_backends": ["aria2"]})
        self.assertEqual(m._order("aria2", POOL), [])

    def test_explicit_enabled_backend_kept(self):
        m = self._mgr({"engine_order": [], "disabled_backends": ["aria2"]})
        self.assertEqual(m._order("thunder_cloud", POOL), ["thunder_cloud"])

    def test_nothing_disabled_keeps_everything(self):
        m = self._mgr({"engine_order": ["aria2"], "disabled_backends": []})
        self.assertEqual(m._order("auto", POOL), ["aria2", "thunder_cloud",
                                                  "thunder_engine"])


class StubBackend(Backend):
    def __init__(self, name, label):
        self.name = name
        self.label = label

    def status(self):
        return {"configured": True, "reachable": True, "detail": "ok"}

    def parse(self, url):
        raise NotImplementedError

    def add(self, url, **kw):
        raise NotImplementedError

    def list_tasks(self):
        return []

    def pause(self, task_id):
        raise NotImplementedError

    def resume(self, task_id):
        raise NotImplementedError

    def delete(self, task_id):
        raise NotImplementedError


class TestBrowseAPI(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = (downloader.STATE_DIR, downloader.RECORDS_PATH,
                      cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        downloader.STATE_DIR = td
        downloader.RECORDS_PATH = td / "tasks.json"
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        self.share = td / "share"
        self.granted = td / "granted"
        (self.share / "movies").mkdir(parents=True)
        (self.share / "movies" / "2026").mkdir()
        (self.granted / "backup").mkdir(parents=True)
        self.client = TestClient(main.app)

    def tearDown(self):
        (downloader.STATE_DIR, downloader.RECORDS_PATH,
         cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        self._td.cleanup()

    def _patch_paths(self, share, granted):
        return mock.patch.multiple(
            cfgmod,
            share_dir=lambda: str(share),
            accessible_paths=lambda: [str(p) for p in granted])

    def _patch_backends(self, pool: dict):
        orig = downloader.DownloadManager.backends
        downloader.DownloadManager.backends = lambda self: pool
        self.addCleanup(lambda: setattr(downloader.DownloadManager,
                                        "backends", orig))

    def test_unknown_source_rejected(self):
        r = self.client.get("/api/fs/browse", params={"source": "smb"})
        self.assertEqual(r.status_code, 400)

    def test_local_lists_roots_first(self):
        with self._patch_paths(self.share, [self.granted]):
            r = self.client.get("/api/fs/browse", params={"source": "local"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["source"], "local")
        self.assertEqual({e["path"] for e in body["entries"]},
                         {str(self.share), str(self.granted)})
        self.assertEqual(body["path"], "")
        self.assertEqual(body["parent"], "")

    def test_local_lists_subdirs_only(self):
        (self.share / "movies" / "note.txt").write_text("x", encoding="utf-8")
        with self._patch_paths(self.share, [self.granted]):
            r = self.client.get("/api/fs/browse",
                                params={"source": "local",
                                        "path": str(self.share)})
        body = r.json()
        self.assertEqual([e["name"] for e in body["entries"]], ["movies"])
        self.assertEqual(body["root"], str(self.share))
        self.assertEqual(body["parent"], "")      # 根级 → 回根列表

    def test_local_navigates_and_builds_crumbs(self):
        with self._patch_paths(self.share, [self.granted]):
            r = self.client.get("/api/fs/browse",
                                params={"source": "local",
                                        "path": str(self.share / "movies")})
        body = r.json()
        self.assertEqual([e["name"] for e in body["entries"]], ["2026"])
        self.assertEqual(body["parent"], str(self.share))
        self.assertEqual([c["name"] for c in body["crumbs"]],
                         [str(self.share), "movies"])
        self.assertEqual(body["entries"][0]["path"],
                         str(self.share / "movies" / "2026"))

    def test_local_rejects_path_outside_roots(self):
        outside = Path(self._td.name).parent
        with self._patch_paths(self.share, [self.granted]):
            r = self.client.get("/api/fs/browse",
                                params={"source": "local", "path": str(outside)})
        self.assertEqual(r.status_code, 403)
        self.assertIn("授权目录", r.json()["detail"])

    def test_local_hides_symlink_escaping_roots(self):
        secret = Path(self._td.name) / "outside-secret"
        secret.mkdir(exist_ok=True)
        link = self.share / "leak"
        try:
            os.symlink(secret, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不支持符号链接")
        with self._patch_paths(self.share, [self.granted]):
            r = self.client.get("/api/fs/browse",
                                params={"source": "local", "path": str(self.share)})
        self.assertEqual([e["name"] for e in r.json()["entries"]], ["movies"])

    def test_local_missing_dir_404(self):
        with self._patch_paths(self.share, [self.granted]):
            r = self.client.get("/api/fs/browse",
                                params={"source": "local",
                                        "path": str(self.share / "nope")})
        self.assertEqual(r.status_code, 404)

    def test_device_missing_backend_404(self):
        self._patch_backends({})
        r = self.client.get("/api/fs/browse", params={"source": "device"})
        self.assertEqual(r.status_code, 404)

    def test_device_uses_thunder_cloud_browse_dirs(self):
        calls = []

        class FakeTC:
            def browse_dirs(self, path=""):
                calls.append(path)
                return {"root_id": "r1", "root": "/downloads/",
                        "path": "/downloads/movies/",
                        "entries": [{"id": "m1", "name": "movies",
                                     "path": "/downloads/movies/"}]}

        self._patch_backends({"thunder_cloud": FakeTC()})
        r = self.client.get("/api/fs/browse",
                            params={"source": "device", "path": "movies"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, ["movies"])
        body = r.json()
        self.assertEqual(body["source"], "device")
        self.assertEqual(body["root"], "/downloads/")
        self.assertEqual(body["path"], "/downloads/movies/")
        self.assertEqual(body["parent"], "/downloads/")
        self.assertEqual(body["entries"][0]["name"], "movies")

    def test_device_error_is_502_with_message(self):
        class Boom:
            def browse_dirs(self, path=""):
                raise BackendError("设备端插件超时")

        self._patch_backends({"thunder_cloud": Boom()})
        r = self.client.get("/api/fs/browse", params={"source": "device"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("设备端插件超时", r.json()["detail"])

    def test_backends_reports_disabled_flag(self):
        self._patch_backends({
            "thunder_cloud": StubBackend("thunder_cloud", "迅雷远程通道"),
            "aria2": StubBackend("aria2", "aria2"),
        })
        cfgmod.save({**cfgmod.load(), "disabled_backends": ["aria2"]})
        r = self.client.get("/api/backends")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["thunder_cloud"]["disabled"])
        self.assertTrue(body["aria2"]["disabled"])

    def test_config_put_merges_disabled_backends_only(self):
        """局部保存 disabled_backends 不得冲掉其它配置键。"""
        cur = cfgmod.load()
        cur["default_dir"] = "/vol1/downloads"
        cfgmod.save(cur)
        r = self.client.put("/api/config",
                            json={"config": {"disabled_backends": ["aria2"]}})
        self.assertEqual(r.status_code, 200)
        after = cfgmod.load()
        self.assertEqual(after["disabled_backends"], ["aria2"])
        self.assertEqual(after["default_dir"], "/vol1/downloads")
        self.assertEqual(after["engine_order"], cur["engine_order"])


class TestBrowseHelpers(unittest.TestCase):
    def test_within(self):
        self.assertTrue(main._within("/a/b", "/a/b"))
        self.assertTrue(main._within("/a/b/c", "/a/b"))
        self.assertFalse(main._within("/a/bc", "/a/b"))
        self.assertFalse(main._within("/a", "/a/b"))

    def test_crumbs(self):
        crumbs = main._crumbs("/downloads/", "/downloads/movies/2026/")
        self.assertEqual([c["name"] for c in crumbs],
                         ["/downloads", "movies", "2026"])
        self.assertEqual([c["path"] for c in crumbs],
                         ["/downloads/", "/downloads/movies", "/downloads/movies/2026"])
        self.assertEqual(main._crumbs("/d/", "/d/"), [{"name": "/d", "path": "/d/"}])

    def test_browse_payload_parent(self):
        p = main._browse_payload("device", "/downloads/", "/downloads/movies/",
                                 [{"name": "2026", "path": "/downloads/movies/2026/"}])
        self.assertEqual(p["parent"], "/downloads/")
        self.assertEqual(p["crumbs"][-1]["name"], "movies")
        self.assertEqual(p["error"], "")


class FakeCore(DriveCore):
    """固定目录树的 DriveCore：_make_folder 一旦被调用即判失败
    （浏览必须只读，缺级目录要报错而不是偷偷建出来）。"""

    TREE = {
        "": [{"id": "root", "name": "downloads",
              "params": {"RealPath": "/downloads/"}},
             {"id": "s1000", "name": "1000",
              "params": {"RealPath": "/vol1/1000/"}}],
        "root": [{"id": "m1", "name": "movies", "params": {"RealPath": "/downloads/movies/"}},
                 {"id": "m2", "name": "music", "params": {"RealPath": "/downloads/music/"}}],
        "m1": [{"id": "m11", "name": "2026", "params": {"RealPath": "/downloads/movies/2026/"}}],
        "m2": [],
        "m11": [],
        "s1000": [{"id": "e1", "name": "movie", "params": {"RealPath": "/vol1/1000/movie/"}}],
        "e1": [],
    }

    def __init__(self):
        super().__init__(None, "http://engine/drive/v1", lambda: {})
        self.made = []

    def _folders(self, space, parent_id="", deadline=None):
        return list(self.TREE.get(parent_id, []))

    def _device_config(self, space="", deadline=None):
        return {"download_paths": ["/downloads/"]}

    def _make_folder(self, space, parent_id, name, deadline=None):
        self.made.append((parent_id, name))
        raise AssertionError("浏览必须只读，不应创建目录")


class TestDriveCoreBrowse(unittest.TestCase):
    def setUp(self):
        self.core = FakeCore()

    def test_root_listing(self):
        d = self.core.browse("sp")
        self.assertEqual(d["root"], "/downloads/")
        self.assertEqual(d["path"], "/downloads/")
        self.assertEqual([e["name"] for e in d["entries"]], ["movies", "music"])
        self.assertEqual(self.core.made, [])

    def test_navigate_by_relative_path(self):
        d = self.core.browse("sp", "movies")
        self.assertEqual(d["path"], "/downloads/movies/")
        self.assertEqual([e["name"] for e in d["entries"]], ["2026"])
        self.assertEqual(d["entries"][0]["path"], "/downloads/movies/2026/")

    def test_navigate_by_absolute_path_under_root(self):
        d = self.core.browse("sp", "/downloads/movies/2026")
        self.assertEqual(d["path"], "/downloads/movies/2026/")
        self.assertEqual(d["entries"], [])

    def test_missing_segment_raises_and_creates_nothing(self):
        with self.assertRaises(BackendError):
            self.core.browse("sp", "movies/nope")
        self.assertEqual(self.core.made, [])

    def test_deep_missing_segment_raises(self):
        with self.assertRaises(BackendError):
            self.core.browse("sp", "nope/nope2")
        self.assertEqual(self.core.made, [])

    def test_navigate_by_absolute_path_outside_root_anchored(self):
        """根外绝对路径按 realPath 锚定真实落点后浏览（与派发同一套落点
        规则，选择器回填的路径派发时解析到同一处）；只读不建目录。"""
        d = self.core.browse("sp", "/vol1/1000/movie")
        self.assertEqual(d["root"], "/vol1/1000/")
        self.assertEqual(d["path"], "/vol1/1000/movie/")
        self.assertEqual(self.core.made, [])

    def test_outside_root_missing_segment_raises_readonly(self):
        with self.assertRaises(BackendError):
            self.core.browse("sp", "/vol1/1000/nope")
        self.assertEqual(self.core.made, [])

    def test_outside_root_unanchorable_falls_back_missing(self):
        """锚不到的根外路径沿用根下相对解释 → 缺级报「目录不存在」，只读。"""
        with self.assertRaises(BackendError):
            self.core.browse("sp", "/vol1/nope/x")
        self.assertEqual(self.core.made, [])


class TestUIMarkup(unittest.TestCase):
    """UI 骨架与脚本绑定一致性——本项目吃过「元素改名后 app.js 静默失效」
    的亏（0.1.20 关于/版本/更新失踪），这里把元素齐备钉成回归测试。"""

    @classmethod
    def setUpClass(cls):
        ui = Path(__file__).resolve().parent.parent / "app" / "ui"
        cls.html = (ui / "index.html").read_text(encoding="utf-8")
        cls.js = (ui / "app.js").read_text(encoding="utf-8")

    def test_dir_picker_markup_complete(self):
        for needle in ('id="dirPicker"', 'id="dpCrumbs"', 'id="dpList"',
                       'id="dpUse"', 'id="dpCancel"', 'id="dpClose"',
                       'id="dpStatus"', 'data-dpsrc="device"',
                       'data-dpsrc="local"'):
            self.assertIn(needle, self.html, needle)
        for pid in ("dirInput", "defaultDir", "tcDir", "teDir", "a2Dir"):
            self.assertIn(f'data-pick="{pid}"', self.html, pid)

    def test_every_id_bound_in_app_js_exists_in_html(self):
        import re
        ids = set(re.findall(r'\$\("#([A-Za-z][A-Za-z0-9_-]*)"\)', self.js))
        missing = [i for i in sorted(ids) if f'id="{i}"' not in self.html]
        self.assertEqual(missing, [], f"index.html 缺少 app.js 引用的元素：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
