#!/usr/bin/env python3
"""统计上报客户端（M1）单测——《fnmedia-dl 统计上报客户端接入规格》§9 客户端验收项。

覆盖（规格 §9 编号）：
  #1/#13 双闸：enabled=false 零足迹；enabled=true 且 endpoint 空 = 只本地统计零外发
  #2     事件形态：字段严格白名单、machine_hash 32 hex、client_date/ts 形态
  #3     每日一 open：last_open_date 同日两开只产 1 条
  #4     outbox 生命周期：204 删行、失败保留、超窗事件照常删行、队列上限
  #5     sync 版本跃迁：update(from,to) 补计、同版本不重复、降级也算跃迁
  #8     关闭即停：enabled 翻 false 后不再外发
  #10    老 config 兼容：无 stats 块深合并出默认、PUT 分段合并不冲 endpoint
  #11    machine_hash：固定输入恒同值、32 位小写 hex、换盐/换机器码值变、三级回退
  #12    退避：1m→5m→30m→6h 封顶、成功清零；400/413 丢批不重试；429 按 Retry-After
  §7.1  开关关→开 触发 sync 补计 install/update + 视同一次 open（on_config_changed）

全程隔离 state/（tempfile 临时目录）与网络（_post 打桩），绝不真实外发。
纯 unittest 风格——项目红线 `python -m unittest discover -s tests` 必须能收集到本文件全部用例（禁 pytest 专属语法）。
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config as cfgmod                             # noqa: E402
import main as mainmod                             # noqa: E402
import stats as statsmod                           # noqa: E402

T0 = datetime.datetime(2026, 11, 14, 10, 30, 5,
                       tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
REAL_KICK = statsmod.kick                          # 线程生命周期用例恢复真身
ENDPOINT = "https://stats.example.com"             # 仅占位示例，无真实域名

_SENTINEL = object()


class _MonkeyPatch:
    """最小 monkeypatch 等价物（setattr/setenv/delenv + undo），免引第三方测试框架。"""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        old = getattr(obj, name)
        self._undo.append(("attr", obj, name, old))
        setattr(obj, name, value)

    def setenv(self, name, value):
        old = os.environ.get(name, _SENTINEL)
        self._undo.append(("env", name, old))
        os.environ[name] = value

    def delenv(self, name, raising=True):
        old = os.environ.get(name, _SENTINEL)
        if old is _SENTINEL and raising:
            raise KeyError(name)
        self._undo.append(("env", name, old))
        os.environ.pop(name, None)

    def undo(self):
        for kind, *rest in reversed(self._undo):
            if kind == "attr":
                obj, name, old = rest
                setattr(obj, name, old)
            else:
                name, old = rest
                if old is _SENTINEL:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
        self._undo.clear()


def _forbidden_post(url, body):
    raise AssertionError(f"测试中不得真实外发：{url}")


def _stub_post(monkeypatch, results):
    """按序回放 results（(status, retry_after) 元组或异常实例），记录每次调用。"""
    calls = []
    queue = list(results)

    def fake(url, body):
        calls.append((url, body))
        item = queue.pop(0) if queue else (204, None)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(statsmod, "_post", fake)
    return calls


def _save_cfg(enabled: bool, endpoint: str = "") -> None:
    cfgmod.save({"stats": {"enabled": enabled, "endpoint": endpoint}})


def _enqueue_bulk(n: int, type_: str = "open", version: str = "1.0.0",
                  from_version: str | None = None,
                  ts_iso: str | None = None,
                  client_date: str | None = None) -> None:
    today = client_date or T0.date().isoformat()
    ts = ts_iso or T0.isoformat(timespec="seconds")
    with statsmod._db_lock, statsmod._db() as conn:
        for _ in range(n):
            statsmod._enqueue_event(conn, type_, version, from_version,
                                    today, ts)


def _outbox() -> list:
    with statsmod._db() as conn:
        return conn.execute(
            "SELECT id, type, version, from_version, client_date, ts"
            " FROM outbox ORDER BY id").fetchall()


def _meta() -> dict:
    with statsmod._db() as conn:
        return {k: v for k, v in conn.execute("SELECT k, v FROM meta")}


class StatsCase(unittest.TestCase):
    """state/ 隔离 + 禁网 + 不拉真线程（kick 换记录器）+ 失败计数归零。"""

    def setUp(self):
        self._mp = _MonkeyPatch()
        self.addCleanup(self._mp.undo)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.kicked = []
        self._mp.setattr(cfgmod, "STATE_DIR", self.tmp_path)
        self._mp.setattr(cfgmod, "CONFIG_PATH", self.tmp_path / "config.json")
        self._mp.setattr(statsmod, "_fail_count", 0)
        self._mp.setattr(statsmod, "_mem_uuid", None)
        self._mp.setattr(statsmod, "kick",
                         lambda: self.kicked.append(True))
        self._mp.setattr(statsmod, "_post", _forbidden_post)


# ---------------------------------------------------------------- §9-11 machine_hash

class TestMachineHash(StatsCase):
    def test_known_answer_and_shape(self):
        """固定输入恒同值（知识答案锁定），32 位小写 hex。"""
        got = statsmod.machine_hash("test-machine-code-1")
        assert got == "b4fd0d5ea92ce2cf333e3c45aaafed7e"
        assert re.fullmatch(r"[0-9a-f]{32}", got)
        assert statsmod.machine_hash("test-machine-code-1") == got  # 恒同值

    def test_formula_separator_is_u2016(self):
        """公式 = sha256_hex(UTF-8(APP_SALT + ‖(U+2016) + machine_code))[0:32]。"""
        assert ord(statsmod.HASH_SEP) == 0x2016
        code = "abc"
        expected = hashlib.sha256(
            (statsmod.APP_SALT + "‖" + code).encode("utf-8")
        ).hexdigest()[:32]
        assert statsmod.machine_hash(code) == expected

    def test_salt_or_code_change_changes_hash(self):
        base = statsmod.machine_hash("machine-a")
        assert statsmod.machine_hash("machine-b") != base  # 换机器码值变
        import unittest.mock as mock
        with mock.patch.object(statsmod, "APP_SALT", "0" * 32):
            assert statsmod.machine_hash("machine-a") != base  # 换盐值变

    def test_machine_code_l1_env_sn_trimmed(self):
        monkeypatch = self._mp
        monkeypatch.setenv("FNOS_DEVICE_SN", "  SN-ABC-123  ")
        assert statsmod._machine_code() == "SN-ABC-123"

    def test_machine_code_l1_machine_id(self):
        monkeypatch = self._mp
        monkeypatch.delenv("FNOS_DEVICE_SN", raising=False)
        monkeypatch.delenv("TRIM_DEVICE_SN", raising=False)
        orig = pathlib.Path.read_text

        def fake(self, *a, **kw):
            if self.name == "machine-id":
                return "mid-42\n"
            return orig(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "read_text", fake)
        assert statsmod._machine_code() == "mid-42"

    def test_machine_code_l2_device_id_persisted(self):
        """L1 空 → meta.device_id（UUID v4 含连字符）首次生成落盘，此后恒用。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_host_code", lambda: "")
        code1 = statsmod._machine_code()
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            code1)
        assert statsmod._machine_code() == code1
        assert _meta().get("device_id") == code1   # 已落盘 meta

    def test_machine_code_l3_memory_fallback(self):
        """state 不可写 → 内存随机 UUID（进程内恒定，视为新机器）。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_host_code", lambda: "")

        def broken():
            raise sqlite3.OperationalError("attempt to write a readonly db")

        monkeypatch.setattr(statsmod, "_db", broken)
        code1 = statsmod._machine_code()
        assert statsmod._machine_code() == code1
        assert re.fullmatch(r"[0-9a-f-]{36}", code1)

    def test_no_raw_code_or_hash_in_outbox(self):
        """隐私红线：outbox 不存 machine_hash/机器码；组包只含 hash 不含原文。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_host_code", lambda: "SECRET-CODE-XYZ")
        _save_cfg(True, ENDPOINT)
        assert statsmod._sync(include_open=True) == 2
        with statsmod._db() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(outbox)")]
        assert cols == ["id", "type", "version", "from_version",
                        "client_date", "ts"]   # 无 machine_hash/机器码列
        calls = _stub_post(monkeypatch, [(204, None)])
        assert statsmod.flush_once()["action"] == "sent"
        body = calls[0][1]
        assert b"SECRET-CODE-XYZ" not in body
        ev = json.loads(body)["events"][0]
        assert re.fullmatch(r"[0-9a-f]{32}", ev["machine_hash"])


