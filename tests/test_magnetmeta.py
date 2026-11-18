#!/usr/bin/env python3
"""magnetmeta.py 单元测试（stdlib unittest，全 mock 不触网）。"""
from __future__ import annotations

import hashlib
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import links  # noqa: E402
import magnetmeta  # noqa: E402


def _make_metadata(name: bytes, extra: dict | None = None) -> tuple[bytes, bytes]:
    """构造 info 字典字节 + 对应 infohash。"""
    info = {b"name": name, b"piece length": 262144, b"length": 123}
    if extra:
        info.update(extra)
    meta = magnetmeta._bencode(info)
    return meta, hashlib.sha1(meta).digest()


class TestBencode(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(magnetmeta._bencode(42), b"i42e")
        self.assertEqual(magnetmeta._bencode(-7), b"i-7e")
        self.assertEqual(magnetmeta._bencode(b"ab"), b"2:ab")
        self.assertEqual(magnetmeta._bencode("中"), "中".encode("utf-8")
                                                    .join((b"3:", b"")))

    def test_dict_key_sorted(self):
        out = magnetmeta._bencode({b"b": 1, b"a": 2})
        self.assertEqual(out, b"d1:ai2e1:bi1ee")

    def test_list_roundtrip(self):
        raw = magnetmeta._bencode({b"m": {b"ut_metadata": 3}, b"v": b"x"})
        obj, pos = links._bdecode(raw)
        self.assertEqual(pos, len(raw))
        self.assertEqual(obj, {b"m": {b"ut_metadata": 3}, b"v": b"x"})


class TestHashNormalize(unittest.TestCase):
    def test_hex_passthrough(self):
        h = "80ae772175e56bcbf9e9a4e1689fa023aae68f9e"
        self.assertEqual(magnetmeta._normalize_hash(h), h)

    def test_base32_to_hex(self):
        raw = bytes(range(20))
        import base64
        b32 = base64.b32encode(raw).decode().rstrip("=")
        self.assertEqual(magnetmeta._normalize_hash(b32), raw.hex())

    def test_bad(self):
        self.assertIsNone(magnetmeta._normalize_hash(""))
        self.assertIsNone(magnetmeta._normalize_hash("zzz"))
        self.assertIsNone(magnetmeta._normalize_hash("abcd"))


class TestCompact(unittest.TestCase):
    def test_peer(self):
        addr = magnetmeta._decode_compact_peer(bytes([10, 0, 0, 1, 0x1A, 0xE1]))
        self.assertEqual(addr, ("10.0.0.1", 6881))
        self.assertIsNone(magnetmeta._decode_compact_peer(b"short"))
        self.assertIsNone(magnetmeta._decode_compact_peer(
            bytes([10, 0, 0, 1, 0, 0])))          # 端口 0 丢弃

    def test_nodes(self):
        nid = b"\x01" * 20
        blob = nid + bytes([10, 0, 0, 2, 0x1A, 0xE0])
        out = magnetmeta._decode_compact_nodes(blob + b"\x00" * 3)   # 尾部坏段跳过
        self.assertEqual(out, [(nid, ("10.0.0.2", 6880))])

    def test_dht_response(self):
        nid = b"\x02" * 20
        body = {
            b"t": b"aa", b"y": b"r",
            b"r": {
                b"id": nid,
                b"values": [bytes([1, 2, 3, 4, 0x1A, 0xE1]), b"bad"],
                b"nodes": nid + bytes([5, 6, 7, 8, 0x1A, 0xE2]),
            },
        }
        peers, nodes = magnetmeta._parse_dht_response(magnetmeta._bencode(body))
        self.assertEqual(peers, [("1.2.3.4", 6881)])
        self.assertEqual(nodes, [(nid, ("5.6.7.8", 6882))])
        self.assertEqual(magnetmeta._parse_dht_response(b"garbage"), ([], []))
        self.assertEqual(
            magnetmeta._parse_dht_response(
                magnetmeta._bencode({b"y": b"q"})), ([], []))


class TestMetadataName(unittest.TestCase):
    def test_name_utf8_preferred(self):
        meta, ih = _make_metadata(b"fallback", {b"name.utf-8": "嫁金钗[全30集]".encode()})
        self.assertTrue(magnetmeta._verify_metadata(meta, ih))
        self.assertEqual(magnetmeta._name_from_metadata(meta), "嫁金钗[全30集]")

    def test_name_fallback(self):
        meta, _ih = _make_metadata(b"Where.the.Mask.Ends.S01")
        self.assertEqual(magnetmeta._name_from_metadata(meta),
                         "Where.the.Mask.Ends.S01")

    def test_verify_mismatch(self):
        meta, _ih = _make_metadata(b"x")
        self.assertFalse(magnetmeta._verify_metadata(meta, b"\x00" * 20))

    def test_bad_metadata(self):
        self.assertIsNone(magnetmeta._name_from_metadata(b"not-bencode"))
        self.assertIsNone(magnetmeta._name_from_metadata(
            magnetmeta._bencode({b"length": 1})))          # 无 name
        long_name = b"a" * 900
        meta, _ih = _make_metadata(long_name)
        self.assertEqual(len(magnetmeta._name_from_metadata(meta)),
                         magnetmeta._NAME_MAX)


class TestReadMsg(unittest.TestCase):
    def test_framing(self):
        a, b = socket.socketpair()
        try:
            payload = bytes([20, 3]) + b"data"
            b.sendall(struct.pack(">I", len(payload)) + payload
                      + struct.pack(">I", 0))             # keep-alive 尾随
            deadline = __import__("time").monotonic() + 5
            mid, body = magnetmeta._read_msg(a, deadline)
            self.assertEqual((mid, body), (20, bytes([3]) + b"data"))
            mid2, body2 = magnetmeta._read_msg(a, deadline)
            self.assertEqual((mid2, body2), (-1, b""))
        finally:
            a.close()
            b.close()

    def test_oversized_rejected(self):
        a, b = socket.socketpair()
        try:
            b.sendall(struct.pack(">I", magnetmeta._MSG_MAX + 1))
            deadline = __import__("time").monotonic() + 5
            with self.assertRaises(ValueError):
                magnetmeta._read_msg(a, deadline)
        finally:
            a.close()
            b.close()


class TestTrackerAnnounce(unittest.TestCase):
    def test_compact_peers(self):
        body = {b"interval": 1800,
                b"peers": bytes([1, 2, 3, 4, 0x1A, 0xE1])
                        + bytes([5, 6, 7, 8, 0x1A, 0xE2])}
        out = magnetmeta._parse_announce(magnetmeta._bencode(body))
        self.assertEqual(out, [("1.2.3.4", 6881), ("5.6.7.8", 6882)])

    def test_dict_peers(self):
        body = {b"peers": [{b"ip": b"10.0.0.9", b"port": 51413, b"peer id": b"x"}]}
        out = magnetmeta._parse_announce(magnetmeta._bencode(body))
        self.assertEqual(out, [("10.0.0.9", 51413)])

    def test_failure_and_garbage(self):
        self.assertEqual(
            magnetmeta._parse_announce(
                magnetmeta._bencode({b"failure reason": b"down"})), [])
        self.assertEqual(magnetmeta._parse_announce(b"not-bencode"), [])


class TestResolve(unittest.TestCase):
    def tearDown(self):
        magnetmeta._CACHE.clear()

    def test_resolve_happy_and_cache(self):
        meta, ih = _make_metadata("嫁金钗[全30集][国语配音+中文字幕]".encode())
        calls = {"dht": 0}

        def fake_dht(infohash, deadline):
            calls["dht"] += 1
            return [("1.2.3.4", 6881)]

        def fake_peer(ip, port, infohash, deadline):
            self.assertEqual(infohash, ih)
            return meta

        saved = (magnetmeta._dht_lookup, magnetmeta._fetch_metadata_from_peer,
                 magnetmeta._tracker_lookup)
        magnetmeta._dht_lookup = fake_dht
        magnetmeta._fetch_metadata_from_peer = fake_peer
        magnetmeta._tracker_lookup = lambda infohash, deadline, trackers=None: []
        try:
            name = magnetmeta.resolve_name(ih.hex(), timeout=5)
            self.assertEqual(name, "嫁金钗[全30集][国语配音+中文字幕]")
            # 缓存命中：不再走 DHT
            self.assertEqual(magnetmeta.resolve_name(ih.hex(), timeout=5), name)
            self.assertEqual(calls["dht"], 1)
        finally:
            (magnetmeta._dht_lookup, magnetmeta._fetch_metadata_from_peer,
             magnetmeta._tracker_lookup) = saved

    def test_resolve_no_peers(self):
        saved = magnetmeta._dht_lookup, magnetmeta._tracker_lookup
        magnetmeta._dht_lookup = lambda infohash, deadline: []
        magnetmeta._tracker_lookup = lambda infohash, deadline, trackers=None: []
        try:
            self.assertIsNone(magnetmeta.resolve_name("0" * 40, timeout=3))
        finally:
            magnetmeta._dht_lookup, magnetmeta._tracker_lookup = saved

    def test_single_flight(self):
        meta, ih = _make_metadata(b"once")
        calls = {"n": 0}
        entered = threading.Event()
        release = threading.Event()
        results: list = [None, None]

        def slow_resolve(digest, deadline, trackers=None):
            calls["n"] += 1
            entered.set()
            release.wait(5)
            return "once"

        orig = magnetmeta._resolve
        magnetmeta._resolve = slow_resolve
        try:
            t1 = threading.Thread(
                target=lambda: results.__setitem__(
                    0, magnetmeta.resolve_name(ih.hex(), timeout=10)))
            t2 = threading.Thread(
                target=lambda: results.__setitem__(
                    1, magnetmeta.resolve_name(ih.hex(), timeout=10)))
            t1.start()
            self.assertTrue(entered.wait(5))
            t2.start()
            release.set()
            t1.join(5)
            t2.join(5)
            self.assertEqual(results, ["once", "once"])
            self.assertEqual(calls["n"], 1)
        finally:
            magnetmeta._resolve = orig


class TestApplyCachedName(unittest.TestCase):
    def tearDown(self):
        magnetmeta._CACHE.clear()

    def test_bare_magnet_filled(self):
        ih = "80ae772175e56bcbf9e9a4e1689fa023aae68f9e"
        magnetmeta._CACHE[ih] = "嫁金钗[全30集]"
        link = links.LinkInfo(kind="magnet", url=f"magnet:?xt=urn:btih:{ih}",
                              display=f"magnet:{ih[:8]}…",
                              meta={"infohash": ih, "dn": ""})
        magnetmeta.apply_cached_name(link)
        self.assertEqual(link.display, "嫁金钗[全30集]")

    def test_with_dn_untouched(self):
        ih = "80ae772175e56bcbf9e9a4e1689fa023aae68f9e"
        magnetmeta._CACHE[ih] = "别的名字"
        link = links.LinkInfo(kind="magnet", url="magnet:?xt=urn:btih:" + ih,
                              display="自带名", meta={"infohash": ih, "dn": "自带名"})
        magnetmeta.apply_cached_name(link)
        self.assertEqual(link.display, "自带名")

    def test_no_cache_untouched(self):
        link = links.LinkInfo(kind="magnet", url="magnet:?xt=urn:btih:" + "0" * 40,
                              display="magnet:00000000…",
                              meta={"infohash": "0" * 40, "dn": ""})
        magnetmeta.apply_cached_name(link)
        self.assertEqual(link.display, "magnet:00000000…")

    def test_base32_meta_filled(self):
        import base64
        raw = bytes(range(20))
        b32 = base64.b32encode(raw).decode().rstrip("=")
        magnetmeta._CACHE[raw.hex()] = "base32 名"
        link = links.LinkInfo(kind="magnet", url=f"magnet:?xt=urn:btih:{b32}",
                              display="magnet:…", meta={"infohash": b32, "dn": ""})
        magnetmeta.apply_cached_name(link)
        self.assertEqual(link.display, "base32 名")


if __name__ == "__main__":
    unittest.main()
