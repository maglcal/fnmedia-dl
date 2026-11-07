#!/usr/bin/env python3
"""Docker 版迅雷（cnk3x/xunlei）一键部署。

面向「不知道怎么配 Docker 版迅雷」的用户：安装向导或管理页点一下，
本模块自动完成 检测 Docker → 准备数据目录 → 拉取镜像 → 启动容器 →
等面板就绪 → 写入 thunder_engine 配置 全流程。

约束与边界：
  - 只管理本插件自建的容器 fnmedia-dl-xunlei（同名容器若为 cnk3x/xunlei
    镜像也会接管重建修复，其他镜像的同名容器不碰、直接报错）；
  - 镜像固定 cnk3x/xunlei:latest（群晖套件系 web 面板）；
    应用中心官方原生迅雷（社区版）无本地面板接口，不支持，见迅雷远程通道；
  - 数据持久化到应用数据共享目录（账号/配置不因重装丢失）；
  - 进度落盘 state/xunlei-deploy.json：独立后台进程（安装脚本触发）与
    服务内 API 都能读到同一份状态；可重复执行（已在跑/已部署都幂等）。

挂载权限（0.1.21）：引擎启动会在容器内 bind mount（/lib /bin /etc/ssl →
chroot 监狱），docker-default AppArmor 不放行 mount，报
`symlink - dir - mount: permission denied`（cnk3x/xunlei#108 同症）。
一级配方 cap SYS_ADMIN+SYS_CHROOT + apparmor=unconfined；容器仍起不来时
自动改用兼容模式（--privileged + seccomp=unconfined，fnOS 社区配方）重建。

用法：
  python app/xunlei_docker.py run [--data-dir DIR] [--dl-dir DIR]
（安装脚本以 nohup 后台调用；--data-dir/--dl-dir 可自定义配置/下载存放位置）
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

# docker run 的安全参数分两级：
#   tier1 = 问题最小的权限组合（cnk3x/xunlei#108 实测可过 AppArmor mount 拦截）
#   tier2 = 兼容模式（privileged），tier1 起不来时自动切换，保证一键部署成功率
TIER1 = ["--cap-add", "SYS_ADMIN", "--cap-add", "SYS_CHROOT",
         "--security-opt", "apparmor=unconfined"]
TIER2 = ["--privileged", "--security-opt", "seccomp=unconfined"]


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


def _container_image() -> str:
    """容器镜像名（判别同名容器是不是 cnk3x/xunlei）。查不到返回空。"""
    rc, out = _docker("inspect", "-f", "{{.Config.Image}}", CONTAINER_NAME,
                      timeout=15)
    return out.strip() if rc == 0 else ""


# ------------------------------------------------------------ 目录与配置

def _safe_dir(raw: str, what: str) -> Path:
    """向导/API 传入的路径视为不可信输入，二次校验后才使用。"""
    text = (raw or "").strip()
    if not text:
        raise DeployError(f"{what}不能为空")
    if "\x00" in text or len(text) > 4000:
        raise DeployError(f"{what}不合法：{text[:80]}")
    p = Path(text)
    # NAS 是 Linux，POSIX 绝对路径（/ 开头）即合法；开发机是 Windows，
    # 本地测试传 /vol1/... 也要放行，故兼容两种判定。
    if not (text.startswith("/") or p.is_absolute()):
        raise DeployError(f"{what}必须是绝对路径（如 /vol1/1000/docker/xunlei/data）：{text}")
    if ".." in p.parts or text.rstrip("/") == "":
        raise DeployError(f"{what}不合法（不能含 .. 或为根目录）：{text}")
    return p


def _volume_dirs(data_dir_raw: str = "", dl_dir_raw: str = "") -> tuple[Path, Path]:
    """(数据目录, 下载共享目录)。数据目录放应用数据共享（持久化账号/配置），
    下载目录挂应用内置下载共享（容器内固定挂 /xunlei/downloads）。
    传入自定义路径则覆盖默认（默认：应用数据共享 / 应用下载共享）。"""
    if (data_dir_raw or "").strip():
        data_dir = _safe_dir(data_dir_raw, "Docker 版配置文件存放位置")
    else:
        data_raw = cfgmod.data_dir()
        data_dir = Path(data_raw) if data_raw else (Path(cfgmod.STATE_DIR) / "xunlei-data")
    if (dl_dir_raw or "").strip():
        dl_dir = _safe_dir(dl_dir_raw, "下载文件存放位置")
    else:
        dl_raw = cfgmod.share_dir()
        dl_dir = Path(dl_raw) if dl_raw else Path("")
    return data_dir, dl_dir


def default_dirs() -> tuple[str, str]:
    """默认 (配置文件存放位置, 下载文件存放位置) 文本值，供 UI 展示。"""
    data_dir, dl_dir = _volume_dirs()
    return str(data_dir), (str(dl_dir) if dl_dir.name else "")


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


def _wait_panel_watchdog(tries: int = 45, interval: float = 2.0) -> tuple[bool, bool]:
    """等面板就绪，同时观察容器是否中途死掉。

    返回 (面板就绪, 容器中途退出)。启动即崩（如 mount 权限被拦）几秒内就会
    退出，正常首次启动（拉 SPK、初始化）则容器一直在。"""
    died = False
    for i in range(tries):
        if _panel_once():
            return True, died
        if i >= 3 and container_state() != "running":
            died = True
            break
        time.sleep(interval)
    return False, died


# ------------------------------------------------------------ 部署编排

def _run_container(sec_args: list[str], data_dir: Path, dl_dir: Path,
                   uid: int, gid: int) -> None:
    args = ["run", "-d", "--name", CONTAINER_NAME,
            "--restart", "unless-stopped",
            "--network", "host", *sec_args,
            "-e", "TZ=Asia/Shanghai",
            "-e", f"XL_UID={uid}", "-e", f"XL_GID={gid}",
            "-v", f"{data_dir}:/xunlei/data"]
    if dl_dir.name:
        args += ["-v", f"{dl_dir}:/xunlei/downloads"]
    args.append(IMAGE)
    rc, out = _docker(*args, timeout=300)
    if rc != 0:
        raise DeployError(f"容器启动失败：{out[-200:]}")


def deploy(data_dir_raw: str = "", dl_dir_raw: str = "") -> dict:
    """完整部署一次（幂等：容器已在跑且面板可用则直接复用）。成功返回最终状态。"""
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
    data_dir, dl_dir = _volume_dirs(data_dir_raw, dl_dir_raw)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if dl_dir.name:
            dl_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeployError(f"目录不可写：{data_dir}（{exc}）") from exc
    uid, gid = _dir_owner(dl_dir if dl_dir.name else data_dir)

    state = container_state()
    if state == "running" and _panel_once():
        # 已部署且面板活着：幂等快路径（不管是不是本插件建的，能用就用）
        _configure(dl_dir)
        return _set("done", 100, _done_detail(), done=True)

    if state != "absent" and state != "unknown":
        # 有同名容器但不健康（旧参数建的起不来/面板不通）：是迅雷容器就重建修复
        img = _container_image()
        if not img.startswith("cnk3x/xunlei"):
            raise DeployError(f"已存在同名容器 {CONTAINER_NAME}（镜像 {img or '未知'}），"
                              "不是 cnk3x/xunlei：请先在 Docker 里处理该容器后重试")
        _set("recreating", 25, "发现同名容器未正常服务，清理重建（自动修复旧参数）…")
        rc, out = _docker("rm", "-f", CONTAINER_NAME, timeout=60)
        if rc != 0:
            raise DeployError(f"清理旧容器失败：{out[-200:]}")
        state = "absent"

    if state == "absent":
        _set("pulling", 35, f"拉取镜像 {IMAGE}（首次可能需要几分钟）…")
        rc, out = _docker("pull", IMAGE, timeout=1800)
        if rc != 0:
            raise DeployError("镜像拉取失败（可检查网络或 Docker 镜像加速配置后重点「一键部署」）"
                              + (f"：{out[-200:]}" if out else ""))

    # —— 一级配方：SYS_ADMIN+SYS_CHROOT + apparmor=unconfined（过 mount 拦截）——
    _set("starting", 60, "启动容器…")
    _run_container(TIER1, data_dir, dl_dir, uid, gid)

    _set("waiting", 85, f"等待引擎面板就绪（{PANEL_URL}）…")
    ok, died = _wait_panel_watchdog()
    if not ok and died:
        # 容器秒崩（常见为宿主安全策略差异）：自动切兼容模式重建再试一次
        _set("retrying", 70, "容器启动即退出，改用兼容模式（特权）重建，请稍候…")
        _docker("rm", "-f", CONTAINER_NAME, timeout=60)
        _run_container(TIER2, data_dir, dl_dir, uid, gid)
        ok, died = _wait_panel_watchdog()
    if not ok:
        rc, logs = _docker("logs", "--tail", "20", CONTAINER_NAME, timeout=30)
        tail = logs[-300:].replace("\n", " ") if rc == 0 else ""
        raise DeployError("容器已启动，但面板 3 分钟内未就绪：稍后回管理页点「测试连接」，"
                          "或到 Docker 容器 fnmedia-dl-xunlei 查看日志"
                          + (f"（日志尾部：{tail}）" if tail else ""))
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


def run(argv: list[str] | None = None) -> int:
    """部署一次（供安装脚本/API 以独立进程调用）。已在部署中则直接退出。

    argv 支持 --data-dir/--dl-dir 自定义「Docker 版配置文件存放位置」与
    「下载文件存放位置」（省略 = 用默认：应用数据共享 / 应用下载共享）。"""
    data_dir_raw, dl_dir_raw = "", ""
    args = list(argv if argv is not None else sys.argv[1:])
    while args:
        tok = args.pop(0)
        if tok == "--data-dir" and args:
            data_dir_raw = args.pop(0)
        elif tok == "--dl-dir" and args:
            dl_dir_raw = args.pop(0)
        elif tok == "run":
            continue
        else:
            _set("error", error=f"未知参数：{tok}", detail=f"部署失败：未知参数 {tok}",
                 done=True)
            return 2

    st = progress()
    if not st.get("done") and _pid_alive(int(st.get("pid") or 0)):
        return 0
    try:
        deploy(data_dir_raw, dl_dir_raw)
        return 0
    except DeployError as exc:
        _set("error", error=str(exc), detail=f"部署失败：{exc}", done=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - 独立进程兜底，转用户可见错误
        _set("error", error=str(exc), detail=f"部署失败：{exc}", done=True)
        return 1


if __name__ == "__main__":
    sys.exit(run())
