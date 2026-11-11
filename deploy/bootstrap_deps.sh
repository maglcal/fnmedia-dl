#!/usr/bin/env bash
# fnmedia-dl 运行时依赖引导（被 install_callback / upgrade_callback 复用）。
#
# 目标：在“干净、可能无法直连 PyPI”的fnOS NAS 上高成功率地准备 .venv。
#   1) 创建独立 venv（系统 python3-venv 缺失时尝试 virtualenv / 自带 ensurepip 兜底）；
#   2) 优先用随包内置的【离线 wheelhouse】安装（无需联网、无需编译）；
#   3) 离线不全时，按国内镜像 → 官方源顺序在线兜底；
#   4) 安装后做真实 import 自检；核心服务依赖不齐全才判定失败。
#
# 环境变量：
#   FNDL_REQUIRE_NET=1   跳过离线、强制在线（排障用）
#   FNDL_PIP_INDEX=URL   追加自定义 pip 源
#   FNDL_LOG=路径        日志文件（由 _common 提供默认值）
set -uo pipefail

LOG="${FNDL_LOG:-/tmp/fnmedia-dl.log}"
# 双日志：主日志 + /tmp 兜底（FNDL_LOG 不可写或未导出时仍留痕）
TRACE="/tmp/fnmedia-dl-bootstrap-trace.log"
log(){ echo "[deps] $*" >> "$LOG" 2>&1 || true; echo "[deps] $*" >> "$TRACE" 2>&1 || true; }

# ---- 诊断头：无论成败都在日志开头留痕，下次失败一眼定案 ----
log "===== 依赖引导开始 $(date '+%F %T') ====="
log "脚本路径: ${BASH_SOURCE[0]}"
log "工作目录: $(pwd)"
log "系统: $(uname -srm 2>/dev/null || echo unknown)"
log "python3: $(command -v python3 2>/dev/null || echo 'NOT FOUND') | $(python3 -V 2>&1 || echo '无版本信息')"
log "FNDL_LOG=${FNDL_LOG:-<未设置>} LOG=${LOG}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "无法进入应用目录" >&2; exit 1; }
VENV="$ROOT/.venv"
PYBIN="$VENV/bin/python"
PIP="$VENV/bin/pip"

# 核心服务启动【强依赖】
CORE_PKGS=(fastapi uvicorn httpx)

PIP_MIRRORS=(
  "https://pypi.tuna.tsinghua.edu.cn/simple"
  "https://mirrors.aliyun.com/pypi/simple"
  "https://mirror.nju.edu.cn/pypi/web/simple"
  "https://pypi.mirrors.ustc.edu.cn/simple"
  "https://pypi.org/simple"
)
[ -n "${FNDL_PIP_INDEX:-}" ] && PIP_MIRRORS=("$FNDL_PIP_INDEX" "${PIP_MIRRORS[@]}")

# ---------- 1. venv ----------
# 已有可用 venv 且核心包齐全：直接通过（升级场景不重复安装，秒退）。
if [ -x "$PYBIN" ] && "$PYBIN" -c 'import fastapi,uvicorn,httpx' >/dev/null 2>&1; then
  log "核心依赖已就绪，跳过安装"
  exit 0
fi

if [ ! -x "$PYBIN" ]; then
  log "创建虚拟环境 $VENV (python3 -> $(command -v python3) $(python3 -V 2>&1))"
  if ! python3 -m venv "$VENV" >> "$LOG" 2>&1; then
    log "python3 -m venv 失败，尝试 ensurepip / virtualenv 兜底"
    python3 -m ensurepip --upgrade >> "$LOG" 2>&1 || true
    if python3 -m venv "$VENV" >> "$LOG" 2>&1; then :; elif command -v virtualenv >/dev/null 2>&1; then
      virtualenv -p python3 "$VENV" >> "$LOG" 2>&1
    else
      python3 -m pip install --user virtualenv >> "$LOG" 2>&1 || true
      python3 -m virtualenv -p python3 "$VENV" >> "$LOG" 2>&1 \
        || { log "FAIL exit=2 venv 创建全部失败"; echo "创建 venv 失败：请在应用中心或终端安装 python3-venv / python3-pip" >&2; exit 2; }
    fi
  fi
