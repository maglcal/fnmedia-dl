#!/usr/bin/env python3
"""磁力名称解析：裸磁力（无 dn）联网拉取种子元数据取真实文件名。

magnet:?xt=urn:btih:<hash> 本身只带信息哈希，剧集/文件名存在 BT 网络的
种子元数据里。本模块用纯标准库实现最小元数据获取链路：

  1. DHT（BEP-5）get_peers 迭代查找，收集持有该种子的 peer 地址；
  2. TCP 连 peer：BT 握手（BEP-3）+ 扩展协议（BEP-10）+ ut_metadata（BEP-9）
     只拉 info 字典（16KiB 分片，总量上限 10MB）；
  3. SHA1(info) == infohash 校验通过后 bdecode 取 name / name.utf-8。

设计约束：
  - 一切失败（无 peer / 超时 / 网络受限 / 校验不符）返回 None，绝不抛进主流程；
  - 并发同哈希共享一次解析（单飞），结果进 FIFO 缓存供派发链路回填任务名；
  - 元数据经 SHA1 校验才采信——DHT/peer 均不可信，只信哈希。
"""
from __future__ import annotations

import base64
import hashlib
import os
import random
import socket
import struct
import threading
import time
import urllib.request
from collections import deque
from urllib.parse import quote_from_bytes

from links import _BencodeError, _bdecode

# ---------------------------------------------------------------- 常量

_META_PIECE = 16 * 1024            # ut_metadata 分片大小（BEP-9 固定 16KiB）
_META_MAX = 10 * 1024 * 1024       # 元数据总量上限（正常种子 info 远小于此）
_PIECE_MAX = 640                   # 分片数上限（与 _META_MAX 联动）
_MSG_MAX = 2 * 1024 * 1024         # 单条 peer 消息上限
_PEER_ID = b"-FN0100-" + os.urandom(12)
_EXT_ID = 3                        # 本端 ut_metadata 扩展消息 id（BEP-10 私有空间）
_NAME_MAX = 512                    # 名称长度上限（防异常元数据撑爆界面）
_BATCH_PEERS = 4                   # 同时尝试的 peer 数
_DHT_WANT = 12                     # 凑够多少 peer 就开拉元数据
_BOOTSTRAP = (
    ("router.bittorrent.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("dht.libtorrent.org", 25401),
)
# 公共 HTTP(S) tracker 兜底（DHT 不可达时的第二 peer 来源；
# tracker 与 DHT 同级：都只看到信息哈希，隐私面一致）
_TRACKERS = (
    "http://tracker.opentrackr.org:1337/announce",
    "http://t.acg.rip:6699/announce",
    "http://tracker.openbittorrent.com:80/announce",
)

_CACHE: dict[str, str] = {}
_CACHE_MAX = 256
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()


# ---------------------------------------------------------------- bencode 编码

def _bencode(obj: object) -> bytes:
    """最小 bencode 编码器（DHT 查询 / 扩展握手用）。key 排序按字节序。"""
    if isinstance(obj, bool):
        raise TypeError("bool 不能 bencode")
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        return _bencode(obj.encode("utf-8"))
    if isinstance(obj, (list, tuple)):
        return b"l" + b"".join(_bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items: list[tuple[bytes, object]] = []
        for k, v in obj.items():
            kb = k.encode("utf-8") if isinstance(k, str) else k
            if not isinstance(kb, bytes):
                raise TypeError("bencode 字典键必须是 bytes/str")
            items.append((kb, v))
        items.sort(key=lambda kv: kv[0])
        return b"d" + b"".join(_bencode(kb) + _bencode(v) for kb, v in items) + b"e"
    raise TypeError(f"无法 bencode：{type(obj)}")


# ---------------------------------------------------------------- 小工具

def _normalize_hash(value: str) -> str | None:
    """信息哈希归一为 40 位小写 hex；hex / base32 均接受，坏值返回 None。"""
    s = (value or "").strip().lower()
    if len(s) == 40 and all(c in "0123456789abcdef" for c in s):
        return s
    try:
        raw = base64.b32decode(s.upper() + "=" * (-len(s) % 8))
    except Exception:
        return None
    if len(raw) == 20:
        return raw.hex()
    return None


def _decode_compact_peer(v: object) -> tuple[str, int] | None:
    """紧凑 peer 地址（4 字节 IP + 2 字节端口）。"""
    if not isinstance(v, bytes) or len(v) != 6:
        return None
    port = struct.unpack(">H", v[4:6])[0]
    if port == 0:
        return None
    return socket.inet_ntoa(v[:4]), port


def _decode_compact_nodes(blob: bytes) -> list[tuple[bytes, tuple[str, int]]]:
    """紧凑节点串（20 字节 node id + 6 字节地址），坏段跳过。"""
    out: list[tuple[bytes, tuple[str, int]]] = []
    for i in range(0, len(blob) - 25, 26):
        addr = _decode_compact_peer(blob[i + 20:i + 26])
        if addr:
            out.append((blob[i:i + 20], addr))
    return out


def _verify_metadata(meta: bytes, infohash: bytes) -> bool:
    """元数据必须与信息哈希对得上才采信（DHT/peer 均不可信）。"""
    return bool(meta) and hashlib.sha1(meta).digest() == infohash


def _name_from_metadata(meta: bytes) -> str | None:
    """info 字典取名：name.utf-8 优先，退 name；空/坏值返回 None。"""
    try:
        info, _ = _bdecode(meta)
    except (_BencodeError, ValueError, RecursionError):
        return None
    if not isinstance(info, dict):
        return None
    raw = info.get(b"name.utf-8") or info.get(b"name")
    if not isinstance(raw, bytes):
        return None
    name = raw.decode("utf-8", errors="replace").replace("\x00", "").strip()
    return name[:_NAME_MAX] or None


# ---------------------------------------------------------------- 缓存 / 单飞

def _cache_put(digest: str, name: str) -> None:
    _CACHE[digest] = name
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))   # dict 保序，FIFO 淘汰


