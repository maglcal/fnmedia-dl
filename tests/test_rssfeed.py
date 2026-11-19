#!/usr/bin/env python3
"""RSS 订阅（rssfeed.py）测试：BEP 36 解析优先级、三轴过滤、指纹去重、
CRUD、check 闭环（MockTransport 注入假 feed）、派发幂等 + API 层。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import httpx                                                # noqa: E402

import config as cfgmod                                     # noqa: E402
import downloader                                           # noqa: E402
import rssfeed as rssmod                                    # noqa: E402

# TestClient 直连绕过网关（同 test_api 口径）：写请求走本地调试豁免开关。
os.environ.setdefault("FNDL_DEV_TRUST", "1")

IH_A = "a" * 40
IH_B = "b" * 40
IH_C = "c" * 40
IH_D = "d" * 40


# ---------------------------------------------------------------- 夹具

def rss_wrap(items_xml: str) -> bytes:
    return (f'<?xml version="1.0"?><rss version="2.0"><channel>'
            f'<title>t</title>{items_xml}</channel></rss>').encode("utf-8")


def entry_link(guid: str, title: str, link: str) -> str:
    """纯 <link> 条目（磁力 / 详情页）。"""
    return f"<item><title>{title}</title><guid>{guid}</guid><link>{link}</link></item>"


def entry_enc(guid: str, title: str, url: str, length: int = 0,
              typ: str = "application/x-bittorrent") -> str:
    return (f'<item><title>{title}</title><guid>{guid}</guid>'
            f'<enclosure url="{url}" length="{length}" type="{typ}"/></item>')


def magnet(ih: str) -> str:
    return f"magnet:?xt=urn:btih:{ih}"


class RssTestBase(unittest.TestCase):
    """state 文件隔离（TemporaryDirectory 同 taskqueue 范式）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.feed = rssmod.RssFeed(self.td / "rss.json")

    def tearDown(self):
        self._td.cleanup()

    # ---- 常用夹具 ----

    def mock_client(self, *, status: int = 200, body: bytes = b"",
                    headers: dict | None = None,
                    capture: list | None = None) -> httpx.Client:
        def handler(request):
            if capture is not None:
                capture.append(request)
            return httpx.Response(status, content=body,
                                  headers=headers or {})
        return httpx.Client(transport=httpx.MockTransport(handler))

    def seed_sub(self, **kw) -> dict:
        return self.feed.add("https://rss.example/feed.xml", "测试源", **kw)

    def seed_item(self, *, entries: str | None = None,
                  history_check=None, **add_kw) -> dict:
        """加订阅 + 一次 check 入库，返回首个条目。"""
        sub = self.seed_sub(**add_kw)
        body = rss_wrap(entries if entries is not None
                        else entry_link("g1", "t1", magnet(IH_A)))
        cli = self.mock_client(body=body)
        res = self.feed.check(sub["id"], client=cli,
                              history_check=history_check)
        self.assertTrue(res["ok"])
        return res["items"][0]


# ---------------------------------------------------------------- 解析

