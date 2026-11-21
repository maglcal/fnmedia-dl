#!/usr/bin/env python3
"""RSS 订阅（P2 自动化）：订阅 CRUD、条目拉取解析、过滤去重、手动/自动派发。

数据文件 state/rss.json（文件锁短临界区同 taskqueue 口径）：
  {"subs":  [{"id","name","url","enabled","interval_min","auto_download",
              "directory","filter","episode","ep_seen","etag",
              "last_modified","last_check","last_check_ts","last_error",
              "fail_count","stale_days","last_new_ts","stale_notified_day"}],
   "items": {sub_id: [{"id","guid","identity","title","url","size",
                       "state","retry_count","first_seen","last_result"}]},
   "seen":  {fingerprint: ts}}        # 条目去重指纹，30 天滚动清理

条目解析（BEP 36 优先级链，Torrent RSS feeds 事实标准）：
  ① enclosure type="application/x-bittorrent"（MUST，优先于一切标签）
  ② <torrent><infoHash> → magnet_from_infohash 直接组磁力（免拉种子）
  ③ <link> 直接放 magnet:
  ④ media:content url（磁力 / .torrent）
  ⑤ description 正则兜底提取磁力 / .torrent 直链
  普通 http(s) <link> 是条目详情页而非下载地址，**不入库**（防误派发）；
  非种子条目（播客 enclosure 等）P1 不入库。RSS 2.0 优先，Atom 容错
  （entry / link href / id）。

去重三层：①seen 指纹缓存（guid/identity 归一，30 天滚动清理）→
  ②links.identity + 下载历史 history_check → ③派发幂等（dispatched 不重发）。
  剧集去重（P3，ani-rss 范式）：episode.enabled 开启后按（订阅,季,集）
  distinct——同集只收一条（ep_seen 落订阅数据持久化，先到先得）；集数由
  自定义正则（regex+group 捕获组+offset 偏移）或内置模式链（SxxExx→
  第N集/话/回→EPxx→Exx→裸集数）解析，解析不出不拦（宁漏勿错杀）；
  过滤排除的条目不占集名额，手动派发仍可越过（显式用户意图）。
过滤（qBittorrent 三轴）：must_not 一票否决 → must 任一命中（空 = 全过）
  → 大小阈值（feed 提供大小时才生效）；regex 模式限长 512 字符（ReDoS 自担）。

P2 自动化：tick() 定时轮询（main.py rss-ticker 每 60s 驱动）——到点订阅
  检查 + auto_download 自动派发（matched 与未超限 failed 条目 FIFO 派发，
  单订阅每轮至多 MAX_AUTO_DISPATCH 条前置节流；失败 retry_count++，超
  MAX_DISPATCH_RETRY 次搁置待手动，不永久卡死）；订阅间 TICK_SUB_GAP 间隔
  防对端限流。「立即检查」永远可用——fail_count ≥ MAX_FAILS 只搁置定时
  轮询，不拦手动检查（手动检查即复位搁置，轮询恢复资格）。

条目 state 取值：new（入库瞬态）→ matched（过滤命中，待派发）/
  skipped（过滤排除 / 历史已有同资源）/ dispatched（已派发）/ failed
  （派发失败，retry_count 记自动重试次数）。

源停滞提醒（P3 轻件）：订阅可设 stale_days（N 天无新条目提醒，0=关闭）。
  last_new_ts 记最后一次发现新条目的时间（add 时初始化为当下防新建即报），
  每次成功检查（含 not_modified）评估：超阈值且当天未提醒过则写
  stale_notified_day（一天至多一次）、返回 stale_notice 文案并可经 notify_fn
  回调外发；新条目到达即刷新 last_new_ts、清提醒标记（再次停滞可再提醒）。
  提醒只反映「源是否还在更新」——过滤排除的条目仍是新发布，算源活跃。

注意：文件名不能叫 queue.py——app 目录在 sys.path 首位，会遮蔽标准库
queue 模块把 threading/anyio 等依赖带崩。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

import config as cfgmod
import filelock
import links as linksmod

_FILE_LOCK = threading.Lock()
_CHECK_GUARD = threading.Lock()      # 检查单飞：手动检查与定时轮询不并发

DEFAULT_FEED_TIMEOUT = 15.0          # 单次拉取超时（秒）
MAX_FEED_BYTES = 2 * 1024 * 1024     # feed 体积极限 2MB（防恶意大包）
MAX_FEED_ITEMS = 500                 # 单次解析条目极顶
MAX_ITEMS_PER_SUB = 500              # 每订阅条目留存量（旧条目滚动丢弃）
MAX_FAILS = 5                        # 连续失败次数上限（定时轮询搁置阈值）
SEEN_TTL = 30 * 86400                # seen 指纹缓存滚动窗口（秒）
REGEX_MAX_LEN = 512                  # 正则规则长度上限（ReDoS 防护）
MIN_INTERVAL_MIN = 10                # 轮询间隔下限（分钟）
TICK_INTERVAL_SEC = 60               # rss-ticker 醒转间隔（秒）
TICK_SUB_GAP = 0.5                   # 订阅间请求间隔（秒，防对端限流）
MAX_AUTO_DISPATCH = 3                # 单订阅每轮自动派发上限（前置节流）
MAX_DISPATCH_RETRY = 3               # 自动派发失败重试上限（超限搁置待手动）
MAX_EP_SEEN = 2000                   # 每订阅已收集数键上限（按时间淘汰旧键）
MAX_STALE_DAYS = 3650                # 停滞阈值上限（天，约 10 年；0=关闭）

_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{32,40}[^\s\"'<>]*")
_TORRENT_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.torrent", re.I)
_SEASON_RE = re.compile(r"[Ss](\d{1,2})\b|第\s*(\d{1,3})\s*[季部]")
# 集数默认模式链（SxxExx → 第N集 → EPxx → Exx → 裸集数）：宁保守勿错杀
# ——错杀同集不同资源会丢下载，漏判只是不去重。裸集数（ - 05）是动画
# 发布命名主流，但多剧混合源可能撞集，靠 episode.enabled 显式开关自担。
_EP_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[Ee][Pp]?(\d{1,3})"),
    re.compile(r"第\s*(\d{1,3})\s*[集话話回]"),
    re.compile(r"\b[Ee][Pp](\d{1,3})\b"),
    re.compile(r"\b[Ee](\d{2,3})\b"),
    re.compile(r" - (\d{2,3})\b"),
)


# ---------------------------------------------------------------- 过滤

def _norm_interval(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("轮询间隔需为整数（分钟）")
    if n < MIN_INTERVAL_MIN:
        raise ValueError(f"轮询间隔最短 {MIN_INTERVAL_MIN} 分钟")
    return n


def _norm_stale_days(v) -> int:
    """停滞提醒阈值归一：0=关闭，正整数上限 MAX_STALE_DAYS。"""
    try:
        n = int(v if v is not None and str(v).strip() != "" else 0)
    except (TypeError, ValueError):
        raise ValueError("停滞提醒天数需为整数（0=关闭）")
    if n < 0:
        raise ValueError("停滞提醒天数不能为负")
    return min(n, MAX_STALE_DAYS)


def _opt_float(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("大小阈值需为数字（MB）")
    if f < 0:
        raise ValueError("大小阈值不能为负")
    return f


def _norm_filter(flt: dict | None) -> dict:
    """过滤规则归一 + 校验（regex 模式逐条编译 + 限长）。"""
    flt = flt or {}
    must = [str(x).strip() for x in (flt.get("must") or []) if str(x).strip()]
    must_not = [str(x).strip() for x in (flt.get("must_not") or []) if str(x).strip()]
    regex = bool(flt.get("regex"))
    if regex:
        for pat in must + must_not:
            if len(pat) > REGEX_MAX_LEN:
                raise ValueError(f"正则规则过长（上限 {REGEX_MAX_LEN} 字符）")
            try:
                re.compile(pat)
            except re.error as exc:
                raise ValueError(f"正则规则无效（{pat}）：{exc}")
    return {
        "must": must, "must_not": must_not, "regex": regex,
        "size_min_mb": _opt_float(flt.get("size_min_mb")),
        "size_max_mb": _opt_float(flt.get("size_max_mb")),
    }


def _filter_entry(title: str, size: int, flt: dict) -> tuple[bool, str]:
    """三轴过滤：must_not 一票否决 → must 任一命中（空 = 全过）→ 大小阈值。"""
    title = title or ""
    must = flt.get("must") or []
    must_not = flt.get("must_not") or []
    if flt.get("regex"):
        for pat in must_not:
            if re.search(pat, title):
                return False, f"命中排除规则：{pat}"
        if must and not any(re.search(p, title) for p in must):
            return False, "未命中任何包含规则"
    else:
        low = title.lower()
        for kw in must_not:
            if kw.lower() in low:
                return False, f"命中排除关键词：{kw}"
        if must and not any(k.lower() in low for k in must):
            return False, "未命中任何包含关键词"
    if size > 0:
        mb = size / (1024 * 1024)
        smin, smax = flt.get("size_min_mb"), flt.get("size_max_mb")
        if smin is not None and mb < smin:
            return False, f"体积小于 {smin}MB"
        if smax is not None and mb > smax:
            return False, f"体积大于 {smax}MB"
    return True, ""


# ---------------------------------------------------------------- 剧集（P3）

def _season_of(title: str) -> int:
    """季号独立探测：Sxx 或 第N季/部；取不到按 0。"""
    m = _SEASON_RE.search(title)
    if not m:
        return 0
    return int(m.group(1) or m.group(2) or 0)


def parse_episode(title: str, pattern: str = "", group: int = 1,
                  offset: int = 0) -> tuple[int, int] | None:
    """标题 → (季, 集)；解析不出返回 None（剧集去重不拦，宁漏勿错杀）。

    自定义正则优先（ani-rss 范式：pattern + group 捕获组 + offset 偏移
    修正）；未配正则走默认模式链 _EP_PATTERNS。季号 _season_of 独立探测。"""
    title = title or ""
    if pattern:
        try:
            m = re.search(pattern, title)
        except re.error:
            return None                 # 运行期不抛：配置期 _norm_episode 已拦
        if not m:
            return None
        try:
            ep = int(m.group(group)) + int(offset)
        except (IndexError, ValueError):
            return None
        return (_season_of(title), ep) if ep > 0 else None  # ep<=0 不产键（同默认链）
    for i, rx in enumerate(_EP_PATTERNS):
        m = rx.search(title)
        if not m:
            continue
        if i == 0:
            season, ep = int(m.group(1)), int(m.group(2))
        else:
            season, ep = _season_of(title), int(m.group(1))
        if ep > 0:
            return season, ep
    return None


def _norm_episode(ep: dict | None) -> dict:
    """剧集去重配置归一 + 校验（正则编译 + 捕获组存在性）。"""
    ep = ep or {}
    regex = str(ep.get("regex") or "").strip()
    if len(regex) > REGEX_MAX_LEN:
        raise ValueError(f"集数正则过长（上限 {REGEX_MAX_LEN} 字符）")
    try:
        # 显式 0 组号必须拒绝（`or` 会把 0 吞成默认 1）；空/缺省才落默认
        group = 1 if ep.get("group") in (None, "") else int(ep.get("group"))
        offset = 0 if ep.get("offset") in (None, "") else int(ep.get("offset"))
    except (TypeError, ValueError):
        raise ValueError("集数捕获组/偏移需为整数")
    if group < 1:
        raise ValueError("集数捕获组从 1 开始")
    if regex:
        try:
            compiled = re.compile(regex)
        except re.error as exc:
            raise ValueError(f"集数正则无效：{exc}")
        if compiled.groups < group:
            raise ValueError(
                f"集数正则只有 {compiled.groups} 个捕获组，取不到第 {group} 组")
    return {"enabled": bool(ep.get("enabled")), "regex": regex,
            "group": group, "offset": offset}


# ---------------------------------------------------------------- 解析

def _local(tag: str) -> str:
    """XML 标签本地名（剥命名空间），小写归一（infoHash → infohash）。"""
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _child_text(elem, name: str) -> str:
    for ch in elem:
        if _local(ch.tag) == name and ch.text:
            return ch.text.strip()
    return ""


def _parse_entry(elem) -> dict | None:
    """单条目 → {guid,title,url,size}；提不出种子/磁力链接返回 None。

    BEP 36 优先级链：enclosure(application/x-bittorrent) → torrent:infoHash
    → link 磁力 → media:content → description 正则兜底。"""
    title = _child_text(elem, "title")
    guid = _child_text(elem, "guid") or _child_text(elem, "id")
    link = ""            # RSS <link> 文本 / Atom <link href>
    desc = ""
    enc_url, enc_len, enc_is_bt = "", 0, False
    infohash, hash_len = "", 0
    mc_url, mc_size = "", 0

    for ch in elem:
        name = _local(ch.tag)
        if name == "link":
            link = (ch.get("href") or ch.text or "").strip() or link
        elif name == "enclosure":
            u = (ch.get("url") or "").strip()
            t = (ch.get("type") or "").lower()
            if u and (t == "application/x-bittorrent"
                      or u.lower().endswith(".torrent")):
                enc_url, enc_is_bt = u, True
                try:
                    enc_len = int(ch.get("length") or 0)
                except ValueError:
                    enc_len = 0
        elif name in ("description", "summary", "encoded"):
            desc += "\n" + (ch.text or "")
        # 扩展标签（EZTV/nyaa 风格）在任意深度，用 iter 兜底
    for ch in elem.iter():
        name = _local(ch.tag)
        if name == "infohash" and ch.text and ch.text.strip():
            infohash = infohash or ch.text.strip()
        elif name == "contentlength" and ch.text:
            try:
                hash_len = hash_len or int(ch.text.strip())
            except ValueError:
                pass
        elif name == "content" and (ch.get("url") or "").strip():
            # media:content（Atom <content> 无 url 属性，不会误命中）
            u = (ch.get("url") or "").strip()
            if u.lower().startswith("magnet:") or u.lower().endswith(".torrent"):
                mc_url = mc_url or u
                try:
                    mc_size = mc_size or int(ch.get("fileSize") or 0)
                except ValueError:
                    pass

    url, size = "", 0
    if enc_url:
        url, size = enc_url, enc_len
    elif infohash and (linksmod.HEX40.match(infohash)
                       or linksmod.BASE32.match(infohash.upper())):
        url = linksmod.magnet_from_infohash(infohash, dn=title)
        size = hash_len
    elif link.lower().startswith("magnet:"):
        url = link
    elif mc_url:
        url, size = mc_url, mc_size
    else:
        m = _MAGNET_RE.search(desc) or _TORRENT_URL_RE.search(desc)
        if m:
            url = m.group(0)
    if not url:
        return None                 # 提不出种子/磁力条目（详情页/播客等）不入库
    return {
        "guid": guid or linksmod.identity(url),
        "title": title or url[:80],
        "url": url,
        "size": size,
    }


def _parse_feed(data: bytes) -> list[dict]:
    """feed 字节 → 条目列表（上限 MAX_FEED_ITEMS）；XML 损坏抛 ValueError。

    安全：拒绝 ENTITY 声明（billion-laughs 实体炸弹的必要条件）；
    条目按文档顺序取前 N 条。"""
    if b"<!ENTITY" in data[:4096]:
        raise ValueError("订阅源包含实体声明，已拒绝解析（安全防护）")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"订阅源 XML 解析失败：{exc}")
    out: list[dict] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        entry = _parse_entry(el)
        if entry:
            out.append(entry)
        if len(out) >= MAX_FEED_ITEMS:
            break
    return out


# ---------------------------------------------------------------- 存储

def _stale_eval(sub: dict, now: int, has_new: bool) -> str:
    """评估源停滞并回写 sub 状态；返回本次提醒文案（空=不提醒）。

    调用方须持文件锁并在之后落盘。has_new=本次检查是否发现新条目
    （added 非空）——新条目刷新 last_new_ts 并清提醒标记（再次停滞可再报）。
    """
    try:
        days = _norm_stale_days(sub.get("stale_days"))
    except ValueError:
        days = 0                 # 历史脏数据退化成关闭，不打断检查
    if days <= 0:
        return ""
    if has_new:
        sub["last_new_ts"] = now
        sub["stale_notified_day"] = ""
        return ""
    last_new = int(sub.get("last_new_ts") or 0)
    if last_new <= 0:
        sub["last_new_ts"] = now   # 老订阅迁移：缺字段从本次检查起算，不误报
        return ""
    idle = now - last_new
    if idle < days * 86400:
        return ""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if sub.get("stale_notified_day") == today:
        return ""                # 一天至多提醒一次
    sub["stale_notified_day"] = today
    name = sub.get("name") or sub.get("url") or sub.get("id") or ""
    return (f"订阅「{name}」源停滞：已 {idle // 86400} 天无新条目"
            f"（阈值 {days} 天）")


def _notify_stale(notify_fn, message: str, sub: dict) -> None:
    """锁外发提醒；通知失败不影响订阅检查结果。"""
    if not notify_fn or not message:
        return
    try:
        notify_fn(message, dict(sub))
    except Exception:  # noqa: BLE001 - 外部通知不可靠，失败静默
        pass


def _fingerprint(guid: str, identity: str) -> str:
    """条目去重指纹：资源级身份（磁力/ed2k 按哈希归一）优先，guid 兜底。"""
    if identity.startswith(("btih:", "ed2k:")):
        return identity
    return guid or identity


class RssFeed:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path(cfgmod.STATE_DIR) / "rss.json"

    # ------------------------------------------------------------ 内部

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data["subs"] = [s for s in (data.get("subs") or []) if isinstance(s, dict)]
        data["items"] = {str(k): [i for i in (v or []) if isinstance(i, dict)]
                         for k, v in (data.get("items") or {}).items()}
        data["seen"] = {str(k): int(v) for k, v in (data.get("seen") or {}).items()}
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.chmod(0o600)   # 订阅 URL 可能内嵌凭据，与 tasks.json 同级收紧
        tmp.replace(self.path)

    @staticmethod
    def _find(data: dict, sub_id: str) -> dict:
        for sub in data["subs"]:
            if sub.get("id") == sub_id:
                return sub
        raise KeyError(f"订阅不存在：{sub_id}")

    @staticmethod
    def _find_item(data: dict, item_id: str) -> tuple[dict, dict | None]:
        for sub_id, lst in (data.get("items") or {}).items():
            for it in lst:
                if it.get("id") == item_id:
                    sub = next((s for s in data.get("subs", [])
                                if s.get("id") == sub_id), None)
                    return it, sub
        raise KeyError(f"条目不存在：{item_id}")

    @staticmethod
    def _prune_seen(data: dict, now: int) -> None:
        """seen 指纹 30 天滚动清理（旧指纹自然过期，条目清理是 P3）。"""
        cutoff = now - SEEN_TTL
        data["seen"] = {k: ts for k, ts in data["seen"].items() if ts >= cutoff}

    def _touch(self, sub_id: str, *, ok: bool, error: str = "",
               stale_has_new: bool | None = None) -> str:
        """检查结果回写订阅状态（成功清零 fail_count，失败累计并留错因）。

        stale_has_new=None 不评估停滞（失败路径）；True/False 评估并返回
        本次提醒文案（由调用方在锁外发给 notify_fn）。
        """
        notice = ""
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            sub = self._find(data, sub_id)
            now = int(time.time())
            sub["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
            sub["last_check_ts"] = now
            if ok:
                sub["fail_count"] = 0
                sub["last_error"] = ""
            else:
                sub["fail_count"] = int(sub.get("fail_count") or 0) + 1
                sub["last_error"] = error[:200]
            if stale_has_new is not None:
                notice = _stale_eval(sub, now, stale_has_new)
            self._save(data)
        return notice

    # ------------------------------------------------------------ CRUD

    def add(self, url: str, name: str = "", *, directory: str = "",
            interval_min: int = 30, auto_download: bool = False,
            flt: dict | None = None, episode: dict | None = None,
            stale_days: int = 0) -> dict:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("订阅地址需为 http(s) 链接")
        sub = {
            "id": uuid.uuid4().hex[:10],
            "name": (name or "").strip() or url.split("//", 1)[-1][:40],
            "url": url,
            "enabled": True,
            "interval_min": _norm_interval(interval_min),
            "auto_download": bool(auto_download),
            "directory": (directory or "").strip(),
            "filter": _norm_filter(flt),
            "episode": _norm_episode(episode),
            "ep_seen": {},
            "etag": "", "last_modified": "",
            "last_check": "", "last_check_ts": 0,
            "last_error": "", "fail_count": 0,
            "stale_days": _norm_stale_days(stale_days),
            "last_new_ts": int(time.time()),
            "stale_notified_day": "",
        }
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            data["subs"].append(sub)
            self._save(data)
        return dict(sub)

    def update(self, sub_id: str, patch: dict) -> dict:
        """局部更新订阅（只动 patch 里出现的键）。"""
        patch = patch or {}
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            sub = self._find(data, sub_id)
            if "name" in patch:
                sub["name"] = str(patch.get("name") or "").strip()
            if "url" in patch:
                url = str(patch.get("url") or "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    raise ValueError("订阅地址需为 http(s) 链接")
                if url != sub.get("url"):
                    sub["url"] = url
                    # 换源必清条件头：拿旧 etag 打新源会收到 304 假「无更新」
                    sub["etag"] = ""
                    sub["last_modified"] = ""
            if "enabled" in patch:
                sub["enabled"] = bool(patch.get("enabled"))
            if "auto_download" in patch:
                sub["auto_download"] = bool(patch.get("auto_download"))
            if "interval_min" in patch:
                sub["interval_min"] = _norm_interval(patch.get("interval_min"))
            if "directory" in patch:
                sub["directory"] = str(patch.get("directory") or "").strip()
            if "filter" in patch:
                sub["filter"] = _norm_filter(patch.get("filter"))
            if "episode" in patch:
                sub["episode"] = _norm_episode(patch.get("episode"))
            if patch.get("ep_reset"):
                sub["ep_seen"] = {}      # 换正则/重收全集时显式清空已收集数
            if "stale_days" in patch:
                sub["stale_days"] = _norm_stale_days(patch.get("stale_days"))
            self._save(data)
            return dict(sub)

    def remove(self, sub_id: str) -> bool:
        """移除订阅与其条目（seen 指纹保留——跨订阅去重仍有效）。"""
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            before = len(data["subs"])
            data["subs"] = [s for s in data["subs"] if s.get("id") != sub_id]
            data["items"].pop(sub_id, None)
            self._save(data)
            return len(data["subs"]) != before

    def snapshot(self) -> dict:
        with _FILE_LOCK:
            data = self._load()
        subs = []
        now = int(time.time())
        for s in data["subs"]:
            d = dict(s)
            lst = data["items"].get(s.get("id"), [])
            d["items_count"] = len(lst)
            d["matched_count"] = sum(1 for i in lst if i.get("state") == "matched")
            d["suspended"] = int(s.get("fail_count") or 0) >= MAX_FAILS
            try:
                days = _norm_stale_days(s.get("stale_days"))
            except ValueError:
                days = 0
            last_new = int(s.get("last_new_ts") or 0)
            idle_days = max(0, (now - last_new) // 86400) if last_new else 0
            d["stale_idle_days"] = idle_days
            d["stale"] = bool(days > 0 and last_new and idle_days >= days)
            subs.append(d)
        return {"subs": subs}

    def items(self, sub_id: str) -> list[dict]:
        """订阅条目（新 → 旧）。"""
        with _FILE_LOCK:
            data = self._load()
        self._find(data, sub_id)          # 不存在抛 KeyError → API 404
        lst = [dict(i) for i in data["items"].get(sub_id, [])]
        return sorted(lst, key=lambda i: int(i.get("first_seen") or 0),
                      reverse=True)

    # ------------------------------------------------------------ 检查

    @staticmethod
    def _fetch(sub: dict, client: httpx.Client) -> dict:
        """条件 GET 拉取 feed（锁外网络段）。返回 {status, ...}。

        304 Not Modified 视为成功（无新条目）——不算失败不动 fail_count；
        计划草案把 304 与失败并列是笔误口径，成功响应不该累计失败次数。"""
        headers = {"User-Agent": "fnmedia-dl/rss"}
        if sub.get("etag"):
            headers["If-None-Match"] = sub["etag"]
        if sub.get("last_modified"):
            headers["If-Modified-Since"] = sub["last_modified"]
        try:
            with client.stream("GET", sub["url"], headers=headers,
                               timeout=DEFAULT_FEED_TIMEOUT,
                               follow_redirects=True) as resp:
                if resp.status_code == 304:
                    return {"status": "not_modified"}
                if resp.status_code >= 400:
                    return {"status": "error",
                            "error": f"订阅源返回 HTTP {resp.status_code}"}
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_FEED_BYTES:
                        return {"status": "error",
                                "error": "订阅源内容超过 2MB 上限"}
                    chunks.append(chunk)
                return {"status": "ok", "body": b"".join(chunks),
                        "etag": resp.headers.get("etag", ""),
                        "last_modified": resp.headers.get("last-modified", "")}
        except httpx.HTTPError as exc:
            return {"status": "error", "error": f"订阅源拉取失败：{exc}"}

    def check(self, sub_id: str, *, client: httpx.Client | None = None,
              history_check=None, manual: bool = False,
              notify_fn=None) -> dict:
        """立即/定时检查：拉取 → 解析 → 三层去重 → 过滤 → 入库（不派发）。

        history_check(url) → {"downloaded": bool}（资源级查重，API 层注入
        downloader.history_check）；None = 跳过该层（测试/离线）。
        manual=True（「立即检查」按钮）：搁置订阅复位 fail_count，轮询恢复
        资格——手动重试即复位。
        并发语义（同 taskqueue O3）：网络段不持文件锁，入库回写短临界区；
        检查整体单飞，撞车直接报错不排队。"""
        if not _CHECK_GUARD.acquire(blocking=False):
            raise RuntimeError("已有 RSS 检查正在进行，请稍后再试")
        try:
            with _FILE_LOCK:
                data = self._load()
                sub = dict(self._find(data, sub_id))
            if manual and int(sub.get("fail_count") or 0) >= MAX_FAILS:
                with _FILE_LOCK, filelock.locked(self.path):
                    data = self._load()
                    self._find(data, sub_id)["fail_count"] = 0
                    self._save(data)
            cli = client or httpx.Client(trust_env=False)
            try:
                fetched = self._fetch(sub, cli)
            finally:
                if client is None:
                    cli.close()
            if fetched["status"] == "not_modified":
                notice = self._touch(sub_id, ok=True, stale_has_new=False)
                _notify_stale(notify_fn, notice, sub)
                return {"ok": True, "sub_id": sub_id, "status": "not_modified",
                        "new": 0, "matched": 0, "skipped": 0, "items": [],
                        "stale_notice": notice}
            if fetched["status"] == "error":
                err = fetched.get("error") or "订阅源拉取失败"
                self._touch(sub_id, ok=False, error=err)
                return {"ok": False, "sub_id": sub_id, "status": "error",
                        "error": err, "stale_notice": ""}
            try:
                entries = _parse_feed(fetched["body"])
            except ValueError as exc:
                self._touch(sub_id, ok=False, error=str(exc))
                return {"ok": False, "sub_id": sub_id, "status": "error",
                        "error": str(exc), "stale_notice": ""}

            # 入库（短锁）：seen 指纹先写（宁漏勿重）→ 历史查重 → 过滤
            now = int(time.time())
            added: list[dict] = []
            with _FILE_LOCK, filelock.locked(self.path):
                data = self._load()
                sub = self._find(data, sub_id)
                flt = sub.get("filter") or {}
                ep_cfg = sub.get("episode") or {}
                ep_on = bool(ep_cfg.get("enabled"))
                ep_seen = sub.setdefault("ep_seen", {})
                lst = data["items"].setdefault(sub_id, [])
                seen = data.setdefault("seen", {})
                existing = ({it.get("guid") for it in lst}
                            | {it.get("identity") for it in lst})
                for raw in entries:
                    ident = linksmod.identity(raw["url"])
                    fp = _fingerprint(raw.get("guid") or "", ident)
                    if fp in seen or fp in existing:
                        seen[fp] = now          # 刷新时间戳，活跃条目不滚出
                        continue
                    seen[fp] = now
                    item = {
                        "id": uuid.uuid4().hex[:10],
                        "guid": raw.get("guid") or "",
                        "identity": ident,
                        "title": raw.get("title") or "",
                        "url": raw.get("url") or "",
                        "size": int(raw.get("size") or 0),
                        "state": "new",
                        "retry_count": 0,
                        "first_seen": now,
                        "last_result": "",
                    }
                    ep_key = ""
                    if ep_on:
                        ep = parse_episode(raw.get("title") or "",
                                           ep_cfg.get("regex") or "",
                                           int(ep_cfg.get("group") or 1),
                                           int(ep_cfg.get("offset") or 0))
                        if ep:
                            ep_key = f"S{ep[0]:02d}E{ep[1]:02d}"
                    # 层②：资源级下载历史查重（命中 = 已有同资源）
                    if history_check is not None:
                        try:
                            hit = history_check(raw["url"]) or {}
                        except Exception:  # noqa: BLE001 - 查重失败不阻断入库
                            hit = {}
                        if hit.get("downloaded"):
                            item["state"] = "skipped"
                            item["last_result"] = "下载历史已有同资源"
                            lst.append(item)
                            added.append(item)
                            # 已有同资源 = 该集已入库，占集名额挡其他版本
                            if ep_key and ep_key not in ep_seen:
                                ep_seen[ep_key] = now
                            continue
                    ok, reason = _filter_entry(item["title"], item["size"], flt)
                    # 层②.5 剧集去重（P3）：同（订阅,季,集）只收一条、先到
                    # 先得；过滤排除的条目不占名额（同集换版本仍可收）
                    if ok and ep_key:
                        if ep_key in ep_seen:
                            ok = False
                            reason = f"同集已收：{ep_key}"
                        else:
                            ep_seen[ep_key] = now
                    item["state"] = "matched" if ok else "skipped"
                    item["last_result"] = "" if ok else reason
                    lst.append(item)
                    added.append(item)
                if len(ep_seen) > MAX_EP_SEEN:   # 已收集数键按时间淘汰旧键
                    keep = sorted(ep_seen.items(), key=lambda kv: kv[1],
                                  reverse=True)[:MAX_EP_SEEN]
                    sub["ep_seen"] = dict(keep)
                if len(lst) > MAX_ITEMS_PER_SUB:
                    data["items"][sub_id] = lst[-MAX_ITEMS_PER_SUB:]
                sub["etag"] = (fetched.get("etag") or sub.get("etag") or "")
                sub["last_modified"] = (fetched.get("last_modified")
                                        or sub.get("last_modified") or "")
                sub["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
                sub["last_check_ts"] = now
                sub["fail_count"] = 0
                sub["last_error"] = ""
                self._prune_seen(data, now)
                notice = _stale_eval(sub, now, bool(added))
                self._save(data)
            _notify_stale(notify_fn, notice, sub)
            matched = sum(1 for i in added if i["state"] == "matched")
            return {"ok": True, "sub_id": sub_id, "status": "ok",
                    "new": len(added), "matched": matched,
                    "skipped": len(added) - matched, "items": added,
                    "stale_notice": notice}
        finally:
            _CHECK_GUARD.release()

    # ------------------------------------------------------------ 派发

    def dispatch_item(self, item_id: str, dispatch_fn) -> dict:
        """手动派发单条（dispatch_fn(url, directory) → 结果 dict）。

        幂等：已派发（dispatched）直接回执不重发；其余状态（matched/skipped/
        failed/new）允许手动派发——手动 = 显式用户意图，可越过过滤/查重。
        并发语义同 taskqueue run_now：网络派发不持文件锁，结果回写短临界区。"""
        with _FILE_LOCK:
            data = self._load()
            item, sub = self._find_item(data, item_id)
            if item.get("state") == "dispatched":
                return {"ok": True, "already": True, "item": dict(item)}
            url = item.get("url", "")
            directory = (sub or {}).get("directory") or None
        try:
            res = dispatch_fn(url, directory) or {}
        except Exception as exc:  # noqa: BLE001 - 派发异常转失败结果
            res = {"ok": False, "error": str(exc)}
        with _FILE_LOCK, filelock.locked(self.path):
            data = self._load()
            try:
                item, _ = self._find_item(data, item_id)
            except KeyError:
                return res           # 派发期间条目被删除：结果照回不回写
            if res.get("ok"):
                item["state"] = "dispatched"
                item["last_result"] = str(res.get("backend") or "")
            else:
                item["state"] = "failed"
                item["retry_count"] = int(item.get("retry_count") or 0) + 1
                item["last_result"] = str(res.get("error") or "派发失败")[:200]
            self._save(data)
        return res

    # ------------------------------------------------------------ 轮询（P2）

    @staticmethod
    def _due(sub: dict, now: int) -> bool:
        """到点判定：从未检查（含旧版无 last_check_ts 的订阅）或距上次检查
        已达 interval_min。"""
        ts = int(sub.get("last_check_ts") or 0)
        return now - ts >= int(sub.get("interval_min") or 30) * 60

    def _auto_dispatch_round(self, dispatch_fn) -> dict:
        """auto_download 订阅的自动派发：matched + 未超重试上限的 failed 条目，
        FIFO（先入先出，积压按到货顺序清空），单订阅每轮至多 MAX_AUTO_DISPATCH
        条（前置节流，防一次涌入大量任务）。"""
        dispatched = failed = 0
        if dispatch_fn is None:
            return {"dispatched": 0, "failed": 0}
        with _FILE_LOCK:
            data = self._load()
            targets: list[str] = []
            for sub in data["subs"]:
                if not (sub.get("enabled") and sub.get("auto_download")):
                    continue
                lst = data["items"].get(sub.get("id"), [])
                cand = [it for it in lst
                        if it.get("state") == "matched"
                        or (it.get("state") == "failed"
                            and int(it.get("retry_count") or 0)
                            < MAX_DISPATCH_RETRY)]
                cand.sort(key=lambda it: int(it.get("first_seen") or 0))
                targets.extend(it["id"] for it in cand[:MAX_AUTO_DISPATCH])
        for item_id in targets:
            res = self.dispatch_item(item_id, dispatch_fn)
            if res.get("ok"):
                dispatched += 1
            else:
                failed += 1
        return {"dispatched": dispatched, "failed": failed}

    def tick(self, dispatch_fn=None, history_check=None,
             client: httpx.Client | None = None, sleep=time.sleep,
             notify_fn=None) -> dict:
        """定时轮询一轮（rss-ticker 每 TICK_INTERVAL_SEC 驱动）：

        ① 到点检查：enabled 且未搁置（fail_count < MAX_FAILS）且距上次检查
        ≥ interval_min 的订阅各查一次，订阅间 sleep TICK_SUB_GAP 防对端限流；
        ② 自动派发：auto_download 订阅按 _auto_dispatch_round 清理积压。
        停滞订阅（stale_days>0 且超阈值）经 notify_fn 提醒，一天至多一次。
        单飞撞车（手动检查进行中）跳过本轮不报错；检查失败只记订阅状态不中断
        后续订阅。返回 {checked, dispatched, failed} 汇总。"""
        now = int(time.time())
        with _FILE_LOCK:
            data = self._load()
            due = [dict(s) for s in data["subs"]
                   if s.get("enabled")
                   and int(s.get("fail_count") or 0) < MAX_FAILS
                   and self._due(s, now)]
        cli = client or httpx.Client(trust_env=False)
        checked = 0
        try:
            for i, sub in enumerate(due):
                if i:
                    sleep(TICK_SUB_GAP)      # 订阅间间隔：防对端限流
                try:
                    self.check(sub["id"], client=cli,
                               history_check=history_check,
                               notify_fn=notify_fn)
                    checked += 1
                except (RuntimeError, KeyError):
                    continue                 # 单飞撞车 / 订阅刚被删：跳过
        finally:
            if client is None:
                cli.close()
        stats = self._auto_dispatch_round(dispatch_fn)
        return {"checked": checked,
                "dispatched": stats["dispatched"], "failed": stats["failed"]}
