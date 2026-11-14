#!/usr/bin/env python3
"""跨进程文件锁（零依赖）：状态文件「读-改-写」临界区互斥。

背景（2026-11-13 代码审查 P0-2）：tasks.json / config.json / quota.json 都是
「读整个文件 → 改内存 → 整体写回」模式。tmp+rename 只防撕裂写、防不了丢
更新；threading.Lock 只护本进程线程，而 DownloadManager 每请求新建、升级
重启窗口还可能两进程并存——并发读改写互相覆盖。必须把「读-改-写」整段
锁住。

实现：POSIX 用 fcntl.flock（LOCK_EX），Windows 用 msvcrt.locking——
测试在 Windows 跑、生产在 fnOS（Linux），两侧都要正确。
锁落在 <数据文件>.lock 伴生文件上，不碰数据文件本身。

注意：同一进程内对同一把锁**不可重入**（flock 按打开文件描述符计），
调用方不得在 locked() 区间内再对同一路径加锁。
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

_TIMEOUT = 10.0
_IS_WIN = sys.platform == "win32"


@contextlib.contextmanager
def locked(path: Path, timeout: float = _TIMEOUT):
    """互斥访问 path 对应的 .lock 伴生文件（不碰数据文件本身）。"""
    lock_path = Path(str(path) + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except PermissionError as exc:
        # 0.1.41.2 事故教训：root 身份写 state 会留下 root 属主 0600 的
        # 锁文件/目录，服务用户打不开 → 所有写配置路径全挂。报错要说人话。
        raise PermissionError(
            f"无法打开状态文件锁 {lock_path}（权限不足）。"
            "多为 state 目录/文件属主不是服务用户（root 身份写入过 state）；"
            "修复属主后重试"
        ) from exc
    try:
        _acquire(fd, timeout)
        try:
            yield
        finally:
            _release(fd)
    finally:
        os.close(fd)


def _acquire(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if _IS_WIN:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("获取文件锁超时（另一进程长时间占用状态文件）")
            time.sleep(0.05)


def _release(fd: int) -> None:
    try:
        if _IS_WIN:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
