#!/usr/bin/env python3
"""下载链接解析：magnet / thunder / http(s) / ftp / ed2k / .torrent。

- magnet  ：解析 xt=urn:btih 信息哈希（hex/base32）、dn、tr
- thunder ：base64 套壳（AA<真实链接>ZZ）解码后按内层类型再解析
- ed2k    ：解析出文件名/大小/哈希供展示；派发标注不支持
- .torrent：URL 或本地种子文件 → 解析 info 哈希 → 归一化为 magnet
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# 可直接派发给下载引擎的类型
DISPATCHABLE = {"magnet", "http", "https", "ftp", "torrent"}
# 解析得出结构但暂不派发
UNSUPPORTED = {"ed2k"}

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
BASE32 = re.compile(r"^[A-Z2-7]{32}$")
BTIH_URN = re.compile(r"^urn:btih:([0-9a-zA-Z]{32,40})$", re.I)


@dataclass
class LinkInfo:
    kind: str                 # magnet|http|https|ftp|ed2k|torrent|unknown
    url: str                  # 归一化后的链接（thundex 解码后为内层链接）
    display: str = ""         # 展示名（磁力 dn / ed2k 文件名 / URL 尾段）
    ok: bool = True           # 能否解析出结构
    dispatchable: bool = True # 能否派发给下载引擎
    reason: str = ""          # 不可用原因
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 解析不出结构的链接一律不可派发（防残缺/坏链接流到下载引擎）
        if not self.ok:
            self.dispatchable = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "url": self.url,
            "display": self.display,
            "ok": self.ok,
            "dispatchable": self.dispatchable,
            "reason": self.reason,
            "meta": self.meta,
        }


# ---------------------------------------------------------------- thunder

def decode_thunder(url: str) -> str | None:
    """thunder://<base64(AA<真实链接>ZZ)> → 真实链接；失败返回 None。"""
    payload = url[len("thunder://"):]
    payload = payload.strip().rstrip("/")
    # 补齐 base64 padding，兼容 urlsafe 变体
    payload += "=" * (-len(payload) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(payload).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            continue
        if raw.startswith("AA") and raw.endswith("ZZ"):
            return raw[2:-2]
        # 少数实现不带 AA/ZZ 包裹
        if raw.startswith(("http://", "https://", "ftp://", "magnet:", "ed2k:")):
            return raw
    return None


# ---------------------------------------------------------------- magnet

def parse_magnet(url: str) -> LinkInfo:
    info = LinkInfo(kind="magnet", url=url)
    query = url.split("?", 1)[1] if "?" in url else ""
    params = parse_qs(query, keep_blank_values=True)

    infohash = ""
    for xt in params.get("xt", []):
        m = BTIH_URN.match(xt.strip())
        if m:
            infohash = m.group(1)
            break
    if not infohash:
        info.ok = False
        info.dispatchable = False
        info.reason = "磁力链接缺少有效的 xt=urn:btih 信息哈希"
        return info

    if HEX40.match(infohash):
        algo, digest = "hex", infohash.lower()
    elif BASE32.match(infohash):
        algo, digest = "base32", infohash.upper()
    else:
        info.ok = False
        info.dispatchable = False
        info.reason = "信息哈希格式不正确（应为 40 位 hex 或 32 位 base32）"
        return info

    dn = unquote((params.get("dn") or [""])[0])
    trackers = [unquote(t) for t in params.get("tr", []) if t]
    info.display = dn or f"magnet:{digest[:8]}…"
    info.meta = {"infohash": digest, "infohash_algo": algo, "dn": dn, "trackers": trackers}
    return info


# ---------------------------------------------------------------- ed2k

def parse_ed2k(url: str) -> LinkInfo:
    """ed2k://|file|<name>|<size>|<md4>|/ —— 解析结构，暂不派发。"""
    info = LinkInfo(kind="ed2k", url=url, dispatchable=False)
    parts = url.split("|")
    if len(parts) < 5 or parts[1] != "file":
        info.ok = False
        info.reason = "ed2k 链接格式不正确"
        return info
    name = unquote(parts[2])
    try:
        size = int(parts[3])
    except ValueError:
        size = 0
    digest = parts[4].lower()
    info.display = name or url[:64]
    info.meta = {"name": name, "size": size, "hash": digest}
    info.reason = "ed2k 协议暂不支持派发（仅解析展示）"
    return info


# ---------------------------------------------------------------- 直链/种子

def parse_direct(url: str) -> LinkInfo:
    parsed = urlparse(url)
    kind = parsed.scheme.lower()
    name = unquote(parsed.path.rsplit("/", 1)[-1]) if parsed.path else ""
    info = LinkInfo(kind=kind, url=url, display=name or url[:64])
    if name.lower().endswith(".torrent"):
        info.kind = "torrent"
        info.meta = {"torrent_url": url, "name": name}
    return info


# ---------------------------------------------------------------- 种子文件

class _BencodeError(ValueError):
    pass


def _bdecode(data: bytes, pos: int = 0) -> tuple[object, int]:
    """最小 bencode 解码器，返回 (值, 结束位置)。"""
    if pos >= len(data):
        raise _BencodeError("unexpected end")
    ch = data[pos:pos + 1]
    if ch == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if ch == b"l":
        pos += 1
        out = []
        while data[pos:pos + 1] != b"e":
            val, pos = _bdecode(data, pos)
            out.append(val)
        return out, pos + 1
    if ch == b"d":
        pos += 1
        out = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            val, pos = _bdecode(data, pos)
            out[key] = val
        return out, pos + 1
    if ch.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise _BencodeError(f"bad token {ch!r} at {pos}")


def torrent_infohash(data: bytes) -> str | None:
    """计算 .torrent 的 info 哈希（SHA1 of 原始 bencoded info 字典）。"""
    try:
        top, _ = _bdecode(data, 0)
        if not isinstance(top, dict) or b"info" not in top:
            return None
        # 重新定位 info 值的原始字节区间
        pos = data.index(b"4:info") + len(b"4:info")
        _, end = _bdecode(data, pos)
        return hashlib.sha1(data[pos:end]).hexdigest()
    except (ValueError, _BencodeError):
        return None


def parse_torrent_file(path: str | Path) -> LinkInfo:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        return LinkInfo(kind="torrent", url=str(p), ok=False,
                        reason=f"无法读取种子文件：{exc}")
    digest = torrent_infohash(data)
    if not digest:
        return LinkInfo(kind="torrent", url=str(p), ok=False,
                        reason="种子文件损坏或缺少 info 字段")
    name = p.stem
    try:
        top, _ = _bdecode(data, 0)
        raw_name = top.get(b"info", {}).get(b"name")  # type: ignore[union-attr]
        if isinstance(raw_name, bytes):
            name = raw_name.decode("utf-8", errors="replace") or name
    except (ValueError, _BencodeError, AttributeError):
        pass
    magnet = magnet_from_infohash(digest, dn=name)
    return LinkInfo(kind="torrent", url=magnet, display=name,
                    meta={"infohash": digest, "source_file": str(p),
                          "resolved_magnet": magnet})


def magnet_from_infohash(infohash: str, dn: str = "", trackers: list[str] | None = None) -> str:
    parts = [f"magnet:?xt=urn:btih:{infohash}"]
    if dn:
        from urllib.parse import quote
        parts.append(f"dn={quote(dn)}")
    for tr in trackers or []:
        from urllib.parse import quote
        parts.append(f"tr={quote(tr, safe='')}")
    return "&".join(parts)


# ---------------------------------------------------------------- 身份标识

def identity(url: str) -> str:
    """链接身份标识，用于下载历史查重。

    - magnet → btih:<infohash>（base32 归一为 hex；不同 tracker/dn 的同资源命中同一条）
    - ed2k   → ed2k:<hash>
    - 其他   → url:<小写完整链接>（thunder 解码后按内层链接归一）
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    info = parse_line(raw)
    meta = info.meta or {}
    if meta.get("infohash"):
        digest = str(meta["infohash"]).lower()
        if meta.get("infohash_algo") == "base32":
            try:
                digest = base64.b32decode(digest.upper()).hex()
            except ValueError:  # binascii.Error 是 ValueError 子类
                pass
        return "btih:" + digest
    if meta.get("hash"):
        return "ed2k:" + str(meta["hash"]).lower()
    return "url:" + (info.url or raw).lower()


# ---------------------------------------------------------------- 入口

def parse_line(line: str) -> LinkInfo:
    url = line.strip()
    if not url:
        return LinkInfo(kind="unknown", url="", ok=False, reason="空链接")

    low = url.lower()
    if low.startswith("thunder://"):
        inner = decode_thunder(url)
        if not inner:
            return LinkInfo(kind="thunder", url=url, ok=False,
                            reason="thunder 链接解码失败（非标准 AA/ZZ 套壳）")
        inner_info = parse_line(inner)
        inner_info.meta["thunder_original"] = url
        return inner_info

    if low.startswith("magnet:"):
        return parse_magnet(url)
    if low.startswith("ed2k://"):
        return parse_ed2k(url)
    if low.startswith(("http://", "https://", "ftp://")):
        return parse_direct(url)
    return LinkInfo(kind="unknown", url=url, ok=False,
                    reason="无法识别的链接协议（支持 magnet/thunder/http/ftp/ed2k/.torrent）")


_SCHEME_RE = re.compile(
    r"(?:magnet:|thunder://|ed2k://|https?://|ftp://)", re.I)
# 新链接只在行首或分隔符后起段：协议前缀出现在 URL 参数值里
# （磁力 &tr=https://…、跳转 ?redirect=https://…）不是新链接，
# 无脑按协议前缀切会把 1 条磁力拆成「截残磁力 + tracker 直链」两条任务。
_SCHEME_SEPS = " \t\r\n,;，；、"


def split_links(text: str) -> list[str]:
    """批量文本切条：协议感知，防止误拆合法 URL。

    - 优先按行拆；行内协议前缀只有挤在分隔符后才算新链接（多条挤一行）；
      出现在查询参数值里的协议前缀（磁力 &tr=https://… 等）不切；
    - 整行不含协议前缀时才按空白/逗号/分号/顿号兜底拆；
    - 不再对逗号/顿号盲拆：它们是合法 URL 字符（查询参数、
      未编码中文文件名），盲拆会把链接截残。"""
    chunks: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        starts = [m.start() for m in _SCHEME_RE.finditer(line)
                  if m.start() == 0 or line[m.start() - 1] in _SCHEME_SEPS]
        if starts:
            for i, s in enumerate(starts):
                end = starts[i + 1] if i + 1 < len(starts) else len(line)
                # 段尾剥离挤在两条之间的分隔符（如 "https://a，ed2k://b"）
                seg = line[s:end].strip().rstrip(",;，；、").strip()
                if seg:
                    chunks.append(seg)
        else:
            for seg in re.split(r"[\s,;，；、]+", line):
                if seg.strip():
                    chunks.append(seg.strip())
    return chunks


def parse_text(text: str) -> list[LinkInfo]:
    """批量解析：协议感知切条后逐条解析（见 split_links）。"""
    return [parse_line(chunk) for chunk in split_links(text)]
