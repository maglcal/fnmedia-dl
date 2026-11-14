#!/usr/bin/env python3
"""updatesrc 多源更新检查测试（httpx.MockTransport 打桩网络）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config as cfgmod                           # noqa: E402
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

CFG_GITEE = {"update": {"sources": [
    {"name": "gitee", "type": "gitee", "repo": "maglcal/fnmedia-dl"},
    {"name": "github", "type": "github", "repo": "maglcal/fnmedia-dl"},
], "timeout": 5, "retries": 2}}

# Gitee 发行版真实形状（2026-11 实测）：release 无 html_url（代码合成）、
# 资产仅 name + browser_download_url（无 size → 置 0）
GITEE_RELEASE = {
    "tag_name": "main-v0.2.0",
    "body": "镜像说明",
    "assets": [
        {"name": "fnmedia-dl-main-v0.2.0.fpk",
         "browser_download_url": "https://gitee.com/maglcal/fnmedia-dl/"
                                 "releases/download/main-v0.2.0/"
                                 "fnmedia-dl-main-v0.2.0.fpk"},
        {"name": "fnmedia-dl-main-v0.2.0.fpk.sha256",
         "browser_download_url": "https://gitee.com/maglcal/fnmedia-dl/"
                                 "releases/download/main-v0.2.0/"
                                 "fnmedia-dl-main-v0.2.0.fpk.sha256"},
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

    def test_gitee_without_repo_skipped(self):
        cfg = {"update": {"sources": [
            {"name": "gt", "type": "gitee", "repo": ""},
            {"name": "g", "type": "github", "repo": "o/r"}]}}
        out = updatesrc.resolve_sources(cfg)
        self.assertEqual([s["name"] for s in out], ["g"])

    def test_gitee_with_repo_kept(self):
        cfg = {"update": {"sources": [
            {"name": "gt", "type": "gitee", "repo": "o/r"},
            {"name": "g", "type": "github", "repo": "o/r"}]}}
        out = updatesrc.resolve_sources(cfg)
        self.assertEqual([s["name"] for s in out], ["gt", "g"])

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

    def test_gitee_download_prefix(self):
        cfg = {"update": {"sources": [
            {"name": "gt", "type": "gitee", "repo": "maglcal/fnmedia-dl"}]}}
        self.assertEqual(updatesrc.allowed_prefixes(cfg),
                         ["https://gitee.com/maglcal/fnmedia-dl/"
                          "releases/download/"])


class TestVerTuple(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(updatesrc._ver_tuple("0.1.23"),
                           updatesrc._ver_tuple("0.1.9"))
        self.assertEqual(updatesrc._ver_tuple("v1.2"), (1, 2, 0))
        self.assertEqual(updatesrc._ver_tuple("1.x.3"), (1, 0, 3))
        self.assertEqual(updatesrc._ver_tuple(""), (0, 0, 0))

    def test_four_segment_branch_version(self):
        # 分支测试版四段号：主版本号 0.1.36 + 分支序号 1，不截断
        self.assertEqual(updatesrc._ver_tuple("0.1.36.1"), (0, 1, 36, 1))
        self.assertEqual(updatesrc._ver_tuple("v0.1.25.3"), (0, 1, 25, 3))

    def test_branch_ordering(self):
        # 主线三段与分支四段混比：0.1.36 < 0.1.36.1 < 0.1.37
        self.assertTrue(updatesrc._ver_newer("0.1.36.1", "0.1.36"))
        self.assertTrue(updatesrc._ver_newer("0.1.37", "0.1.36.1"))
        self.assertFalse(updatesrc._ver_newer("0.1.36", "0.1.36.1"))
        self.assertFalse(updatesrc._ver_newer("0.1.36.1", "0.1.37"))
        # 补零对齐：0.1.36 与 0.1.36.0 等价，不判更新
        self.assertFalse(updatesrc._ver_newer("0.1.36.0", "0.1.36"))
        self.assertFalse(updatesrc._ver_newer("0.1.36", "0.1.36.0"))
        # 分支序号递增
        self.assertTrue(updatesrc._ver_newer("0.1.36.2", "0.1.36.1"))


class TestStripVer(unittest.TestCase):
    """发布标识 → 纯版本号：新格式 main-vX.Y.Z 与旧格式 vX.Y.Z 兼容。"""

    def test_main_v_prefix(self):
        self.assertEqual(updatesrc.strip_ver("main-v0.2.0"), "0.2.0")
        self.assertEqual(updatesrc.strip_ver("main-V1.2.3"), "1.2.3")

    def test_legacy_v_prefix(self):
        self.assertEqual(updatesrc.strip_ver("v0.1.22"), "0.1.22")
        self.assertEqual(updatesrc.strip_ver("V0.1.34"), "0.1.34")

    def test_plain_and_edge(self):
        self.assertEqual(updatesrc.strip_ver("0.2.0"), "0.2.0")
        self.assertEqual(updatesrc.strip_ver(""), "")
        self.assertEqual(updatesrc.strip_ver(None), "")
        self.assertEqual(updatesrc.strip_ver("  main-v0.2.0  "), "0.2.0")

    def test_branch_four_segment(self):
        # 分支测试版四段号剥前缀后原样保留
        self.assertEqual(updatesrc.strip_ver("v0.1.36.1"), "0.1.36.1")
        self.assertEqual(updatesrc.strip_ver("main-v0.1.36.1"), "0.1.36.1")


class TestMainVTagFormat(unittest.TestCase):
    """新命名 main-vX.Y.Z 的解析：version 字段剥前缀、tag 原样保留。"""

    def test_github_main_v_tag(self):
        # 真实新格式 release：tag_name 与资产下载 URL 都是 main-vX.Y.Z 路径
        rel = dict(GH_RELEASE, tag_name="main-v0.2.0",
                   html_url="https://github.com/maglcal/fnmedia-dl/"
                            "releases/tag/main-v0.2.0",
                   assets=[{**a,
                            "browser_download_url":
                                a["browser_download_url"].replace(
                                    "/download/v0.2.0/",
                                    "/download/main-v0.2.0/")}
                           for a in GH_RELEASE["assets"]])

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/releases/latest"):
                return httpx.Response(200, json=rel)
            return httpx.Response(200, text="ab" * 32 + "  x.fpk\n")

        d = updatesrc.check(CFG, "0.1.34", client=_client(handler))
        self.assertEqual(d["source"], "github")
        self.assertEqual(d["latest"], "0.2.0")           # 已剥 main-v 前缀
        self.assertTrue(d["has_update"])
        # 回退候选含另一命名（旧 vX.Y.Z）与镜像两种布局的合成地址
        fpk = next(a for a in d["assets"] if a["name"].endswith(".fpk"))
        self.assertIn("https://github.com/maglcal/fnmedia-dl/releases/"
                      "download/v0.2.0/fnmedia-dl-0.2.0.fpk",
                      fpk["fallback_urls"])
        self.assertIn("http://mirror.example.com/raw/fnmedia-dl/"
                      "releases/main-v0.2.0/fnmedia-dl-0.2.0.fpk",
                      fpk["fallback_urls"])

    def test_repobox_main_v_version_field(self):
        # 真实新格式清单：sync 脚本按 tag 原文存 releases/main-vX.Y.Z/ 布局
        man = dict(MANIFEST, version="main-v0.2.0", tag="main-v0.2.0",
                   assets=[dict(MANIFEST["assets"][0],
                                path="releases/main-v0.2.0/fnmedia-dl-main-v0.2.0.fpk",
                                name="fnmedia-dl-main-v0.2.0.fpk")])

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=man)

        d = updatesrc.check(CFG, "0.1.34", client=_client(handler))
        self.assertEqual(d["latest"], "0.2.0")
        self.assertTrue(d["has_update"])
        self.assertEqual(d["assets"][0]["url"],
                         "http://mirror.example.com/raw/fnmedia-dl/"
                         "releases/main-v0.2.0/fnmedia-dl-main-v0.2.0.fpk")

    def test_no_update_when_current_is_newer(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=GH_RELEASE)   # tag v0.2.0

        d = updatesrc.check(CFG, "0.3.0", client=_client(handler))
        self.assertFalse(d["has_update"])


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
        # 跨源回退候选（新旧 tag 命名各合成一份；自身主 url 去重不重复）
        self.assertEqual(a["fallback_urls"], [
            "http://mirror.example.com/raw/fnmedia-dl/"
            "releases/main-v0.2.0/fnmedia-dl-0.2.0.fpk",
            "https://github.com/maglcal/fnmedia-dl/releases/"
            "download/v0.2.0/fnmedia-dl-0.2.0.fpk",
            "https://github.com/maglcal/fnmedia-dl/releases/"
            "download/main-v0.2.0/fnmedia-dl-0.2.0.fpk"])

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
        # 回退候选含镜像拼接地址（同步脚本布局 releases/{tag}/{name}）与另一命名
        self.assertEqual(fpk["fallback_urls"], [
            "http://mirror.example.com/raw/fnmedia-dl/"
            "releases/v0.2.0/fnmedia-dl-0.2.0.fpk",
            "http://mirror.example.com/raw/fnmedia-dl/"
            "releases/main-v0.2.0/fnmedia-dl-0.2.0.fpk",
            "https://github.com/maglcal/fnmedia-dl/releases/"
            "download/main-v0.2.0/fnmedia-dl-0.2.0.fpk"])

    def test_github_sib_sha256_follows_redirect(self):
        # GitHub 资产下载恒 302 到 release-assets CDN（实测）：哈希抓取不
        # 跟随跳转会抓空 → fail-closed 拒绝下载（潜伏 bug 回归钉子）
        cfg = {"update": {"sources": [
            {"name": "github", "type": "github",
             "repo": "maglcal/fnmedia-dl"}]}}

        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if u.endswith("/releases/latest"):
                return httpx.Response(200, json=GH_RELEASE)
            if "release-assets.githubusercontent.com" in u:
                return httpx.Response(200, text="cd" * 32 + "  x.fpk\n")
            if u.endswith(".sha256"):
                return httpx.Response(
                    302, headers={"location":
                                  "https://release-assets.githubusercontent."
                                  "com/x"})
            return httpx.Response(404)

        d = updatesrc.check(cfg, "0.1.0", client=_client(handler))
        fpk = next(a for a in d["assets"] if a["name"].endswith(".fpk"))
        self.assertEqual(fpk["sha256"], "cd" * 32)

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


class TestGiteeSource(unittest.TestCase):
    """Gitee 镜像源：实测响应形状（release 无 html_url、资产无 size）、
    404/缺 tag 落下一源、兄弟哈希抓取跟随 302。"""

    def test_gitee_success(self):
        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if u.endswith("/releases/latest"):
                self.assertIn("/repos/maglcal/fnmedia-dl/releases/latest", u)
                return httpx.Response(200, json=GITEE_RELEASE)
            if u.endswith(".sha256"):
                return httpx.Response(200, text="ab" * 32 + "  x.fpk\n")
            return httpx.Response(404)

        d = updatesrc.check(CFG_GITEE, "0.1.0", client=_client(handler))
        self.assertEqual(d["source"], "gitee")
        self.assertEqual(d["latest"], "0.2.0")           # 剥 main-v 前缀
        self.assertTrue(d["has_update"])
        self.assertEqual(d["url"],
                         "https://gitee.com/maglcal/fnmedia-dl/"
                         "releases/tag/main-v0.2.0")     # 无 html_url → 合成
        self.assertEqual(d["notes"], "镜像说明")
        fpk = next(a for a in d["assets"] if a["name"].endswith(".fpk"))
        self.assertEqual(fpk["url"],
                         "https://gitee.com/maglcal/fnmedia-dl/releases/"
                         "download/main-v0.2.0/fnmedia-dl-main-v0.2.0.fpk")
        self.assertEqual(fpk["sha256"], "ab" * 32)       # 兄弟 .sha256 附件
        self.assertEqual(fpk["size"], 0)                 # Gitee 资产无 size
        # 跨源回退候选：另一 tag 命名 + GitHub 兜底合成地址
        self.assertEqual(fpk["fallback_urls"], [
            "https://gitee.com/maglcal/fnmedia-dl/releases/"
            "download/v0.2.0/fnmedia-dl-main-v0.2.0.fpk",
            "https://github.com/maglcal/fnmedia-dl/releases/"
            "download/main-v0.2.0/fnmedia-dl-main-v0.2.0.fpk",
            "https://github.com/maglcal/fnmedia-dl/releases/"
            "download/v0.2.0/fnmedia-dl-main-v0.2.0.fpk"])
        # 归一化格式与其它源完全一致（切换源不改变消费方）
        self.assertEqual(set(d.keys()),
                         {"ok", "current", "latest", "has_update", "url",
                          "notes", "source", "notice", "assets", "fetched_at"})
        self.assertEqual(set(fpk.keys()),
                         {"name", "url", "size", "sha256", "fallback_urls"})

    def test_gitee_404_falls_to_github(self):
        # 镜像仓未建/未公开/未同步（实测 404 Not Found Project）→ 落 GitHub，
        # 绝不能把空清单误报成「无更新」
        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if "gitee.com" in u:
                return httpx.Response(404,
                                      json={"message": "Not Found Project"})
            if u.endswith("/releases/latest"):
                return httpx.Response(200, json=GH_RELEASE)
            return httpx.Response(200, text="cd" * 32 + "  x.fpk\n")

        d = updatesrc.check(CFG_GITEE, "0.1.0", client=_client(handler))
        self.assertEqual(d["source"], "github")
        self.assertEqual(d["latest"], "0.2.0")
        self.assertIn("已自动切换到备用源", d["notice"])
        self.assertIn("gitee", d["notice"])

    def test_gitee_missing_tag_falls_to_next(self):
        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if "gitee.com" in u:
                return httpx.Response(200, json={"assets": []})
            if u.endswith("/releases/latest"):
                return httpx.Response(200, json=GH_RELEASE)
            return httpx.Response(200, text="cd" * 32 + "  x.fpk\n")

        d = updatesrc.check(CFG_GITEE, "0.1.0", client=_client(handler))
        self.assertEqual(d["source"], "github")

    def test_gitee_sib_sha256_follows_redirect(self):
        # Gitee 资产下载恒 302 到 foruda.gitee.com 附件 CDN：哈希抓取须跟随
        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if u.endswith("/releases/latest"):
                return httpx.Response(200, json=GITEE_RELEASE)
            if "foruda.gitee.com" in u:
                return httpx.Response(200, text="ab" * 32 + "  x.fpk\n")
            if u.endswith(".sha256"):
                return httpx.Response(
                    302, headers={"location":
                                  "https://foruda.gitee.com/"
                                  "attach_file?token=t"})
            return httpx.Response(404)

        d = updatesrc.check(CFG_GITEE, "0.1.0", client=_client(handler))
        fpk = next(a for a in d["assets"] if a["name"].endswith(".fpk"))
        self.assertEqual(fpk["sha256"], "ab" * 32)


class TestFallbackNotice(unittest.TestCase):
    """回退透明提示：备用源被启用时说明原因与切换目标；首选成功不提示。"""

    def test_first_source_ok_no_notice(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=MANIFEST)

        d = updatesrc.check(CFG, "0.1.0", client=_client(handler))
        self.assertEqual(d["notice"], "")

    def test_fallback_notice_explains_switch(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "mirror.example.com" in str(req.url):
                return httpx.Response(500)
            if req.url.path.endswith("/releases/latest"):
                return httpx.Response(200, json=GH_RELEASE)
            return httpx.Response(200, text="cd" * 32 + "  x.fpk\n")

        d = updatesrc.check(CFG, "0.1.0", client=_client(handler))
        self.assertIn("已自动切换到备用源", d["notice"])
        self.assertIn("github", d["notice"])          # 切换目标
        self.assertIn("repobox", d["notice"])         # 失败源
        self.assertIn("HTTP 500", d["notice"])        # 失败原因

    def test_all_sources_fail_lists_names(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with self.assertRaises(updatesrc.SourceError) as cm:
            updatesrc.check(CFG, "0.1.0", client=_client(handler))
        msg = str(cm.exception)
        self.assertIn("repobox", msg)
        self.assertIn("github", msg)


class TestMigrateUpdateSources(unittest.TestCase):
    """update.sources v2 迁移：出厂单 github 源 → 前置 gitee 国内源。"""

    OLD_DEFAULT = [{"name": "github", "type": "github",
                    "repo": "maglcal/fnmedia-dl"}]
    NEW_DEFAULT = [{"name": "gitee", "type": "gitee",
                    "repo": "maglcal/fnmedia-dl"},
                   {"name": "github", "type": "github",
                    "repo": "maglcal/fnmedia-dl"}]

    def test_default_config_is_gitee_first(self):
        self.assertEqual(cfgmod.DEFAULT_CONFIG["update"]["sources"],
                         self.NEW_DEFAULT)

    def test_sources_v_not_in_default(self):
        # 版本戳不进 DEFAULT_CONFIG：进了默认配置 merge 后恒为 2、迁移永不触发
        self.assertNotIn("sources_v", cfgmod.DEFAULT_CONFIG["update"])

    def test_factory_default_gets_gitee_prepended(self):
        cfg = {"update": {"sources": [dict(s) for s in self.OLD_DEFAULT]}}
        out = cfgmod._migrate_update_sources(cfg)
        self.assertEqual(out["update"]["sources"], self.NEW_DEFAULT)
        self.assertEqual(out["update"]["sources_v"], 2)

    def test_customized_sources_untouched(self):
        srcs = [{"name": "repobox", "type": "repobox",
                 "base_url": "http://m", "repo": "x"},
                {"name": "github", "type": "github",
                 "repo": "maglcal/fnmedia-dl"}]
        cfg = {"update": {"sources": [dict(s) for s in srcs]}}
        out = cfgmod._migrate_update_sources(cfg)
        self.assertEqual(out["update"]["sources"], srcs)   # 改过的一律不动
        self.assertEqual(out["update"]["sources_v"], 2)

    def test_already_migrated_untouched(self):
        cfg = {"update": {"sources": [dict(s) for s in self.NEW_DEFAULT],
                          "sources_v": 2}}
        out = cfgmod._migrate_update_sources(cfg)
        self.assertEqual(out["update"]["sources"], self.NEW_DEFAULT)

    def test_no_update_section_untouched(self):
        self.assertEqual(cfgmod._migrate_update_sources({}), {})


class TestConfigLoadMigration(unittest.TestCase):
    """经 load()/save() 的迁移集成：老配置读出即前置 gitee 并落版本戳。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._orig = (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        cfgmod.STATE_DIR = Path(self._td.name)
        cfgmod.CONFIG_PATH = Path(self._td.name) / "config.json"

    def tearDown(self):
        (cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig

    def _write_old_default(self):
        cfgmod.CONFIG_PATH.write_text(json.dumps(
            {"update": {"sources": [{"name": "github", "type": "github",
                                     "repo": "maglcal/fnmedia-dl"}]}}),
            encoding="utf-8")

    def test_load_prepends_gitee(self):
        self._write_old_default()
        out = cfgmod.load()
        self.assertEqual([s["name"] for s in out["update"]["sources"]],
                         ["gitee", "github"])

    def test_save_roundtrip_persists_migration(self):
        self._write_old_default()
        cfgmod.save(cfgmod.load())
        raw = json.loads(cfgmod.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in raw["update"]["sources"]],
                         ["gitee", "github"])
        self.assertEqual(raw["update"]["sources_v"], 2)

    def test_marker_set_skips_migration(self):
        # 用户删条目只想单源：写 "sources_v": 2 跳过迁移（文档化逃生口）
        cfgmod.CONFIG_PATH.write_text(json.dumps(
            {"update": {"sources": [{"name": "github", "type": "github",
                                     "repo": "maglcal/fnmedia-dl"}],
                        "sources_v": 2}}), encoding="utf-8")
        out = cfgmod.load()
        self.assertEqual([s["name"] for s in out["update"]["sources"]],
                         ["github"])


class TestFormatConsistency(unittest.TestCase):
    """两源更新格式一致：归一化后字段结构完全相同（切换源不改变消费方）。"""

    def test_normalized_shape_identical(self):
        def h_mirror(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=MANIFEST)

        def h_github(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/releases/latest"):
                return httpx.Response(200, json=GH_RELEASE)
            return httpx.Response(200, text="cd" * 32 + "  x.fpk\n")

        d1 = updatesrc.check(CFG, "0.1.0", client=_client(h_mirror))
        cfg_gh = {"update": {"sources": [CFG["update"]["sources"][1]]}}
        d2 = updatesrc.check(cfg_gh, "0.1.0", client=_client(h_github))
        self.assertEqual(set(d1.keys()), set(d2.keys()))
        self.assertEqual(set(d1["assets"][0].keys()),
                         set(d2["assets"][0].keys()))
        for k in ("ok", "current", "latest", "has_update", "url", "notes",
                  "source", "notice", "assets", "fetched_at"):
            self.assertIn(k, d1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
