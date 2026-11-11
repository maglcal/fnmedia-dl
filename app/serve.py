#!/usr/bin/env python3
"""fnmedia-dl 服务入口：uvicorn（Unix socket）+ fnOS 桌面网关线程。

生产（fnOS）：
  .venv/bin/python serve.py
    - uvicorn 监听 state/app.sock（私有 socket，不占端口）
    - 桌面网关在应用根目录提供 fnmedia-dl.sock，供应用中心 iframe 同源访问
开发（任意平台）：
  python serve.py --http 127.0.0.1:8787   直接 TCP 监听，不开网关
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))

STATE_DIR = REPO_ROOT / "state"
APP_SOCK = STATE_DIR / "app.sock"


def app_root() -> Path:
    """FPK 安装形态：repo 与 ui 同级 → 应用根为 repo 的父目录。"""
    parent = REPO_ROOT.parent
    if (parent / "ui").is_dir() and REPO_ROOT.name == "repo":
        return parent
    return REPO_ROOT


# 注意：gateway_socket_path 的入参是 repo 目录（内部判定 FPK 形态），
# 不要传应用根目录，否则会误判并把 socket 放到 /run 下导致 502。


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", default="", help="开发用：TCP 监听 host:port")
    args = parser.parse_args()

    import uvicorn

    if args.http:
        host, _, port = args.http.rpartition(":")
        uvicorn.run("main:app", app_dir=str(APP_DIR),
                    host=host or "127.0.0.1", port=int(port or 8787))
        return 0

    # ---- 生产形态：UDS + 桌面网关 ----
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    APP_SOCK.unlink(missing_ok=True)

    import webui_gateway
    gw_sock = webui_gateway.gateway_socket_path(REPO_ROOT)
    threading.Thread(target=webui_gateway.serve,
                     args=(gw_sock, APP_SOCK), daemon=True).start()

    # 私有 socket 收紧为 600（仅属主可读写）：uvicorn 绑定后立即生效，
    # 守护线程轮询补位（绑定完成即可收紧，无需等待服务就绪）。
    def _tighten_sock():
        import time
        for _ in range(200):
            if APP_SOCK.exists():
                try:
                    os.chmod(APP_SOCK, 0o600)
                except OSError:
                    pass
                return
            time.sleep(0.05)
    threading.Thread(target=_tighten_sock, daemon=True).start()

    uvicorn.run("main:app", app_dir=str(APP_DIR), uds=str(APP_SOCK),
                log_level="info", access_log=False)
    APP_SOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    os.chdir(APP_DIR)
    raise SystemExit(main())
