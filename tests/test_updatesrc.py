#!/usr/bin/env python3
"""updatesrc 多源更新检查测试（httpx.MockTransport 打桩网络）。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import httpx                                     # noqa: E402
import updatesrc                                 # noqa: E402


CFG = {"update": {"sources": [
    {"name": "repobox", "type": "repobox",
     "base_url": "http://mirror.example.com", "repo": "fnmedia-dl",
     "token": "rbx_test"},
    {"name": "github", "type": "github", "repo": "maglcal/fnmedia-dl"},
], "timeout": 5, "retries": 2}}

MANIFEST = {
    "version": "0.2.0", "tag": "v0.2.0",
    "published_at": "2026-01-01T00:00:00Z", "notes": "镜像更新说明",
    "html_url": "https://github.com/maglcal/fnmedia-dl/releases/tag/v0.2.0",
    "assets": [{"name": "fnmedia-dl-0.2.0.fpk",
                "path": "releases/v0.2.0/fnmedia-dl-0.2.0.fpk",
                "size": 123, "sha256": "AB" * 32}],
}

GH_RELEASE = {
    "tag_name": "v0.2.0",
    "html_url": "https://github.com/maglcal/fnmedia-dl/releases/tag/v0.2.0",
    "body": "官方说明",
    "assets": [
        {"name": "fnmedia-dl-0.2.0.fpk",
         "browser_download_url": "https://github.com/maglcal/fnmedia-dl/"
                                 "releases/download/v0.2.0/fnmedia-dl-0.2.0.fpk",
         "size": 123},
        {"name": "fnmedia-dl-0.2.0.fpk.sha256",
         "browser_download_url": "https://github.com/maglcal/fnmedia-dl/"
                                 "releases/download/v0.2.0/fnmedia-dl-0.2.0.fpk.sha256",
         "size": 65},
    ],
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestResolveSources(unittest.TestCase):
    def test_repobox_without_base_skipped(self):
        cfg = {"update": {"sources": [
            {"name": "r", "type": "repobox", "base_url": "", "repo": "x"},
            {"name": "g", "type": "github", "repo": "o/r"}]}}
        out = updatesrc.resolve_sources(cfg)
        self.assertEqual([s["name"] for s in out], ["g"])

    def test_github_without_repo_skipped(self):
        cfg = {"update": {"sources": [
            {"name": "g", "type": "github", "repo": ""},
            {"name": "r", "type": "repobox", "base_url": "http://m", "repo": "x"}]}}
        out = updatesrc.resolve_sources(cfg)
        self.assertEqual([s["name"] for s in out], ["r"])

    def test_order_preserved(self):
        out = updatesrc.resolve_sources(CFG)
        self.assertEqual([s["name"] for s in out], ["repobox", "github"])

    def test_no_sources(self):
        self.assertEqual(updatesrc.resolve_sources({}), [])
        self.assertEqual(updatesrc.resolve_sources({"update": {}}), [])


class TestAllowedPrefixes(unittest.TestCase):
    def test_repobox_raw_prefix(self):
        self.assertEqual(updatesrc.allowed_prefixes(CFG),
                         ["http://mirror.example.com/raw/fnmedia-dl/"])

    def test_no_repobox_source(self):
        cfg = {"update": {"sources": [{"name": "g", "type": "github", "repo": "o/r"}]}}
        self.assertEqual(updatesrc.allowed_prefixes(cfg), [])


class TestVerTuple(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(updatesrc._ver_tuple("0.1.23"),
                           updatesrc._ver_tuple("0.1.9"))
        self.assertEqual(updatesrc._ver_tuple("v1.2"), (1, 2, 0))
        self.assertEqual(updatesrc._ver_tuple("1.x.3"), (1, 0, 3))
        self.assertEqual(updatesrc._ver_tuple(""), (0, 0, 0))


class TestCheck(unittest.TestCase):
    def test_repobox_success(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.assertIn("/raw/fnmedia-dl/latest.json", str(req.url))
            self.assertEqual(req.headers.get("authorization"), "Bearer rbx_test")
            return httpx.Response(200, json=MANIFEST)

        d = updatesrc.check(CFG, "0.1.0", client=_client(handler))
        self.assertTrue(d["ok"])
        self.assertEqual(d["latest"], "0.2.0")
        self.assertEqual(d["source"], "repobox")
        self.assertTrue(d["has_update"])
        a = d["assets"][0]
        self.assertEqual(a["url"],
                         "http://mirror.example.com/raw/fnmedia-dl/"
                         "releases/v0.2.0/fnmedia-dl-0.2.0.fpk")
        self.assertEqual(a["sha256"], "ab" * 32)          # 归一小写
        # 跨源回退候选（github 按约定拼接，不含自身）
        self.assertEqual(a["fallback_urls"], [
            "https://github.com/maglcal/fnmedia-dl/releases/"
            "download/v0.2.0/fnmedia-dl-0.2.0.fpk"])

    def test_repobox_no_update_when_same_version(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=MANIFEST)

        d = updatesrc.check(CFG, "0.2.0", client=_client(handler))
        self.assertFalse(d["has_update"])
        self.assertEqual(d["latest"], "0.2.0")

    def test_fallback_to_github_on_repobox_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "mirror.example.com" in str(req.url):
                return httpx.Response(500)
            if req.url.path.endswith("/releases/latest"):
                return httpx.Response(200, json=GH_RELEASE)
            if req.url.path.endswith(".sha256"):
                return httpx.Response(200, text="cd" * 32 + "  x.fpk\n")
            return httpx.Response(404)

        d = updatesrc.check(CFG, "0.1.0", client=_client(handler))
        self.assertEqual(d["source"], "github")
        self.assertEqual(d["latest"], "0.2.0")
        fpk = next(a for a in d["assets"] if a["name"].endswith(".fpk"))
        self.assertEqual(fpk["sha256"], "cd" * 32)         # 从 .sha256 资产取哈希
        # 回退候选包含镜像拼接地址（同步脚本布局 releases/{tag}/{name}）
        self.assertEqual(fpk["fallback_urls"], [
            "http://mirror.example.com/raw/fnmedia-dl/"
            "releases/v0.2.0/fnmedia-dl-0.2.0.fpk"])

    def test_github_404_empty_manifest(self):
        cfg = {"update": {"sources": [
            {"name": "g", "type": "github", "repo": "maglcal/fnmedia-dl"}]}}

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        d = updatesrc.check(cfg, "0.1.0", client=_client(handler))
        self.assertTrue(d["ok"])
        self.assertFalse(d["has_update"])
        self.assertEqual(d["assets"], [])
        self.assertIn("暂无发布", d["notes"])

    def test_all_sources_fail_raises(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with self.assertRaises(updatesrc.SourceError) as cm:
            updatesrc.check(CFG, "0.1.0", client=_client(handler))
        msg = str(cm.exception)
        self.assertIn("repobox", msg)
        self.assertIn("github", msg)

    def test_no_sources_configured_raises(self):
        with self.assertRaises(updatesrc.SourceError):
            updatesrc.check({}, "0.1.0", client=_client(
                lambda req: httpx.Response(200)))

    def test_repobox_manifest_missing_version(self):
        cfg = {"update": {"sources": [
            {"name": "r", "type": "repobox",
             "base_url": "http://mirror.example.com", "repo": "fnmedia-dl"}]}}

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=json.dumps({"tag": "v1"}))

        with self.assertRaises(updatesrc.SourceError):
            updatesrc.check(cfg, "0.1.0", client=_client(handler))


if __name__ == "__main__":
    unittest.main(verbosity=2)
