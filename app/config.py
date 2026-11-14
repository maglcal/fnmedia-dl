#!/usr/bin/env python3
"""fnmedia-dl 配置存取：state/config.json（chmod 600）。"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path

import filelock

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
CONFIG_PATH = STATE_DIR / "config.json"

DEFAULT_CONFIG: dict = {
    # 下载通道优先级（auto 派发时按序尝试，失败自动降级）
    "engine_order": ["thunder_cloud", "thunder_engine", "aria2"],
    "default_backend": "auto",          # auto | thunder_cloud | thunder_engine | aria2
    "default_dir": "",                  # 全局默认保存目录（空 = 各引擎默认）
    "disabled_backends": [],            # 隐藏的下载通道 key（thunder_cloud/thunder_engine/aria2）；空 = 全部显示
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
    # 默认 Gitee 国内镜像仓（发行版）优先、GitHub 官方源兜底——镜像仓
    # 未建/未同步/异常时自动落入 GitHub 并透明提示，不误报「无更新」。
    # 个人镜像源（type=repobox）属**私有配置**——在本机 state/config.json 的
    # update.sources 里按下列形状自行添加（排最前优先），勿把个人镜像地址
    # 写进公开源码/默认配置/安装包：
    #   {"name": "repobox", "type": "repobox",
    #    "base_url": "https://your-repo.example.com", "repo": "<仓库名>", "token": ""}
    # repobox raw 清单/资产公开可读无需 token（token 仅同步写入用）；
    # base_url 留空自动跳过。只想单源的用户删掉多余条目，并在 update 段
    # 写 "sources_v": 2 跳过老配置迁移。
    "update": {
        "sources": [
            {"name": "gitee", "type": "gitee",
             "repo": "maglcal/fnmedia-dl"},
            {"name": "github", "type": "github",
             "repo": "maglcal/fnmedia-dl"},
        ],
        "timeout": 15,                  # 单次请求超时（秒）
        "retries": 2,                   # 单个源的请求尝试次数
    },
    # 匿名使用统计（默认关，详见《fnmedia-dl 统计上报客户端接入规格》）：
    # enabled  = 「参与匿名统计」开关；false = 不采集不上传（零足迹）。
    # endpoint = 统计服务根地址（不含路径），上报 POST {endpoint}/collect；
    #            留空 = 只本地统计不上报（开发/离线形态）。
    # 真实地址由打包链在安装/初始化时注入 state/config.json（D-7）；
    # 公开源码/默认配置/文档一律 stats.example.com 示例，此处保持空串。
    "stats": {
        "enabled": False,
        "endpoint": "",
    },
}

_lock = threading.Lock()


class ConfigError(RuntimeError):
    """配置文件无法读取（权限/属主错乱等），区别于「文件不存在」。

    0.1.41.2 事故教训：读失败被静默吞成默认配置，把「读不到」伪装成
    「用户没配置过」（登录信息凭空"丢失"），且后续 save() 会用默认值
    覆盖真实配置。必须大声失败，把真因露给用户。"""


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate_update_sources(cfg: dict) -> dict:
    """update.sources 老配置迁移（sources_v=2）：出厂单 github 源 →
    前置 gitee 国内镜像源。只对「出厂默认形状」触发；用户改过 sources
    的原样保留（仅盖版本戳）。sources_v 版本戳**不进 DEFAULT_CONFIG**——
    进了默认配置 merge 后恒为 2，迁移永不触发。"""
    upd = cfg.get("update")
    if not isinstance(upd, dict):
        return cfg
    ver = upd.get("sources_v")
    if isinstance(ver, (int, float)) and ver >= 2:
        return cfg
    srcs = upd.get("sources") or []
    old_default = [{"name": "github", "type": "github",
                    "repo": "maglcal/fnmedia-dl"}]
    if srcs == old_default:
        upd["sources"] = [{"name": "gitee", "type": "gitee",
                           "repo": "maglcal/fnmedia-dl"}, *srcs]
    upd["sources_v"] = 2
    return cfg


def _load_unlocked() -> dict:
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _migrate_update_sources(copy.deepcopy(DEFAULT_CONFIG))
    except OSError as exc:
        # 权限/IO 失败 ≠ 文件不存在：真实配置可能完好躺在盘上，只是读不到。
        # 回退默认配置会让用户误以为数据丢失，且 save() 会覆盖真实配置。
        raise ConfigError(
            f"无法读取配置文件 {CONFIG_PATH}（{exc.strerror or exc}）。"
            "多为 state 目录或文件属主不是服务用户（root 身份写入过 state）；"
            "修复属主后重试，配置数据不会丢失"
        ) from exc
    try:
        data = json.loads(text)
    except ValueError:
        # 文件损坏（非权限问题）：回退默认配置，但必须留痕
        print(f"[fnmedia-dl] 配置文件损坏，已回退默认配置：{CONFIG_PATH}",
              file=sys.stderr)
        return _migrate_update_sources(copy.deepcopy(DEFAULT_CONFIG))
    return _migrate_update_sources(_merge(DEFAULT_CONFIG, data))


def load() -> dict:
    with _lock:
        return _load_unlocked()


def _save_unlocked(cfg: dict) -> dict:
    merged = _migrate_update_sources(_merge(DEFAULT_CONFIG, cfg))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    return merged


def save(cfg: dict) -> dict:
    with _lock, filelock.locked(CONFIG_PATH):
        return _save_unlocked(cfg)


def update(mutator) -> dict:
    """读-改-写原子区间（P0-2 并发竞态）：文件锁内 load → mutator → save。

    mutator(cur) 就地修改 cur 并返回（或返回新 dict）；返回最终落盘配置。
    DownloadManager 每请求新建、升级窗口可能两进程并存——裸 load→save
    会互相覆盖，必须整段互斥。mutator 内不得再调 save/update（锁不重入）。"""
    with _lock, filelock.locked(CONFIG_PATH):
        cur = _load_unlocked()
        new = mutator(cur)
        return _save_unlocked(new if new is not None else cur)


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
