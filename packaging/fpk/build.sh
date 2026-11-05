#!/usr/bin/env bash
# fnmedia-dl FPK 打包：组装骨架 + 项目主体 → fnpack build → dist/*.fpk
# 用法：packaging/fpk/build.sh [--stage-only DIR] [--fnpack /path] [--out dist]
set -euo pipefail

FPK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${FPK_DIR}/../.." && pwd)"
VERSION="$(head -n1 "${REPO_ROOT}/VERSION" | tr -d '[:space:]')"
[ -n "${VERSION}" ] || { echo "ERROR: 无法读取 VERSION" >&2; exit 1; }

FNPACK_BIN="${FNPACK:-}"; STAGE=""; STAGE_ONLY=0; OUT_DIR="${REPO_ROOT}/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --stage-only) STAGE_ONLY=1; STAGE="$2"; shift 2 ;;
    --fnpack) FNPACK_BIN="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done
[ -n "${STAGE}" ] || STAGE="${FPK_DIR}/.build/fpk"

rm -rf "${STAGE}"
mkdir -p "${STAGE}/app/repo" "${STAGE}/app/ui"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
    --exclude='.git/' --exclude='dist/' --exclude='logs/' \
    --exclude='state/' --exclude='packaging/fpk/.build/' \
    --exclude='.tools/' --exclude='*.fpk' --exclude='.DS_Store' \
    --exclude='tests/' \
    "${REPO_ROOT}/" "${STAGE}/app/repo/"
else
  # 无 rsync 的环境（如 Windows Git Bash）：用 Python 等价复制
  "${PYTHON:-python3}" - "${REPO_ROOT}" "${STAGE}/app/repo" <<'PYSTAGE'
import shutil, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "dist", "logs", "state",
                ".build", ".tools", "tests", "node_modules"}
EXCLUDE_NAMES = {".DS_Store"}

def ignore(directory, names):
    out = []
    d = Path(directory)
    for n in names:
        if n in EXCLUDE_NAMES:
            out.append(n); continue
        if (d / n).is_dir() and (n in EXCLUDE_DIRS or n.endswith(".fpk")):
            out.append(n); continue
        if n.endswith(".pyc") or n.endswith(".fpk"):
            out.append(n)
    return set(out)

shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
print("[fpk] stage 复制完成（python fallback）")
PYSTAGE
fi

MANIFEST_VER="$(echo "${VERSION}" | sed -E 's/([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
sed "s/@VERSION@/${MANIFEST_VER}/" "${FPK_DIR}/manifest.in" > "${STAGE}/manifest"
cp "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/" 2>/dev/null || true
cp -a "${FPK_DIR}/app/ui/." "${STAGE}/app/ui/"
cp -a "${FPK_DIR}/cmd" "${STAGE}/cmd"
cp -a "${FPK_DIR}/config" "${STAGE}/config"
# 安装向导（安装时展示免责声明，需用户同意）
if [ -d "${FPK_DIR}/wizard" ]; then
  mkdir -p "${STAGE}/wizard"
  cp -a "${FPK_DIR}/wizard/." "${STAGE}/wizard/"
fi
chmod +x "${STAGE}"/cmd/*

echo "[fpk] 组装完成: ${STAGE} (version=${VERSION})"
[ "${STAGE_ONLY}" -eq 1 ] && exit 0

if [ -z "${FNPACK_BIN}" ]; then
  if command -v fnpack >/dev/null 2>&1; then FNPACK_BIN="$(command -v fnpack)"; else
    case "$(uname -m)" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) echo "不支持的架构" >&2; exit 1;; esac
    FNPACK_BIN="${REPO_ROOT}/.tools/fnpack"; mkdir -p "$(dirname "${FNPACK_BIN}")"
    [ -x "${FNPACK_BIN}" ] || curl -fsSL -o "${FNPACK_BIN}" "https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-${A}"
    chmod +x "${FNPACK_BIN}"
  fi
fi
WORK="$(mktemp -d)"; trap 'rm -rf "${WORK}"' EXIT
( cd "${WORK}" && "${FNPACK_BIN}" build --directory "${STAGE}" )
BUILT="$(find "${WORK}" "${STAGE}" -maxdepth 2 -name '*.fpk' -print -quit 2>/dev/null || true)"
[ -n "${BUILT}" ] || { echo "ERROR: fnpack 未产出 .fpk" >&2; exit 1; }
mkdir -p "${OUT_DIR}"
FINAL="${OUT_DIR}/fnmedia-dl-${VERSION}.fpk"
mv "${BUILT}" "${FINAL}"
( cd "$(dirname "${FINAL}")" && sha256sum "$(basename "${FINAL}")" > "$(basename "${FINAL}").sha256" )
echo "[fpk] 打包完成: ${FINAL}"
