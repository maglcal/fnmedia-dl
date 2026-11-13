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
        def fake(url, dest, client=None, extra_hosts=()):
            self.calls.append(url)
            return impl(url, dest)
        updater.download_fpk = fake

    def test_first_candidate_ok(self):
        def impl(url, dest):
            dest.write_bytes(b"hello")
            return 5
        self._stub_download(impl)
        size = updater.download_verified(
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest, expected_size=5,
            allow_unverified=True)
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
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest,
            allow_unverified=True)
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
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest, expected_size=5,
            allow_unverified=True)
        self.assertIn("http://b/x.fpk", self.calls)

    def test_all_fail_raises_with_summary(self):
        def impl(url, dest):
            raise updater.UpdateError("下载失败：HTTP 500")
        self._stub_download(impl)
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_verified(
                ["http://a/x.fpk", "http://b/x.fpk"], self.dest,
                allow_unverified=True)
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
            ["http://a/x.fpk", "http://b/x.fpk"], self.dest,
            allow_unverified=True)
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


class TestMaskSecrets(unittest.TestCase):
    """错误/进度文案的 URL 敏感参数掩码（防凭据经提示泄露）。"""

    def test_masks_sensitive_query_params(self):
        s = updater._mask_secrets(
            "https://x/y?token=abc123&repo=fnmedia-dl&secret=zz99&key=k1")
        self.assertNotIn("abc123", s)
        self.assertNotIn("zz99", s)
        self.assertIn("token=******", s)
        self.assertIn("secret=******", s)
        self.assertIn("key=******", s)
        self.assertIn("repo=fnmedia-dl", s)          # 非敏感参数保留

    def test_plain_text_unchanged(self):
        self.assertEqual(updater._mask_secrets("HTTP 502 bad gateway"),
                         "HTTP 502 bad gateway")


class TestFallbackSwitchNotice(unittest.TestCase):
    """下载换源透明提示：切换备用源在进度文案留痕，全程对用户可见。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dest = Path(self._td.name) / "u.fpk"
        with updater._lock:
            updater._state.update(stage="idle", percent=0, detail="", error="",
                                 from_version="", to_version="", done=False)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, impl, urls):
        from unittest import mock
        with mock.patch.object(updater, "download_fpk", side_effect=impl), \
             mock.patch.object(updater.time, "sleep"):
            updater.download_verified(urls, self.dest, allow_unverified=True)
        return updater.progress()

    def test_switch_notice_persists_after_success(self):
        def impl(url, dest, client=None, extra_hosts=()):
            if "http://a/" in url:
                raise updater.UpdateError("下载失败：HTTP 502")
            dest.write_bytes(b"ok!")
            return 3

        p = self._run(impl, ["http://a/x.fpk", "http://b/x.fpk"])
        self.assertIn("已自动切换备用源", p["detail"])
        self.assertIn("1 次", p["detail"])             # 成功后留痕计数

    def test_no_switch_keeps_plain_detail(self):
        def impl(url, dest, client=None, extra_hosts=()):
            dest.write_bytes(b"ok!")
            return 3

        p = self._run(impl, ["http://a/x.fpk"])
        self.assertEqual(p["detail"], "下载校验通过")


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


class TestFailClosedSha256(unittest.TestCase):
    """P0-3 fail-closed：空 sha256 直接拒绝下载，仅手动通道显式豁免。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dest = Path(self._td.name) / "a.fpk"
        self.called = False
        self._orig = updater.download_fpk

        def fake(url, dest, client=None, extra_hosts=()):
            self.called = True
            dest.write_bytes(b"pkg")
            return 3
        updater.download_fpk = fake
        self.addCleanup(self._restore)

    def _restore(self):
        updater.download_fpk = self._orig
        self._td.cleanup()

    def test_empty_sha256_rejected_without_exemption(self):
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_verified(["http://a/x.fpk"], self.dest)
        msg = str(cm.exception)
        self.assertIn("拒绝下载", msg)
        self.assertIn("SHA256", msg)
        self.assertFalse(self.called, "fail-closed 必须在下载前拒绝")
        self.assertFalse(self.dest.exists())

    def test_allow_unverified_exemption_passes(self):
        size = updater.download_verified(
            ["http://a/x.fpk"], self.dest, allow_unverified=True)
        self.assertEqual(size, 3)
        self.assertTrue(self.called)

    def test_nonempty_sha256_proceeds(self):
        good = hashlib.sha256(b"pkg").hexdigest()
        size = updater.download_verified(
            ["http://a/x.fpk"], self.dest, sha256=good)
        self.assertEqual(size, 3)


