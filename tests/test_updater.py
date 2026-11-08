#!/usr/bin/env python3
"""在线更新器测试（假 fpk + 临时目录，全程无网络）。"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import updater                                    # noqa: E402


def make_fpk(path: Path, version: str = "9.9.9",
             with_app: bool = True, with_repo: bool = True) -> Path:
    """构造与 fnpack 同构的假 fpk：外层 tar（manifest + app.tgz），
    app.tgz 内含 repo/（VERSION、app/main.py 等）。"""
    inner_buf = io.BytesIO()
    with tarfile.open(fileobj=inner_buf, mode="w:gz") as inner:
        def _add(name: str, data: bytes):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            inner.addfile(info, io.BytesIO(data))

        if with_repo:
            info = tarfile.TarInfo("repo")
            info.type = tarfile.DIRTYPE
            inner.addfile(info)
            _add("repo/VERSION", (version + "\n").encode())
            _add("repo/requirements.txt", b"fastapi==0.162.0\n")
            _add("repo/README.md", b"# fake\n")
            if with_app:
                info = tarfile.TarInfo("repo/app")
                info.type = tarfile.DIRTYPE
                inner.addfile(info)
                _add("repo/app/main.py", b"VERSION = '%s'\n" % version.encode())
                _add("repo/app/only_in_new.py", b"NEW = True\n")
            info = tarfile.TarInfo("repo/state")  # 白名单之外的目录也进包
            info.type = tarfile.DIRTYPE
            inner.addfile(info)
            _add("repo/state/evil.txt", b"should never be applied\n")
    outer_buf = io.BytesIO()
    with tarfile.open(fileobj=outer_buf, mode="w") as outer:
        info = tarfile.TarInfo("manifest")
        info.size = 10
        outer.addfile(info, io.BytesIO(b"version=x\n"))
        info = tarfile.TarInfo("app.tgz")
        data = inner_buf.getvalue()
        info.size = len(data)
        outer.addfile(info, io.BytesIO(data))
    path.write_bytes(outer_buf.getvalue())
    return path


class TestExtract(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_extract_repo_tree(self):
        fpk = make_fpk(self.tmp / "u.fpk", version="1.2.3")
        tree = updater.extract_repo_tree(fpk, self.tmp / "work")
        self.assertEqual(updater._read_version(tree), "1.2.3")
        self.assertTrue((tree / "app" / "main.py").is_file())

    def test_extract_missing_app_tgz_fails(self):
        fpk = self.tmp / "bad.fpk"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as outer:
            info = tarfile.TarInfo("manifest")
            info.size = 3
            outer.addfile(info, io.BytesIO(b"x=1"))
        fpk.write_bytes(buf.getvalue())
        with self.assertRaises(updater.UpdateError):
            updater.extract_repo_tree(fpk, self.tmp / "work")

    def test_extract_empty_repo_fails(self):
        fpk = make_fpk(self.tmp / "u.fpk", with_repo=False)
        with self.assertRaises(updater.UpdateError):
            updater.extract_repo_tree(fpk, self.tmp / "work")

    def test_extract_incomplete_repo_fails(self):
        fpk = make_fpk(self.tmp / "u.fpk", with_app=False)
        with self.assertRaises(updater.UpdateError):
            updater.extract_repo_tree(fpk, self.tmp / "work")


class TestReplaceAndRollback(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.repo = self.tmp / "repo"
        (self.repo / "app").mkdir(parents=True)
        (self.repo / "state").mkdir()
        (self.repo / ".venv").mkdir()
        (self.repo / "app" / "old.py").write_text("OLD\n", encoding="utf-8")
        (self.repo / "app" / "main.py").write_text("OLD_MAIN\n", encoding="utf-8")
        (self.repo / "VERSION").write_text("0.0.1\n", encoding="utf-8")
        (self.repo / "state" / "config.json").write_text("SECRET\n", encoding="utf-8")
        (self.repo / ".venv" / "pyvenv.cfg").write_text("keep\n", encoding="utf-8")
        # 打桩仓库根与备份目录
        self._orig = (updater.REPO_ROOT, updater.BACKUP_DIR)
        updater.REPO_ROOT = self.repo
        updater.BACKUP_DIR = self.tmp / "backup"

    def tearDown(self):
        updater.REPO_ROOT, updater.BACKUP_DIR = self._orig
        self._td.cleanup()

    def _mk_new_tree(self, version: str = "2.0.0") -> Path:
        tree = self.tmp / "new"
        (tree / "app").mkdir(parents=True)
        (tree / "state").mkdir()  # 新包里的 state/ 不得被应用
        (tree / "app" / "main.py").write_text("NEW_MAIN\n", encoding="utf-8")
        (tree / "app" / "only_in_new.py").write_text("NEW\n", encoding="utf-8")
        (tree / "state" / "evil.txt").write_text("evil\n", encoding="utf-8")
        (tree / "VERSION").write_text(version + "\n", encoding="utf-8")
        return tree

    def test_apply_whitelist_only(self):
        updater.backup_entries()
        updater.apply_tree(self._mk_new_tree())
        # 白名单内：整体替换（旧文件消失、新文件出现、VERSION 更新）
        self.assertFalse((self.repo / "app" / "old.py").exists())
        self.assertTrue((self.repo / "app" / "only_in_new.py").is_file())
        self.assertEqual(updater._read_version(self.repo), "2.0.0")
        # 白名单外：state/ .venv/ 原样，且新包 state/ 不落盘
        self.assertEqual(
            (self.repo / "state" / "config.json").read_text(encoding="utf-8"),
            "SECRET\n")
        self.assertFalse((self.repo / "state" / "evil.txt").exists())
        self.assertTrue((self.repo / ".venv" / "pyvenv.cfg").is_file())

    def test_rollback_restores_original(self):
        updater.backup_entries()
        updater.apply_tree(self._mk_new_tree(version="2.0.0"))
        updater.rollback()
        self.assertTrue((self.repo / "app" / "old.py").is_file())
        self.assertFalse((self.repo / "app" / "only_in_new.py").exists())
        self.assertEqual(updater._read_version(self.repo), "0.0.1")
        self.assertEqual(
            (self.repo / "state" / "config.json").read_text(encoding="utf-8"),
            "SECRET\n")

    def test_backup_keeps_only_latest(self):
        updater.BACKUP_DIR.mkdir(parents=True)
        (updater.BACKUP_DIR / "stale.txt").write_text("old\n", encoding="utf-8")
        updater.backup_entries()
        self.assertFalse((updater.BACKUP_DIR / "stale.txt").exists())
        self.assertTrue((updater.BACKUP_DIR / "app" / "old.py").is_file())


class TestValidateUrl(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(updater.validate_asset_url(
            "https://github.com/maglcal/fnmedia-dl/releases/download/"
            "v0.1.14/fnmedia-dl-0.1.14.fpk"))

    def test_reject_other_host(self):
        self.assertFalse(updater.validate_asset_url(
            "https://evil.example.com/fnmedia-dl-0.1.14.fpk"))

    def test_reject_other_repo(self):
        self.assertFalse(updater.validate_asset_url(
            "https://github.com/other/repo/releases/download/v1/x.fpk"))

    def test_reject_non_fpk(self):
        self.assertFalse(updater.validate_asset_url(
            "https://github.com/maglcal/fnmedia-dl/releases/download/"
            "v0.1.14/malware.exe"))

    def test_reject_traversal(self):
        self.assertFalse(updater.validate_asset_url(
            "https://github.com/maglcal/fnmedia-dl/releases/download/"
            "v0.1.14/../../evil.fpk"))

    def test_reject_empty(self):
        self.assertFalse(updater.validate_asset_url(""))

    def test_mirror_prefix_allowed(self):
        """已配置镜像源前缀放行（含跨源回退候选）。"""
        self.assertTrue(updater.validate_asset_url(
            "http://mirror.example.com/raw/fnmedia-dl/releases/"
            "v0.1.24/fnmedia-dl-0.1.24.fpk",
            ["http://mirror.example.com/raw/fnmedia-dl/"]))

    def test_mirror_prefix_still_rejects_non_fpk_and_traversal(self):
        p = ["http://mirror.example.com/raw/fnmedia-dl/"]
        self.assertFalse(updater.validate_asset_url(
            "http://mirror.example.com/raw/fnmedia-dl/x.exe", p))
        self.assertFalse(updater.validate_asset_url(
            "http://mirror.example.com/raw/fnmedia-dl/../evil.fpk", p))

    def test_unlisted_mirror_rejected(self):
        self.assertFalse(updater.validate_asset_url(
            "http://other.example.com/raw/fnmedia-dl/x.fpk",
            ["http://mirror.example.com/raw/fnmedia-dl/"]))


class TestDownloadVerified(unittest.TestCase):
    def setUp(self):
        # 打桩下载与退避，专注候选切换/校验逻辑
        self.calls: list[str] = []
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self._tmp.name) / "a.fpk"
        self._orig_download = updater.download_fpk
        self._orig_sleep = updater.time.sleep
        updater.time.sleep = lambda s: None
        self.addCleanup(self._restore)

    def _restore(self):
        updater.download_fpk = self._orig_download
        updater.time.sleep = self._orig_sleep
        self._tmp.cleanup()

    def _stub_download(self, impl):
        def fake(url, dest, client=None):
            self.calls.append(url)
            return impl(url, dest)
        updater.download_fpk = fake

    def test_first_candidate_ok(self):
        def impl(url, dest):
            dest.write_bytes(b"hello")
            return 5
        self._stub_download(impl)
        size = updater.download_verified(
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest, expected_size=5)
        self.assertEqual(size, 5)
        self.assertEqual(self.calls, ["http://a/x.fpk"])

    def test_fallback_on_download_error(self):
        def impl(url, dest):
            if "http://a/" in url:
                raise updater.UpdateError("下载失败：HTTP 500")
            dest.write_bytes(b"hello")
            return 5
        self._stub_download(impl)
        updater.download_verified(
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest)
        self.assertIn("http://b/x.fpk", self.calls)

    def test_sha256_mismatch_switches_candidate(self):
        def impl(url, dest):
            dest.write_bytes(b"hello" if "http://b/" in url else b"evil!")
            return 5
        self._stub_download(impl)
        good = hashlib.sha256(b"hello").hexdigest()
        size = updater.download_verified(
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest, sha256=good)
        self.assertEqual(size, 5)
        self.assertIn("http://b/x.fpk", self.calls)

    def test_size_mismatch_switches_candidate(self):
        def impl(url, dest):
            dest.write_bytes(b"hello" if "http://b/" in url else b"hi")
            return 2 if "http://a/" in url else 5
        self._stub_download(impl)
        updater.download_verified(
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest, expected_size=5)
        self.assertIn("http://b/x.fpk", self.calls)

    def test_all_fail_raises_with_summary(self):
        def impl(url, dest):
            raise updater.UpdateError("下载失败：HTTP 500")
        self._stub_download(impl)
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_verified(
                ["http://a/x.fpk", "http://b/x.fpk"], self.dest)
        msg = str(cm.exception)
        self.assertIn("所有下载候选均失败", msg)
        self.assertIn("候选1", msg)
        self.assertIn("候选2", msg)
        # 每候选重试 2 次
        self.assertEqual(len(self.calls), 4)

    def test_network_error_also_retries_and_falls_back(self):
        def impl(url, dest):
            if "http://a/" in url:
                raise updater.httpx.ConnectError("boom")
            dest.write_bytes(b"ok!")
            return 3
        self._stub_download(impl)
        updater.download_verified(
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest)
        self.assertIn("http://b/x.fpk", self.calls)


class TestProgressState(unittest.TestCase):
    def test_set_and_progress_snapshot(self):
        with updater._lock:
            updater._state.update(stage="idle", percent=0, detail="", error="",
                                 done=False)
        updater._set("downloading", 42, "下载中 1MB / 2MB")
        p = updater.progress()
        self.assertEqual(p["stage"], "downloading")
        self.assertEqual(p["percent"], 42)
        self.assertEqual(p["detail"], "下载中 1MB / 2MB")
        self.assertFalse(p["done"])
        # 不带 stage 的局部更新（回滚兜底路径）
        updater._set(detail="已回滚到更新前版本")
        self.assertEqual(updater.progress()["stage"], "downloading")


class TestStartVersionStrip(unittest.TestCase):
    """start() 的 to_version 归一：新格式 main-vX.Y.Z 与旧 vX.Y.Z 均剥成纯 X.Y.Z。"""

    URL = ("https://github.com/maglcal/fnmedia-dl/releases/download/"
           "main-v0.2.0/fnmedia-dl-main-v0.2.0.fpk")

    def tearDown(self):
        with updater._lock:
            updater._state.update(stage="idle", percent=0, detail="", error="",
                                 from_version="", to_version="", done=False)

    def _start(self, version: str) -> dict:
        from unittest import mock
        with mock.patch.object(updater, "_run"):   # 不真下载
            updater.start(self.URL, version=version)
            return updater.progress()

    def test_main_v_prefix_stripped(self):
        self.assertEqual(self._start("main-v0.2.0")["to_version"], "0.2.0")

    def test_legacy_v_prefix_stripped(self):
        self.assertEqual(self._start("v0.2.0")["to_version"], "0.2.0")

    def test_plain_version_kept(self):
        self.assertEqual(self._start("0.2.0")["to_version"], "0.2.0")


class TestRequirementsChanged(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._orig = updater.REPO_ROOT
        updater.REPO_ROOT = self.tmp / "repo"
        updater.REPO_ROOT.mkdir()
        (updater.REPO_ROOT / "requirements.txt").write_text("a==1\n")

    def tearDown(self):
        updater.REPO_ROOT = self._orig
        self._td.cleanup()

    def test_changed(self):
        tree = self.tmp / "new"
        tree.mkdir()
        (tree / "requirements.txt").write_text("a==2\n")
        self.assertTrue(updater._requirements_changed(tree))

    def test_unchanged(self):
        tree = self.tmp / "new"
        tree.mkdir()
        (tree / "requirements.txt").write_text("a==1\n")
        self.assertFalse(updater._requirements_changed(tree))


class TestRestartService(unittest.TestCase):
    """重启策略：systemd 托管自终止（免 root），否则退回 systemctl。"""

    def test_systemd_self_terminate(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"INVOCATION_ID": "test-id"}), \
             mock.patch.object(updater.subprocess, "Popen") as popen:
            self.assertTrue(updater._restart_service())
        args = popen.call_args[0][0]
        self.assertIn(f"kill -TERM {os.getpid()}", args[2])

    def test_fallback_systemctl(self):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(updater.shutil, "which", return_value="/bin/systemctl"), \
             mock.patch.object(updater.subprocess, "Popen") as popen:
            self.assertTrue(updater._restart_service())
        self.assertIn("systemctl restart", popen.call_args[0][0][2])

    def test_no_restart_means_returns_false(self):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(updater.shutil, "which", return_value=None):
            self.assertFalse(updater._restart_service())


if __name__ == "__main__":
    unittest.main(verbosity=2)
