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


def _run_handle(client: FakeSocket, upstream: FakeSocket,
                auth_token: str = "") -> None:
    orig = webui_gateway.socket
    webui_gateway.socket = _upstream_shim(upstream)
    try:
        webui_gateway._handle(client, Path("/fake/upstream.sock"),
                              auth_token=auth_token)
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


class TestAuthHeaderRewrite(unittest.TestCase):
    """P0-1：客户端自带 X-Fndl-Auth 必须被剥离，网关只注入自己的真值。"""

    def test_client_supplied_auth_stripped(self):
        out = webui_gateway._rewrite_request(
            _req("POST", PREFIX + "/api/tasks", b"{}",
                 headers="X-Fndl-Auth: forged\r\n"))
        self.assertNotIn(b"forged", out)
        self.assertNotIn(b"x-fndl-auth", out.lower())

    def test_client_supplied_auth_case_insensitive(self):
        out = webui_gateway._rewrite_request(
            _req("POST", PREFIX + "/api/tasks", b"{}",
                 headers="x-FnDl-AuTh: forged2\r\n"))
        self.assertNotIn(b"forged2", out)
        self.assertNotIn(b"fndl-auth", out.lower())

    def test_gateway_token_injected(self):
        out = webui_gateway._rewrite_request(
            _req("POST", PREFIX + "/api/tasks", b"{}"),
            auth_token="tok123")
        self.assertIn(b"X-Fndl-Auth: tok123", out)

    def test_forged_replaced_by_real_token(self):
        out = webui_gateway._rewrite_request(
            _req("POST", PREFIX + "/api/tasks", b"{}",
                 headers="X-Fndl-Auth: forged\r\n"),
            auth_token="realtok")
        self.assertNotIn(b"forged", out)
        self.assertIn(b"X-Fndl-Auth: realtok", out)
        self.assertEqual(out.count(b"X-Fndl-Auth"), 1)

    def test_empty_token_injects_nothing(self):
        out = webui_gateway._rewrite_request(
            _req("GET", PREFIX + "/api/config"),
            auth_token="")
        self.assertNotIn(b"fndl-auth", out.lower())

    def test_end_to_end_upstream_sees_gateway_token_only(self):
        client = FakeSocket([
            _req("POST", PREFIX + "/api/tasks", b"{}",
                 headers="X-Fndl-Auth: forged\r\n")])
        upstream = FakeSocket([RESPONSE])
        _run_handle(client, upstream, auth_token="realtok")
        self.assertIn(b"X-Fndl-Auth: realtok", upstream.sent)
        self.assertNotIn(b"forged", upstream.sent)
        self.assertEqual(upstream.sent.count(b"X-Fndl-Auth"), 1)


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


class TimeoutSocket(FakeSocket):
    """上游读超时：recv 抛 TimeoutError（socket.timeout 的内置别名）。"""

    def recv(self, n: int) -> bytes:
        raise TimeoutError("timed out")


class RefusedSocket(FakeSocket):
    """上游连接被拒：connect 抛 OSError。"""

    def connect(self, addr) -> None:
        raise ConnectionRefusedError("connection refused")


class SilentSocket(FakeSocket):
    """上游连接即断：recv 立刻回空字节，一个响应字节都没有。"""

    def recv(self, n: int) -> bytes:
        return b""


class TestExplicitErrors(unittest.TestCase):
    """0.1.31：中继层错误显式回 502/504 JSON——原实现超时/断连时
    静默关连接（except OSError: pass），桌面网关只能给浏览器回 502 白页，
    用户只见「派发失败 提示502」无线索（0.1.31 确认的直接原因）。"""

    def test_upstream_timeout_replies_504_json(self):
        client = FakeSocket([_req("POST", PREFIX + "/api/tasks", b"{}")])
        _run_handle(client, TimeoutSocket())
        self.assertIn(b"HTTP/1.1 504", client.sent)
        self.assertIn(b"application/json", client.sent)
        self.assertIn("上游响应超时".encode("utf-8"), client.sent)

    def test_connect_refused_replies_502_json(self):
        client = FakeSocket([_req("GET", PREFIX + "/api/tasks")])
        _run_handle(client, RefusedSocket())
        self.assertIn(b"HTTP/1.1 502", client.sent)
        self.assertIn(b"application/json", client.sent)
        self.assertIn("上游服务未就绪".encode("utf-8"), client.sent)

    def test_zero_byte_response_replies_502_json(self):
        """上游连接即断（0 字节响应）也必须显式回 JSON，不许静默收尾。"""
        client = FakeSocket([_req("GET", PREFIX + "/api/tasks")])
        _run_handle(client, SilentSocket())
        self.assertIn(b"HTTP/1.1 502", client.sent)
        self.assertIn("上游未返回任何响应".encode("utf-8"), client.sent)

    def test_partial_response_not_duplicated(self):
        """已回传部分响应后超时：不能补第二份响应（会拼成非法报文）。"""
        class PartialThenTimeout(FakeSocket):
            def __init__(self):
                super().__init__([b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nxx"])
                self._n = 0

            def recv(self, n: int) -> bytes:
                self._n += 1
                if self._n == 1:
                    return self._chunks.pop(0)
                raise TimeoutError("timed out")

        client = FakeSocket([_req("GET", PREFIX + "/api/tasks")])
        _run_handle(client, PartialThenTimeout())
        self.assertIn(b"HTTP/1.1 200 OK", client.sent)
        self.assertNotIn(b"504", client.sent)
        self.assertEqual(client.sent.count(b"HTTP/1.1"), 1)


if __name__ == "__main__":
    unittest.main()
