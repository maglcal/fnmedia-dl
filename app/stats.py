#!/usr/bin/env python3
"""fnmedia-dl 匿名使用统计客户端（M1）：采集 / outbox / 上报线程。

依据《fnmedia-dl 统计上报客户端接入规格》（Phase 1 定稿）与
《多应用统计后端系统-需求规格与接口约定》§5/§6 契约实现：

- 双闸（D-1）：采集闸 = stats.enabled（关 = 不采集不上传，零足迹不写 stats.db）；
  上报闸 = enabled ∧ endpoint≠空（enabled=true 且 endpoint 空 = 只本地统计不上报）。
- 事件口径：install = sync 发现无统计 meta（首次进入统计口径，一生一次）；
  update = meta.last_version ≠ VERSION 的任意版本跃迁（含降级；关闭期跃迁在
  下次启用 sync 补计一条）；open = /api/backends 钩子且 last_open_date ≠ 本地
  今日（每日本机一条；startup 不判 open；开关开启动作视同一次打开并触发 sync）。
- machine_hash = sha256_hex(UTF-8(APP_SALT + "‖" + machine_code))[0:32]；
  机器码三级回退（宿主稳定标识 → meta.device_id → 内存随机），原文永不离开
  本机：不入请求体、不入日志、不入 outbox（组包时现算 hash）。
- outbox（state/stats.db，SQLite 标准库零新增依赖）：批量 ≤50 条 FIFO，
  body 64KB 硬校验超限拆批；队列上限 1000 条 / 30 天（超限删最旧）；
  上报成功（204）才删；400/413 丢弃本批不重试；429 按 Retry-After（缺省
  退避）；5xx/网络错/超时保留本批按 1m→5m→30m→6h 退避。
- 传输：POST {endpoint}/collect，超时 5s（urllib 标准库），fire-and-forget
  绝不阻塞主流程；endpoint 真实地址由打包链注入 state/config.json，
  公开源码/注释/文档一律 stats.example.com 占位。

隐私红线：日志不落 machine_hash / 机器码原文；DEFAULT_CONFIG endpoint 为空串。
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import config as cfgmod

# ---------------------------------------------------------------- 常量（锁定）

APP_ID = "fnmedia-dl"                   # 固定字面量，不做配置
CLIENT_V = "fnd-stats/1"                # client.v（≤16 字符，供服务端排查）

# 应用盐：公开盐可入源码，与 appstats config/apps.yaml 中 fnmedia-dl 条目的
# app_salt 同值（D-3）。**一经生成永不更换**——换盐 = 实例身份全体重置
# （machine_hash 全变、服务端去重键失效重来），等同数据事故，禁止发生。
APP_SALT = "b404d0f70e66d3c034bc6181ddf1bac4"

HASH_SEP = "‖"                          # U+2016 DOUBLE VERTICAL LINE（UTF-8 E2 80 96）

BATCH_LIMIT = 50                        # 单批 ≤50 条（契约 §5.4）
BODY_LIMIT = 64 * 1024                  # 请求体 64KB 硬校验（超限拆批）
QUEUE_MAX_ROWS = 1000                   # 队列上限 1000 条
QUEUE_MAX_AGE_DAYS = 30                 # 队列上限 30 天（超限删最旧）
POST_TIMEOUT = 5                        # 上报超时 5s
BACKOFF_STEPS = (60, 300, 1800, 21600)  # 失败退避 1m→5m→30m→6h 封顶
IDLE_POLL = 300                         # 空闲/关闸轮询兜底（秒）

_warn = lambda msg: print(f"[stats] {msg}", file=sys.stderr)  # noqa: E731


def _read_version() -> str:
    """版本号以仓库根部 VERSION 文件为准（与 main.VERSION 同源）。"""
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:3]:
        vf = parent / "VERSION"
        if vf.is_file():
            try:
                text = vf.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                return text.splitlines()[0].strip()
    return "0.0.0"


VERSION = _read_version()

# ---------------------------------------------------------------- 时间（测试可打桩）

def _now() -> datetime.datetime:
    """当前本地时刻（含偏移）。事件产生时刻/日期一律由此定格，补报不刷新。"""
    return datetime.datetime.now().astimezone()


# ---------------------------------------------------------------- 双闸（D-1）

def _load_cfg_quiet() -> dict | None:
    try:
        return cfgmod.load()
    except Exception:  # noqa: BLE001 - 配置读不到时不采集不上传（fail-closed）
        return None


def _gate_collect(cfg: dict) -> bool:
    """采集闸：stats.enabled（false = 不采集不上传，零足迹）。"""
    return bool((cfg.get("stats") or {}).get("enabled"))


def _gate_report(cfg: dict) -> bool:
    """上报闸：enabled ∧ endpoint≠空（enabled=true+endpoint 空 = 只本地统计）。"""
    s = cfg.get("stats") or {}
    return bool(s.get("enabled")) and bool((s.get("endpoint") or "").strip())


# ---------------------------------------------------------------- 机器码与哈希（§5）

def _host_code() -> str:
    """L1 宿主稳定标识：fnOS 设备 SN（环境注入可取时优先）→ /etc/machine-id。
    均去除首尾空白，空则降级。原文永不入日志/请求体/outbox。"""
    for key in ("FNOS_DEVICE_SN", "TRIM_DEVICE_SN"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    with contextlib.suppress(OSError):
        val = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        if val:
            return val
    return ""


_mem_uuid: str | None = None            # L3 内存随机（state 不可写等极端情形）


def _machine_code() -> str:
    """机器码三级回退（§5.2）：L1 宿主稳定标识 → L2 meta.device_id（首次
    需要哈希时生成 UUID v4 落盘，跟随 state/ 持久）→ L3 内存随机 UUID
    （视为新机器）。原文不出本机。"""
    code = _host_code()
    if code:
        return code
    try:
        with _db_lock, _db() as conn:
            row = conn.execute(
                "SELECT v FROM meta WHERE k='device_id'").fetchone()
            if row and row[0]:
                return row[0]
            new_id = str(uuid.uuid4())
            conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES('device_id', ?)",
                (new_id,))
            return new_id
    except Exception:  # noqa: BLE001 - 落盘失败降级 L3
        pass
    global _mem_uuid
    if _mem_uuid is None:
        _mem_uuid = str(uuid.uuid4())
    return _mem_uuid


def machine_hash(machine_code: str) -> str:
    """machine_hash = sha256_hex(UTF-8(APP_SALT + "‖" + machine_code))[0:32]。"""
    digest = hashlib.sha256(
        (APP_SALT + HASH_SEP + machine_code).encode("utf-8")).hexdigest()
    return digest[:32]


# ---------------------------------------------------------------- 本地存储（§6.1）

_db_lock = threading.Lock()


def _db_path() -> Path:
    """state/stats.db（跟随 cfgmod.STATE_DIR；升级白名单不含 state，不丢身份）。"""
    return Path(cfgmod.STATE_DIR) / "stats.db"


def _open() -> sqlite3.Connection:
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path()), timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "type TEXT NOT NULL,"           # install | update | open
        "version TEXT, from_version TEXT,"
        "client_date TEXT NOT NULL,"    # 事件产生日 YYYY-MM-DD（本地时区）
        "ts TEXT NOT NULL)")
    return conn


@contextlib.contextmanager
def _db():
    conn = _open()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _meta_get(conn: sqlite3.Connection) -> dict:
    return {k: v for k, v in conn.execute("SELECT k, v FROM meta")}


def _meta_set(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))


def _enqueue_event(conn: sqlite3.Connection, type_: str, version: str,
                   from_version: str | None, today: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO outbox(type, version, from_version, client_date, ts)"
        " VALUES(?, ?, ?, ?, ?)",
        (type_, version or "", from_version, today, ts))


def _prune(conn: sqlite3.Connection, now: datetime.datetime) -> None:
    """队列上限 1000 条 / 30 天：超限删最旧行（入队与每次 flush 前检查）。"""
    cutoff = (now - datetime.timedelta(days=QUEUE_MAX_AGE_DAYS)
              ).isoformat(timespec="seconds")
    conn.execute("DELETE FROM outbox WHERE ts < ?", (cutoff,))
    (n,) = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
    if n > QUEUE_MAX_ROWS:
        conn.execute(
            "DELETE FROM outbox WHERE id IN ("
            "SELECT id FROM outbox ORDER BY id ASC LIMIT ?)",
            (n - QUEUE_MAX_ROWS,))


# ---------------------------------------------------------------- 事件采集（§4.1）

def _sync(include_open: bool = False) -> int:
    """sync 同步检测（采集闸内才落盘；判定幂等，锁内先写 meta 再入队）：
    1) 无统计 meta（installed_at 缺失）→ 入队 install（version=当前 VERSION）→ 建 meta；
    2) meta.last_version ≠ VERSION → 入队 update（from=旧 to=新，任意跃迁含降级，
       关闭期跃迁在下次启用的 sync 补计）→ 更新 last_version；
    3) include_open 且 meta.last_open_date ≠ 今日 → 入队 open → 更新 last_open_date
       （仅 init 钩子/开关开启路径执行；startup 不判 open）。
    返回入队条数。"""
    cfg = _load_cfg_quiet()
    if cfg is None or not _gate_collect(cfg):
        return 0
    now = _now()
    today = now.date().isoformat()
    ts = now.isoformat(timespec="seconds")
    count = 0
    with _db_lock, _db() as conn:
        meta = _meta_get(conn)
        if "installed_at" not in meta:
            _meta_set(conn, "installed_at", ts)      # 先写 meta 再入队（防并发重复）
            _meta_set(conn, "last_version", VERSION)
            _enqueue_event(conn, "install", VERSION, None, today, ts)
            count += 1
        elif meta.get("last_version") != VERSION:
            _meta_set(conn, "last_version", VERSION)
            _enqueue_event(conn, "update", VERSION,
                           meta.get("last_version") or "", today, ts)
            count += 1
        if include_open and meta.get("last_open_date") != today:
            _meta_set(conn, "last_open_date", today)
            _enqueue_event(conn, "open", VERSION, None, today, ts)
            count += 1
        _prune(conn, now)
    return count


def _touch_open() -> int:
    """open 跨日检查（/api/backends 钩子与开关开启路径）：每日最多一条。"""
    cfg = _load_cfg_quiet()
    if cfg is None or not _gate_collect(cfg):
        return 0
    now = _now()
    today = now.date().isoformat()
    ts = now.isoformat(timespec="seconds")
    with _db_lock, _db() as conn:
        meta = _meta_get(conn)
        if meta.get("last_open_date") == today:
            return 0
        _meta_set(conn, "last_open_date", today)     # 先写 meta 再入队
        _enqueue_event(conn, "open", VERSION, None, today, ts)
        _prune(conn, now)
    return 1


# ---------------------------------------------------------------- 对外钩子（fire-and-forget）

def on_startup() -> None:
    """应用 startup 钩子：sync 补计 install/update（startup 不判 open）+ 拉起上报线程。"""
    try:
        _sync(include_open=False)
    except Exception as exc:  # noqa: BLE001 - 绝不影响主流程
        _warn(f"startup sync 跳过（{type(exc).__name__}）")
    kick()


def on_manage_page_open() -> int:
    """GET /api/backends 钩子（管理页首载必经）：open 跨日检查，幂等。
    埋点在后端路由（浏览器预取/缓存不算打开）。"""
    try:
        n = _touch_open()
    except Exception as exc:  # noqa: BLE001
        _warn(f"open 埋点跳过（{type(exc).__name__}）")
        return 0
    if n:
        kick()
    return n


def on_toggle_on() -> int:
    """开关开启动作（关→开）：触发 sync（补计 install/update）并视同一次管理页
    打开（open 跨日检查）。"""
    n = 0
    try:
        n = _sync(include_open=True)
    except Exception as exc:  # noqa: BLE001
        _warn(f"开关开启 sync 跳过（{type(exc).__name__}）")
    kick()
    return n


def on_config_changed(before: dict, after: dict) -> None:
    """config stats 块变更回调（PUT /api/config 末尾、文件锁外调用）：
    开关关→开 = 视同打开并触发 sync；endpoint 变化 = 唤醒上报线程立即尝试。"""
    try:
        was_on = bool((before or {}).get("enabled"))
        now_on = bool((after or {}).get("enabled"))
        if now_on and not was_on:
            on_toggle_on()
            return
        if (after or {}).get("endpoint") != (before or {}).get("endpoint"):
            kick()
    except Exception as exc:  # noqa: BLE001
        _warn(f"config 回调跳过（{type(exc).__name__}）")


# ---------------------------------------------------------------- 组包与上报（§6.3/§6.4）

def _build_body(rows: list[dict], mhash: str) -> bytes:
    """组包（字段严格白名单 §4.2：type/machine_hash/version/from_version(update)/
    client_date/ts，多余字段不加）；machine_hash 组包现算，不落 outbox。"""
    events = []
    for r in rows:
        ev = {"type": r["type"], "machine_hash": mhash,
              "version": r["version"] or ""}
        if r["type"] == "update":
            ev["from_version"] = r["from_version"] or ""
        ev["client_date"] = r["client_date"]
        ev["ts"] = r["ts"]
        events.append(ev)
    payload = {"app_id": APP_ID, "events": events, "client": {"v": CLIENT_V}}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _fit(rows: list[dict]) -> list[dict]:
    """body 64KB 硬校验：超限拆批（取前半自成一批，余量留待下一轮）。"""
    while len(rows) > 1 and len(_build_body(rows, "0" * 32)) > BODY_LIMIT:
        rows = rows[:len(rows) // 2]
    return rows


def _fetch_batch(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT id, type, version, from_version, client_date, ts"
        " FROM outbox ORDER BY id ASC LIMIT ?", (BATCH_LIMIT,))
    cols = ("id", "type", "version", "from_version", "client_date", "ts")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _delete_ids(conn: sqlite3.Connection, ids: list[int]) -> None:
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        conn.execute(
            "DELETE FROM outbox WHERE id IN (%s)"
            % ",".join("?" * len(chunk)), chunk)


def _retry_after(headers) -> float | None:
    """Retry-After: <秒>（契约 §6.1）；缺省 None。钳制 1s~6h（封顶同退避）。"""
    if headers is None:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(1.0, min(val, float(BACKOFF_STEPS[-1])))


def _post(url: str, body: bytes) -> tuple[int, float | None]:
    """POST {endpoint}/collect（urllib 标准库，超时 5s，仅 Content-Type 头）。
    返回 (HTTP 状态码, Retry-After 秒或 None)；网络错误/超时抛 OSError 系。"""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            return getattr(resp, "status", 204), _retry_after(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _retry_after(exc.headers)


def _backoff() -> float:
    idx = min(max(_fail_count, 1), len(BACKOFF_STEPS)) - 1
    return float(BACKOFF_STEPS[idx])


_fail_count = 0                         # 连续失败计数（204 成功归零）


def flush_once() -> dict:
    """上报一轮（同步执行、不睡眠；退避时长由返回值携带）。
    返回 {"action": …, "sleep": 秒}；action ∈ gate-closed / idle / sent /
    dropped / retry。失败保留本批；成功才删；400/413 丢批不重试。"""
    global _fail_count
    cfg = _load_cfg_quiet()
    if cfg is None or not _gate_report(cfg):
        return {"action": "gate-closed", "sleep": 0.0}
    endpoint = ((cfg.get("stats") or {}).get("endpoint") or "").strip().rstrip("/")
    now = _now()
    with _db_lock, _db() as conn:
        _prune(conn, now)
        rows = _fetch_batch(conn)
    if not rows:
        return {"action": "idle", "sleep": 0.0}
    rows = _fit(rows)
    body = _build_body(rows, machine_hash(_machine_code()))
    ids = [r["id"] for r in rows]
    try:
        status, retry_after = _post(endpoint + "/collect", body)
    except Exception as exc:  # noqa: BLE001 - 网络错/超时：保留本批退避重试
        _fail_count += 1
        return {"action": "retry", "sleep": _backoff(),
                "error": type(exc).__name__}
    if status == 204:
        with _db_lock, _db() as conn:
            _delete_ids(conn, ids)
        _fail_count = 0
        return {"action": "sent", "sleep": 0.0, "count": len(ids)}
    if status in (400, 413):
        # 永久性错误：丢弃本批（宁少计不阻塞队列），stderr 记因不重试
        with _db_lock, _db() as conn:
            _delete_ids(conn, ids)
        _warn(f"上报批次被服务端拒绝（HTTP {status}），已丢弃 {len(ids)} 条不重试")
        return {"action": "dropped", "sleep": 0.0, "status": status}
    if status == 429:
        _fail_count += 1
        sleep = retry_after if retry_after is not None else _backoff()
        return {"action": "retry", "sleep": sleep, "status": 429}
    _fail_count += 1                    # 5xx 及其他：保留本批退避重试
    return {"action": "retry", "sleep": _backoff(), "status": status}


# ---------------------------------------------------------------- 上报线程（daemon）

_wake = threading.Event()
_worker_guard = threading.Lock()
_worker: threading.Thread | None = None


def _ensure_worker() -> None:
    global _worker
    with _worker_guard:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, daemon=True,
                                   name="stats-reporter")
        _worker.start()


def kick() -> None:
    """上报闸可用时拉起上报线程并唤醒立即尝试（其余情形零动作）。"""
    try:
        cfg = _load_cfg_quiet()
        if cfg is not None and _gate_report(cfg):
            _ensure_worker()
            _wake.set()
    except Exception:  # noqa: BLE001
        pass


def _worker_loop() -> None:
    """入队后唤醒立即尝试 + 失败按退避定时重试；daemon 线程随上报闸首次
    可用启动（kick），关闸空闲不删行（D-5）。"""
    global _fail_count
    while True:
        _wake.clear()
        try:
            res = flush_once()
        except Exception:  # noqa: BLE001 - 后台线程绝不因异常退出
            _fail_count += 1
            res = {"action": "error", "sleep": _backoff()}
        action = res.get("action")
        if action in ("sent", "dropped"):
            continue                    # 队列还有存量 → 立即下一批
        if action in ("retry", "error"):
            time.sleep(max(float(res.get("sleep") or 60), 1.0))
            continue
        _wake.wait(IDLE_POLL)           # idle / gate-closed：等唤醒或轮询兜底
