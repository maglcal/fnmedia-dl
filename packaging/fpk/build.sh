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
    --exclude='.tools/' --exclude='.playwright-cli/' --exclude='*.fpk' --exclude='.DS_Store' \
    --exclude='tests/' \
    "${REPO_ROOT}/" "${STAGE}/app/repo/"
else
  # 无 rsync 的环境（如 Windows Git Bash）：用 Python 等价复制
  "${PYTHON:-python3}" - "${REPO_ROOT}" "${STAGE}/app/repo" <<'PYSTAGE'
import shutil, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "dist", "logs", "state",
                ".build", ".tools", ".playwright-cli", "tests", "node_modules"}
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

# 版本号段数判定（用户定规范）：三段=正式发布版 main-vX.Y.Z，四段=分支测试版 vX.Y.Z.x
SEG_COUNT="$(echo "${VERSION}" | awk -F. '{print NF}')"
if [ "${SEG_COUNT}" -ge 4 ]; then
  RELEASE_TAG="v${VERSION}"        # 四段及以上：分支测试版（如 v0.1.36.1）
else
  RELEASE_TAG="main-v${VERSION}"   # 三段：正式发布版（如 main-v0.1.36）
fi
# manifest version 用完整版本号（支持四段分支号；fnOS 宽松接受，安装校验若拒则回退 0.1.36-1 形式）
MANIFEST_VER="${VERSION}"
# manifest 生成：@VERSION@/@TAG@/@CHANGELOG@ 一次性替换（Python 处理多字节与转义）。
# changelog 从随包 CHANGELOG.md 提取当前版本段落单行化（≤280 字符），
# 提取不到回退 releases 链接——内嵌真实修改项，不再只放一个链接。
PYBIN="${PYTHON:-python3}"; command -v "${PYBIN}" >/dev/null 2>&1 || PYBIN=python
"${PYBIN}" - "${FPK_DIR}/manifest.in" "${STAGE}/manifest" \
    "${MANIFEST_VER}" "${RELEASE_TAG}" "${REPO_ROOT}/CHANGELOG.md" <<'PYMANIFEST'
import re, sys
from pathlib import Path
src, dst, version, tag, changelog_path = sys.argv[1:6]
text = ""
try:
    text = Path(changelog_path).read_text(encoding="utf-8")
except OSError:
    pass
sections, cur, lines = [], None, []
for line in text.splitlines():
    m = re.match(r"^##\s+\**\s*(?:main-)?v?([0-9][^\s*]*)", line)
    if m:
        if cur is not None:
            sections.append((cur, "\n".join(lines).strip()))
        cur, lines = m.group(1), []
    elif cur is not None:
        lines.append(line)
if cur is not None:
    sections.append((cur, "\n".join(lines).strip()))
body = ""
for ver, b in sections:
    if ver == version:
        body = b
        break
if not body and sections:
    body = sections[0][1]
one = body.lstrip("- ").replace("\n- ", "；").replace("\n", " ")
one = re.sub(r"\*\*", "", one)
one = re.sub(r"\s+", " ", one).strip()
if len(one) > 280:
    one = one[:280] + "…"
if not one:
    one = "更新内容详见 https://github.com/maglcal/fnmedia-dl/releases/tag/" + tag
tpl = Path(src).read_text(encoding="utf-8")
tpl = (tpl.replace("@VERSION@", version)
          .replace("@TAG@", tag)
          .replace("@CHANGELOG@", one))
Path(dst).write_text(tpl, encoding="utf-8")
print("[fpk] manifest 生成完成（changelog %d 字符）" % len(one))
PYMANIFEST
# UI 资源版本戳（?v=@VERSION@）防浏览器旧缓存：与 manifest 同步替换
if [ -f "${STAGE}/app/repo/app/ui/index.html" ]; then
  sed -i "s/@VERSION@/${MANIFEST_VER}/g" "${STAGE}/app/repo/app/ui/index.html"
fi
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
# 发布标识：三段 main-vX.Y.Z（正式发布）/ 四段 vX.Y.Z.x（分支测试）；VERSION 文件存完整版本号
FINAL="${OUT_DIR}/fnmedia-dl-${RELEASE_TAG}.fpk"
mv "${BUILT}" "${FINAL}"
( cd "$(dirname "${FINAL}")" && sha256sum "$(basename "${FINAL}")" > "$(basename "${FINAL}").sha256" )
echo "[fpk] 打包完成: ${FINAL}"
