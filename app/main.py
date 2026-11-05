#!/usr/bin/env python3
"""fnmedia-dl FastAPI 服务：管理页 + JSON API。

页面：
  GET  /                     管理页（单页 UI）
  GET  /static/*             静态资源
接口：
  GET  /api/health           健康检查（含版本号）
  GET  /api/update/check     在线更新检查（GitHub Releases）
  GET  /api/backends         各下载通道状态
  POST /api/backends/test    单通道连通性测试
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
  POST /api/tasks/{backend}/{id}/pause|resume
  DELETE /api/tasks/{backend}/{id}
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config as cfgmod
import links as linksmod
from backends.base import BackendError
from downloader import DownloadManager

UI_DIR = Path(__file__).resolve().parent / "ui"
GITHUB_REPO = "maglcal/fnmedia-dl"


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


def _manager() -> DownloadManager:
    return DownloadManager(cfgmod.load())


# ------------------------------------------------------------ 页面

@app.get("/")
def index():
    return FileResponse(str(UI_DIR / "index.html"))


# ------------------------------------------------------------ 基础

@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "name": "fnmedia-dl"}


def _ver_tuple(v: str) -> tuple[int, int, int]:
    nums: list[int] = []
    for piece in (v or "").split("."):
        m = re.match(r"\d+", piece)
        nums.append(int(m.group()) if m else 0)
        if len(nums) == 3:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


@app.get("/api/update/check")
def update_check():
    """在线更新：查 GitHub Releases 最新版（境外接口，trust_env 放行走代理）。"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        resp = httpx.get(url, timeout=15, trust_env=True, headers={
            "accept": "application/vnd.github+json",
            "user-agent": f"fnmedia-dl/{VERSION}"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"连接 GitHub 失败（可检查网络/代理设置）：{exc}")
    if resp.status_code == 404:
        return {"ok": True, "current": VERSION, "latest": VERSION,
                "has_update": False, "detail": "仓库暂无发布版本"}
    if resp.status_code != 200:
        raise HTTPException(502, f"GitHub API 返回 HTTP {resp.status_code}")
    data = resp.json()
    latest = str(data.get("tag_name") or "").lstrip("vV").strip()
    has = bool(latest) and _ver_tuple(latest) > _ver_tuple(VERSION)
    return {
        "ok": True, "current": VERSION, "latest": latest or VERSION,
        "has_update": has,
        "url": data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases",
        "notes": (data.get("body") or "")[:2000],
        "assets": [{"name": a.get("name"), "url": a.get("browser_download_url"),
                    "size": a.get("size")}
                   for a in (data.get("assets") or [])],
    }


class BackendTestReq(BaseModel):
    backend: str


@app.get("/api/backends")
def backends_status():
    mgr = _manager()
    out = {}
    for key, impl in mgr.backends().items():
        out[key] = {"label": impl.label, **impl.status()}
    return out


@app.post("/api/backends/test")
def backends_test(req: BackendTestReq):
    mgr = _manager()
    pool = mgr.backends()
    impl = pool.get(req.backend)
    if not impl:
        raise HTTPException(404, f"未知通道：{req.backend}")
    return {"backend": req.backend, **impl.status()}


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


@app.post("/api/tasks/{backend}/{task_id}/pause")
def pause_task(backend: str, task_id: str):
    return _control("pause", backend, task_id)


@app.post("/api/tasks/{backend}/{task_id}/resume")
def resume_task(backend: str, task_id: str):
    return _control("resume", backend, task_id)


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


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"内部错误：{exc}"})
