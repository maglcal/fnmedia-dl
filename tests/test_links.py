#!/usr/bin/env python3
"""links.py 单元测试（stdlib unittest，无第三方依赖）。"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import links  # noqa: E402


class TestThunder(unittest.TestCase):
    def test_decode_magnet_inner(self):
        import base64
        inner = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        url = "thunder://" + base64.b64encode(f"AA{inner}ZZ".encode()).decode()
        self.assertEqual(links.decode_thunder(url), inner)

    def test_decode_http_inner(self):
        import base64
        inner = "https://example.com/a.iso"
        url = "thunder://" + base64.b64encode(f"AA{inner}ZZ".encode()).decode()
        info = links.parse_line(url)
        self.assertTrue(info.ok)
        self.assertEqual(info.kind, "https")
        self.assertEqual(info.url, inner)
        self.assertEqual(info.meta.get("thunder_original"), url)

    def test_decode_bad(self):
        self.assertIsNone(links.decode_thunder("thunder://!!!not-base64!!!"))
        info = links.parse_line("thunder://!!!not-base64!!!")
        self.assertFalse(info.ok)


class TestMagnet(unittest.TestCase):
    HEX = "0123456789abcdef0123456789abcdef01234567"

    def test_hex_infohash(self):
        info = links.parse_line(f"magnet:?xt=urn:btih:{self.HEX}&dn=My+File")
        self.assertTrue(info.ok)
        self.assertEqual(info.kind, "magnet")
        self.assertEqual(info.meta["infohash"], self.HEX)
        self.assertEqual(info.meta["infohash_algo"], "hex")
        self.assertEqual(info.display, "My File")

    def test_base32_infohash(self):
        b32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        info = links.parse_line(f"magnet:?xt=urn:btih:{b32}")
        self.assertTrue(info.ok)
        self.assertEqual(info.meta["infohash_algo"], "base32")
        self.assertEqual(info.meta["infohash"], b32)

    def test_trackers(self):
        info = links.parse_line(
            f"magnet:?xt=urn:btih:{self.HEX}&tr=udp%3A%2F%2Ft.example%3A80")
        self.assertEqual(info.meta["trackers"], ["udp://t.example:80"])

    def test_missing_xt(self):
        info = links.parse_line("magnet:?dn=only-name")
        self.assertFalse(info.ok)

    def test_bad_hash(self):
        info = links.parse_line("magnet:?xt=urn:btih:xyz")
        self.assertFalse(info.ok)


class TestEd2k(unittest.TestCase):
    def test_parse(self):
        url = "ed2k://|file|demo.mkv|12345|0123456789abcdef0123456789abcdef|/"
        info = links.parse_line(url)
        self.assertTrue(info.ok)
        self.assertFalse(info.dispatchable)
        self.assertEqual(info.meta["name"], "demo.mkv")
        self.assertEqual(info.meta["size"], 12345)


class TestDirect(unittest.TestCase):
    def test_http(self):
        info = links.parse_line("https://example.com/path/file.iso")
        self.assertTrue(info.ok)
        self.assertEqual(info.kind, "https")
        self.assertEqual(info.display, "file.iso")

    def test_torrent_url(self):
        info = links.parse_line("https://example.com/file.torrent")
        self.assertEqual(info.kind, "torrent")
        self.assertIn("torrent_url", info.meta)


class TestTorrentFile(unittest.TestCase):
    def _make_torrent(self) -> tuple[bytes, str]:
        info_raw = (b"d6:lengthi12345e4:name4:test"
                    b"12:piece lengthi16384e6:pieces20:" + b"\x00" * 20 + b"e")
        data = b"d4:info" + info_raw + b"e"
        return data, hashlib.sha1(info_raw).hexdigest()

    def test_infohash(self):
        data, expect = self._make_torrent()
        self.assertEqual(links.torrent_infohash(data), expect)

    def test_parse_file(self):
        import tempfile
        data, expect = self._make_torrent()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "demo.torrent"
            p.write_bytes(data)
            info = links.parse_torrent_file(p)
        self.assertTrue(info.ok)
        self.assertEqual(info.meta["infohash"], expect)
        self.assertTrue(info.url.startswith("magnet:?xt=urn:btih:" + expect))

    def test_corrupt(self):
        self.assertIsNone(links.torrent_infohash(b"not a torrent"))


class TestParseText(unittest.TestCase):
    HEX = "0123456789abcdef0123456789abcdef01234567"

    def test_batch_split(self):
        text = (f"magnet:?xt=urn:btih:{self.HEX}\n"
                "https://example.com/a.iso，ed2k://|file|d.mkv|1|aa|/")
        got = links.parse_text(text)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0].kind, "magnet")
        self.assertEqual(got[1].kind, "https")
        self.assertEqual(got[2].kind, "ed2k")

    def test_no_split_on_comma_in_url(self):
        """直链查询参数带逗号不得被拆断（回归）。"""
        got = links.parse_text("https://example.com/dl/f.iso?name=a,b&x=1")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].ok)
        self.assertEqual(got[0].url, "https://example.com/dl/f.iso?name=a,b&x=1")

    def test_no_split_on_dunhao_in_magnet_dn(self):
        """磁力 dn 含裸顿号不得被拆断（回归）。"""
        url = f"magnet:?xt=urn:btih:{self.HEX}&dn=电影、电视剧合集"
        got = links.parse_text(url)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].ok)
        self.assertEqual(got[0].url, url)
        self.assertEqual(got[0].display, "电影、电视剧合集")

    def test_split_multiple_schemes_in_one_line(self):
        """一行挤多条：按协议前缀切，剥掉段尾分隔符。"""
        got = links.parse_text(
            f"https://example.com/a.iso,magnet:?xt=urn:btih:{self.HEX}")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].url, "https://example.com/a.iso")
        self.assertEqual(got[1].kind, "magnet")

    def test_no_split_on_unencoded_magnet_tracker(self):
        """磁力 &tr= 带未编码 https tracker 不得被拆成两条（回归）。

        此前按协议前缀无脑切，1 条磁力被拆成「截残磁力 + tracker 直链」
        两条派发，任务列表凭空多出一条任务且磁力丢光 tracker。"""
        url = (f"magnet:?xt=urn:btih:{self.HEX}&dn=Test"
               "&tr=https://tracker.example.com/announce")
        got = links.parse_text(url)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].ok)
        self.assertEqual(got[0].url, url)

    def test_no_split_on_multiple_unencoded_trackers(self):
        """多个未编码 https tracker + udp tracker 整条不拆。"""
        url = (f"magnet:?xt=urn:btih:{self.HEX}"
               "&tr=https://t1.example.com/a&tr=https://t2.example.com/b"
               "&tr=udp://open.demonii.com:1337/announce")
        got = links.parse_text(url)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, url)

    def test_no_split_on_redirect_param_in_direct_url(self):
        """直链查询参数值里的 https:// 不是新链接（回归）。"""
        url = "https://proxy.example.com/f?src=https://origin.example.com/x.zip"
        got = links.parse_text(url)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, url)

    def test_split_still_parts_after_tracker_on_separator(self):
        """磁力带 tracker 后，分隔符后的第二条链接照常切开。"""
        url = (f"magnet:?xt=urn:btih:{self.HEX}&tr=https://t.example.com/a，"
               f"magnet:?xt=urn:btih:{self.HEX}")
        got = links.parse_text(url)
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].url.endswith("&tr=https://t.example.com/a"))
        self.assertEqual(got[1].kind, "magnet")

    def test_split_magnet_tracker_then_space_direct(self):
        """磁力（含未编码 tracker）与空格后的直链切成两条，磁力不截残。"""
        url = (f"magnet:?xt=urn:btih:{self.HEX}&tr=https://t.example.com/a"
               " https://dl.example.com/f.zip")
        got = links.parse_text(url)
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].url.startswith("magnet:"))
        self.assertIn("&tr=https://t.example.com/a", got[0].url)
        self.assertEqual(got[1].url, "https://dl.example.com/f.zip")

    def test_split_strips_numbering_prefix(self):
        """行首序号/说明前缀丢弃，保留协议段。"""
        got = links.parse_text(f"1、magnet:?xt=urn:btih:{self.HEX}")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, f"magnet:?xt=urn:btih:{self.HEX}")

    def test_split_fallback_without_scheme(self):
        """无协议前缀的行仍按分隔符兜底拆。"""
        got = links.parse_text("abc，def xyz")
        self.assertEqual([g.url for g in got], ["abc", "def", "xyz"])

    def test_bad_link_not_dispatchable(self):
        """解析失败的链接不可派发（回归：残缺链接曾被照常派发）。"""
        for bad in (f"magnet:?xt=urn:btih:{'z' * 40}",   # 哈希格式非法
                    "magnet:?dn=x",                        # 缺哈希
                    "foobar://x"):                         # 未知协议
            infos = links.parse_text(bad)
            self.assertEqual(len(infos), 1, bad)
            self.assertFalse(infos[0].ok, bad)
            self.assertFalse(infos[0].dispatchable, bad)

    def test_ed2k_ok_but_not_dispatchable(self):
        info = links.parse_line("ed2k://|file|d.mkv|1|aa|/")
        self.assertTrue(info.ok)
        self.assertFalse(info.dispatchable)

    def test_empty(self):
        self.assertEqual(links.parse_text("  \n "), [])


if __name__ == "__main__":
    unittest.main()
