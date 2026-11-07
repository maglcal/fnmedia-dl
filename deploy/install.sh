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

# 专用运行用户（最小权限）：fnOS 按 config/privilege 创建；手动部署时补建系统用户。
APP_USER="fnmedia-dl"; APP_GROUP="fnmedia-dl"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  groupadd --system "$APP_GROUP" 2>/dev/null || true
  useradd --system --gid "$APP_GROUP" --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$APP_USER" 2>/dev/null || true
fi
# Docker 版迅雷一键部署需要访问容器引擎（docker 组管 socket 权限）。
# 组不存在（未装 Docker）时静默跳过，不影响安装；后续装了 Docker 重跑安装/升级即可生效。
if id -u "$APP_USER" >/dev/null 2>&1 && getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$APP_USER" 2>/dev/null || true
fi

# 渲染绝对路径（用户不存在则去掉 User=/Group=，root 兜底）
sed -e "s#@ROOT@#$ROOT#g" "$SRC_IN" > "$DST"
if id -u "$APP_USER" >/dev/null 2>&1; then
  # 服务进程需写 state/、.update-backup/、重建 .venv，并在应用根目录建网关 socket
  chown -R "$APP_USER:$APP_GROUP" "$ROOT"
  # FPK 形态（repo 与 ui 同级）：网关 socket 落应用根目录，放开该目录写权限
  if [ "$(basename "$ROOT")" = "repo" ] && [ -d "$(dirname "$ROOT")/ui" ]; then
    chown "$APP_USER:$APP_GROUP" "$(dirname "$ROOT")" 2>/dev/null || true
  else
    mkdir -p /run/fnmedia-dl 2>/dev/null || true
    chown "$APP_USER:$APP_GROUP" /run/fnmedia-dl 2>/dev/null || true
  fi
else
  echo "WARN: 用户 $APP_USER 不可用，服务以 root 运行（不符合最小权限）" >&2
  sed -i '/^User=/d;/^Group=/d' "$DST"
fi
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
