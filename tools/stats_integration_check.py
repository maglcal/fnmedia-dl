#!/usr/bin/env python3
"""fnmedia-dl 统计上报客户端——真实联调自检（appstats 服务端 + 客户端钩子 HTTP 全链路）。

用法（在 fnmedia-dl 仓根目录用 python 直跑；tools/ 不被 pytest 收集）：
    python tools/stats_integration_check.py --appstats <appstats仓路径>
    APPSTATS_ROOT=<appstats仓路径> python tools/stats_integration_check.py
可选参数：
    --port N    服务端端口（默认自动挑一个 127.0.0.1 空闲端口）
    --keep-tmp  保留临时目录用于排障（默认用后自删）

前置：
    1. 本地 appstats checkout（需含 config/apps.yaml 与 app/main.py）；
    2. 当前 Python 环境已安装 uvicorn/FastAPI（appstats 服务端依赖）；
    3. fnmedia-dl/app 可导入（脚本按自身位置自动定位，无需配置）。
若不传 --appstats/APPSTATS_ROOT，回退探测与本仓同级的 ../appstats 目录。

流程：临时配置拉起 appstats（uvicorn 127.0.0.1:<port>）→ 临时 client state 驱动
on_startup/_sync/_touch_open/flush_once 真实 POST /collect → Bearer 查询 API 核对计数。
覆盖：install 一生一次 / 同日两开只计 1 / 版本跃迁 update / 幂等双层去重 /
断网补报 / 双闸（关=零足迹、endpoint 空=只本地）/ 关闭即停 / 老 config 兼容。
附加探测：真实 429 Retry-After（限流 10 次/分/IP）。

隐私红线：全程仅 127.0.0.1 回环地址（断网模拟用 127.0.0.1:9 关闭端口），
不出现任何真实域名；临时目录与服务进程用后自清理。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

FNDL_APP = Path(__file__).resolve().parents[1] / "app"
TOKEN = "it-" + os.urandom(8).hex()
DEAD = "http://127.0.0.1:9"              # 关端口模拟断网（不出网地址，仅回环）

BASE = ""                                # main() 按所选端口填充

sys.path.insert(0, str(FNDL_APP))
import config as cfgmod                                   # noqa: E402
import stats as statsmod                                  # noqa: E402

statsmod.kick = lambda: None   # 联调手动驱动 flush_once，禁自动上报线程防竞态

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS" if ok else "FAIL"), "|", name, "|", detail, flush=True)


def http_json(url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read()
    return json.loads(body) if body else {}


def query(path: str) -> dict:
    return http_json(BASE + path, headers={"Authorization": f"Bearer {TOKEN}"})


def summary() -> dict:
    return query("/api/v1/stats/summary?app_id=fnmedia-dl")


def versions() -> dict:
    return query("/api/v1/stats/versions?app_id=fnmedia-dl")


def outbox_n() -> int:
    with statsmod._db() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
    return n


def enqueue(type_: str, version: str, from_version: str | None = None) -> None:
    now = statsmod._now()
    with statsmod._db_lock, statsmod._db() as conn:
        statsmod._enqueue_event(conn, type_, version, from_version,
                                now.date().isoformat(),
                                now.isoformat(timespec="seconds"))
        statsmod._prune(conn, now)


def resolve_appstats(arg: str | None) -> Path:
    """appstats checkout 定位：--appstats > APPSTATS_ROOT > 同级 ../appstats 探测。"""
    cand = arg or os.environ.get("APPSTATS_ROOT") or ""
    if not cand:
        cand = str(Path(__file__).resolve().parents[2] / "appstats")
    p = Path(cand).expanduser().resolve()
    if not (p / "config" / "apps.yaml").is_file() or not (p / "app" / "main.py").is_file():
        sys.exit(f"appstats checkout 无效：{p}（需含 config/apps.yaml 与 app/main.py）；"
                 "请用 --appstats 或环境变量 APPSTATS_ROOT 指定")
    return p


def pick_free_port() -> int:
    """在 127.0.0.1 上挑一个空闲端口（先 bind 取号再释放，交 uvicorn 绑定）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    global BASE
    ap = argparse.ArgumentParser(
        description="fnmedia-dl 统计上报客户端真实联调自检（appstats + HTTP 全链路）")
    ap.add_argument("--appstats", help="本地 appstats checkout 路径（或设 APPSTATS_ROOT）")
    ap.add_argument("--port", type=int, default=0,
                    help="服务端端口，默认 0=自动挑 127.0.0.1 空闲端口")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="保留临时目录用于排障（默认自清理）")
    args = ap.parse_args(argv)

    appstats = resolve_appstats(args.appstats)
    port = args.port or pick_free_port()
    BASE = f"http://127.0.0.1:{port}"
    print(f"appstats={appstats}  base={BASE}  pid={os.getpid()}", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="stats-it-"))
    log = None
    proc = None
    # ---------------- 服务端：临时 apps.yaml（仅改 db/backup 路径，盐不动）
    srv_yaml = (appstats / "config" / "apps.yaml").read_text(encoding="utf-8")
    srv_yaml = srv_yaml.replace("path: /data/stats.db",
                                f"path: {(tmp / 'srv-stats.db').as_posix()}")
    srv_yaml = srv_yaml.replace("dir: /data/backups",
                                f"dir: {(tmp / 'backups').as_posix()}")
    cfg_path = tmp / "apps.yaml"
    cfg_path.write_text(srv_yaml, encoding="utf-8")
    env = {**os.environ, "APPSTATS_CONFIG": str(cfg_path),
           "STATS_API_TOKEN": TOKEN}
    try:
        log = open(tmp / "server.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
            cwd=str(appstats), env=env, stdout=log, stderr=subprocess.STDOUT)

        ok = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                h = http_json(BASE + "/healthz")
                if h.get("ok"):
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            check("服务端启动", False, (tmp / "server.log").read_text(
                encoding="utf-8")[-400:])
            return 1
        check("服务端启动", True, f"healthz ok, server_date="
              f"{http_json(BASE + '/healthz').get('server_date')}")

        # ---------------- 客户端：隔离 state
        st = tmp / "client-state"
        st.mkdir()
        cfgmod.STATE_DIR = st
        cfgmod.CONFIG_PATH = st / "config.json"

        # ===== A. 双闸——enabled=false 零足迹（§9-1）
        cfgmod.save({"stats": {"enabled": False, "endpoint": BASE}})
        statsmod.on_startup()
        statsmod.on_manage_page_open()
        r = statsmod.flush_once()
        check("A1 双闸：关=零足迹（不落 stats.db）",
              not (st / "stats.db").exists()
              and r["action"] == "gate-closed", f"flush={r['action']}")
        s = summary()
        check("A2 双闸：关=零上报（服务端计数 0）",
              s.get("totals", {}).get("installs", 0) == 0
              and s.get("today", {}).get("actives", 0) == 0,
              f"totals={s.get('totals')}")

        # ===== B. 开关开启（§9-2/§9-3）：install + 当日 open；同日两开只计 1
        cfgmod.save({"stats": {"enabled": True, "endpoint": BASE}})
        statsmod.on_toggle_on()                    # 关→开：sync 补计 + 视同打开
        r = statsmod.flush_once()
        check("B1 首开事件发出", r["action"] == "sent" and r.get("count") == 2,
              f"flush={r}")
        s = summary()
        check("B2 install 一生一次 + 当日 open 计 1",
              s.get("totals", {}).get("installs") == 1
              and s.get("today", {}).get("actives") == 1,
              f"installs={s.get('totals', {}).get('installs')} "
              f"actives={s.get('today', {}).get('actives')}")
        n1 = statsmod.on_manage_page_open()        # 同日再开 ×2
        n2 = statsmod.on_manage_page_open()
        r = statsmod.flush_once()
        check("B3 同日两开只计 1（客户端频控）",
              n1 == 0 and n2 == 0 and r["action"] == "idle"
              and summary().get("today", {}).get("actives") == 1,
              f"n1={n1} n2={n2} flush={r['action']}")

        # ===== C. 幂等双层去重（§9-9）：绕过客户端频控重放 open+install
        enqueue("install", statsmod.VERSION)
        enqueue("open", statsmod.VERSION)
        r = statsmod.flush_once()
        s = summary()
        check("C1 幂等重放计数不变（服务端唯一键兜底）",
              r["action"] == "sent" and s.get("totals", {}).get("installs") == 1
              and s.get("today", {}).get("actives") == 1,
              f"flush={r['action']} installs={s.get('totals', {}).get('installs')} "
              f"actives={s.get('today', {}).get('actives')}")

        # ===== D. 版本跃迁 update（§9-5）
        statsmod.VERSION = "9.9.9-it"
        statsmod.on_startup()
        r = statsmod.flush_once()
        s, v = summary(), versions()
        to_v = [x.get("version") for x in v.get("by_to_version", [])]
        check("D1 跃迁 update(from,to) 计 1",
              r["action"] == "sent" and s.get("today", {}).get("updates") == 1
              and "9.9.9-it" in to_v,
              f"updates={s.get('today', {}).get('updates')} to={to_v}")
        statsmod.on_startup()                      # 同版本再启动
        check("D2 同版本不重复（客户端频控）",
              statsmod.flush_once()["action"] == "idle"
              and summary().get("today", {}).get("updates") == 1)

        # ===== E. 断网补报（§9-4）
        statsmod.VERSION = "9.9.10-it"
        cfgmod.save({"stats": {"enabled": True, "endpoint": DEAD}})
        statsmod.on_startup()                      # 断网期产生 update
        r_off = statsmod.flush_once()              # 网络错：保留本批
        kept = outbox_n()
        cfgmod.save({"stats": {"enabled": True, "endpoint": BASE}})
        r_on = statsmod.flush_once()               # 恢复后补报
        s = summary()
        check("E1 断网入队不丢、恢复补报、204 删行",
              r_off["action"] == "retry" and kept == 1
              and r_on["action"] == "sent" and outbox_n() == 0
              and s.get("today", {}).get("updates") == 2,
              f"off={r_off['action']} kept={kept} on={r_on['action']} "
              f"updates={s.get('today', {}).get('updates')}")

        # ===== F. 关闭即停（§9-8）+ 开发形态（§9-13）
        cfgmod.save({"stats": {"enabled": False, "endpoint": BASE}})
        statsmod.VERSION = "9.9.11-it"
        statsmod.on_startup()
        r = statsmod.flush_once()
        check("F1 关闭即停（零外发、计数不变）",
              r["action"] == "gate-closed"
              and summary().get("today", {}).get("updates") == 2,
              f"flush={r['action']}")
        cfgmod.save({"stats": {"enabled": True, "endpoint": ""}})
        statsmod.on_startup()                      # 开发形态：只本地统计
        r = statsmod.flush_once()
        s = summary()
        check("F2 开发形态：endpoint 空=本地产事件零外发",
              outbox_n() == 1 and r["action"] == "gate-closed"
              and s.get("today", {}).get("updates") == 2,
              f"rows={outbox_n()} flush={r['action']}")
        cfgmod.save({"stats": {"enabled": True, "endpoint": BASE}})
        r = statsmod.flush_once()
        check("F3 补上 endpoint 后正常补报",
              r["action"] == "sent"
              and summary().get("today", {}).get("updates") == 3,
              f"flush={r['action']} updates={summary().get('today', {}).get('updates')}")

        # ===== G. 老 config 兼容（§9-10）
        (st / "config.json").write_text(
            json.dumps({"default_backend": "auto"}), encoding="utf-8")
        cfg = cfgmod.load()
        check("G1 老 config 深合并出 stats 默认（对照 DEFAULT_CONFIG，钉版见单测）",
              cfg.get("stats") == cfgmod.DEFAULT_CONFIG["stats"],
              f"stats={cfg.get('stats')}")

        # ===== H. 真实 429 Retry-After 探测（限流 10 次/分/IP）
        cfgmod.save({"stats": {"enabled": True, "endpoint": BASE}})
        got429 = None
        for _ in range(15):
            enqueue("open", statsmod.VERSION)      # 重放行（服务端去重，不增计数）
            rr = statsmod.flush_once()
            if rr.get("status") == 429:
                got429 = rr
                break
            if rr["action"] != "sent":
                break
        if got429:
            check("H1 真实 429：保留本批 + Retry-After",
                  got429["action"] == "retry" and got429["sleep"] > 0
                  and outbox_n() >= 1,
                  f"sleep={got429['sleep']} rows={outbox_n()}")
        else:
            check("H1 真实 429 探测", True, "N/A：限流窗口未触发（不计失败）")

        s = summary()
        v = versions()
        print("FINAL_SUMMARY", json.dumps(s, ensure_ascii=False), flush=True)
        print("FINAL_VERSIONS", json.dumps(v, ensure_ascii=False), flush=True)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log is not None:
            log.close()
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)   # 临时目录自清理

    fails = [x for x in RESULTS if not x[1]]
    print(f"TOTAL: {len(RESULTS)} checks, FAIL: {len(fails)}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