class TestParse(RssTestBase):
    def test_enclosure_wins_over_all(self):
        """① enclosure(type=application/x-bittorrent) 优先于 infoHash/link/描述。"""
        xml = (f'<item><title>combo</title><guid>g</guid>'
               f'<link>{magnet(IH_B)}</link>'
               f'<torrent:infoHash xmlns:torrent="http://x.example">{IH_A}'
               f'</torrent:infoHash>'
               f'<enclosure url="https://cdn.example/f.torrent" length="77"'
               f' type="application/x-bittorrent"/>'
               f'<description>get {magnet(IH_C)}</description></item>')
        out = rssmod._parse_feed(rss_wrap(xml))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://cdn.example/f.torrent")
        self.assertEqual(out[0]["size"], 77)

    def test_enclosure_torrent_suffix_without_type(self):
        xml = entry_enc("g", "t", "https://cdn.example/x.torrent", 5)
        out = rssmod._parse_feed(rss_wrap(xml))
        self.assertEqual(out[0]["url"], "https://cdn.example/x.torrent")

    def test_infohash_builds_magnet(self):
        """② <torrent><infoHash> → 组磁力，contentLength 作大小。"""
        xml = (f'<item><title>ih</title><guid>g</guid>'
               f'<torrent:infoHash xmlns:torrent="http://x.example">{IH_A}'
               f'</torrent:infoHash>'
               f'<torrent:contentLength xmlns:torrent="http://x.example">'
               f'4096</torrent:contentLength></item>')
        out = rssmod._parse_feed(rss_wrap(xml))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["url"].startswith(f"magnet:?xt=urn:btih:{IH_A}"))
        self.assertEqual(out[0]["size"], 4096)

    def test_invalid_infohash_ignored(self):
        xml = ('<item><title>bad</title><guid>g</guid>'
               '<torrent:infoHash xmlns:torrent="http://x.example">zzz'
               '</torrent:infoHash></item>')
        self.assertEqual(rssmod._parse_feed(rss_wrap(xml)), [])

    def test_link_magnet(self):
        out = rssmod._parse_feed(rss_wrap(entry_link("g", "t", magnet(IH_B))))
        self.assertEqual(out[0]["url"], magnet(IH_B))

    def test_media_content_magnet(self):
        xml = (f'<item><title>mc</title><guid>g</guid>'
               f'<media:content url="{magnet(IH_D)}" fileSize="2048"'
               f' xmlns:media="http://search.yahoo.com/mrss/"/></item>')
        out = rssmod._parse_feed(rss_wrap(xml))
        self.assertEqual(out[0]["url"], magnet(IH_D))
        self.assertEqual(out[0]["size"], 2048)

    def test_description_magnet_fallback(self):
        """⑤ 描述正则兜底提取磁力。"""
        xml = (f'<item><title>d</title><guid>g</guid>'
               f'<description>get {magnet(IH_A)} now</description></item>')
        out = rssmod._parse_feed(rss_wrap(xml))
        self.assertTrue(out[0]["url"].startswith(f"magnet:?xt=urn:btih:{IH_A}"))

    def test_description_torrent_url_fallback(self):
        xml = ('<item><title>d</title><guid>g</guid>'
               '<description>download https://x.example/dl.torrent now'
               '</description></item>')
        out = rssmod._parse_feed(rss_wrap(xml))
        self.assertEqual(out[0]["url"], "https://x.example/dl.torrent")

    def test_plain_link_not_stored(self):
        """普通 http(s) <link> 是详情页，不入库（防误派发）。"""
        xml = entry_link("g", "t", "https://example.com/detail/1")
        self.assertEqual(rssmod._parse_feed(rss_wrap(xml)), [])

    def test_podcast_enclosure_not_stored(self):
        """非种子 enclosure（播客 mp3 等）P1 不入库。"""
        xml = entry_enc("g", "t", "https://cdn.example/ep1.mp3", 9,
                        typ="audio/mpeg")
        self.assertEqual(rssmod._parse_feed(rss_wrap(xml)), [])

    def test_atom_entry_link_href(self):
        """Atom 容错：entry / link href / id。"""
        feed = (f'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                f'<title>a1</title><id>urn:x</id>'
                f'<link href="{magnet(IH_A)}"/></entry></feed>').encode()
        out = rssmod._parse_feed(feed)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["guid"], "urn:x")
        self.assertEqual(out[0]["url"], magnet(IH_A))

    def test_entity_rejected(self):
        """billion-laughs 防护：ENTITY 声明直接拒绝。"""
        data = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]>'
                b'<rss><channel><item><title>t</title></item></channel></rss>')
        with self.assertRaises(ValueError):
            rssmod._parse_feed(data)

    def test_broken_xml_rejected(self):
        with self.assertRaises(ValueError):
            rssmod._parse_feed(b"<rss><channel>")

    def test_item_cap(self):
        items = "".join(entry_link(f"g{i}", f"t{i}", magnet(f"{i:x}" * 40))
                        for i in range(5))
        with mock.patch.object(rssmod, "MAX_FEED_ITEMS", 3):
            out = rssmod._parse_feed(rss_wrap(items))
        self.assertEqual(len(out), 3)


