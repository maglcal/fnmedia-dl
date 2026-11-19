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

# 防误删（Y28）：--stage-only 指定的目录同样必须位于 .build/ 之下，
# 杜绝 rm -rf 打到任意路径（dev 工具误用风险）
case "${STAGE}/" in
  "${FPK_DIR}/.build/"*) : ;;
  *) echo "ERROR: stage 目录必须位于 ${FPK_DIR}/.build/ 之下（拒绝 rm -rf）：${STAGE}" >&2; exit 1 ;;
esac

rm -rf "${STAGE}"
mkdir -p "${STAGE}/app/repo" "${STAGE}/app/ui"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
    --exclude='.git/' --exclude='dist/' --exclude='logs/' \
    --exclude='state/' --exclude='packaging/fpk/.build/' \
    --exclude='.tools/' --exclude='.playwright-cli/' --exclude='*.fpk' --exclude='.DS_Store' \
    --exclude='tests/' --exclude='.workbuddy/' \
    "${REPO_ROOT}/" "${STAGE}/app/repo/"
else
  # 无 rsync 的环境（如 Windows Git Bash）：用 Python 等价复制
  "${PYTHON:-python3}" - "${REPO_ROOT}" "${STAGE}/app/repo" <<'PYSTAGE'
import shutil, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
EXCLUDE_DIRS = {".venv", "__pycache__", ".git", "dist", "logs", "state",
                ".build", ".tools", ".playwright-cli", "tests", "node_modules",
                ".workbuddy"}
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

# ---- 强制 LF：Windows 打包环境会把 CRLF 混进 shell 脚本，导致 bash \ 续行断裂 ----
PYBIN="${PYTHON:-python3}"; command -v "${PYBIN}" >/dev/null 2>&1 || PYBIN=python
"${PYBIN}" - "${STAGE}" <<'PYCRLF'
import sys
from pathlib import Path
root = Path(sys.argv[1])
TEXT_EXT = {'.sh', '.py', '.html', '.css', '.js', '.md', '.json', '.txt', '.in', '.yml', '.yaml', '.toml', '.cfg'}
count = 0
for f in root.rglob('*'):
    if not f.is_file():
        continue
    if f.suffix.lower() not in TEXT_EXT:
        # 无扩展名文本（wizard/install、config/resource、ui/config、LICENSE、
        # cmd/* 等）用 UTF-8 可解码性判定（二进制误入 cmd/ 不再被字节级破坏）；
        # 二进制（wheel/PNG）均带扩展名不会进来
        try:
            f.read_bytes().decode('utf-8')
        except (UnicodeDecodeError, OSError):
            continue
    data = f.read_bytes()
    if b'\r\n' in data or (b'\r' in data and b'\n' in data):
        f.write_bytes(data.replace(b'\r\n', b'\n').replace(b'\r', b'\n'))
        count += 1
print(f"[fpk] CRLF 清理完成（{count} 个文件）")
PYCRLF

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
# changelog 从随包 CHANGELOG.md 提取当前版本段落单行化（≤400 字符，
# 尾部恒带「最近更新」概括——截断只砍正文不砍尾），提取不到回退 releases
# 链接——内嵌真实修改项，不再只放一个链接。
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
    # 支持 v0.2.0 / main-v0.2.0 / [0.2.0]（keep-a-changelog）/ 标题带冒号摘要
    m = re.match(r"^##\s+\**\s*(?:main-)?v?\[?([0-9][^\]\s*]*)(?:\])?", line)
    if m:
        if cur is not None:
            sections.append((cur, "\n".join(lines).strip()))
        cur, lines = m.group(1).rstrip(":："), []
    elif cur is not None:
        lines.append(line)
if cur is not None:
    sections.append((cur, "\n".join(lines).strip()))
# 断言提取命中：CHANGELOG 非空却一段都没解析出来 = 标题格式变了，
# 必须 fail-loud，不许静默回退「首段当变更」
if text.strip() and not sections:
    raise SystemExit("[fpk] CHANGELOG 标题无法解析（未提取到任何版本段），请检查格式")
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
# 常驻「最近更新」概括块（## 最近更新，置于版本段之前）压缩成单行尾附
# （更新信息规范：更新内容展示尾部必带该段）；版本分段解析天然忽略它
recent_lines, grab = [], False
for line in text.splitlines():
    if line.startswith("## "):
        grab = line[3:].strip() == "最近更新"
        continue
    if grab and line.strip():
        recent_lines.append(line.strip())
recent = "；".join(x.lstrip("- ").strip() for x in recent_lines)
recent = re.sub(r"\*\*", "", recent)
recent = re.sub(r"\s+", " ", recent).strip()
if recent:
    tail = "最近更新：" + recent
    if len(tail) > 150:
        tail = tail[:150] + "…"
    if one:
        budget = 400 - len(tail) - 1        # 截断砍正文、保尾部「最近更新」
        if len(one) > budget:
            one = one[:max(0, budget)] + "…"
        one = one + "；" + tail
    else:
        one = tail
elif len(one) > 400:
    one = one[:400] + "…"
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

# fnpack 官方 CDN 产物钉死哈希（Y30 供应链信任）：换版本必须同步更新；
# 空 = 该架构无已钉死产物 → 禁止自动下载，须 --fnpack 显式提供
FNPACK_SHA256_AMD64="54b97fa7b70968c4d05c79840f5daeff508957d0bb2062fdb0376d00d9615c93"
FNPACK_SHA256_ARM64=""

if [ -z "${FNPACK_BIN}" ]; then
  if command -v fnpack >/dev/null 2>&1; then FNPACK_BIN="$(command -v fnpack)"; else
    case "$(uname -m)" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) echo "不支持的架构" >&2; exit 1;; esac
    case "${A}" in amd64) EXP="${FNPACK_SHA256_AMD64}";; *) EXP="${FNPACK_SHA256_ARM64}";; esac
    [ -n "${EXP}" ] || { echo "ERROR: 架构 ${A} 无已钉死哈希的 fnpack 产物，请用 --fnpack 显式指定" >&2; exit 1; }
    FNPACK_BIN="${REPO_ROOT}/.tools/fnpack"; mkdir -p "$(dirname "${FNPACK_BIN}")"
    fetch=1
    if [ -f "${FNPACK_BIN}" ] && [ "$(sha256sum "${FNPACK_BIN}" | awk '{print $1}')" = "${EXP}" ]; then
      fetch=0   # 缓存产物也验一遍，验过才复用
    fi
    if [ "${fetch}" = 1 ]; then
      curl -fsSL -o "${FNPACK_BIN}" "https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-${A}"
      GOT="$(sha256sum "${FNPACK_BIN}" | awk '{print $1}')"
      if [ "${GOT}" != "${EXP}" ]; then
        rm -f "${FNPACK_BIN}"
        echo "ERROR: fnpack 下载 SHA256 不符（${GOT}），已删除，拒绝出包" >&2; exit 1
      fi
    fi
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