fi
[ -x "$PYBIN" ] || { log "FAIL exit=2 venv python 不可执行: $PYBIN"; echo "虚拟环境 python 不可用" >&2; exit 2; }

# pip 基础参数：安静、不超时太死、必要时信任国内镜像 http/自签
PIP_BASE=(--disable-pip-version-check --retries 2 --timeout 25)

WHEELHOUSE="$ROOT/wheelhouse"

# ---- pip 自更新兜底：旧 pip（<21）可能无法解析新版 wheel 元数据 ----
# 若 wheelhouse 带了新版 pip wheel，先离线自更新，避免后续安装因 pip 太旧而失败。
if [ "${FNDL_REQUIRE_NET:-0}" != "1" ] && [ -d "$WHEELHOUSE" ]; then
  PIP_WHEEL="$(ls "$WHEELHOUSE"/pip-*.whl 2>/dev/null | head -n1)"
  if [ -n "$PIP_WHEEL" ]; then
    log "检测到内置 pip wheel，尝试离线自更新 pip（兜旧版 pip 元数据解析）"
    "$PIP" install "${PIP_BASE[@]}" --no-index --find-links "$WHEELHOUSE" --upgrade pip >> "$LOG" 2>&1 \
      && log "pip 自更新成功（$("$PIP" --version 2>&1 | head -n1)）" \
      || log "pip 自更新失败（不影响后续，继续尝试安装）"
  fi
fi

# ---------- 2. 离线 wheelhouse 优先 ----------
offline_ok=0
if [ "${FNDL_REQUIRE_NET:-0}" != "1" ] && [ -d "$WHEELHOUSE" ]; then
  if ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
    log "尝试离线安装（内置 wheelhouse：$(ls "$WHEELHOUSE"/*.whl | wc -l | tr -d ' ') 个 wheel）"
    if "$PIP" install "${PIP_BASE[@]}" --no-index --find-links "$WHEELHOUSE" \
         "${CORE_PKGS[@]}" >> "$LOG" 2>&1; then
      offline_ok=1
      log "离线安装成功"
    else
      log "离线 wheelhouse 未覆盖当前平台/Python 版本，转在线兜底"
    fi
  fi
fi

# ---------- 3. 在线多镜像兜底 ----------
if [ "$offline_ok" != "1" ]; then
  installed=0
  for idx in "${PIP_MIRRORS[@]}"; do
    log "在线安装尝试：$idx"
    if "$PIP" install "${PIP_BASE[@]}" \
         --index-url "$idx" --trusted-host "$(echo "$idx" | awk -F/ '{print $3}')" \
         "${CORE_PKGS[@]}" >> "$LOG" 2>&1; then
      installed=1; log "在线安装成功（$idx）"; break
    fi
    log "源失败：$idx"
  done
  [ "$installed" = "1" ] || { log "FAIL exit=3 在线安装全部镜像失败（离线也未成功）"; echo "依赖在线安装全部失败（已尝试内置离线包与多个镜像）" >&2; exit 3; }
fi

# ---------- 4. 真实 import 自检 ----------
if ! "$PYBIN" - <<'PY' >> "$LOG" 2>&1
import fastapi, uvicorn, httpx, starlette, pydantic
print("[deps] import OK:",
      "fastapi", fastapi.__version__,
      "uvicorn", uvicorn.__version__,
      "httpx", httpx.__version__)
PY
then
  log "FAIL exit=4 import 自检失败（平台/Python 版本不兼容）"
  echo "依赖已安装但自检 import 失败（平台/Python 版本不兼容）" >&2
  exit 4
fi

log "依赖就绪"
exit 0
