#!/usr/bin/env python3
"""应用内一键在线更新：下载官方 fpk → 解包 → 备份 → 白名单替换 → 重启服务。

流程（单例后台线程，进度经 progress() 轮询）：
  downloading → extracting → backing_up → replacing → [deps] → restarting → done
任一步失败：整体回滚到备份，状态置 error 并说明原因。

安全边界：
  - 只接受本仓库 GitHub Releases 下载地址（validate_asset_url）；
  - 只替换白名单条目（app/deploy/packaging/wheelhouse/VERSION 等），
    state/（配置、凭据、任务记录）与 .venv/ 永不触碰；
  - 替换前整份备份到 <repo>/.update-backup/，失败即回滚；
  - requirements.txt 变更时才重建运行环境（离线 wheelhouse 优先）。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_REPO = "maglcal/fnmedia-dl"
DOWNLOAD_URL_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"

# 替换白名单：随新版本整体替换。state/ 与 .venv/ 永不在列（凭据/任务记录/运行环境）。
REPLACE_ENTRIES = ("app", "deploy", "packaging", "wheelhouse",
                   "VERSION", "requirements.txt", "README.md", "LICENSE")

BACKUP_DIR = REPO_ROOT / ".update-backup"
SERVICE_NAME = "fnmedia-dl.service"


class UpdateError(RuntimeError):
    """更新失败（用户可见的中文原因）。"""


# ------------------------------------------------------------ 进度状态

_lock = threading.Lock()
_state: dict = {
    "stage": "idle", "percent": 0, "detail": "", "error": "",
    "from_version": "", "to_version": "", "done": False,
}
_thread: threading.Thread | None = None


def progress() -> dict:
    with _lock:
        return dict(_state)


def _set(stage: str | None = None, percent: int | None = None,
         detail: str | None = None, error: str | None = None,
         done: bool | None = None) -> None:
    with _lock:
        if stage is not None:
            _state["stage"] = stage
        if percent is not None:
            _state["percent"] = percent
        if detail is not None:
            _state["detail"] = detail
        if error is not None:
            _state["error"] = error
        if done is not None:
            _state["done"] = done


# ------------------------------------------------------------ 小工具

def _read_version(root: Path | None = None) -> str:
    vf = (root or REPO_ROOT) / "VERSION"
    try:
        return vf.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except OSError:
        return "0.0.0"


def validate_asset_url(url: str) -> bool:
    """只放行本仓库 GitHub Releases 的 .fpk 资产下载地址。"""
    return (isinstance(url, str)
            and url.startswith(DOWNLOAD_URL_PREFIX)
            and url.endswith(".fpk")
            and ".." not in url)


# ------------------------------------------------------------ 解包

def extract_repo_tree(fpk_path: Path, workdir: Path) -> Path:
    """解 fpk 两层包（外层 tar 内含 app.tgz，app.tgz 内含 repo/），
    返回新 repo 树路径并做结构校验。"""
    out = workdir / "extract"
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fpk_path, "r:*") as outer:
        member = next((m for m in outer.getmembers()
                       if m.isfile() and m.name.rsplit("/", 1)[-1] == "app.tgz"),
                      None)
        if member is None:
            raise UpdateError("安装包结构异常：缺少 app.tgz")
        inner_bytes = outer.extractfile(member).read()
    with tarfile.open(fileobj=io.BytesIO(inner_bytes), mode="r:*") as inner:
        members = [m for m in inner.getmembers()
                   if m.name.split("/")[0] == "repo" and ".." not in m.name]
        if not members:
            raise UpdateError("安装包结构异常：缺少 repo/ 目录")
        # 逐条解包并先建父目录（兼容缺失目录条目的 tar 布局）
        for m in members:
            target = out / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = inner.extractfile(m)
            if src is None:
                continue
            with open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
    repo = out / "repo"
    if not (repo / "VERSION").is_file() or not (repo / "app" / "main.py").is_file():
        raise UpdateError("安装包内容不完整（缺少 VERSION 或 app/main.py）")
    return repo


# ------------------------------------------------------------ 备份/替换/回滚

def backup_entries(entries: tuple[str, ...] = REPLACE_ENTRIES) -> Path:
    """把当前仓库白名单条目整份备份到 .update-backup/（仅保留最近一份）。"""
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    BACKUP_DIR.mkdir(parents=True)
    for name in entries:
        src = REPO_ROOT / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, BACKUP_DIR / name)
        else:
            shutil.copy2(src, BACKUP_DIR / name)
    return BACKUP_DIR


def apply_tree(new_tree: Path) -> None:
    """白名单替换：只动 REPLACE_ENTRIES 内条目，其余（state/.venv 等）绝不触碰。"""
    for name in REPLACE_ENTRIES:
        src = new_tree / name
        if not src.exists():
            continue
        dst = REPO_ROOT / name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def rollback() -> None:
    """从 .update-backup/ 整体恢复白名单条目（替换失败后的兜底）。"""
    for name in REPLACE_ENTRIES:
        bak = BACKUP_DIR / name
        if not bak.exists():
            continue
        dst = REPO_ROOT / name
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if bak.is_dir():
            shutil.copytree(bak, dst)
        else:
            shutil.copy2(bak, dst)


# ------------------------------------------------------------ 下载

def download_fpk(url: str, dest: Path,
                 client: httpx.Client | None = None) -> int:
    """流式下载 fpk 到 dest，过程回报进度；返回文件大小。"""
    own_client = client is None
    client = client or httpx.Client(timeout=120, follow_redirects=True,
                                    trust_env=True,
                                    headers={"user-agent": f"fnmedia-dl/{_read_version()}"})
    try:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise UpdateError(f"下载失败：HTTP {resp.status_code}")
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 5 + int(done * 75 / total)
                        _set("downloading", min(pct, 80),
                             f"下载中 {done >> 20}MB / {total >> 20}MB")
                    else:
                        _set("downloading", 5, f"下载中 {done >> 20}MB")
    finally:
        if own_client:
            client.close()
    if not dest.is_file() or dest.stat().st_size == 0:
        raise UpdateError("下载失败：文件为空")
    return dest.stat().st_size


# ------------------------------------------------------------ 依赖与重启

def _requirements_changed(new_tree: Path) -> bool:
    def _txt(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    return _txt(new_tree / "requirements.txt") != _txt(REPO_ROOT / "requirements.txt")


def _rebuild_deps() -> None:
    """requirements 变更后重建 venv（bootstrap_deps.sh：离线 wheelhouse 优先）。"""
    script = REPO_ROOT / "deploy" / "bootstrap_deps.sh"
    venv = REPO_ROOT / ".venv"
    if not venv.exists() or not script.is_file() or not shutil.which("bash"):
        return  # 无 venv（开发态）或脚本缺失 → 跳过，不阻塞更新
    proc = subprocess.run(["bash", str(script)], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise UpdateError(f"依赖重建失败，已回滚：{tail or 'bootstrap_deps.sh'}")


def _restart_service() -> bool:
    """延迟 1 秒重启服务（先让 HTTP 响应落地）。

    服务以专用应用用户运行时无权 systemctl：systemd 托管环境
    （INVOCATION_ID 存在）改为自终止，由单元 Restart=always 拉起新进程；
    非托管环境退回 systemctl（root 手动部署场景）；都没有返回 False。"""
    if os.environ.get("INVOCATION_ID"):
        subprocess.Popen(
            ["sh", "-c", f"sleep 1; kill -TERM {os.getpid()}"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    if not shutil.which("systemctl"):
        return False
    subprocess.Popen(
        ["sh", "-c", f"sleep 1; exec systemctl restart {SERVICE_NAME}"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ------------------------------------------------------------ 编排

def start(url: str, version: str = "") -> dict:
    """启动后台更新线程（单例）。返回当前进度快照。"""
    global _thread
    if not validate_asset_url(url):
        raise UpdateError("仅支持本项目 GitHub Releases 的 .fpk 下载地址")
    with _lock:
        if _thread is not None and _thread.is_alive():
            raise UpdateError("已有更新正在进行中")
        _state.update(stage="downloading", percent=0, detail="准备下载…",
                      error="", from_version=_read_version(),
                      to_version=(version or "").lstrip("vV"), done=False)
    _thread = threading.Thread(target=_run, args=(url,), daemon=True)
    _thread.start()
    return progress()


def _run(url: str) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="fndl-update-"))
    fpk_path = workdir / "update.fpk"
    replaced = False
    try:
        download_fpk(url, fpk_path)                              # 5..80%
        _set("extracting", 82, "解包校验…")
        new_tree = extract_repo_tree(fpk_path, workdir)
        new_ver = _read_version(new_tree)
        with _lock:
            _state["to_version"] = new_ver
        req_changed = _requirements_changed(new_tree)
        _set("backing_up", 86, "备份当前版本…")
        backup_entries()
        _set("replacing", 90, "替换程序文件…")
        replaced = True
        apply_tree(new_tree)
        if req_changed:
            _set("deps", 94, "依赖变更，重建运行环境…")
            _rebuild_deps()
        _set("restarting", 98, "重启服务…")
        if _restart_service():
            _set("done", 100, f"更新完成（v{new_ver}）：服务重启中，稍候自动恢复",
                 done=True)
        else:
            _set("done", 100, f"更新完成（v{new_ver}）：请手动重启服务后生效",
                 done=True)
    except UpdateError as exc:
        if replaced:
            _try_rollback()
        _set("error", error=str(exc), detail=f"更新失败：{exc}", done=True)
    except Exception as exc:  # noqa: BLE001 - 后台线程兜底，转用户可见错误
        if replaced:
            _try_rollback()
        _set("error", error=str(exc), detail=f"更新失败：{exc}", done=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _try_rollback() -> None:
    try:
        rollback()
        _set(detail="已回滚到更新前版本")
    except Exception as exc:  # noqa: BLE001 - 回滚失败需明示，不能吞
        _set(detail=f"更新失败且回滚失败（请用应用中心重装恢复）：{exc}")
