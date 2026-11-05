#!/usr/bin/env bash
# fnmedia-dl 卸载：停止并移除 systemd 单元。
# 用法：sudo bash deploy/uninstall.sh [--purge]
#   --purge 同时删除运行配置与任务记录（state/）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

systemctl stop fnmedia-dl 2>/dev/null || true
systemctl disable fnmedia-dl.service 2>/dev/null || true
rm -f /etc/systemd/system/fnmedia-dl.service
systemctl daemon-reload || true

# 清理可能残留的桌面网关 socket
for _root in "$ROOT" "$ROOT/.." ; do
  rm -f "${_root}/fnmedia-dl.sock" 2>/dev/null || true
done

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$ROOT/state"
  echo "[OK] 已删除运行配置与任务记录 $ROOT/state"
fi
echo "[OK] fnmedia-dl 已卸载"