def cached_name(infohash: str) -> str | None:
    """查缓存真名（不做网络）。"""
    digest = _normalize_hash(infohash)
    return _CACHE.get(digest) if digest else None


def apply_cached_name(link) -> None:
    """无 dn 裸磁力若缓存已有真名，直接回填 display（派发任务名随真名）。

    静默失败：非磁力/无缓存一律不动。
    """
    try:
        meta = getattr(link, "meta", None) or {}
        if getattr(link, "kind", "") != "magnet" or meta.get("dn"):
            return
        digest = _normalize_hash(str(meta.get("infohash") or ""))
        name = _CACHE.get(digest) if digest else None
        if name:
            link.display = name
    except Exception:
        pass


def resolve_name(infohash: str, timeout: float = 15.0,
                 trackers: list[str] | None = None) -> str | None:
    """联网解析磁力真名。同哈希并发共享一次解析；结果进缓存。

    trackers 可选：磁力自带的 tr=（HTTP(S) 形态）会并入 peer 来源。
    超时/失败返回 None——调用方保留兜底显示即可。
    """
    digest = _normalize_hash(infohash)
    if not digest:
        return None
    hit = _CACHE.get(digest)
    if hit:
        return hit
    deadline = time.monotonic() + max(3.0, float(timeout))
    with _INFLIGHT_LOCK:
        ev = _INFLIGHT.get(digest)
        if ev is None:
            ev = threading.Event()
            _INFLIGHT[digest] = ev
            owner = True
        else:
            owner = False
    if not owner:
        ev.wait(max(0.0, deadline - time.monotonic()))
        return _CACHE.get(digest)
    try:
        name = _resolve(digest, deadline, trackers)
        if name:
            _cache_put(digest, name)
        return name
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(digest, None)
        ev.set()


def _resolve(digest: str, deadline: float,
             trackers: list[str] | None = None) -> str | None:
    try:
        infohash = bytes.fromhex(digest)
    except ValueError:
        return None
    peers = _dht_lookup(infohash, deadline)
    if time.monotonic() < deadline:
        got = _tracker_lookup(infohash, deadline, trackers)
        peers = list(dict.fromkeys(peers + got))
    if not peers:
        return None
    random.shuffle(peers)
    meta = _fetch_metadata(infohash, peers[:12], deadline)
    if meta is None:
        return None
    return _name_from_metadata(meta)


# ---------------------------------------------------------------- DHT（BEP-5）

def _parse_dht_response(data: bytes) -> tuple[list[tuple[str, int]],
                                              list[tuple[bytes, tuple[str, int]]]]:
    """get_peers 回复 → (peer 地址, 邻近节点)；坏包返回空。"""
    try:
        msg, _ = _bdecode(data)
    except (_BencodeError, ValueError, RecursionError):
        return [], []
    if not isinstance(msg, dict) or msg.get(b"y") != b"r":
        return [], []
    r = msg.get(b"r")
    if not isinstance(r, dict):
        return [], []
    peers: list[tuple[str, int]] = []
    values = r.get(b"values")
    if isinstance(values, list):
        for v in values[:64]:
            addr = _decode_compact_peer(v)
            if addr:
                peers.append(addr)
    nodes: list[tuple[bytes, tuple[str, int]]] = []
    blob = r.get(b"nodes")
    if isinstance(blob, bytes):
        nodes = _decode_compact_nodes(blob)
    return peers, nodes