# ---------------------------------------------------------------- §9-1 / #13 双闸

class TestGates(StatsCase):
    def test_disabled_zero_footprint(self):
        """enabled=false：不产事件、不外发、不写 stats.db（零足迹）。"""
        tmp_path = self.tmp_path
        _save_cfg(False, ENDPOINT)
        assert statsmod._sync(include_open=True) == 0
        assert statsmod.on_manage_page_open() == 0
        assert statsmod.flush_once()["action"] == "gate-closed"
        assert not (tmp_path / "stats.db").exists()

    def test_enabled_empty_endpoint_local_only(self):
        """enabled=true 且 endpoint 空 = 只本地统计：事件入 outbox 零外发；
        补上 endpoint 后正常补报。"""
        monkeypatch = self._mp
        _save_cfg(True, "")
        assert statsmod._sync(include_open=True) == 2
        assert statsmod.flush_once()["action"] == "gate-closed"
        assert len(_outbox()) == 2                     # 行保留（不发也不删）
        _save_cfg(True, ENDPOINT)
        calls = _stub_post(monkeypatch, [(204, None)])
        assert statsmod.flush_once()["action"] == "sent"
        assert len(_outbox()) == 0
        assert calls and calls[0][0] == ENDPOINT + "/collect"

    def test_close_stops_sending(self):
        """§9-8 关闭即停：enabled 翻 false 后不再外发（线程空闲不删行）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        statsmod._sync(include_open=True)
        _save_cfg(False, ENDPOINT)
        calls = _stub_post(monkeypatch, [(204, None)])
        assert statsmod.flush_once()["action"] == "gate-closed"
        assert calls == []
        assert len(_outbox()) == 2                     # 积压行保留（D-5）


# ---------------------------------------------------------------- §9-2 事件形态

class TestEventShape(StatsCase):
    def test_event_shape_exact_fields(self):
        """字段严格白名单：install 无 from_version；update 有；多余字段不加。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_host_code", lambda: "C1")
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        monkeypatch.setattr(statsmod, "VERSION", "1.2.3")
        _save_cfg(True, ENDPOINT)
        statsmod._sync(include_open=True)             # install + open
        monkeypatch.setattr(statsmod, "VERSION", "1.3.0")
        statsmod._sync()                              # update(1.2.3 -> 1.3.0)
        calls = _stub_post(monkeypatch, [(204, None), (204, None)])
        for _ in range(5):                                # 有界循环防死等
            if not _outbox():
                break
            statsmod.flush_once()
        events = [ev for _, body in calls
                  for ev in json.loads(body)["events"]]
        by_type = {e["type"]: e for e in events}
        assert set(by_type) == {"install", "update", "open"}
        assert set(by_type["install"]) == {"type", "machine_hash", "version",
                                           "client_date", "ts"}
        assert set(by_type["open"]) == {"type", "machine_hash", "version",
                                        "client_date", "ts"}
        assert set(by_type["update"]) == {"type", "machine_hash", "version",
                                          "from_version", "client_date", "ts"}
        assert by_type["update"]["from_version"] == "1.2.3"
        assert by_type["update"]["version"] == "1.3.0"
        for ev in events:
            assert re.fullmatch(r"[0-9a-f]{32}", ev["machine_hash"])
            assert ev["client_date"] == "2026-11-14"          # 事件产生日定格
            assert ev["ts"] == "2026-11-14T10:30:05+08:00"    # ISO 8601 本地偏移
        payload = json.loads(calls[0][1])
        assert set(payload) == {"app_id", "events", "client"}
        assert payload["app_id"] == "fnmedia-dl"
        assert payload["client"] == {"v": "fnd-stats/1"}

    def test_client_date_frozen_on_late_report(self):
        """补报不刷新 client_date/ts（事件产生时刻定格，归日由服务端处理）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(1, ts_iso="2026-11-14T10:00:00+08:00",
                      client_date="2026-11-14")
        monkeypatch.setattr(statsmod, "_now", lambda: T0
                            + datetime.timedelta(days=10))
        calls = _stub_post(monkeypatch, [(204, None)])
        assert statsmod.flush_once()["action"] == "sent"
        ev = json.loads(calls[0][1])["events"][0]
        assert ev["client_date"] == "2026-11-14"
        assert ev["ts"] == "2026-11-14T10:00:00+08:00"


# ---------------------------------------------------------------- §9-3 每日一 open

class TestDailyOpen(StatsCase):
    def test_open_once_per_day(self):
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        _save_cfg(True, ENDPOINT)
        assert statsmod._sync(include_open=True) == 2      # install + 当日 open
        assert statsmod.on_manage_page_open() == 0         # 同日再开不计
        assert statsmod._touch_open() == 0
        assert len([r for r in _outbox() if r[1] == "open"]) == 1

    def test_open_counts_next_day(self):
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        statsmod._sync(include_open=True)
        monkeypatch.setattr(statsmod, "_now", lambda: T0
                            + datetime.timedelta(days=1))
        assert statsmod.on_manage_page_open() == 1         # 跨日 → 第二条
        assert len([r for r in _outbox() if r[1] == "open"]) == 2

    def test_startup_does_not_emit_open(self):
        """startup 只 sync install/update，不判 open（启动≠打开管理页）。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        _save_cfg(True, ENDPOINT)
        statsmod.on_startup()
        assert [r[1] for r in _outbox()] == ["install"]


