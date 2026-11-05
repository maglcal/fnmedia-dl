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

    def test_empty(self):
        self.assertEqual(links.parse_text("  \n "), [])


if __name__ == "__main__":
    unittest.main()
