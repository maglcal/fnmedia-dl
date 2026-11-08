#!/usr/bin/env python3
"""fnmedia-dl 配置存取：state/config.json（chmod 600）。"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
CONFIG_PATH = STATE_DIR / "config.json"

DEFAULT_CONFIG: dict = {
    # 下载通道优先级（auto 派发时按序尝试，失败自动降级）
    "engine_order": ["thunder_cloud", "thunder_engine", "aria2"],
    "default_backend": "auto",          # auto | thunder_cloud | thunder_engine | aria2
    "default_dir": "",                  # 全局默认保存目录（空 = 各引擎默认）
    "aria2": {
        "rpc_url": "http://127.0.0.1:6800/jsonrpc",
        "secret": "",
        "dir": "",
    },
    "thunder_engine": {
        "base_url": "http://127.0.0.1:2345",
        "dir": "",
    },
    "thunder_cloud": {
        "login_name": "",
        "password": "",
        "device_id": "",                # 32 位 hex；留空自动生成并持久化
        "target": "",                   # 远程设备 target；留空自动发现
        "dir": "",
        "creditkey": "",                # 风控验证（review_panel）信任密钥；登录成功后自动清空
        # 登录态（自动持久化，勿手改）
        "access_token": "",
        "token_type": "Bearer",
        "refresh_token": "",
        "user_id": "",
    },
    # 在线更新源：按序尝试，第一个可用为准；下载失败自动换下一个源。
    # repobox = 自建仓库（your-repo.example.com，raw 公开可读无需 token；token 仅同步写入用，
    #           勿写进公开配置——检查/下载是只读操作）
    # github = 官方源（备用，自建源连不上/超时/报错时自动切换并提示）
    "update": {
        "sources": [
            {"name": "repobox", "type": "repobox",
             "base_url": "https://your-repo.example.com", "repo": "fnmedia-dl", "token": ""},
            {"name": "github", "type": "github",
             "repo": "maglcal/fnmedia-dl"},
        ],
        "timeout": 15,                  # 单次请求超时（秒）
        "retries": 2,                   # 单个源的请求尝试次数
    },
}

_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _lock:
        if CONFIG_PATH.is_file():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return _merge(DEFAULT_CONFIG, data)
            except (OSError, ValueError):
                pass
        return copy.deepcopy(DEFAULT_CONFIG)


def save(cfg: dict) -> dict:
    merged = _merge(DEFAULT_CONFIG, cfg)
    with _lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    return merged


def redacted(cfg: dict) -> dict:
    """对外展示用：隐去密码与令牌。"""
    out = copy.deepcopy(cfg)
    tc = out.get("thunder_cloud", {})
    for key in ("password", "access_token", "refresh_token"):
        if tc.get(key):
            tc[key] = "******"
    if out.get("aria2", {}).get("secret"):
        out["aria2"]["secret"] = "******"
    for s in out.get("update", {}).get("sources", []) or []:
        if isinstance(s, dict) and s.get("token"):
            s["token"] = "******"
    return out


# ---------------------------------------------------------------- 路径清单
# fnOS 打包形态下，共享目录 / 授权目录由 cmd 生命周期脚本落盘到
# state/paths.env（systemd 服务拿不到 TRIM_* 环境变量）。
# 环境变量优先（开发/测试可直接注入覆盖）。

def _paths_env_file() -> Path:
    return STATE_DIR / "paths.env"


def runtime_paths() -> dict:
    """读取 FNDL_SHARES_DIR / FNDL_DATA_DIR / FNDL_ACCESSIBLE_PATHS（文件 + 环境变量覆盖）。"""
    out = {"FNDL_SHARES_DIR": "", "FNDL_DATA_DIR": "", "FNDL_ACCESSIBLE_PATHS": ""}
    pf = _paths_env_file()
    if pf.is_file():
        try:
            for line in pf.read_text(encoding="utf-8").splitlines():
                key, sep, val = line.partition("=")
                if sep and key.strip() in out:
                    out[key.strip()] = val.strip()
        except OSError:
            pass
    for key in out:
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def share_dir() -> str:
    """应用共享目录（data-share 创建的默认下载目录，免授权）。"""
    return runtime_paths().get("FNDL_SHARES_DIR", "")


def data_dir() -> str:
    """应用数据共享目录（data-share 的 fnmedia-dl 根共享，持久化运行数据）。"""
    return runtime_paths().get("FNDL_DATA_DIR", "")


def accessible_paths() -> list[str]:
    """用户在 应用设置 → 授权目录 中授予的目录列表。"""
    raw = runtime_paths().get("FNDL_ACCESSIBLE_PATHS", "")
    return [p for p in raw.split(":") if p]