# ---------------------------------------------------------------- 过滤

class TestFilter(RssTestBase):
    def test_must_not_veto(self):
        flt = rssmod._norm_filter({"must_not": ["cam"]})
        ok, reason = rssmod._filter_entry("Movie CAM版", 0, flt)
        self.assertFalse(ok)
        self.assertIn("cam", reason)

    def test_must_not_case_insensitive(self):
        flt = rssmod._norm_filter({"must_not": ["CAM"]})
        ok, _ = rssmod._filter_entry("movie camrip", 0, flt)
        self.assertFalse(ok)

    def test_must_any_hit(self):
        flt = rssmod._norm_filter({"must": ["1080p", "2160p"]})
        self.assertTrue(rssmod._filter_entry("Movie 2160p", 0, flt)[0])
        self.assertFalse(rssmod._filter_entry("Movie 720p", 0, flt)[0])

    def test_empty_must_passes_all(self):
        flt = rssmod._norm_filter({})
        self.assertTrue(rssmod._filter_entry("随便什么", 0, flt)[0])

    def test_size_thresholds(self):
        flt = rssmod._norm_filter({"size_min_mb": 10, "size_max_mb": 100})
        mb = 1024 * 1024
        self.assertTrue(rssmod._filter_entry("t", 50 * mb, flt)[0])
        ok, reason = rssmod._filter_entry("t", 5 * mb, flt)
        self.assertFalse(ok)
        self.assertIn("体积小于", reason)
        ok, reason = rssmod._filter_entry("t", 200 * mb, flt)
        self.assertFalse(ok)
        self.assertIn("体积大于", reason)

    def test_size_zero_ignores_thresholds(self):
        """feed 未提供大小（0）时大小阈值不生效。"""
        flt = rssmod._norm_filter({"size_min_mb": 10, "size_max_mb": 100})
        self.assertTrue(rssmod._filter_entry("t", 0, flt)[0])

    def test_regex_mode(self):
        flt = rssmod._norm_filter({"must": [r"S\d{2}E\d{2}"],
                                   "must_not": [r"(?i)cam"], "regex": True})
        self.assertTrue(rssmod._filter_entry("Show S01E02", 0, flt)[0])
        self.assertFalse(rssmod._filter_entry("Show S01", 0, flt)[0])
        self.assertFalse(rssmod._filter_entry("S01E02 CAM", 0, flt)[0])

    def test_regex_invalid_rejected(self):
        with self.assertRaises(ValueError):
            rssmod._norm_filter({"must": ["("], "regex": True})

    def test_regex_too_long_rejected(self):
        with self.assertRaises(ValueError):
            rssmod._norm_filter({"must": ["a" * (rssmod.REGEX_MAX_LEN + 1)],
                                 "regex": True})

    def test_norm_filter_strips_blanks(self):
        flt = rssmod._norm_filter({"must": [" a ", "", "  "], "must_not": [""]})
        self.assertEqual(flt["must"], ["a"])
        self.assertEqual(flt["must_not"], [])

    def test_norm_interval(self):
        self.assertEqual(rssmod._norm_interval(30), 30)
        with self.assertRaises(ValueError):
            rssmod._norm_interval(5)          # 低于 10 分钟下限
        with self.assertRaises(ValueError):
            rssmod._norm_interval("abc")

    def test_opt_float(self):
        self.assertIsNone(rssmod._opt_float(None))
        self.assertIsNone(rssmod._opt_float(""))
        self.assertEqual(rssmod._opt_float("1.5"), 1.5)
        with self.assertRaises(ValueError):
            rssmod._opt_float("x")
        with self.assertRaises(ValueError):
            rssmod._opt_float(-1)


# ---------------------------------------------------------------- 指纹