class _FakeResp:
    """下载打桩响应：可作 context manager，headers 走小写键。"""

    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._chunks = list(chunks)

    def iter_bytes(self, n):
        for c in self._chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClient:
    """按 URL 预置响应序列的假 httpx.Client（只实现 stream/close）。"""

    def __init__(self, routes: dict):
        self.routes = {u: list(seq) for u, seq in routes.items()}
        self.requested: list[str] = []

    def stream(self, method, url, follow_redirects=True):
        self.requested.append(url)
        seq = self.routes.get(url)
        if not seq:
            raise updater.httpx.ConnectError(f"no route: {url}")
        return seq.pop(0)

    def close(self):
        pass


class TestDownloadFpkRedirectPolicy(unittest.TestCase):
    """P0-3 下载闸：重定向白名单/跳数上限/非 http(s) 拒绝/大小上限。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dest = Path(self._td.name) / "a.fpk"
        with updater._lock:
            updater._state.update(stage="idle", percent=0, detail="", error="",
                                 done=False)
        self.addCleanup(self._td.cleanup)

    def test_redirect_to_off_whitelist_host_rejected(self):
        client = _FakeClient({
            "http://a/x.fpk": [
                _FakeResp(302, {"location": "http://evil.example.com/y"})],
        })
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_fpk("http://a/x.fpk", self.dest, client=client)
        self.assertIn("白名单外主机", str(cm.exception))
        self.assertNotIn("evil.example.com", client.requested[1:]
                         if len(client.requested) > 1 else [])

    def test_redirect_to_extra_host_allowed(self):
        client = _FakeClient({
            "http://a/x.fpk": [
                _FakeResp(302, {"location": "http://b/x.fpk"})],
            "http://b/x.fpk": [
                _FakeResp(200, {"content-length": "5"}, [b"hello"])],
        })
        size = updater.download_fpk("http://a/x.fpk", self.dest, client=client,
                                    extra_hosts=("http://b/x.fpk",))
        self.assertEqual(size, 5)
        self.assertEqual(self.dest.read_bytes(), b"hello")
        self.assertEqual(client.requested, ["http://a/x.fpk", "http://b/x.fpk"])

    def test_redirect_to_github_host_allowed(self):
        # GitHub 资产主机永远在白名单（release 跳转 objects.githubusercontent.com）
        target = "https://objects.githubusercontent.com/y"
        client = _FakeClient({
            "http://a/x.fpk": [_FakeResp(302, {"location": target})],
            target: [_FakeResp(200, {}, [b"ok"])],
        })
        size = updater.download_fpk("http://a/x.fpk", self.dest, client=client)
        self.assertEqual(size, 2)

    def test_relative_redirect_stays_on_own_host(self):
        client = _FakeClient({
            "http://a/x.fpk": [_FakeResp(302, {"location": "/y"})],
            "http://a/y": [_FakeResp(200, {}, [b"ok!"])],
        })
        size = updater.download_fpk("http://a/x.fpk", self.dest, client=client)
        self.assertEqual(size, 3)
        self.assertEqual(client.requested, ["http://a/x.fpk", "http://a/y"])

    def test_too_many_redirects_rejected(self):
        routes = {}
        for i in range(updater.MAX_REDIRECTS + 2):
            routes[f"http://a/r{i}"] = [
                _FakeResp(302, {"location": f"/r{i + 1}"})]
        client = _FakeClient(routes)
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_fpk("http://a/r0", self.dest, client=client)
        self.assertIn("重定向超过", str(cm.exception))

    def test_non_http_scheme_rejected(self):
        for loc in ("ftp://a/y", "file:///etc/passwd", "gopher://a/y"):
            client = _FakeClient({
                "http://a/x.fpk": [_FakeResp(302, {"location": loc})],
            })
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download_fpk("http://a/x.fpk", self.dest, client=client)
            self.assertIn("非 http(s)", str(cm.exception), loc)

    def test_content_length_over_limit_rejected(self):
        over = updater.MAX_DOWNLOAD_BYTES + 1
        client = _FakeClient({
            "http://a/x.fpk": [_FakeResp(200, {"content-length": str(over)})],
        })
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download_fpk("http://a/x.fpk", self.dest, client=client)
        self.assertIn("大小上限", str(cm.exception))

    def test_streamed_overflow_rejected_without_content_length(self):
        # content-length 缺失时靠流式计数兜底
        from unittest import mock
        client = _FakeClient({
            "http://a/x.fpk": [_FakeResp(200, {}, [b"12345", b"67890"])],
        })
        with mock.patch.object(updater, "MAX_DOWNLOAD_BYTES", 8):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download_fpk("http://a/x.fpk", self.dest, client=client)
        self.assertIn("大小上限", str(cm.exception))


class TestSwapInAtomic(unittest.TestCase):
    """P0-3 原子顶替：staging 改名替换，全程无半覆盖、无残留中间文件。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _residue(self, name: str) -> list[str]:
        return [p.name for p in self.tmp.iterdir()
                if p.name in (f".{name}.fndl-new", f".{name}.fndl-old")]

    def test_file_replaced_without_residue(self):
        dst = self.tmp / "app.py"
        dst.write_text("old")
        src = self.tmp / "src.py"
        src.write_text("new")
        updater._swap_in(src, dst)
        self.assertEqual(dst.read_text(), "new")
        self.assertEqual(self._residue("app.py"), [])

    def test_dir_replaced_without_residue(self):
        dst = self.tmp / "webui"
        dst.mkdir()
        (dst / "index.html").write_text("old")
        src = self.tmp / "new_webui"
        src.mkdir()
        (src / "index.html").write_text("new")
        updater._swap_in(src, dst)
        self.assertEqual((dst / "index.html").read_text(), "new")
        self.assertEqual(self._residue("webui"), [])

    def test_fresh_target_created(self):
        src = self.tmp / "src.py"
        src.write_text("new")
        dst = self.tmp / "fresh.py"
        updater._swap_in(src, dst)
        self.assertEqual(dst.read_text(), "new")
        self.assertEqual(self._residue("fresh.py"), [])

    def test_stale_staging_and_old_cleared(self):
        # 上次中断残留的 staging/old 会被清掉而不是叠加成垃圾
        dst = self.tmp / "app.py"
        dst.write_text("old")
        (self.tmp / ".app.py.fndl-new").write_text("stale")
        (self.tmp / ".app.py.fndl-old").write_text("stale")
        src = self.tmp / "src.py"
        src.write_text("new")
        updater._swap_in(src, dst)
        self.assertEqual(dst.read_text(), "new")
        self.assertEqual(self._residue("app.py"), [])

    def test_apply_tree_touches_whitelist_only(self):
        from unittest import mock
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / "app").mkdir()
        (repo / "app" / "main.py").write_text("old")
        (repo / "state").mkdir()
        (repo / "state" / "keep.txt").write_text("keep")
        new_tree = self.tmp / "new_tree"
        (new_tree / "app").mkdir(parents=True)
        (new_tree / "app" / "main.py").write_text("new")
        with mock.patch.object(updater, "REPO_ROOT", repo), \
             mock.patch.object(updater, "REPLACE_ENTRIES", ("app",)):
            updater.apply_tree(new_tree)
        self.assertEqual((repo / "app" / "main.py").read_text(), "new")
        self.assertEqual((repo / "state" / "keep.txt").read_text(), "keep")
        self.assertEqual(
            [p.name for p in repo.iterdir()
             if p.name in (".app.fndl-new", ".app.fndl-old")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
