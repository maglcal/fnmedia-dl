#!/usr/bin/env python3
"""fnmedia-dl FastAPI 服务：管理页 + JSON API。

页面：
  GET  /                     管理页（单页 UI）
  GET  /static/*             静态资源
接口：
  GET  /api/health           健康检查（含版本号）
  GET  /api/update/check     在线更新检查（GitHub Releases）
  POST /api/update/download  一键在线更新（下载 fpk → 替换 → 重启，后台执行）
  GET  /api/update/progress  更新进度轮询（stage/percent/detail）
  GET  /api/backends         各下载通道状态
  POST /api/backends/test    单通道连通性测试
  GET  /api/xunlei_docker/status   Docker 版迅雷部署状态
  POST /api/xunlei_docker/deploy   一键部署 Docker 版迅雷（cnk3x/xunlei，后台执行）
  GET  /api/thunder_cloud/review         迅雷风控短信验证状态
  POST /api/thunder_cloud/review/send    获取短信验证码
  POST /api/thunder_cloud/review/check   提交短信验证码
  GET  /api/config           配置（脱敏）
  PUT  /api/config           更新配置
  GET  /api/fs/paths         共享目录 / 已授权目录清单
  GET  /api/fs/check         目录可用性检测（存在 + 可写探针）
  POST /api/parse            批量链接解析
  POST /api/tasks            批量派发下载
  GET  /api/tasks            统一任务列表
  POST /api/history/check    下载历史查重（是否下载过某链接）
  POST /api/tasks/{backend}/{id}/pause|resume|retry
  DELETE /api/tasks/{backend}/{id}
  GET  /api/queue            任务队列与定时设置
  POST /api/queue            链接入队
  DELETE /api/queue/{id}     移除队列项
  POST /api/queue/run        立即派发全部待发项
  POST /api/queue/settings   每日定时派发设置
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config as cfgmod
import links as linksmod
import taskqueue as tqmod
import updatesrc
import updater as updmod
import xunlei_docker as xdmod
from backends.base import BackendError
from downloader import DownloadManager

UI_DIR = Path(__file__).resolve().parent / "ui"


def _read_version() -> str:
    """版本号以仓库根部 VERSION 文件为准（随包安装），失败回退兜底值。"""
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:3]:
        vf = parent / "VERSION"
        if vf.is_file():
            try:
                text = vf.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                return text.splitlines()[0].strip()
    return "0.0.0"


VERSION = _read_version()

app = FastAPI(title="fnmedia-dl", version=VERSION)
app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """UI 页面/脚本/样式禁协商缓存：在线更新后 iframe 不吃旧页（版本号新、页面旧）。"""
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if any(t in ct for t in ("text/html", "javascript", "text/css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _admin_guard(request, call_next):
    """管理接口鉴权（官方网关规范）：统一网关转发时注入 X-Trim-Userid/Isadmin/Username，
    写操作只放行管理员（X-Trim-Isadmin: true）。
    不信任客户端自带的 uid；无任何 X-Trim-* 头 = 未经网关（本地调试/兼容环境），放行。"""
    if request.method in _MUTATING:
        headers = request.headers
        via_gateway = any(k.startswith("x-trim-") for k in headers)
        if via_gateway and headers.get("x-trim-isadmin", "").lower() != "true":
            return JSONResponse(status_code=403,
                                content={"detail": "仅管理员可执行此操作"})
    return await call_next(request)


def _manager() -> DownloadManager:
    return DownloadManager(cfgmod.load())


def _queue() -> tqmod.TaskQueue:
    return tqmod.TaskQueue(Path(cfgmod.STATE_DIR) / "queue.json")


def _queue_dispatch(mgr: DownloadManager):
    """队列派发闭包：auto 降级链 + 全局默认目录兜底。"""
    def _fn(url: str, directory: str | None) -> dict:
        return mgr.dispatch(linksmod.parse_line(url), backend="auto",
                            directory=directory
                            or mgr.cfg.get("default_dir") or None)
    return _fn


# ------------------------------------------------------------ 页面

@app.get("/")
def index():
    return FileResponse(str(UI_DIR / "index.html"))


# ------------------------------------------------------------ 基础

@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "name": "fnmedia-dl"}


@app.get("/api/update/check")
def update_check():
    """在线更新检查：多源按序尝试（自建 RepoBox 镜像优先，GitHub 兜底），
    每个源内部重试；全部失败才报错。"""
    try:
        return updatesrc.check(cfgmod.load(), VERSION)
    except updatesrc.SourceError as exc:
        raise HTTPException(502, str(exc))


class UpdateDownloadReq(BaseModel):
    url: str = ""
    version: str = ""


@app.post("/api/update/download")
def update_download(req: UpdateDownloadReq):
    """一键在线更新：后台线程下载 fpk → 替换 → 重启服务，进度走 /api/update/progress。

    下载地址来自检查结果（镜像源优先），并带齐其余源的回退候选——
    下载失败/校验不符时自动重试并换源（updater.download_verified）。"""
    info = update_check()
    fpk_assets = [a for a in info.get("assets", [])
                  if (a.get("name") or "").endswith(".fpk")]
    asset = next((a for a in fpk_assets
                  if req.url and a.get("url") == req.url), None)
    if asset is None and req.url:
        # 手动指定地址：无清单信息，仅做地址形状放行
        asset = {"url": req.url, "fallback_urls": [], "sha256": "", "size": 0}
    if asset is None:
        asset = fpk_assets[0] if fpk_assets else {}
    url = asset.get("url") or ""
    if not url:
        raise HTTPException(400, "没有可用的 .fpk 资产")
    try:
        return {"ok": True, **updmod.start(
            url, req.version,
            fallback_urls=asset.get("fallback_urls") or [],
            sha256=asset.get("sha256") or "",
            expected_size=int(asset.get("size") or 0),
            allowed_prefixes=updatesrc.allowed_prefixes(cfgmod.load()))}
    except updmod.UpdateError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/update/progress")
def update_progress():
    return updmod.progress()


class BackendTestReq(BaseModel):
    backend: str


@app.get("/api/backends")
def backends_status():
    mgr = _manager()
    out = {}
    for key, impl in mgr.backends().items():
        out[key] = {"label": impl.label, **impl.status()}
    if "thunder_engine" in out:
        # 今日配额用量（非会员每日创建次数有限），UI 引擎卡展示
        out["thunder_engine"]["quota"] = mgr.quota.snapshot()
    return out


@app.post("/api/backends/test")
def backends_test(req: BackendTestReq):
    mgr = _manager()
    pool = mgr.backends()
    impl = pool.get(req.backend)
    if not impl:
        raise HTTPException(404, f"未知通道：{req.backend}")
    return {"backend": req.backend, **impl.status()}


# ------------------------------------------------------------ Docker 版迅雷一键部署

class XdDeployIn(BaseModel):
    data_dir: str = ""   # Docker 版配置文件存放位置（空 = 默认应用数据共享）
    dl_dir: str = ""     # 下载文件存放位置（空 = 默认应用下载共享）


@app.get("/api/xunlei_docker/status")
def xd_status():
    def_data, def_dl = xdmod.default_dirs()
    return {
        "deploy": xdmod.progress(),
        "running": xdmod.running(),
        "container": xdmod.container_state(),
        "docker": xdmod.docker_bin() is not None,
        "panel_url": xdmod.PANEL_URL,
        "default_data_dir": def_data,
        "default_dl_dir": def_dl,
    }


@app.post("/api/xunlei_docker/deploy")
def xd_deploy(body: XdDeployIn = XdDeployIn()):
    """一键部署 Docker 版迅雷：独立后台进程执行（服务重启不影响），
    进度走 GET /api/xunlei_docker/status 轮询。
    body 可选传 data_dir/dl_dir 自定义配置/下载存放位置（空 = 用默认）。"""
    if xdmod.running():
        raise HTTPException(400, "已有部署正在进行，请稍候")
    opts = body or XdDeployIn()
    # 向导/API 传入的路径视为不可信输入，先二次校验再交给部署进程
    try:
        xdmod._volume_dirs(opts.data_dir, opts.dl_dir)
    except xdmod.DeployError as exc:
        raise HTTPException(400, str(exc)) from exc
    argv = ["run"]
    if opts.data_dir.strip():
        argv += ["--data-dir", opts.data_dir.strip()]
    if opts.dl_dir.strip():
        argv += ["--dl-dir", opts.dl_dir.strip()]
    log = Path(cfgmod.STATE_DIR) / "xunlei-deploy.log"
    logfh = None
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        logfh = open(log, "a", encoding="utf-8")
    except OSError:
        logfh = None  # 日志不可写不阻塞部署
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent / "xunlei_docker.py"), *argv],
            start_new_session=True,
            stdout=logfh if logfh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
    finally:
        # Popen 已把句柄带入子进程，父进程这份及时关闭（避免句柄泄漏）
        if logfh is not None:
            logfh.close()
    return {"ok": True, "started": True,
            "detail": "已开始部署：拉取镜像可能需要几分钟，进度见卡片内状态"}


# ------------------------------------------------------------ 迅雷风控短信验证

def _tc():
    impl = _manager().backends().get("thunder_cloud")
    if impl is None:
        raise HTTPException(404, "迅雷远程通道不可用")
    return impl


@app.get("/api/thunder_cloud/review")
def tc_review_state():
    return _tc().review_state()


@app.post("/api/thunder_cloud/review/send")
def tc_review_send():
    try:
        return _tc().review_send_sms()
    except BackendError as exc:
        return {"ok": False, "detail": str(exc)}


class SmsCheckReq(BaseModel):
    code: str = ""


@app.post("/api/thunder_cloud/review/check")
def tc_review_check(req: SmsCheckReq):
    try:
        return _tc().review_check_sms(req.code)
    except BackendError as exc:
        return {"ok": False, "detail": str(exc)}


# ------------------------------------------------------------ 配置

@app.get("/api/config")
def config_get():
    return cfgmod.redacted(cfgmod.load())


class ConfigPut(BaseModel):
    config: dict


@app.put("/api/config")
def config_put(req: ConfigPut):
    cur = cfgmod.load()
    incoming = req.config or {}
    # 传入 ****** 的敏感字段表示“保持不变”，不覆盖
    tc_in = incoming.get("thunder_cloud", {})
    for key in ("password", "access_token", "refresh_token"):
        if tc_in.get(key) == "******":
            tc_in[key] = cur.get("thunder_cloud", {}).get(key, "")
    if incoming.get("aria2", {}).get("secret") == "******":
        incoming["aria2"]["secret"] = cur.get("aria2", {}).get("secret", "")
    # update.sources 数组整体替换；其中 token 传 ****** 表示“保持不变”
    upd_in = incoming.get("update", {})
    if isinstance(upd_in, dict) and isinstance(upd_in.get("sources"), list):
        cur_tokens = {s.get("name"): s.get("token", "")
                      for s in cur.get("update", {}).get("sources", [])
                      if isinstance(s, dict)}
        for s in upd_in["sources"]:
            if isinstance(s, dict) and s.get("token") == "******":
                s["token"] = cur_tokens.get(s.get("name"), "")
    # 分段合并而非整段替换：表单只带表单键，device_id / 登录令牌 /
    # 风控验证状态（review_url/review_token）等隐藏键必须保留。
    merged_cfg = {**cur, **incoming}
    for section, vals in incoming.items():
        if isinstance(vals, dict) and isinstance(cur.get(section), dict):
            merged_cfg[section] = {**cur[section], **vals}
    merged = cfgmod.save(merged_cfg)
    return cfgmod.redacted(merged)


# ------------------------------------------------------------ 目录/授权

AUTH_HINT = ("到 应用中心 → 飞牛下载助手 → 应用设置 → 授权目录 授予该目录读写权限"
             "（fnOS 系统机制）。下载文件由引擎写入，若引擎报目录不可写，"
             "还需在对应引擎（迅雷插件 / aria2）自身的设置里确认该目录可写。")


@app.get("/api/fs/paths")
def fs_paths():
    return {
        "share_dir": cfgmod.share_dir(),
        "accessible": cfgmod.accessible_paths(),
    }


@app.get("/api/fs/check")
def fs_check(path: str):
    """目录可用性检测：是否存在、本应用是否可写（实际写探针，不靠 os.access）。"""
    target = Path(path).expanduser()
    exists = target.is_dir()
    writable = False
    if exists:
        try:
            fd, tmp = tempfile.mkstemp(prefix=".fndl-check-", dir=str(target))
            os.close(fd)
            os.unlink(tmp)
            writable = True
        except OSError:
            writable = False
    return {
        "path": path,
        "exists": exists,
        "writable": writable,
        "ok": exists and writable,
        "share_dir": cfgmod.share_dir(),
        "hint": "" if (exists and writable) else AUTH_HINT,
    }


# ------------------------------------------------------------ 解析/派发

class ParseReq(BaseModel):
    text: str


@app.post("/api/parse")
def parse_links(req: ParseReq):
    return {"links": [li.to_dict() for li in linksmod.parse_text(req.text)]}


class TaskCreateReq(BaseModel):
    links: list[str] = []
    text: str = ""
    backend: str = "auto"
    directory: str | None = None


@app.post("/api/tasks")
def create_tasks(req: TaskCreateReq):
    infos = [linksmod.parse_line(u) for u in req.links if u.strip()]
    if req.text.strip():
        infos.extend(linksmod.parse_text(req.text))
    if not infos:
        raise HTTPException(400, "没有可派发的链接")
    mgr = _manager()
    directory = req.directory or mgr.cfg.get("default_dir") or None
    results = mgr.dispatch_many(infos, backend=req.backend, directory=directory)
    return {"results": results}


@app.get("/api/tasks")
def list_tasks():
    mgr = _manager()
    tasks, errors = mgr.unified_tasks()
    return {"tasks": tasks, "backend_errors": errors}


class HistoryCheckReq(BaseModel):
    links: list[str] = []
    text: str = ""


@app.post("/api/history/check")
def history_check(req: HistoryCheckReq):
    """下载历史查重：查询链接是否派发过（magnet 按信息哈希归一）。"""
    mgr = _manager()
    urls = [u.strip() for u in req.links if u.strip()]
    if req.text.strip():
        urls.extend(li.url for li in linksmod.parse_text(req.text) if li.url)
    if not urls:
        raise HTTPException(400, "没有可查询的链接")
    return {"results": [mgr.history_check(u) for u in urls]}


@app.post("/api/tasks/{backend}/{task_id}/pause")
def pause_task(backend: str, task_id: str):
    return _control("pause", backend, task_id)


@app.post("/api/tasks/{backend}/{task_id}/resume")
def resume_task(backend: str, task_id: str):
    return _control("resume", backend, task_id)


@app.post("/api/tasks/{backend}/{task_id}/retry")
def retry_task(backend: str, task_id: str):
    """失败/已清理任务重试：删旧任务后按原始链接原通道重新派发；
    原通道撞每日配额时自动换通道（结果带 switched/message）。"""
    mgr = _manager()
    try:
        res = mgr.retry(backend, task_id)
    except Exception as exc:  # noqa: BLE001 - 统一转 HTTP 错误
        raise HTTPException(502, str(exc))
    return {"ok": True, "backend": res.get("backend"),
            "id": res.get("id"), "label": res.get("label"),
            "switched": bool(res.get("switched")),
            "message": res.get("message", "")}


@app.delete("/api/tasks/{backend}/{task_id}")
def delete_task(backend: str, task_id: str):
    return _control("delete", backend, task_id)


def _control(action: str, backend: str, task_id: str):
    mgr = _manager()
    try:
        mgr.control(action, backend, task_id)
    except Exception as exc:  # noqa: BLE001 - 统一转 HTTP 错误
        raise HTTPException(502, str(exc))
    return {"ok": True}


# ------------------------------------------------------------ 任务队列（每日定时派发）

@app.get("/api/queue")
def queue_state():
    return {"ok": True, **_queue().snapshot()}


class QueueAddReq(BaseModel):
    links: list[str] = []
    text: str = ""
    directory: str | None = None


@app.post("/api/queue")
def queue_add(req: QueueAddReq):
    urls = [u.strip() for u in req.links if u.strip()]
    if req.text.strip():
        urls.extend(li.url for li in linksmod.parse_text(req.text) if li.url)
    if not urls:
        raise HTTPException(400, "没有可入队的链接")
    q = _queue()
    added = q.add(urls, directory=req.directory or None)
    return {"ok": True, "added": [i["id"] for i in added], **q.snapshot()}


@app.delete("/api/queue/{item_id}")
def queue_remove(item_id: str):
    q = _queue()
    if not q.remove(item_id):
        raise HTTPException(404, "队列项不存在")
    return {"ok": True, **q.snapshot()}


@app.post("/api/queue/run")
def queue_run():
    """立即派发全部待发项（auto 降级链）。"""
    mgr = _manager()
    q = _queue()
    results = q.run_now(_queue_dispatch(mgr))
    return {"ok": True, "results": results, **q.snapshot()}


class QueueSettingsReq(BaseModel):
    enabled: bool = False
    time: str = "00:05"


@app.post("/api/queue/settings")
def queue_settings(req: QueueSettingsReq):
    q = _queue()
    try:
        q.set_settings(enabled=req.enabled, time_str=req.time)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **q.snapshot()}


@app.on_event("startup")
def _queue_worker_start():
    """队列定时派发守护线程：每 30s 检查一次，到点且当天未跑过则派发待发项。"""
    def _loop():
        while True:
            time.sleep(30)
            try:
                q = _queue()
                if not q.should_run():
                    continue
                mgr = _manager()
                q.tick(_queue_dispatch(mgr))
            except Exception:  # noqa: BLE001 - 后台线程绝不因异常退出
                pass
    threading.Thread(target=_loop, daemon=True, name="queue-ticker").start()


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"内部错误：{exc}"})