# ---------------------------------------------------------------- §9-5 sync 跃迁

class TestSyncTransitions(StatsCase):
    def test_install_once_lifetime(self):
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        _save_cfg(True, ENDPOINT)
        assert statsmod._sync() == 1                      # install
        assert statsmod._sync() == 0                      # 一生一次，不重复
        assert [r[1] for r in _outbox()] == ["install"]
        assert _meta()["installed_at"] == T0.isoformat(timespec="seconds")
        assert _meta()["last_version"] == statsmod.VERSION

    def test_update_on_version_jump_and_no_repeat(self):
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        _save_cfg(True, ENDPOINT)
        monkeypatch.setattr(statsmod, "VERSION", "1.0.0")
        statsmod._sync()
        monkeypatch.setattr(statsmod, "VERSION", "1.1.0")
        assert statsmod._sync() == 1                      # 跃迁 → update
        assert statsmod._sync() == 0                      # 同版本不重复
        upd = [r for r in _outbox() if r[1] == "update"]
        assert len(upd) == 1
        assert upd[0][2] == "1.1.0" and upd[0][3] == "1.0.0"  # to / from

    def test_downgrade_is_also_transition(self):
        """D-6：任意方向的版本跃迁都产生 update（to=降级版本）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        monkeypatch.setattr(statsmod, "VERSION", "2.0.0")
        statsmod._sync()
        monkeypatch.setattr(statsmod, "VERSION", "1.9.0")
        assert statsmod._sync() == 1
        upd = [r for r in _outbox() if r[1] == "update"]
        assert upd[0][2] == "1.9.0" and upd[0][3] == "2.0.0"

    def test_update_backfilled_after_disabled_period(self):
        """关闭期版本跃迁在下次启用 sync 补计一条（from=旧 to=新，D-6）。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "VERSION", "1.0.0")
        _save_cfg(True, ENDPOINT)
        statsmod._sync()                                  # install, last=1.0.0
        _save_cfg(False, ENDPOINT)
        monkeypatch.setattr(statsmod, "VERSION", "1.2.0")
        assert statsmod._sync() == 0                      # 关闭期不采集
        kicked = _save_and_toggle_on(monkeypatch)         # 重新启用（开关开启）
        assert kicked                                    # 触发 kick
        upd = [r for r in _outbox() if r[1] == "update"]
        assert len(upd) == 1
        assert upd[0][2] == "1.2.0" and upd[0][3] == "1.0.0"


