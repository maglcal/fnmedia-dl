#!/usr/bin/env python3
"""网关转发回归（fake socket，无真实网络）。

背景（2026-11-05 404 事故）：_handle 原来只改写每条连接的首个请求，
keep-alive 连接上的后续请求被盲转发、带 /app/fnmedia-dl 前缀直达
uvicorn → FastAPI 返回 {"detail": "Not Found"}。修复后单请求单连接
（Connection: close），任何请求都必须先剥离前缀再转发。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import webui_gateway                                     # noqa: E402

PREFIX = webui_gateway.APP_PREFIX


class FakeSocket:
    """脚本化 socket：recv 依次吐出预置块，sendall 累积记录。"""

    def __init__(self, recv_chunks: list[bytes] | None = None):
        self._chunks = list(recv_chunks or [])
        self.sent = b""
        self.closed = False

    def recv(self, n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, t) -> None:
        pass

    def connect(self, addr) -> None:
        pass

    def shutdown(self, how) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _upstream_shim(upstream: FakeSocket):
    """让 _handle 内部 socket.socket(AF_UNIX, ...) 拿到 fake 上游。"""
    return SimpleNamespace(
        socket=lambda fam, typ: upstream,
        AF_UNIX=1, SOCK_STREAM=1, SHUT_WR=2,
    )


def _run_handle(client: FakeSocket, upstream: FakeSocket) -> None:
    orig = webui_gateway.socket
    webui_gateway.socket = _upstream_shim(upstream)
    try:
        webui_gateway._handle(client, Path("/fake/upstream.sock"))
    finally:
        webui_gateway.socket = orig


def _req(method: str, path: str, body: bytes = b"", headers: str = "") -> bytes:
    head = f"{method} {path} HTTP/1.1\r\nHost: nas\r\n"
    if body:
        head += f"Content-Length: {len(body)}\r\n"
    head += headers + "\r\n"
    return head.encode() + body


RESPONSE = (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            b"Content-Type: application/json\r\n\r\n{}")


class TestRewrite(unittest.TestCase):
    def test_prefix_stripped(self):
        out = webui_gateway._rewrite_request(
            _req("GET", PREFIX + "/api/config"))
        self.assertTrue(out.startswith(b"GET /api/config HTTP/1.1\r\n"))

    def test_root_mapped(self):
        for p in (PREFIX, PREFIX + "/", PREFIX + "/?x=1", "/"):
            out = webui_gateway._rewrite_request(_req("GET", p))
            line = out.split(b"\r\n")[0]
            self.assertNotIn(b"app/fnmedia-dl", line, p)

    def test_forces_connection_close(self):
        out = webui_gateway._rewrite_request(
            _req("GET", PREFIX + "/", headers="Connection: keep-alive\r\n"))
        self.assertNotIn(b"keep-alive", out.lower())
        self.assertIn(b"Connection: close", out)

    def test_strips_expect(self):
        out = webui_gateway._rewrite_request(
            _req("POST", PREFIX + "/api/x",
                 headers="Expect: 100-continue\r\n"))
        self.assertNotIn(b"expect", out.lower())


class TestRelay(unittest.TestCase):
    def test_request_reaches_upstream_rewritten(self):
        client = FakeSocket([_req("POST", PREFIX + "/api/backends/test",
                                  b'{"backend":"thunder_cloud"}')])
        upstream = FakeSocket([RESPONSE])
        _run_handle(client, upstream)
        self.assertTrue(upstream.sent.startswith(
            b"POST /api/backends/test HTTP/1.1"), upstream.sent[:80])
        self.assertNotIn(b"app/fnmedia-dl", upstream.sent)
        self.assertIn(b'{"backend":"thunder_cloud"}', upstream.sent)
        self.assertIn(b"{}", client.sent)

    def test_pipelined_second_request_not_forwarded_raw(self):
        """同一连接上第二请求（响应后才到达的 keep-alive 复用）绝不能
        绕过改写直达上游（404 事故原因）。单请求单连接契约：
        第二个请求不转发，响应后连接关闭。"""
        client = FakeSocket([
            _req("GET", PREFIX + "/api/config"),
            _req("POST", PREFIX + "/api/backends/test", b"{}"),
        ])
        upstream = FakeSocket([RESPONSE])
        _run_handle(client, upstream)
        self.assertEqual(upstream.sent.count(b"HTTP/1.1"), 1)
        self.assertNotIn(b"api/backends/test", upstream.sent)
        self.assertTrue(client.closed and upstream.closed)

    def test_chunked_body_forwarded_intact(self):
        chunked = (b"POST " + PREFIX.encode() + b"/api/tasks HTTP/1.1\r\n"
                   b"Host: nas\r\nTransfer-Encoding: chunked\r\n\r\n"
                   b"5\r\nhello\r\n3\r\n!!!\r\n0\r\n\r\n")
        client = FakeSocket([chunked])
        upstream = FakeSocket([RESPONSE])
        _run_handle(client, upstream)
        self.assertIn(b"hello", upstream.sent)
        self.assertIn(b"0\r\n\r\n", upstream.sent)

    def test_expect_continue_sent_before_body(self):
        client = FakeSocket([
            _req("POST", PREFIX + "/api/tasks", b"",
                 headers="Expect: 100-continue\r\nContent-Length: 2\r\n"),
            b"{}",
        ])
        upstream = FakeSocket([RESPONSE])
        _run_handle(client, upstream)
        self.assertIn(b"100 Continue", client.sent)
        self.assertIn(b"\r\n\r\n{}", upstream.sent)  # body 完整到达上游

    def test_every_fresh_connection_rewrites(self):
        """连续两条新连接（keep-alive 失效后的实际形态）都正确改写。"""
        for _ in range(2):
            client = FakeSocket([_req("GET", PREFIX + "/api/backends")])
            upstream = FakeSocket([RESPONSE])
            _run_handle(client, upstream)
            self.assertTrue(upstream.sent.startswith(b"GET /api/backends "),
                            upstream.sent[:60])


if __name__ == "__main__":
    unittest.main()
