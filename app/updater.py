#!/usr/bin/env python3
"""应用内一键在线更新：下载官方 fpk → 解包 → 备份 → 白名单替换 → 重启服务。

流程（单例后台线程，进度经 progress() 轮询）：
  downloading → extracting → backing_up → replacing → [deps] → restarting → done
任一步失败：整体回滚到备份，状态置 error 并说明原因。

下载（多源回退）：下载候选按序「镜像源 → 官方源」，每个候选重试 2 次，
校验大小/SHA256，不符或失败自动换下一个候选（download_verified）。

安全边界：
  - 只接受官方 GitHub Releases 与「用户已配置镜像源」的 .fpk 地址
    （validate_asset_url，镜像前缀由 main 依配置传入）；
  - 重定向逐跳手动跟随（≤5 跳）：跳点主机限「候选源主机 ∪ GitHub/Gitee
    资产主机」白名单、scheme 限 http/https，防合法源 302 跳去任意主机
    绕过地址校验；单文件下载上限 512MB（download_fpk）；
  - SHA256 fail-closed：清单缺校验值直接拒绝下载（allow_unverified 仅
    「用户手动输入地址」通道显式豁免）；
  - 只替换白名单条目（app/deploy/packaging/wheelhouse/VERSION 等），
    state/（配置、凭据、任务记录）与 .venv/ 永不触碰；
  - 替换前整份备份到 <repo>/.update-backup/，失败即回滚；替换/回滚均
    经 staging 后改名顶替（_swap_in），不留半覆盖窗口；
  - requirements.txt 变更时才重建运行环境（离线 wheelhouse 优先）。
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from updatesrc import strip_ver

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_REPO = "maglcal/fnmedia-dl"
DOWNLOAD_URL_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"
# 重定向白名单主机：GitHub 下载链路（github.com → 资产主机）恒可信；
# 镜像源主机由调用方按候选 URL 并入（download_fpk 内部再并本 URL 主机）。
GITHUB_HOSTS = ("github.com", "objects.githubusercontent.com",
                "release-assets.githubusercontent.com")
# Gitee 下载链路（gitee.com → attach_files → foruda.gitee.com 附件 CDN）
# 同样恒可信（实测两级跳转，foruda 是 Gitee 附件分发主机）。
GITEE_HOSTS = ("gitee.com", "foruda.gitee.com")
MAX_REDIRECTS = 5
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

# 替换白名单：随新版本整体替换。state/ 与 .venv/ 永不在列（凭据/任务记录/运行环境）。
REPLACE_ENTRIES = ("app", "deploy", "packaging", "wheelhouse",
                   "VERSION", "requirements.txt", "README.md", "LICENSE",
                   "CHANGELOG.md")

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


def validate_asset_url(url: str, extra_prefixes: list[str] | None = None) -> bool:
    """只放行官方 GitHub Releases 与「已配置镜像源」的 .fpk 下载地址。"""
    allowed = [DOWNLOAD_URL_PREFIX, *(extra_prefixes or [])]
    return (isinstance(url, str)
            and any(url.startswith(p) for p in allowed if p)
            and url.endswith(".fpk")
            and ".." not in url)


def _mask_secrets(text: str) -> str:
    """错误/进度文案里的 URL 敏感 query 参数（token/secret/key/password/auth）
    值掩码——httpx 异常会带完整 URL，防凭据经错误提示泄露。"""
    import re
    return re.sub(r"((?:token|secret|key|password|passwd|auth)=)[^&\s\"')]+",
                  r"\1******", text, flags=re.IGNORECASE)


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


def _swap_in(src: Path, dst: Path) -> None:
    """staging 后原子改名顶替（P0-3）。

    旧实现是「rmtree(dst) → copytree(src, dst)」——中间任何时刻挂掉/断电
    都留下半覆盖目录，运行中的服务随即崩在残缺代码上。改为：
    ① 整条目复制到旁路 staging（.名.fndl-new）；② 原 dst 改名挪到
    .名.fndl-old；③ staging 改名顶替。改名是原子操作，任何时刻
    dst 要么是旧版要么是完整新版。"""
    staging = dst.parent / f".{dst.name}.fndl-new"
    old = dst.parent / f".{dst.name}.fndl-old"
    for p in (staging, old):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    if src.is_dir():
        shutil.copytree(src, staging)
    else:
        shutil.copy2(src, staging)
    if dst.exists():
        os.replace(dst, old)          # 旧版完整挪开（rename 原子）
    os.replace(staging, dst)          # 新版整体顶替（rename 原子）
    for p in (old,):                  # 顶替成功后清掉旧版
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def apply_tree(new_tree: Path) -> None:
    """白名单替换：只动 REPLACE_ENTRIES 内条目，其余（state/.venv 等）绝不触碰。
    每条目经 _swap_in 原子顶替，不留半覆盖窗口。"""
    for name in REPLACE_ENTRIES:
        src = new_tree / name
        if not src.exists():
            continue
        _swap_in(src, REPO_ROOT / name)


def rollback() -> None:
    """从 .update-backup/ 整体恢复白名单条目（替换失败后的兜底），
    与 apply_tree 同用 _swap_in 原子顶替。"""
    for name in REPLACE_ENTRIES:
        bak = BACKUP_DIR / name
        if not bak.exists():
            continue
        _swap_in(bak, REPO_ROOT / name)


# ------------------------------------------------------------ 下载

def download_fpk(url: str, dest: Path,
                 client: httpx.Client | None = None,
                 extra_hosts: tuple[str, ...] = ()) -> int:
    """流式下载 fpk 到 dest，过程回报进度；返回文件大小。

    P0-3 重定向加固：关掉自动重定向，手动逐跳跟随（≤5 跳）——
    每跳 host 必须在白名单（本 URL 主机 ∪ extra_hosts ∪ GitHub/Gitee
    资产主机），scheme 限 http/https；下载总量上限 512MB，防跳转钓鱼
    与超大文件灌盘。"""
    own_client = client is None
    allowed = {urlparse(url).hostname or "",
               *(urlparse(u).hostname or "" for u in extra_hosts),
               *GITHUB_HOSTS, *GITEE_HOSTS}
    client = client or httpx.Client(timeout=120, follow_redirects=False,
                                    trust_env=True,
                                    headers={"user-agent": f"fnmedia-dl/{_read_version()}"})
    try:
        cur = url
        hops = 0
        while True:
            with client.stream("GET", cur, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    hops += 1
                    if hops > MAX_REDIRECTS:
                        raise UpdateError(
                            f"下载失败：重定向超过 {MAX_REDIRECTS} 次")
                    loc = resp.headers.get("location", "")
                    nxt = urljoin(cur, loc)
                    parsed = urlparse(nxt)
                    if parsed.scheme not in ("http", "https"):
                        raise UpdateError("下载失败：重定向到非 http(s) 地址，已拒绝")
                    host = parsed.hostname or ""
                    if host not in allowed:
                        raise UpdateError(
                            f"下载失败：重定向到白名单外主机 {host}，已拒绝")
                    cur = nxt
                    continue
                if resp.status_code != 200:
                    raise UpdateError(f"下载失败：HTTP {resp.status_code}")
                total = int(resp.headers.get("content-length") or 0)
                if total and total > MAX_DOWNLOAD_BYTES:
                    raise UpdateError("下载失败：文件超过大小上限（512MB）")
                done = 0
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 16):
                        done += len(chunk)
                        if done > MAX_DOWNLOAD_BYTES:
                            raise UpdateError("下载失败：文件超过大小上限（512MB）")
                        fh.write(chunk)
                        if total:
                            pct = 5 + int(done * 75 / total)
                            _set("downloading", min(pct, 80),
                                 f"下载中 {done >> 20}MB / {total >> 20}MB")
                        else:
                            _set("downloading", 5, f"下载中 {done >> 20}MB")
                break
    finally:
        if own_client:
            client.close()
    if not dest.is_file() or dest.stat().st_size == 0:
        raise UpdateError("下载失败：文件为空")
    return dest.stat().st_size


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_verified(urls: list[str], dest: Path,
                      sha256: str = "", expected_size: int = 0,
                      client: httpx.Client | None = None,
                      allow_unverified: bool = False) -> int:
    """多候选下载并校验：逐候选「下载 → 校验大小/SHA256」，
    每候选最多重试 2 次（递增退避），失败或校验不符自动换下一个候选。
    全部失败抛 UpdateError（含逐候选原因汇总）。返回文件大小。

    P0-3 fail-closed：sha256 为空且未显式 allow_unverified 时直接拒绝
    下载——GitHub 资产的 sha256 从兄弟 .sha256 资产抓取、失败不阻断
    （可能为空），空校验值继续下载等于把更新链暴露给中间人。
    allow_unverified=True 仅限「用户手动输入下载地址」通道（人工点了
    「更新」明确意图），自动更新链永不豁免。"""
    if not sha256 and not allow_unverified:
        raise UpdateError("拒绝下载：该更新源缺少 SHA256 校验值，"
                          "无法验证包完整性（可稍后重试或换用官方源）")
    errors: list[str] = []
    switched = 0                      # 换源次数（成功后仍在进度里留痕，切换透明）
    extra = tuple(urls)               # 重定向白名单并入全部候选源主机
    for idx, url in enumerate(urls, 1):
        tag = f"候选{idx}"
        for attempt in (1, 2):
            try:
                size = download_fpk(url, dest, client=client, extra_hosts=extra)
                if expected_size and size != expected_size:
                    raise UpdateError(
                        f"大小不符（{size} ≠ {expected_size} 字节）")
                if sha256:
                    actual = _sha256_of(dest)
                    if actual.lower() != sha256.lower():
                        raise UpdateError("SHA256 校验不符")
                done_note = "下载校验通过"
                if switched:
                    done_note += f"（中途已自动切换备用源 {switched} 次）"
                _set("downloading", 80, done_note)
                return size
            except UpdateError as exc:
                errors.append(f"{tag}第{attempt}次：{exc}")
                if attempt == 1:
                    time.sleep(1.5 * idx)   # 递增退避后重试同候选
                elif idx < len(urls):
                    # 换源透明提示：告知用户正在切换备用源（更新自动继续）
                    switched += 1
                    _set(detail=f"{tag}失败（{_mask_secrets(str(exc))[:80]}），"
                                f"自动切换备用源继续下载…")
            except httpx.HTTPError as exc:
                errors.append(f"{tag}第{attempt}次：网络错误 "
                              f"{_mask_secrets(str(exc))}")
                if attempt == 1:
                    time.sleep(1.5 * idx)
                elif idx < len(urls):
                    switched += 1
                    _set(detail=f"{tag}失败（网络错误），"
                                f"自动切换备用源继续下载…")
    raise UpdateError("所有下载候选均失败：" + "；".join(errors))


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

def start(url: str, version: str = "", *,
          fallback_urls: list[str] | None = None,
          sha256: str = "", expected_size: int = 0,
          allowed_prefixes: list[str] | None = None,
          allow_unverified: bool = False) -> dict:
    """启动后台更新线程（单例）。返回当前进度快照。

    url 为主下载候选（镜像源优先），fallback_urls 为回退候选（官方源兜底），
    两者按序组成下载候选列表；sha256/expected_size 为清单校验值（可空），
    allowed_prefixes 为镜像源地址前缀（由 main 依配置传入）。
    allow_unverified=True 仅限「用户手动输入下载地址」通道显式豁免
    SHA256 fail-closed（自动更新链永不豁免）。"""
    global _thread
    candidates = [url, *(fallback_urls or [])]
    for u in candidates:
        if not validate_asset_url(u, allowed_prefixes):
            raise UpdateError("仅支持本项目官方 Releases 与已配置镜像源的 .fpk 地址")
    with _lock:
        if _thread is not None and _thread.is_alive():
            raise UpdateError("已有更新正在进行中")
        _state.update(stage="downloading", percent=0, detail="准备下载…",
                      error="", from_version=_read_version(),
                      to_version=strip_ver(version or ""), done=False)
    _thread = threading.Thread(
        target=_run, args=(candidates, sha256, expected_size),
        kwargs={"allow_unverified": allow_unverified}, daemon=True)
    _thread.start()
    return progress()


def _run(urls: list[str], sha256: str = "", expected_size: int = 0,
         allow_unverified: bool = False) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="fndl-update-"))
    fpk_path = workdir / "update.fpk"
    replaced = False
    try:
        # 5..80%
        download_verified(urls, fpk_path, sha256, expected_size,
                          allow_unverified=allow_unverified)
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