def _save_and_toggle_on(monkeypatch):
    """经 on_config_changed 走开关开启动作（关→开），返回 kick 记录。
    与 config_put 语义一致：新配置先落盘，再回调 stats 模块。"""
    _save_cfg(True, ENDPOINT)
    kicked = []
    monkeypatch.setattr(statsmod, "kick", lambda: kicked.append(True))
    statsmod.on_config_changed({"enabled": False, "endpoint": ENDPOINT},
                               {"enabled": True, "endpoint": ENDPOINT})
    return kicked


# ---------------------------------------------------------------- §9-4 outbox 生命周期

class TestOutboxLifecycle(StatsCase):
    def test_204_deletes_batch_fifo(self):
        """≤50 条/批 FIFO；204 删本批行（上报成功才删）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(55)
        calls = _stub_post(monkeypatch, [(204, None), (204, None)])
        r1 = statsmod.flush_once()
        assert r1["action"] == "sent" and r1["count"] == 50
        assert len(json.loads(calls[0][1])["events"]) == 50
        r2 = statsmod.flush_once()
        assert r2["action"] == "sent" and r2["count"] == 5
        assert _outbox() == []

    def test_failure_keeps_rows(self):
        """失败保留本批（上报成功才删）；>3 天补报窗由服务端裁剪，客户端
        照常上报并在 204 后删行（不伪造日期、不无限囤积）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(3, ts_iso=(T0 - datetime.timedelta(days=10)
                                ).isoformat(timespec="seconds"),
                      client_date=(T0 - datetime.timedelta(days=10)
                                   ).date().isoformat())
        _stub_post(monkeypatch, [(500, None)])
        assert statsmod.flush_once()["action"] == "retry"
        assert len(_outbox()) == 3                        # 保留
        _stub_post(monkeypatch, [(204, None)])
        assert statsmod.flush_once()["action"] == "sent"
        assert _outbox() == []                            # 204 后删行

    def test_queue_cap_1000_and_30_days(self):
        """队列上限 1000 条 / 30 天，超限删最旧行。"""
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(1005, ts_iso=T0.isoformat(timespec="seconds"))
        old = (T0 - datetime.timedelta(days=40)).isoformat(timespec="seconds")
        _enqueue_bulk(3, ts_iso=old, client_date="2026-10-05")
        with statsmod._db() as conn:
            statsmod._prune(conn, T0)
        rows = _outbox()
        assert len(rows) == 1000                          # 超限删最旧
        ts_list = [r[5] for r in rows]
        assert old not in ts_list                         # 30 天前的已清
        assert rows[0][0] == 6                            # 容量超限删掉最旧 5 行（id 1~5）

    def test_body_oversize_splits_batch(self):
        """body 64KB 硬校验：超限拆批（缩半分批发出，服务端 413 兜底单条）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(10, version="v" * 40)
        monkeypatch.setattr(statsmod, "BODY_LIMIT", 260)  # 压小触发拆批
        calls = _stub_post(monkeypatch, [(204, None)] * 10)
        for _ in range(10):
            if not _outbox():
                break
            statsmod.flush_once()
        sizes = [len(body) for _, body in calls]
        assert calls and max(sizes) <= 260
        assert sum(len(json.loads(b)["events"]) for _, b in calls) == 10


# ---------------------------------------------------------------- §9-12 退避与响应处理

class TestBackoffAndResponses(StatsCase):
    def test_backoff_sequence_cap_and_reset(self):
        """5xx/网络错：1m→5m→30m→6h 封顶；204 成功清零后从 1m 重新起算。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(1)
        results = [(500, None)] * 5 + [(204, None)]
        _stub_post(monkeypatch, results)
        sleeps = []
        for _ in range(5):
            r = statsmod.flush_once()
            assert r["action"] == "retry"
            sleeps.append(r["sleep"])
        assert sleeps == [60.0, 300.0, 1800.0, 21600.0, 21600.0]
        assert statsmod.flush_once()["action"] == "sent"  # 成功清零
        assert len(_outbox()) == 0
        _enqueue_bulk(1)
        _stub_post(monkeypatch, [(500, None)])
        assert statsmod.flush_once()["sleep"] == 60.0     # 归零后从 1m 起

    def test_network_error_keeps_rows_and_retries(self):
        monkeypatch = self._mp
        import urllib.error as ue
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(2)
        _stub_post(monkeypatch, [ue.URLError("net down")])
        r = statsmod.flush_once()
        assert r["action"] == "retry" and r["sleep"] == 60.0
        assert len(_outbox()) == 2

    def test_400_drops_batch_no_retry(self):
        """400 永久错：丢弃本批不重试，stderr 记因（不落 hash/机器码）。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_host_code", lambda: "SECRET-RAW-CODE")
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(3)
        calls = _stub_post(monkeypatch, [(400, None)])
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r = statsmod.flush_once()
        assert r["action"] == "dropped" and r["status"] == 400
        assert _outbox() == []                            # 本批已丢弃
        assert len(calls) == 1                            # 不重试
        err = err_buf.getvalue()
        assert "HTTP 400" in err                          # 记因
        assert "SECRET-RAW-CODE" not in err
        assert not re.search(r"[0-9a-f]{32}", err)        # 无 hash 泄漏

    def test_413_drops_batch(self):
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(1)
        _stub_post(monkeypatch, [(413, None)])
        r = statsmod.flush_once()
        assert r["action"] == "dropped" and r["status"] == 413
        assert _outbox() == []

    def test_429_respects_retry_after(self):
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        _enqueue_bulk(1)
        calls = _stub_post(monkeypatch, [(429, 42.0), (204, None), (429, None)])
        r = statsmod.flush_once()
        assert r["action"] == "retry" and r["status"] == 429
        assert r["sleep"] == 42.0                         # 尊重 Retry-After
        assert len(_outbox()) == 1                        # 保留本批
        assert statsmod.flush_once()["action"] == "sent"  # 退避归零
        _enqueue_bulk(1)
        r = statsmod.flush_once()                         # 缺省退避序列（1m 起）
        assert r["status"] == 429 and r["sleep"] == 60.0
        assert len(_outbox()) == 1                        # 429 保留本批
        assert len(calls) == 3

    def test_retry_after_header_parsing(self):
        assert statsmod._retry_after({"Retry-After": "42"}) == 42.0
        assert statsmod._retry_after({"Retry-After": "999999"}) == 21600.0
        assert statsmod._retry_after({"Retry-After": "abc"}) is None
        assert statsmod._retry_after(None) is None


# ---------------------------------------------------------------- §9-10 老 config 兼容

class TestConfigCompat(StatsCase):
    def test_old_config_merges_stats_defaults(self):
        """无 stats 块的老 config → 深合并出默认（默认关=零上报，行为零变化）。"""
        tmp_path = self.tmp_path
        (tmp_path / "config.json").write_text(
            json.dumps({"default_backend": "auto"}), encoding="utf-8")
        cfg = cfgmod.load()
        assert cfg["stats"] == {"enabled": False, "endpoint": ""}
        assert cfg["default_backend"] == "auto"           # 原有键不受影响

    def test_default_config_endpoint_empty(self):
        """隐私红线：DEFAULT_CONFIG endpoint 为空串（源码零域名）。"""
        assert cfgmod.DEFAULT_CONFIG["stats"] == {"enabled": False,
                                                  "endpoint": ""}

    def test_put_config_segment_merge_keeps_endpoint(self):
        """PUT /api/config 分段合并：只发 enabled 不冲 endpoint（§3.3/§7.1）。"""
        monkeypatch = self._mp
        _save_cfg(False, ENDPOINT)
        monkeypatch.setattr(statsmod, "kick", lambda: None)
        monkeypatch.setattr(statsmod, "_post", _forbidden_post)
        out = mainmod.config_put(mainmod.ConfigPut(
            config={"stats": {"enabled": True}}))
        assert out["stats"]["enabled"] is True
        assert out["stats"]["endpoint"] == ENDPOINT
        assert cfgmod.load()["stats"]["endpoint"] == ENDPOINT


# ---------------------------------------------------------------- §7.1 开关开启动作

class TestToggleOn(StatsCase):
    def test_toggle_on_syncs_and_counts_open(self):
        """关→开：触发 sync 补计 install/update 并视同一次打开（open 跨日检查）。"""
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        monkeypatch.setattr(statsmod, "VERSION", "1.0.0")
        _save_cfg(True, ENDPOINT)
        statsmod._sync()                                  # 建 meta，install
        _save_cfg(False, ENDPOINT)
        monkeypatch.setattr(statsmod, "VERSION", "1.1.0")
        kicked = _save_and_toggle_on(monkeypatch)
        assert kicked                                     # 唤醒上报
        types = [r[1] for r in _outbox()]
        assert types == ["install", "update", "open"]     # 补计 update + 视同打开
        upd = [r for r in _outbox() if r[1] == "update"][0]
        assert upd[2] == "1.1.0" and upd[3] == "1.0.0"

    def test_config_changed_endpoint_only_kicks(self):
        """endpoint 补上（enabled 不变）→ 唤醒上报线程（积压行可补报）。"""
        monkeypatch = self._mp
        kicked = []
        monkeypatch.setattr(statsmod, "kick", lambda: kicked.append(True))
        statsmod.on_config_changed({"enabled": True, "endpoint": ""},
                                   {"enabled": True, "endpoint": ENDPOINT})
        assert len(kicked) == 1
        statsmod.on_config_changed({"enabled": True, "endpoint": ENDPOINT},
                                   {"enabled": True, "endpoint": ENDPOINT})
        assert len(kicked) == 1                           # 无变化不唤醒


class TestHookWiring(StatsCase):
    def test_backends_route_triggers_open_hook(self):
        """open 埋点挂在 GET /api/backends 处理开头（后端路由，幂等）。"""
        monkeypatch = self._mp
        calls = []
        monkeypatch.setattr(statsmod, "on_manage_page_open",
                            lambda: calls.append(True))

        class FakeMgr:
            cfg = {}

            def backends(self):
                return {}

        monkeypatch.setattr(mainmod, "_manager", lambda: FakeMgr())
        mainmod.backends_status()
        assert calls == [True]

    def test_startup_hook_no_open(self):
        monkeypatch = self._mp
        monkeypatch.setattr(statsmod, "_now", lambda: T0)
        _save_cfg(True, ENDPOINT)
        mainmod._stats_start()
        assert [r[1] for r in _outbox()] == ["install"]   # startup 不判 open


class TestWorkerLifecycle(StatsCase):
    def test_worker_daemon_and_single(self):
        """上报线程：daemon、幂等单实例、入队唤醒后 FIFO 发出（204 删行）。"""
        monkeypatch = self._mp
        _save_cfg(True, ENDPOINT)
        monkeypatch.setattr(statsmod, "kick", REAL_KICK)   # 恢复真身
        _stub_post(monkeypatch, [(204, None)] * 5)
        statsmod.on_toggle_on()                            # sync 入队 + kick 拉线程
        import time
        deadline = time.monotonic() + 5
        while _outbox() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _outbox() == []                             # 已被线程发出并删除
        w1 = statsmod._worker
        assert w1 is not None and w1.daemon and w1.is_alive()
        statsmod._ensure_worker()
        assert statsmod._worker is w1                      # 不重复拉起