class TestFingerprint(unittest.TestCase):
    def test_btih_identity_preferred(self):
        self.assertEqual(rssmod._fingerprint("g1", "btih:" + IH_A),
                         "btih:" + IH_A)

    def test_ed2k_identity_preferred(self):
        self.assertEqual(rssmod._fingerprint("g1", "ed2k:abc"),
                         "ed2k:abc")

    def test_guid_fallback(self):
        self.assertEqual(rssmod._fingerprint("g1", "url:x"), "g1")
        self.assertEqual(rssmod._fingerprint("", "url:x"), "url:x")


# ---------------------------------------------------------------- CRUD

class TestCrud(RssTestBase):
    def test_add_defaults(self):
        sub = self.feed.add("https://rss.example/feed.xml")
        self.assertTrue(sub["enabled"])
        self.assertEqual(sub["interval_min"], 30)
        self.assertEqual(sub["name"], "rss.example/feed.xml")
        self.assertEqual(sub["fail_count"], 0)

    def test_add_rejects_bad_url(self):
        with self.assertRaises(ValueError):
            self.feed.add("ftp://x/feed")

    def test_add_rejects_short_interval(self):
        with self.assertRaises(ValueError):
            self.feed.add("https://x.example/f", interval_min=5)

    def test_update_partial(self):
        sub = self.seed_sub()
        out = self.feed.update(sub["id"], {"name": "新名"})
        self.assertEqual(out["name"], "新名")
        self.assertEqual(out["url"], sub["url"])       # 未出现的键不动
        out = self.feed.update(sub["id"], {"enabled": False})
        self.assertFalse(out["enabled"])
        self.assertEqual(out["name"], "新名")

    def test_update_rejects_bad_url(self):
        sub = self.seed_sub()
        with self.assertRaises(ValueError):
            self.feed.update(sub["id"], {"url": "notaurl"})

    def test_update_missing_raises(self):
        with self.assertRaises(KeyError):
            self.feed.update("nosuch", {"name": "x"})

    def test_remove_drops_items_keeps_seen(self):
        item = self.seed_item()
        sub_id = self.feed.snapshot()["subs"][0]["id"]
        self.assertTrue(self.feed.remove(sub_id))
        self.assertFalse(self.feed.remove(sub_id))
        data = self.feed._load()
        self.assertEqual(data["subs"], [])
        self.assertEqual(data["items"], {})
        self.assertTrue(data["seen"])                  # seen 保留：跨订阅去重
        with self.assertRaises(KeyError):
            self.feed.items(sub_id)

    def test_snapshot_counts_and_suspend(self):
        item = self.seed_item()
        snap = self.feed.snapshot()["subs"][0]
        self.assertEqual(snap["items_count"], 1)
        self.assertEqual(snap["matched_count"], 1)
        self.assertFalse(snap["suspended"])

    def test_items_sorted_new_first(self):
        sub = self.seed_sub()
        entries = (entry_link("g1", "t1", magnet(IH_A))
                   + entry_link("g2", "t2", magnet(IH_B)))
        cli = self.mock_client(body=rss_wrap(entries))
        self.feed.check(sub["id"], client=cli)
        items = self.feed.items(sub["id"])
        self.assertEqual(len(items), 2)
        self.assertGreaterEqual(items[0]["first_seen"], items[1]["first_seen"])

    def test_items_missing_raises(self):
        with self.assertRaises(KeyError):
            self.feed.items("nosuch")


# ---------------------------------------------------------------- check

