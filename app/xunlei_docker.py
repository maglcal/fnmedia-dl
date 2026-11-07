#!/usr/bin/env python3
"""Docker 版迅雷（cnk3x/xunlei）一键部署。

面向「不知道怎么配 Docker 版迅雷」的用户：安装向导或管理页点一下，
本模块自动完成 检测 Docker → 准备数据目录 → 拉取镜像 → 启动容器 →
等面板就绪 → 写入 thunder_engine 配置 全流程。

约束与边界：
  - 只管理本插件自建的容器 fnmedia-dl-xunlei，不碰用户自己的任何容器；
  - 镜像固定 cnk3x/xunlei:latest（群晖套件系 web 面板）；
    应用中心官方原生迅雷（社区版）无本地面板接口，不支持，见迅雷远程通道；
  - 数据持久化到应用数据共享目录（账号/配置不因重装丢失）；
  - 进度落盘 state/xunlei-deploy.json：独立后台进程（安装脚本触发）与
    服务内 API 都能读到同一份状态；可重复执行（已在跑/已部署都幂等）。

用法：
  python app/xunlei_docker.py run   # 前台执行一次部署（安装脚本以 nohup 后台调用）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

import config as cfgmod

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTAINER_NAME = "fnmedia-dl-xunlei"
IMAGE = "cnk3x/xunlei:latest"
PANEL_URL = "http://127.0.0.1:2345"
DOCKER_CANDIDATES = ("/usr/local/bin/docker", "/usr/bin/docker", "/bin/docker")


class DeployError(RuntimeError):
    """部署失败（用户可见的中文原因）。"""


# ------------------------------------------------------------ 状态文件
# 独立部署进程与 FastAPI 服务是两个进程，状态必须落盘共享。

def _state_file() -> Path:
    return Path(cfgmod.STATE_DIR) / "xunlei-deploy.json"


def progress() -> dict:
    st = {"stage": "idle", "percent": 0, "detail": "", "error": "",
          "done": False, "pid": 0}
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            st.update({k: data.get(k, st[k]) for k in st})
    except (OSError, ValueError):
        pass
    return st


def _set(stage: str | None = None, percent: int | None = None,
         detail: str | None = None, error: str | None = None,
         done: bool | None = None, pid: int | None = None) -> dict:
    st = progress()
    for key, val in (("stage", stage), ("percent", percent),
                     ("detail", detail), ("error", error),
                     ("done", done), ("pid", pid)):
        if val is not None:
            st[key] = val
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        tmp = _state_file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, _state_file())
    except OSError:
        pass
    return st


# ------------------------------------------------------------ docker 底座

def docker_bin() -> str | None:
    import shutil
    found = shutil.which("docker")
    if found:
        return found
    for cand in DOCKER_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _docker(*args: str, timeout: int = 60) -> tuple[int, str]:
    """执行 docker 子命令，返回 (退出码, 合并输出尾部)。不抛异常。"""
    exe = docker_bin()
    if not exe:
        return 127, "docker 不存在"
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out[-2000:]


def container_state() -> str:
    """running | stopped | absent | unknown（本插件自建容器，不动用户容器）。"""
    rc, out = _docker("inspect", "-f", "{{.State.Running}}", CONTAINER_NAME,
                      timeout=15)
    if rc != 0:
        return "unknown" if docker_bin() is None else "absent"
    return "running" if out.strip() == "true" else "stopped"


# ------------------------------------------------------------ 目录与配置

def _volume_dirs() -> tuple[Path, Path]:
    """(数据目录, 下载共享目录)。数据目录放应用数据共享（持久化账号/配置），
    下载目录挂应用内置下载共享（面板默认 /xunlei/downloads 即共享目录）。"""
    data_raw = cfgmod.data_dir()
    data_dir = Path(data_raw) if data_raw else (Path(cfgmod.STATE_DIR) / "xunlei-data")
    dl_raw = cfgmod.share_dir()
    dl_dir = Path(dl_raw) if dl_raw else Path("")
    return data_dir, dl_dir


def _dir_owner(path: Path) -> tuple[int, int]:
    """取目录属主 uid/gid（容器内进程同 uid 写入才不掉权限）；失败回退 1000。"""
    try:
        st = path.stat()
        return st.st_uid, st.st_gid
    except OSError:
        return 1000, 1000


def _configure(dl_dir: Path) -> None:
    """部署成功后自动写 thunder_engine 配置（用户不用手填面板地址）。"""
    cfg = cfgmod.load()
    te = cfg.setdefault("thunder_engine", {})
    if not (te.get("base_url") or "").strip():
        te["base_url"] = PANEL_URL
    # 面板内保存目录 = 容器内下载挂载点；只在用户没填过时补默认
    if dl_dir.name and not (te.get("dir") or "").strip():
        te["dir"] = "/xunlei/downloads"
    cfgmod.save(cfg)


# ------------------------------------------------------------ 面板就绪

def _panel_once(url: str = PANEL_URL) -> bool:
    try:
        resp = httpx.get(url, timeout=3)
        return resp.status_code < 500
    except httpx.HTTPError:
        return False


def _wait_panel(tries: int = 30, interval: float = 2.0) -> bool:
    for _ in range(tries):
        if _panel_once():
            return True
        time.sleep(interval)
    return False


# ------------------------------------------------------------ 部署编排

def deploy() -> dict:
    """完整部署一次（幂等：容器已在跑则直接复用）。成功返回最终状态。"""
    _set("checking", 5, "检测 Docker 环境…", pid=os.getpid())
    if docker_bin() is None:
        raise DeployError("NAS 未安装 Docker：请先到 应用中心 → Docker 安装并启动，"
                          "再回管理页点「一键部署」")
    rc, out = _docker("version", "--format", "{{.Server.Version}}", timeout=20)
    if rc != 0:
        low = out.lower()
        if "permission denied" in low or "dial unix" in low or "access is denied" in low:
            raise DeployError("应用用户无权访问 Docker（docker 组权限未生效）："
                              "请到应用中心重装或升级一次「飞牛下载助手」让权限生效后重试")
        raise DeployError("Docker 服务未运行：请到 应用中心 → Docker 启动后重试"
                          + (f"（{out[-120:]}）" if out else ""))

    _set("preparing", 15, "准备数据目录…")
    data_dir, dl_dir = _volume_dirs()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeployError(f"数据目录不可写：{data_dir}（{exc}）") from exc
    uid, gid = _dir_owner(dl_dir if dl_dir.name else data_dir)

    state = container_state()
    if state == "running":
        _configure(dl_dir)
        return _set("done", 100, _done_detail(), done=True)
    if state == "stopped":
        _set("starting", 60, "发现已有容器（非本插件创建也会复用同名容器），启动中…")
        rc, out = _docker("start", CONTAINER_NAME, timeout=120)
        if rc != 0:
            raise DeployError(f"启动已有容器失败：{out[-200:]}")
    else:
        _set("pulling", 30, f"拉取镜像 {IMAGE}（首次可能需要几分钟）…")
        rc, out = _docker("pull", IMAGE, timeout=1800)
        if rc != 0:
            raise DeployError("镜像拉取失败（可检查网络或 Docker 镜像加速配置后重点「一键部署」）"
                              + (f"：{out[-200:]}" if out else ""))
        _set("starting", 75, "启动容器…")
        args = ["run", "-d", "--name", CONTAINER_NAME,
                "--restart", "unless-stopped",
                "--network", "host", "--cap-add", "SYS_ADMIN",
                "-e", "TZ=Asia/Shanghai",
                "-e", f"XL_UID={uid}", "-e", f"XL_GID={gid}",
                "-v", f"{data_dir}:/xunlei/data"]
        if dl_dir.name:
            args += ["-v", f"{dl_dir}:/xunlei/downloads"]
        args.append(IMAGE)
        rc, out = _docker(*args, timeout=300)
        if rc != 0:
            raise DeployError(f"容器启动失败：{out[-200:]}")

    _set("waiting", 88, f"等待引擎面板就绪（{PANEL_URL}）…")
    if not _wait_panel():
        raise DeployError("容器已启动，但面板 60 秒内未就绪：稍后回管理页点「测试连接」，"
                          "或到 Docker 容器 fnmedia-dl-xunlei 查看日志")
    _configure(dl_dir)
    return _set("done", 100, _done_detail(), done=True)


def _done_detail() -> str:
    return (f"部署完成：面板 {PANEL_URL.replace('127.0.0.1', 'NAS-IP')}。"
            "首次打开需登录迅雷账号并填内测邀请码「迅雷牛通」，"
            "面板里的保存目录照抄容器内路径（如 /xunlei/downloads）即可。")


# ------------------------------------------------------------ 独立入口

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def running() -> bool:
    """是否有一个部署进程正在跑（未完成且 pid 存活）。"""
    st = progress()
    return (not st.get("done")) and _pid_alive(int(st.get("pid") or 0))


def run() -> int:
    """部署一次（供安装脚本/API 以独立进程调用）。已在部署中则直接退出。"""
    st = progress()
    if not st.get("done") and _pid_alive(int(st.get("pid") or 0)):
        return 0
    try:
        deploy()
        return 0
    except DeployError as exc:
        _set("error", error=str(exc), detail=f"部署失败：{exc}", done=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - 独立进程兜底，转用户可见错误
        _set("error", error=str(exc), detail=f"部署失败：{exc}", done=True)
        return 1


if __name__ == "__main__":
    sys.exit(run())
