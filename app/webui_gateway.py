#!/usr/bin/env python3
"""飞牛桌面应用网关：/app/fnmedia-dl → fnmedia-dl FastAPI Unix socket。

App Center 桌面入口使用同源 HTTPS 路径 `/app/fnmedia-dl/`，并按
ui/config 中的 gatewaySocket 连接应用根目录下的 fnmedia-dl.sock。
本模块负责：
1. 接受网关注入的 HTTP 请求；
2. 去掉 `/app/fnmedia-dl` 前缀；
3. 原样转发到主服务的 Unix socket。

契约：单请求单连接。响应带 Connection: close，上游回完即双关。
绝不在同一连接上盲转发后续请求——keep-alive 复用会绕过前缀改写，
带 /app/fnmedia-dl 直达 uvicorn 返回 {"detail": "Not Found"}
（2026-11-05 404 事故原因）。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

APP_PREFIX = "/app/fnmedia-dl"
SOCK_NAME = "fnmedia-dl.sock"
MAX_HEADER = 1 << 20
MAX_BODY = 64 << 20  # 管理页提交数据很小；留出足够安全余量
# 上游响应等待上限。原 60s 是「派发失败 提示502」的直接原因（0.1.31 实测）：
# 同步派发链被慢/挂起的迅雷接口拖过 60s 时，中继静默断连不回响应，
# 桌面网关只能给浏览器回 502 白页。放宽到 180s 兜住慢批量，同时错误路径
# 改为显式回 JSON（见 _reply_error），任何失败都带原因不再裸 502。
RELAY_TIMEOUT = 180


def gateway_socket_path(repo: Path) -> Path:
    """网关 socket 落点。入参必须是 repo 目录：
    FPK 安装形态（repo 与 ui 同级）→ 应用根目录/<sock>；
    其他形态（手动部署）→ /run/fnmedia-dl/<sock>。"""
    parent = repo.resolve().parent
    if (parent / "ui").is_dir() and repo.name == "repo":
        return parent / SOCK_NAME
    return Path("/run/fnmedia-dl") / SOCK_NAME


def _recv_until_headers(client: socket.socket, initial: bytes = b"") -> tuple[bytes, bytes] | None:
    data = initial
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEADER:
            return None
        chunk = client.recv(8192)
        if not chunk:
            return None
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    return head, body


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    return int(value.strip())
                except ValueError:
                    return -1
    return 0


def _is_chunked(head: bytes) -> bool:
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"transfer-encoding":
                return b"chunked" in value.lower()
    return False


def _find_chunked_end(data: bytes) -> int:
    """chunked 体结束位置（含终止块与 trailer）；未收全返回 -1。"""
    pos = 0
    while True:
        eol = data.find(b"\r\n", pos)
        if eol < 0:
            return -1
        token = data[pos:eol].split(b";", 1)[0].strip()
        if not token:
            return -1
        try:
            size = int(token, 16)
        except ValueError:
            return -1
        if size < 0:
            return -1
        start = eol + 2
        if size == 0:
            # 终止块：0\r\n[trailer 头…]\r\n
            tail = data[start:]
            if tail.startswith(b"\r\n"):
                return start + 2
            idx = tail.find(b"\r\n\r\n")
            return -1 if idx < 0 else start + idx + 4
        end = start + size + 2  # 数据 + 块尾 CRLF
        if len(data) < end:
            return -1
        pos = end


def _recv_body(client: socket.socket, partial: bytes, head: bytes) -> bytes | None:
    """按 head 声明读完整请求体（Content-Length 或 chunked），失败返回 None。"""
    if _is_chunked(head):
        data = partial
        end = _find_chunked_end(data)
        while end < 0:
            if len(data) > MAX_BODY:
                return None
            chunk = client.recv(65536)
            if not chunk:
                return None
            data += chunk
            end = _find_chunked_end(data)
        return data[:end]
    length = _content_length(head)
    if length < 0 or length > MAX_BODY:
        return None
    body = partial
    while len(body) < length:
        chunk = client.recv(min(65536, length - len(body)))
        if not chunk:
            return None
        body += chunk
    return body[:length]


def _rewrite_request(head: bytes) -> bytes:
    lines = head.split(b"\r\n")
    parts = lines[0].split()
    if len(parts) >= 2:
        method, target = parts[0], parts[1].decode("latin-1")
        path = target.split("?", 1)[0]
        query = target[len(path):]  # "" 或 "?..."
        if path == APP_PREFIX or path == APP_PREFIX + "/":
            target = "/" + query
        elif path.startswith(APP_PREFIX + "/"):
            target = target[len(APP_PREFIX):]
            if not target.startswith("/"):
                target = "/" + target
        lines[0] = b" ".join([method, target.encode("latin-1"), parts[2]])

    # 逐头重建：剥掉 hop-by-hop 头（Expect 由网关代答、Connection 强制
    # close），其余原样保留；追加 Connection: close 保证单请求单连接。
    drop = {b"expect", b"connection", b"keep-alive", b"proxy-connection"}
    rebuilt = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name in drop:
            continue
        rebuilt.append(line)
    rebuilt.append(b"Connection: close")
    return b"\r\n".join(rebuilt) + b"\r\n\r\n"


def _reply_error(client: socket.socket, status: int, detail: str) -> None:
    """中继层错误显式回 JSON 响应（0.1.31）。

    原实现上游超时/断连时 `except OSError: pass` 静默关连接——桌面网关
    收不到任何响应只能自己回 502 白页，用户只见「提示502」无线索。
    显式回带原因的 JSON 后，UI 的 errText 直接显示失败层级与建议。"""
    reason = "Gateway Timeout" if status == 504 else "Bad Gateway"
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n").encode("latin-1")
    try:
        client.sendall(head + body)
    except OSError:
        pass


def _pump(src: socket.socket, dst: socket.socket, sent: list) -> None:
    """上游→客户端单向泵流。已回传字节数累加进 sent[0]（可变计数器：
    超时异常抛出时调用方仍能读到部分进度，决定能否补错误响应）。"""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
            sent[0] += len(data)
    except TimeoutError:
        raise
    except OSError:
        pass


def _handle(client: socket.socket, upstream_sock: Path) -> None:
    """单请求单连接：读一条完整请求 → 剥前缀改写 → 转发 → 响应读到 EOF 回传。

    上游连接失败/响应超时必须显式回 502/504 JSON（_reply_error）；
    只有已回传部分响应后才允许静默收尾（不能发第二份响应）。"""
    remote: socket.socket | None = None
    sent = [0]                     # 已回传响应字节数（跨异常可见）
    try:
        received = _recv_until_headers(client)
        if received is None:
            return
        head, partial_body = received

        # Expect: 100-continue 必须在读 body 之前确认，客户端才会发体。
        if b"\r\nexpect:" in b"\r\n" + head.lower():
            client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

        body = _recv_body(client, partial_body, head)
        if body is None:
            return

        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remote.settimeout(RELAY_TIMEOUT)
        try:
            remote.connect(str(upstream_sock))
        except OSError as exc:
            _reply_error(client, 502,
                         f"上游服务未就绪：无法连接主服务 {upstream_sock.name}"
                         f"（{exc}）——请在应用中心确认 fnmedia-dl 服务已启动，"
                         "或在管理页点「自诊断」查看服务状态")
            return
        try:
            remote.sendall(_rewrite_request(head) + body)
            # 只回传响应；不再从 client 盲泵数据到上游——同连接后续请求
            # 若被转发会绕过改写（404 事故原因），这里直接随连接关闭丢弃。
            _pump(remote, client, sent)
            if not sent[0]:
                # 上游连接即断、一个字节都没回：桌面网关拿到空响应同样
                # 只能回 502 白页——显式补 JSON 错误保住线索。
                _reply_error(client, 502,
                             "上游未返回任何响应（连接即断）：主服务可能正在"
                             "重启或崩溃——请稍后重试，或在管理页点「自诊断」"
                             "查看服务状态")
        except TimeoutError:
            if sent[0]:
                return       # 已回传部分响应，不能补第二份响应
            _reply_error(client, 504,
                         f"上游响应超时（>{RELAY_TIMEOUT}s）：主服务处理时间过长"
                         "——常见为迅雷接口慢/挂起拖长派发链；请在管理页点"
                         "「自诊断」定位慢点，或减少单次批量条数后重试")
        except OSError as exc:
            if not sent[0]:
                _reply_error(client, 502, f"上游传输中断：{exc}")
    except OSError:
        pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def serve(gateway_sock: Path, upstream_sock: Path) -> None:
    gateway_sock.parent.mkdir(parents=True, exist_ok=True)
    try:
        gateway_sock.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(gateway_sock))
    os.chmod(gateway_sock, 0o666)
    server.listen(64)
    print(f"[fndl-gateway] {gateway_sock} -> {upstream_sock}", flush=True)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client, upstream_sock), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    repo = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    upstream = Path(args[1]) if len(args) > 1 else Path("/run/fnmedia-dl/app.sock")
    serve(gateway_socket_path(repo), upstream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