def _dht_lookup(infohash: bytes, deadline: float) -> list[tuple[str, int]]:
    """get_peers 迭代查找：自举节点起，沿回复里的邻近节点收敛，凑 peer 就停。"""
    peers: dict[tuple[str, int], None] = {}      # 保序去重
    candidates: deque[tuple[str, int]] = deque()
    queried: set[tuple[str, int]] = set()
    my_id = os.urandom(20)

    for host, port in _BOOTSTRAP:
        try:
            for fam, _typ, _proto, _canon, addr in socket.getaddrinfo(host, port):
                if fam == socket.AF_INET:
                    candidates.append((addr[0], addr[1]))
        except OSError:
            continue

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        while time.monotonic() < deadline and len(peers) < _DHT_WANT:
            sent = 0
            while candidates and sent < 8:
                addr = candidates.popleft()
                if addr in queried:
                    continue
                queried.add(addr)
                t = os.urandom(2)
                msg = _bencode({
                    b"t": t, b"y": b"q", b"q": b"get_peers",
                    b"a": {b"id": my_id, b"infohash": infohash},
                })
                try:
                    sock.sendto(msg, addr)
                    sent += 1
                except OSError:
                    continue
            if sent == 0 and not candidates:
                break
            window_end = min(deadline, time.monotonic() + 1.0)
            while time.monotonic() < window_end:
                try:
                    sock.settimeout(max(0.05, window_end - time.monotonic()))
                    data, _src = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    break
                got, nodes = _parse_dht_response(data)
                for a in got:
                    peers.setdefault(a)
                    if len(peers) >= _DHT_WANT:
                        break
                if len(peers) >= _DHT_WANT:
                    break
                for _nid, a in nodes:
                    if a not in queried:
                        candidates.append(a)
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return list(peers)[:_DHT_WANT]


# ---------------------------------------------------------------- tracker（announce）

def _parse_announce(data: bytes) -> list[tuple[str, int]]:
    """tracker announce 回复 → peer 地址。兼容紧凑 peers 与字典列表两种形态。"""
    try:
        d, _ = _bdecode(data)
    except (_BencodeError, ValueError, RecursionError):
        return []
    if not isinstance(d, dict):
        return []
    out: list[tuple[str, int]] = []
    raw = d.get(b"peers")
    if isinstance(raw, bytes):
        for i in range(0, len(raw) - 5, 6):
            addr = _decode_compact_peer(raw[i:i + 6])
            if addr:
                out.append(addr)
    elif isinstance(raw, list):
        for p in raw[:64]:
            if isinstance(p, dict):
                ip, port = p.get(b"ip"), p.get(b"port")
                if isinstance(ip, bytes) and isinstance(port, int) and 0 < port < 65536:
                    out.append((ip.decode("ascii", "replace"), port))
    return out[:_DHT_WANT]


def _tracker_lookup(infohash: bytes, deadline: float,
                    trackers: list[str] | None = None) -> list[tuple[str, int]]:
    """HTTP(S) tracker announce 收 peer（DHT 不可达时的兜底来源）。
    自带 tr= 优先，公共 tracker 兜底；单个失败不中断，全部失败返回空。"""
    urls: list[str] = []
    for t in (trackers or []) + list(_TRACKERS):
        t = (t or "").strip()
        if t.startswith(("http://", "https://")) and t not in urls:
            urls.append(t)
    peers: dict[tuple[str, int], None] = {}
    # 强制直连：不走环境代理（与 stats 上报同一铁律）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in urls[:6]:
        if time.monotonic() >= deadline or len(peers) >= _DHT_WANT:
            break
        qs = ("?info_hash=" + quote_from_bytes(infohash)
              + "&peer_id=" + quote_from_bytes(_PEER_ID)
              + "&port=6881&uploaded=0&downloaded=0&left=0&compact=1&numwant=50")
        try:
            req = urllib.request.Request(
                url + qs, headers={"User-Agent": "fnmedia-dl/1.0"})
            timeout = max(1.0, min(5.0, deadline - time.monotonic()))
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read(256 * 1024)
        except Exception:
            continue
        for a in _parse_announce(data):
            peers.setdefault(a)
    return list(peers)[:_DHT_WANT]


# ---------------------------------------------------------------- peer 元数据（BEP-9/10）

def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_msg(sock: socket.socket, deadline: float) -> tuple[int | None, bytes]:
    """读一条 BT 消息：(消息 id, 载荷)。keep-alive → (-1, b"")，EOF → (None, b"")。"""
    sock.settimeout(max(0.1, deadline - time.monotonic()))
    head = _recv_exact(sock, 4)
    if head is None:
        return None, b""
    (length,) = struct.unpack(">I", head)
    if length == 0:
        return -1, b""
    if length > _MSG_MAX:
        raise ValueError("peer 消息过大")
    body = _recv_exact(sock, length)
    if body is None:
        return None, b""
    return body[0], body[1:]


