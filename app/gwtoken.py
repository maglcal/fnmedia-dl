#!/usr/bin/env python3
"""网关共享密钥（X-Fndl-Auth）：本地 socket 链路的不可伪造鉴权凭据。

背景（2026-11-13 代码审查 P0-1）：管理鉴权此前只看 X-Trim-* 头——那只是
普通 HTTP 头，本地任意进程连上 socket 就能伪造 `X-Trim-Isadmin: true`，
且「不带头直接放行」。修复后的信任链：

  1. 桌面网关（webui_gateway）转发时注入本密钥，客户端自带的一律剥掉；
  2. main._admin_guard 对写操作恒校验密钥，缺失/不符一律 401；
  3. 仅 FNDL_DEV_TRUST=1 的本地调试环境放行无密钥请求（显式开关，
     生产绝不设置）。

密钥落 state/gateway.token（0600，仅应用用户可读），首次使用自动生成、
此后恒定；不进任何公开文件/安装包。生产 socket 已收紧 0600（仅 root 与
应用属主可连），密钥是第二道闸——即使 socket 权限被文件系统重置也能挡住
伪造直连。
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from pathlib import Path

import config as cfgmod

TOKEN_FILE = "gateway.token"
HEADER = "x-fndl-auth"


def token_path(state_dir: Path | None = None) -> Path:
    return Path(state_dir or cfgmod.STATE_DIR) / TOKEN_FILE


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def load_or_create_token(state_dir: Path | None = None) -> str:
    """读取网关共享密钥；不存在则生成（0600）。并发创建用 O_EXCL 收敛到同一值。"""
    p = token_path(state_dir)
    tok = _read(p)
    if tok:
        return tok
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(32)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(10):           # 胜者可能正在写盘：先等它落盘再判空，
            existing = _read(p)       # 防误删在写文件导致并发双方拿到不同密钥
            if existing:
                return existing       # 并发创建：以先落盘者为准
            time.sleep(0.05)
        try:
            p.unlink()               # 持续为空才是残留空文件（创建中断）：删掉重建一次
        except OSError:
            pass
        return load_or_create_token(state_dir)
    try:
        os.write(fd, tok.encode("ascii"))
    finally:
        os.close(fd)
    return tok


def verify(provided: str | None, state_dir: Path | None = None) -> bool:
    """常量时间比对请求携带的密钥（防时序侧信道）；非法编码一律 False。"""
    try:
        got = (provided or "").encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(got, load_or_create_token(state_dir).encode("ascii"))
