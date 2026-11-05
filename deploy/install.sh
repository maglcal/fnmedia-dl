#!/usr/bin/env bash
# fnmedia-dl 安装：渲染 systemd 单元（路径自适应）并启动服务。
# 用法：sudo bash deploy/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
SRC_IN="$ROOT/deploy/fnmedia-dl.service.in"
DST="/etc/systemd/system/fnmedia-dl.service"

if [ ! -x "$PY" ]; then
  echo "ERROR: 未找到虚拟环境 $PY；请先：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# 停掉可能存在的旧单元
systemctl stop fnmedia-dl 2>/dev/null || true

# 渲染绝对路径
sed -e "s#@ROOT@#$ROOT#g" "$SRC_IN" > "$DST"
chmod 0644 "$DST"
systemctl daemon-reload
systemctl enable fnmedia-dl.service
systemctl restart fnmedia-dl.service

sleep 4
if systemctl is-active --quiet fnmedia-dl; then
  echo "[OK] fnmedia-dl 已安装并运行（ROOT=$ROOT）"
  echo "[OK] 管理页：应用中心/桌面 打开「飞牛下载助手」"
  echo "[OK] 卸载：bash $ROOT/deploy/uninstall.sh"
else
  echo "ERROR: 服务未正常启动；journalctl -u fnmedia-dl -n 50" >&2
  exit 1
fi