def _fetch_metadata_from_peer(ip: str, port: int, infohash: bytes,
                              deadline: float) -> bytes | None:
    """单 peer 拉元数据：握手 → 扩展握手取 metadata_size/对方 ut_metadata id →
    分片请求 → 组装 → SHA1 校验。任何异常收敛为 None。"""
    if deadline - time.monotonic() < 2.0:
        return None
    sock = socket.create_connection((ip, port), timeout=min(5.0, deadline - time.monotonic()))
    try:
        reserved = bytearray(8)
        reserved[5] = 0x10                      # 扩展协议位（BEP-10）
        sock.sendall(bytes([19]) + b"BitTorrent protocol" + bytes(reserved)
                     + infohash + _PEER_ID)
        hs = _recv_exact(sock, 68)
        if hs is None or hs[1:20] != b"BitTorrent protocol":
            return None
        if hs[20:28][5] & 0x10 == 0:            # 对方不支持扩展协议
            return None

        payload = bytes([20, 0]) + _bencode({b"m": {b"ut_metadata": _EXT_ID},
                                             b"v": b"fnmedia-dl"})
        sock.sendall(struct.pack(">I", len(payload)) + payload)

        peer_meta_id: int | None = None
        metadata_size: int | None = None
        end = min(deadline, time.monotonic() + 8.0)
        while time.monotonic() < end:
            msg_id, body = _read_msg(sock, end)
            if msg_id is None:
                return None
            if msg_id != 20 or not body or body[0] != 0:
                continue
            try:
                d, _pos = _bdecode(body[1:])
            except (_BencodeError, ValueError, RecursionError):
                continue
            if not isinstance(d, dict):
                continue
            m = d.get(b"m")
            if isinstance(m, dict):
                pid = m.get(b"ut_metadata")
                if isinstance(pid, int) and 0 < pid < 256:
                    peer_meta_id = pid
            sz = d.get(b"metadata_size")
            if isinstance(sz, int) and sz > 0:
                metadata_size = sz
            if peer_meta_id is not None and metadata_size is not None:
                break
        if peer_meta_id is None or metadata_size is None:
            return None
        if not (0 < metadata_size <= _META_MAX):
            return None
        n_pieces = (metadata_size + _META_PIECE - 1) // _META_PIECE
        if n_pieces > _PIECE_MAX:
            return None

        for i in range(n_pieces):
            req = bytes([20, peer_meta_id]) + _bencode({b"msg_type": 0, b"piece": i})
            sock.sendall(struct.pack(">I", len(req)) + req)

        pieces: dict[int, bytes] = {}
        end = min(deadline, time.monotonic() + 10.0)
        while len(pieces) < n_pieces and time.monotonic() < end:
            msg_id, body = _read_msg(sock, end)
            if msg_id is None:
                break
            if msg_id != 20 or not body or body[0] != _EXT_ID:
                continue
            try:
                hdr, pos = _bdecode(body[1:])
            except (_BencodeError, ValueError, RecursionError):
                continue
            if not isinstance(hdr, dict) or hdr.get(b"msg_type") != 1:
                continue
            idx = hdr.get(b"piece")
            chunk = body[1 + pos:]
            if (isinstance(idx, int) and 0 <= idx < n_pieces
                    and idx not in pieces and len(chunk) <= _META_PIECE):
                pieces[idx] = chunk
        if len(pieces) != n_pieces:
            return None
        meta = b"".join(pieces[i] for i in range(n_pieces))[:metadata_size]
        return meta if _verify_metadata(meta, infohash) else None
    except (OSError, ValueError, _BencodeError, RecursionError, struct.error):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _fetch_metadata(infohash: bytes, peers: list[tuple[str, int]],
                    deadline: float) -> bytes | None:
    """小并发逐批试 peer，拉到即返回（SHA1 校验在单 peer 内完成）。"""
    queue = list(peers)
    while queue and time.monotonic() < deadline:
        batch, queue = queue[:_BATCH_PEERS], queue[_BATCH_PEERS:]
        results: list[bytes | None] = [None] * len(batch)

        def worker(i: int, addr: tuple[str, int]) -> None:
            try:
                results[i] = _fetch_metadata_from_peer(
                    addr[0], addr[1], infohash, deadline)
            except Exception:
                results[i] = None

        threads = []
        for i, addr in enumerate(batch):
            t = threading.Thread(target=worker, args=(i, addr), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(max(0.1, deadline - time.monotonic()))
        for r in results:
            if r:
                return r
    return None