class TestCheck(RssTestBase):
    def test_check_dedupe_filter_history(self):
        """入库三态：过滤命中 matched / 排除 skipped / 历史查重 skipped。"""
        sub = self.seed_sub(flt={"must_not": ["cam"]})
        entries = (entry_link("gA", "keep 1080p", magnet(IH_A))
                   + entry_link("gB", "bad CAM", magnet(IH_B))
                   + entry_link("gC", "dup", magnet(IH_C)))
        cli = self.mock_client(body=rss_wrap(entries))

        def hist(url):
            return {"downloaded": IH_C in url}
        res = self.feed.check(sub["id"], client=cli, history_check=hist)
        self.assertTrue(res["ok"])
        self.assertEqual(res["new"], 3)
        self.assertEqual(res["matched"], 1)
        self.assertEqual(res["skipped"], 2)
        by_title = {i["title"]: i for i in res["items"]}
        self.assertEqual(by_title["keep 1080p"]["state"], "matched")
        self.assertEqual(by_title["bad CAM"]["state"], "skipped")
        self.assertIn("排除", by_title["bad CAM"]["last_result"])
        self.assertEqual(by_title["dup"]["state"], "skipped")
        self.assertIn("历史", by_title["dup"]["last_result"])

    def test_check_seen_dedupe(self):
        """seen 指纹层：同一 feed 再检查零新增。"""
        sub = self.seed_sub()
        cli = self.mock_client(body=rss_wrap(entry_link("g1", "t1", magnet(IH_A))))
        first = self.feed.check(sub["id"], client=cli)
        second = self.feed.check(sub["id"], client=cli)
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["new"], 0)
        self.assertEqual(len(self.feed.items(sub["id"])), 1)

    def test_check_conditional_get_304(self):
        """ETag/Last-Modified 回存 + 二次请求带条件头 + 304 不算失败。"""
        sub = self.seed_sub()
        cap: list = []
        cli1 = self.mock_client(
            body=rss_wrap(entry_link("g1", "t1", magnet(IH_A))),
            headers={"etag": 'W/"x"',
                     "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
            capture=cap)
        self.feed.check(sub["id"], client=cli1)
        cli2 = self.mock_client(status=304, capture=cap)
        res = self.feed.check(sub["id"], client=cli2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "not_modified")
        self.assertEqual(res["new"], 0)
        self.assertEqual(cap[1].headers.get("if-none-match"), 'W/"x"')
        self.assertEqual(cap[1].headers.get("if-modified-since"),
                         "Mon, 01 Jan 2024 00:00:00 GMT")
        snap = self.feed.snapshot()["subs"][0]
        self.assertEqual(snap["fail_count"], 0)

    def test_check_http_error_counts_fails(self):
        sub = self.seed_sub()
        cli = self.mock_client(status=500)
        res = self.feed.check(sub["id"], client=cli)
        self.assertFalse(res["ok"])
        self.assertIn("HTTP 500", res["error"])
        snap = self.feed.snapshot()["subs"][0]
        self.assertEqual(snap["fail_count"], 1)
        self.assertIn("HTTP 500", snap["last_error"])
        # 连续失败到 MAX_FAILS → suspended（只搁置 P2 轮询）
        for _ in range(rssmod.MAX_FAILS - 1):
            self.feed.check(sub["id"], client=cli)
        self.assertTrue(self.feed.snapshot()["subs"][0]["suspended"])

    def test_check_oversized_body(self):
        sub = self.seed_sub()
        cli = self.mock_client(body=b"x" * (rssmod.MAX_FEED_BYTES + 1))
        res = self.feed.check(sub["id"], client=cli)
        self.assertFalse(res["ok"])
        self.assertIn("2MB", res["error"])

    def test_check_broken_xml(self):
        sub = self.seed_sub()
        cli = self.mock_client(body=b"<rss><channel>")
        res = self.feed.check(sub["id"], client=cli)
        self.assertFalse(res["ok"])
        self.assertIn("XML", res["error"])

    def test_check_entity_rejected(self):
        sub = self.seed_sub()
        body = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]>'
                b'<rss><channel/></rss>')
        res = self.feed.check(sub["id"], client=self.mock_client(body=body))
        self.assertFalse(res["ok"])
        self.assertIn("实体", res["error"])

    def test_check_missing_raises(self):
        with self.assertRaises(KeyError):
            self.feed.check("nosuch", client=self.mock_client(body=b"<rss/>"))

    def test_history_check_exception_tolerated(self):
        """查重层异常不阻断入库（宁漏勿重退化为按过滤处理）。"""
        sub = self.seed_sub()
        cli = self.mock_client(body=rss_wrap(entry_link("g1", "t1", magnet(IH_A))))

        def boom(url):
            raise RuntimeError("history down")
        res = self.feed.check(sub["id"], client=cli, history_check=boom)
        self.assertTrue(res["ok"])
        self.assertEqual(res["items"][0]["state"], "matched")

    def test_success_resets_fail_count(self):
        sub = self.seed_sub()
        self.feed.check(sub["id"], client=self.mock_client(status=500))
        self.assertEqual(self.feed.snapshot()["subs"][0]["fail_count"], 1)
        res = self.feed.check(sub["id"],
                              client=self.mock_client(
                                  body=rss_wrap(entry_link("g1", "t1",
                                                           magnet(IH_A)))))
        self.assertTrue(res["ok"])
        self.assertEqual(self.feed.snapshot()["subs"][0]["fail_count"], 0)


# ---------------------------------------------------------------- 派发

class TestDispatch(RssTestBase):
    def test_dispatch_success_and_idempotent(self):
        item = self.seed_item()
        calls: list = []

        def ok_fn(url, directory):
            calls.append((url, directory))
            return {"ok": True, "backend": "aria2"}
        res = self.feed.dispatch_item(item["id"], ok_fn)
        self.assertTrue(res["ok"])
        self.assertEqual(calls, [(magnet(IH_A), None)])
        again = self.feed.dispatch_item(item["id"], ok_fn)
        self.assertTrue(again["ok"])
        self.assertTrue(again["already"])              # 幂等不重发
        self.assertEqual(len(calls), 1)
        stored = self.feed.items(self.feed.snapshot()["subs"][0]["id"])[0]
        self.assertEqual(stored["state"], "dispatched")
        self.assertEqual(stored["last_result"], "aria2")

    def test_dispatch_passes_sub_directory(self):
        item = self.seed_item(directory="dl/dir")
        got: list = []
        self.feed.dispatch_item(item["id"],
                                lambda u, d: got.append(d) or {"ok": True})
        self.assertEqual(got, ["dl/dir"])

    def test_dispatch_failure_marks_failed_retry_allowed(self):
        item = self.seed_item()
        res = self.feed.dispatch_item(
            item["id"], lambda u, d: {"ok": False, "error": "boom"})
        self.assertFalse(res["ok"])
        stored = self.feed.items(self.feed.snapshot()["subs"][0]["id"])[0]
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(stored["last_result"], "boom")
        # failed 允许手动重试（手动 = 显式用户意图）
        res2 = self.feed.dispatch_item(item["id"],
                                       lambda u, d: {"ok": True, "backend": "b"})
        self.assertTrue(res2["ok"])
        self.assertEqual(self.feed.items(self.feed.snapshot()["subs"][0]["id"])[0]
                         ["state"], "dispatched")

    def test_dispatch_exception_marks_failed(self):
        item = self.seed_item()

        def boom(u, d):
            raise RuntimeError("net down")
        res = self.feed.dispatch_item(item["id"], boom)
        self.assertFalse(res["ok"])
        self.assertIn("net down", res["error"])

    def test_dispatch_skipped_item_allowed(self):
        """手动派发可越过过滤（显式用户意图）。"""
        item = self.seed_item(
            entries=entry_link("g1", "bad CAM", magnet(IH_A)),
            flt={"must_not": ["cam"]})
        self.assertEqual(item["state"], "skipped")
        res = self.feed.dispatch_item(item["id"],
                                      lambda u, d: {"ok": True, "backend": "x"})
        self.assertTrue(res["ok"])

    def test_dispatch_missing_raises(self):
        with self.assertRaises(KeyError):
            self.feed.dispatch_item("nosuch", lambda u, d: {"ok": True})


# ---------------------------------------------------------------- API

class TestRssAPI(unittest.TestCase):
    """FastAPI TestClient（同 test_api 口径：state 隔离 + 豁免开关）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._orig = (downloader.STATE_DIR, downloader.RECORDS_PATH,
                      cfgmod.STATE_DIR, cfgmod.CONFIG_PATH)
        downloader.STATE_DIR = td
        downloader.RECORDS_PATH = td / "tasks.json"
        cfgmod.STATE_DIR = td
        cfgmod.CONFIG_PATH = td / "config.json"
        from fastapi.testclient import TestClient
        import main
        import stats as statsmod
        self._statsmod = statsmod
        self.client = TestClient(main.app)

    def tearDown(self):
        (downloader.STATE_DIR, downloader.RECORDS_PATH,
         cfgmod.STATE_DIR, cfgmod.CONFIG_PATH) = self._orig
        self._statsmod.shutdown()   # 停上报线程：句柄占用挡临时目录删除
        self._td.cleanup()

    def _add(self, **kw) -> dict:
        payload = {"url": "https://rss.example/feed.xml", "name": "API源"}
        payload.update(kw)
        r = self.client.post("/api/rss/subs", json=payload)
        self.assertEqual(r.status_code, 200)
        return r.json()["sub"]

    def test_crud_flow(self):
        r = self.client.get("/api/rss/subs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["subs"], [])
        sub = self._add()
        r = self.client.get("/api/rss/subs")
        self.assertEqual(len(r.json()["subs"]), 1)
        r = self.client.get(f"/api/rss/subs/{sub['id']}/items")
        self.assertEqual(r.json()["items"], [])
        r = self.client.put(f"/api/rss/subs/{sub['id']}", json={"name": "改名"})
        self.assertEqual(r.json()["sub"]["name"], "改名")
        r = self.client.delete(f"/api/rss/subs/{sub['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.delete(
            f"/api/rss/subs/{sub['id']}").status_code, 404)

    def test_add_validation(self):
        self.assertEqual(self.client.post(
            "/api/rss/subs", json={"url": "ftp://x/f"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/rss/subs",
            json={"url": "https://x.example/f", "interval_min": 5}
        ).status_code, 400)

    def test_update_validation(self):
        sub = self._add()
        self.assertEqual(self.client.put(
            f"/api/rss/subs/{sub['id']}", json={"url": "bad"}).status_code, 400)
        self.assertEqual(self.client.put(
            "/api/rss/subs/nosuch", json={"name": "x"}).status_code, 404)

    def test_items_and_check_404(self):
        self.assertEqual(self.client.get(
            "/api/rss/subs/nosuch/items").status_code, 404)
        self.assertEqual(self.client.post(
            "/api/rss/subs/nosuch/check").status_code, 404)

    def test_check_flow_mocked_fetch(self):
        sub = self._add()
        body = rss_wrap(entry_link("g1", "t1", magnet(IH_A)))
        with mock.patch.object(
                rssmod.RssFeed, "_fetch",
                return_value={"status": "ok", "body": body,
                              "etag": "", "last_modified": ""}):
            r = self.client.post(f"/api/rss/subs/{sub['id']}/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["new"], 1)
        self.assertEqual(r.json()["matched"], 1)
        r = self.client.get(f"/api/rss/subs/{sub['id']}/items")
        self.assertEqual(r.json()["items"][0]["state"], "matched")

    def test_dispatch_flow_mocked(self):
        sub = self._add()
        body = rss_wrap(entry_link("g1", "t1", magnet(IH_A)))
        with mock.patch.object(
                rssmod.RssFeed, "_fetch",
                return_value={"status": "ok", "body": body,
                              "etag": "", "last_modified": ""}):
            self.client.post(f"/api/rss/subs/{sub['id']}/check")
        item_id = self.client.get(
            f"/api/rss/subs/{sub['id']}/items").json()["items"][0]["id"]
        with mock.patch.object(
                downloader.DownloadManager, "dispatch",
                return_value={"ok": True, "backend": "stub"}):
            r = self.client.post(f"/api/rss/items/{item_id}/dispatch",
                                 json={"backend": "auto"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            again = self.client.post(f"/api/rss/items/{item_id}/dispatch",
                                     json={"backend": "auto"})
            self.assertTrue(again.json()["already"])
        self.assertEqual(self.client.post(
            "/api/rss/items/nosuch/dispatch", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
