#!/usr/bin/env python3
"""三通道适配器测试（httpx.MockTransport，全程无网络）。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from backends.aria2 import Aria2                      # noqa: E402
from backends.base import BackendError, ParsedResource    # noqa: E402
from backends.drive_core import DriveCore, task_to_view  # noqa: E402
from backends.thunder_cloud import (                   # noqa: E402
    ALGORITHMS, CLIENT_ID, CLIENT_VERSION, PACKAGE_NAME, REVIEW_UA,
    ThunderCloud, captcha_sign, device_sign)
from backends.thunder_engine import ThunderEngine      # noqa: E402


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- aria2

class TestAria2(unittest.TestCase):
    def _mk(self, handler):
        return Aria2(_client(handler), {"rpc_url": "http://aria2/jsonrpc", "secret": "s3"})

    def _rpc_handler(self, store: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body["method"]
            params = body["params"]
            # token 前缀应存在
            self.assertEqual(params[0], "token:s3")
            result = store.get(method)
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"], "result": result})
        return handler

    def test_add_and_list(self):
        store = {
            "aria2.addUri": "gid001",
            "aria2.getVersion": {"version": "1.37.0"},
            "aria2.tellActive": [{
                "gid": "gid001", "status": "active",
                "totalLength": "1000", "completedLength": "250",
                "downloadSpeed": "100",
                "files": [{"path": "/dl/a.iso", "uris": []}],
            }],
            "aria2.tellWaiting": [], "aria2.tellStopped": [],
        }
        a2 = self._mk(self._rpc_handler(store))
        gid = a2.add("https://x/a.iso", directory="/dl")
        self.assertEqual(gid, "gid001")
        self.assertTrue(a2.status()["reachable"])
        tasks = a2.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].name, "a.iso")
        self.assertEqual(tasks[0].state, "running")
        self.assertAlmostEqual(tasks[0].progress, 25.0)
        self.assertEqual(tasks[0].speed, 100)

    def test_error(self):
        def handler(request):
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": "1",
                "error": {"code": 1, "message": "bad"}})
        a2 = self._mk(handler)
        self.assertFalse(a2.status()["reachable"])
        with self.assertRaises(BackendError):
            a2.add("https://x/a.iso")


# ---------------------------------------------------------------- drive core

DRIVE_TASKS = [{
    "id": "t1", "name": "demo", "phase": "PHASE_TYPE_RUNNING",
    "file_size": "2000", "status_size": "500",
    "params": {"url": "magnet:?xt=urn:btih:aa"},
}]


class TestDriveCore(unittest.TestCase):
    def _mk(self, handler):
        return DriveCore(_client(handler), "http://engine/drive/v1", lambda: {"h": "1"})

    def test_parse_resource(self):
        def handler(request):
            self.assertEqual(request.url.path, "/drive/v1/resource/list")
            body = json.loads(request.content)
            self.assertEqual(body["urls"], "magnet:?xt=urn:btih:aa")
            self.assertEqual(body["page_size"], 2000)
            return httpx.Response(200, json={
                "list": {"resources": [{
                    "name": "demo.bin", "file_size": 123, "file_count": 2,
                    "mime_type": "application/x-bittorrent", "file_id": "f1",
                }]}})
        core = self._mk(handler)
        r = core.parse_resource("magnet:?xt=urn:btih:aa")
        self.assertEqual(r.name, "demo.bin")
        self.assertEqual(r.size, 123)
        self.assertEqual(r.file_count, 2)

    def test_create_task_body(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"task": {"id": "new1"}})
        core = self._mk(handler)
        from backends.base import ParsedResource
        res = ParsedResource(name="demo", size=10, file_count=1,
                             mime_type="video/mp4", file_id="fid")
        tid = core.create_task("https://x/a", res, "space-1",
                               directory="/dl", parent_folder_id="pf1")
        self.assertEqual(tid, "new1")
        self.assertEqual(captured["type"], "user#download-url")
        self.assertEqual(captured["space"], "space-1")
        self.assertEqual(captured["params"]["target"], "space-1")
        self.assertEqual(captured["params"]["parent_folder_path"], "/dl")
        self.assertEqual(captured["params"]["parent_folder_id"], "pf1")
        self.assertEqual(captured["params"]["sub_file_index"], "--1,")
        self.assertEqual(captured["file_size"], "10")

    # ---- parent_folder_id 解析（2026-11-08 用户实测任务永远排队中 0.0% 的原因：
    #      缺省 parent_folder_id 的任务不会开始下载，已验证） ----

    def _folder_handler(self, *, config=None, files=None, made=None, post_seen=None):
        """目录解析链 mock：/device/config + GET/POST /drive/v1/files。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json=config or {})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": files or []})
            if path.endswith("/files") and request.method == "POST":
                if post_seen is not None:
                    post_seen.append(json.loads(request.content))
                return httpx.Response(200, json={"file": {"id": made or "made-1"}})
            return httpx.Response(200, json={"task": {"id": "t-x"}})
        return handler

    def test_resolve_root_by_config_hint(self):
        """下载根 = /device/config download_paths 与目录表 realPath 匹配。"""
        core = self._mk(self._folder_handler(
            config={"download_paths": ["/downloads"]},
            files=[{"id": "dir-1", "name": "downloads", "params": {"RealPath": "/downloads/"}},
                   {"id": "dir-2", "name": "other", "params": {"RealPath": "/other/"}}]))
        self.assertEqual(core.resolve_dest_folder("sp", ""),
                         ("dir-1", "/downloads/"))

    def test_resolve_root_single_folder_and_name(self):
        """无 /device/config：唯一顶层目录 / 常见下载根名兜底。"""
        core = self._mk(self._folder_handler(
            files=[{"id": "only", "name": "x", "params": {"RealPath": "/x/"}}]))
        self.assertEqual(core.resolve_dest_folder("sp", ""),
                         ("only", "/x/"))
        core = self._mk(self._folder_handler(
            files=[{"id": "a", "name": "aaa", "params": {"RealPath": "/aaa/"}},
                   {"id": "b", "name": "downloads", "params": {"RealPath": "/data/downloads/"}}]))
        self.assertEqual(core.resolve_dest_folder("sp", ""),
                         ("b", "/data/downloads/"))

    def test_resolve_root_heuristic_fallback(self):
        """最后兜底：首个顶层目录的 parent_id 或 id。"""
        core = self._mk(self._folder_handler(
            files=[{"id": "a", "name": "aaa", "parent_id": "root-9"},
                   {"id": "b", "name": "bbb", "parent_id": "root-9"}]))
        self.assertEqual(core.resolve_dest_folder("sp", ""), ("root-9", ""))

    def test_folders_shape_retry_space_shape(self):
        """目录列表 扩展形状（带 with）为空/报错时自动换 空间形状
        （无 with + device_space= 空参数，同生态实测可用）——0.1.28 用户
        实测其环境 扩展形状取不到目录表，整条解析链跟着崩。"""
        seen = []

        def handler(request):
            if request.url.path.endswith("/files") and request.method == "GET":
                seen.append(dict(request.url.params))
                if "with" in request.url.params:
                    return httpx.Response(200, json={"files": []})
                return httpx.Response(200, json={"files": [
                    {"id": "a", "name": "电影", "parent_id": "root-9"},
                    {"id": "b", "name": "动漫", "parent_id": "root-9"}]})
            return httpx.Response(200, json={})
        core = self._mk(handler)
        self.assertEqual(core.resolve_dest_folder("sp", ""), ("root-9", ""))
        self.assertEqual(len(seen), 2)          # 两种形状都试过
        self.assertIn("with", seen[0])
        self.assertNotIn("with", seen[1])
        self.assertIn("device_space", seen[1])

    def test_folders_shape_ladder_to_minimal(self):
        """引擎形状（扩展/空间）全挂时还有标准/极简形状兜底——
        0.1.29：云端 api-pan 网关 502 引擎形状，极简形状（无 filters/
        page_token/with/device_space）是兼容面最大的一种。"""
        seen = []

        def handler(request):
            if request.url.path.endswith("/files") and request.method == "GET":
                p = dict(request.url.params)
                seen.append(p)
                if "with" in p or "device_space" in p or "filters" in p:
                    return httpx.Response(502, text="bad gateway")
                return httpx.Response(200, json={"files": [
                    {"id": "a", "name": "电影", "parent_id": "root-9"},
                    {"id": "b", "name": "动漫", "parent_id": "root-9"}]})
            return httpx.Response(200, json={})
        core = self._mk(handler)
        self.assertEqual(core.resolve_dest_folder("sp", ""), ("root-9", ""))
        self.assertEqual(len(seen), 4)          # 扩展 → 空间 → 标准 → 极简
        self.assertNotIn("filters", seen[3])
        self.assertNotIn("page_token", seen[3])
        self.assertNotIn("with", seen[3])
        self.assertNotIn("device_space", seen[3])

    def test_folders_all_shapes_fail_aggregate_error(self):
        """全形状报错 → 聚合错误（每形状错误都保留，502/超时一眼可辨）。"""
        def handler(request):
            return httpx.Response(502, text="bad gateway")
        core = self._mk(handler)
        with self.assertRaises(BackendError) as ctx:
            core.resolve_dest_folder("sp", "")
        msg = str(ctx.exception)
        for label in ("扩展形状", "空间形状", "标准形状", "极简形状"):
            self.assertIn(label, msg)
        self.assertIn("502", msg)

    def test_cloud_folder_shapes_standard_first(self):
        """云端通道（_CloudDrive）api-pan 回退阶梯裁剪为标准/极简 2 形状
        （0.1.31）：引擎形状（with/device_space）对 api-pan 直接 502，
        多试纯属浪费响应预算。"""
        from backends.thunder_cloud import ThunderCloud
        tc = ThunderCloud(httpx.Client(), {}, None)
        labels = [label for label, _ in tc.core._folder_shapes("sp", "")]
        self.assertEqual(labels, ["标准形状", "极简形状"])

    def test_folders_kind_filter_client_side(self):
        """极简形状无服务端过滤：kind 非 drive#folder 的条目客户端兜底剔除。"""
        core = self._mk(self._folder_handler(files=[
            {"id": "f1", "name": "file.bin", "kind": "drive#file"},
            {"id": "d1", "name": "电影", "kind": "drive#folder"},
            {"id": "d2", "name": "剧集"}]))
        self.assertEqual(core.resolve_dest_folder("sp", ""), ("d1", ""))

    def test_resolve_subdir_walk_and_create(self):
        """子目录逐段解析，缺级自动建。"""
        made = []
        subs = [{"id": "sub-1", "name": "电影", "params": {"RealPath": "/downloads/电影/"}}]

        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/files") and request.method == "GET":
                pid = request.url.params.get("parent_id", "")
                if pid == "":
                    return httpx.Response(200, json={"files": [
                        {"id": "dir-1", "name": "downloads",
                         "params": {"RealPath": "/downloads/"}}]})
                return httpx.Response(200, json={"files": subs})
            if path.endswith("/files") and request.method == "POST":
                made.append(json.loads(request.content))
                return httpx.Response(200, json={"file": {"id": "made-%d" % len(made)}})
            return httpx.Response(200, json={})
        core = self._mk(handler)
        self.assertEqual(core.resolve_dest_folder("sp", "电影/2025"),
                         ("made-1", "/downloads/电影/2025/"))
        self.assertEqual(made, [{"parent_id": "sub-1", "name": "2025",
                                 "space": "sp", "kind": "drive#folder"}])

    def test_resolve_outside_root_keeps_root_id(self):
        """根外绝对目录不再报错（0.1.26 硬拒是回归）：保留根 id 让任务能
        开始下载，路径原样给引擎尽力落盘。"""
        core = self._mk(self._folder_handler(
            config={"download_paths": ["/downloads"]},
            files=[{"id": "dir-1", "name": "downloads", "params": {"RealPath": "/downloads/"}}]))
        self.assertEqual(core.resolve_dest_folder("sp", "/vol1/other"),
                         ("dir-1", "/vol1/other"))

    def test_create_task_resolves_parent_folder(self):
        """未显式给 parent_folder_id 时自动解析——这是任务开始下载的必要字段。"""
        captured = {}

        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": [
                    {"id": "dir-1", "name": "downloads",
                     "params": {"RealPath": "/downloads/"}}]})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"task": {"id": 4242}})
        core = self._mk(handler)
        tid = core.create_task("https://x/a", ParsedResource(name="a", size=1),
                               "space-1")
        self.assertEqual(tid, "4242")            # 引擎整型 id 归一字符串
        self.assertEqual(captured["params"]["parent_folder_id"], "dir-1")
        self.assertEqual(captured["params"]["parent_folder_path"], "/downloads/")

    # ---- 解析阶梯式降级（0.1.28）：id 缺省任务永远不启动——只要还解析得出
    #      目录 id 就带上 id 让任务能下载，路径尽力而为；全解析失败才报错 ----

    def test_create_task_partial_when_folder_setup_denied(self):
        """建目录被拒（引擎账号对该目录无读写权）→ 降级用根 id +
        完整目标路径，任务照常能开始下载。"""
        captured = {}

        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": [
                    {"id": "dir-1", "name": "downloads",
                     "params": {"RealPath": "/downloads/"}}]})
            if path.endswith("/files") and request.method == "POST":
                return httpx.Response(200, json={"error_code": 403,
                                                 "error": "permission denied"})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"task": {"id": "t-1"}})
        core = self._mk(handler)
        tid = core.create_task("https://x/a", ParsedResource(name="a", size=1),
                               "sp", directory="电影/2025")
        self.assertEqual(tid, "t-1")
        self.assertEqual(captured["params"]["parent_folder_id"], "dir-1")
        self.assertEqual(captured["params"]["parent_folder_path"],
                         "/downloads/电影/2025/")

    def test_create_task_keeps_root_for_dir_outside_root(self):
        """根外绝对目录：保留根 id + 路径原样（不报错也不丢 id）。"""
        captured = {}

        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": [
                    {"id": "dir-1", "name": "downloads",
                     "params": {"RealPath": "/downloads/"}}]})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"task": {"id": "t-2"}})
        core = self._mk(handler)
        tid = core.create_task("https://x/a", ParsedResource(name="a", size=1),
                               "sp", directory="/vol1/media/剧集")
        self.assertEqual(tid, "t-2")
        self.assertEqual(captured["params"]["parent_folder_id"], "dir-1")
        self.assertEqual(captured["params"]["parent_folder_path"],
                         "/vol1/media/剧集")

    def test_create_task_no_silent_task_when_root_unresolvable(self):
        """目录表全挂、任何 id 都拿不到时显式报错——静默建 id 空的任务 =
        永远排队中（0.1.27 回退直传已被用户实测证明不启动下载）。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": []})
            return httpx.Response(200, json={"task": {"id": "t-3"}})
        core = self._mk(handler)
        with self.assertRaises(BackendError):
            core.create_task("https://x/a", ParsedResource(name="a", size=1),
                             "sp", directory="/vol1/media")

    def test_create_task_empty_dir_needs_resolvable_root(self):
        """无设置目录且下载根解析失败 → 显式报错
        （回退两字段全空 = 任务永远排队中，静默建出来比报错更糟）。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": []})
            return httpx.Response(200, json={"task": {"id": "t-4"}})
        core = self._mk(handler)
        with self.assertRaises(BackendError):
            core.create_task("https://x/a", ParsedResource(name="a", size=1),
                             "sp")

    def test_create_task_engine_direct_fallback_when_unresolvable(self):
        """本地引擎最后兜底（on_unresolvable="direct"）：目录表查询全挂时
        回退 0.1.22 直传形状（id 空 + 路径原样）——引擎按路径自行落盘
        （用户环境实测可用）；默认（云端）仍报错不建死任务。"""
        captured = {}

        def handler(request):
            path = request.url.path
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(502, text="bad gateway")
            if request.method == "GET" and path.endswith("/tasks"):
                # 0.1.32 反查兜底会拉任务列表：无可反查条目 → 维持直传兜底
                return httpx.Response(200, json={"tasks": []})
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"task": {"id": "t-5"}})
        core = self._mk(handler)
        tid = core.create_task("https://x/a", ParsedResource(name="a", size=1),
                               "sp", directory="/vol1/media",
                               on_unresolvable="direct")
        self.assertEqual(tid, "t-5")
        self.assertEqual(captured["params"]["parent_folder_id"], "")
        self.assertEqual(captured["params"]["parent_folder_path"], "/vol1/media")

    def test_set_phase_and_delete(self):
        calls = []

        def handler(request):
            calls.append((request.method, str(request.url), request.content))
            return httpx.Response(200, json={})
        core = self._mk(handler)
        core.set_phase("sp", "user#download-url", "t9", "pause")
        patch = next(c for c in calls if c[0] == "PATCH")
        body = json.loads(patch[2])
        self.assertEqual(json.loads(body["set_params"]["spec"]), {"phase": "pause"})
        # 查不到任务时回退调用方给的 space/type
        self.assertEqual(body["space"], "sp")
        self.assertEqual(body["type"], "user#download-url")
        core.delete_task("sp", "t9")
        self.assertTrue(any(c[0] == "DELETE" and "task_ids=t9" in c[1]
                            for c in calls))

    def test_set_phase_uses_real_task_type_space(self):
        """任务操作回归（2026-11-05 用户实测暂停无效）：
        PATCH 前先查任务真实 space/type——硬编码值与任务不符时
        引擎静默忽略，UI 状态不翻转。"""
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "GET" and request.url.path.endswith("/tasks"):
                return httpx.Response(200, json={"tasks": [{
                    "id": "t9", "type": "user#download",
                    "space": "real-space", "phase": "PHASE_TYPE_RUNNING"}]})
            return httpx.Response(200, json={})
        core = self._mk(handler)
        core.set_phase("sp", "user#download-url", "t9", "pause")
        patch = next(c for c in calls if c.method == "PATCH")
        body = json.loads(patch.content)
        self.assertEqual(body["space"], "real-space")     # 任务真实 space
        self.assertEqual(body["type"], "user#download")   # 任务真实 type
        self.assertEqual(body["id"], "t9")

    def test_request_detects_error_payload(self):
        """HTTP 200 + 错误体必须报错：只看状态码会把暂停/删除的失败当成功。"""
        def handler(request):
            return httpx.Response(200, json={"error_code": 16,
                                             "errorDesc": "任务不存在"})
        core = self._mk(handler)
        with self.assertRaises(BackendError) as cm:
            core.delete_task("sp", "t9")
        self.assertIn("任务不存在", str(cm.exception))

    def test_request_success_payload_passes(self):
        def handler(request):
            return httpx.Response(200, json={"error": "success",
                                             "error_code": 0})
        core = self._mk(handler)
        core.delete_task("sp", "t9")     # 不抛即通过

    def test_task_to_view_unknown_phase_fallback(self):
        """未知 phase 拼写按关键词兜底（防暂停后状态不翻转、按钮卡住）。"""
        t = dict(DRIVE_TASKS[0], phase="PHASE_TYPE_STOPPED")
        self.assertEqual(task_to_view("x", t).state, "paused")
        t["phase"] = "PAUSED"
        self.assertEqual(task_to_view("x", t).state, "paused")
        t["phase"] = "PHASE_TYPE_FAIL"
        self.assertEqual(task_to_view("x", t).state, "error")
        t["phase"] = ""
        self.assertEqual(task_to_view("x", t).state, "waiting")

    def test_task_to_view(self):
        v = task_to_view("aria2-like", DRIVE_TASKS[0])
        self.assertEqual(v.state, "running")
        self.assertAlmostEqual(v.progress, 25.0)


# ---------------------------------------------------------------- thunder engine

PANEL_HTML = '<script>function uiauth(value){return "JWT-TOKEN-123";}</script>'


class TestThunderEngine(unittest.TestCase):
    def test_login_and_add(self):
        seen = {}

        def handler(request):
            path = request.url.path
            if path.endswith("index.cgi/"):
                return httpx.Response(200, text=PANEL_HTML)
            seen.setdefault(path, []).append(request)
            if path.endswith("/drive/v1/tasks"):
                return httpx.Response(200, json={"tasks": [{
                    "params": {"target": "dev-space"}}]})
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/drive/v1/files") and request.method == "GET":
                return httpx.Response(200, json={"files": [
                    {"id": "dir-1", "name": "downloads", "parent_id": "root-1",
                     "params": {"RealPath": "/downloads/"}}]})
            if path.endswith("/drive/v1/resource/list"):
                return httpx.Response(200, json={
                    "list": {"resources": [{"name": "f", "file_size": 1}]}})
            if path.endswith("/drive/v1/task") and request.method == "POST":
                return httpx.Response(200, json={"task": {"id": "eng-1"}})
            return httpx.Response(200, json={})

        eng = ThunderEngine(_client(handler), {"base_url": "http://nas:2345"})
        st = eng.status()
        self.assertTrue(st["reachable"], st)
        tid = eng.add("magnet:?xt=urn:btih:aa")
        self.assertEqual(tid, "eng-1")
        # pan-auth 头应携带从面板提取的 JWT
        task_req = seen["/webman/3rdparty/pan-xunlei-com/index.cgi/drive/v1/task"][0]
        self.assertEqual(task_req.headers.get("pan-auth"), "JWT-TOKEN-123")
        # parent_folder_id/path 必须是解析出的真实下载目录（缺失任务不会开始下载）
        body = json.loads(task_req.content)
        self.assertEqual(body["params"]["parent_folder_id"], "dir-1")
        self.assertEqual(body["params"]["parent_folder_path"], "/downloads/")

    def test_bad_panel(self):
        def handler(request):
            return httpx.Response(200, text="login page")
        eng = ThunderEngine(_client(handler), {"base_url": "http://nas:2345"})
        self.assertFalse(eng.status()["reachable"])


# ---------------------------------------------------------------- thunder cloud

class TestThunderCloudSign(unittest.TestCase):
    def test_captcha_sign_chain(self):
        ts, sign = captcha_sign("a" * 32)
        self.assertTrue(sign.startswith("1."))
        s = f"{CLIENT_ID}{CLIENT_VERSION}{PACKAGE_NAME}{'a' * 32}{ts}"
        for algo in ALGORITHMS:
            s = hashlib.md5((s + algo).encode()).hexdigest()
        self.assertEqual(sign, "1." + s)

    def test_device_sign(self):
        ds = device_sign("b" * 32)
        self.assertTrue(ds.startswith("div101." + "b" * 32))
        inner = hashlib.sha1(f"{'b' * 32}{PACKAGE_NAME}4034a062aaa22f906fca4fefe9fb3a3021".encode())
        # APP_ID="40", APP_KEY 拼接
        self.assertEqual(len(ds), len("div101.") + 32 + 32)


class TestThunderCloudFlow(unittest.TestCase):
    def _conf(self):
        return {"login_name": "13800000000", "password": "pw",
                "device_id": "c" * 32, "target": "remote-1"}

    def _handler(self, seen: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            seen.setdefault(url, []).append(request)
            if path.endswith("/xluser.core.login/v3/login"):
                return httpx.Response(200, json={"sessionID": "sess-1"})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct-1"})
            if path.endswith("/auth/signin/token"):
                return httpx.Response(200, json={
                    "token_type": "Bearer", "access_token": "at-1",
                    "refresh_token": "rt-1", "user_id": "u1"})
            if path.endswith("/user/me"):
                return httpx.Response(200, json={"user_id": "u1"})
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/drive/v1/files") and request.method == "GET":
                return httpx.Response(200, json={"files": [
                    {"id": "cdir-1", "name": "downloads", "parent_id": "root-1",
                     "params": {"RealPath": "/downloads/"}}]})
            if path.endswith("/drive/v1/tasks"):
                return httpx.Response(200, json={"tasks": [{
                    "params": {"target": "remote-1"}, "name": "runner"}]})
            if path.endswith("/drive/v1/resource/list"):
                return httpx.Response(200, json={
                    "list": {"resources": [{"name": "movie", "file_size": 5}]}})
            if path.endswith("/drive/v1/task") and request.method == "POST":
                return httpx.Response(200, json={"task": {"id": "cloud-1"}})
            return httpx.Response(200, json={})
        return handler

    def test_login_and_add(self):
        seen = {}
        saved = []
        tc = ThunderCloud(_client(self._handler(seen)), self._conf(),
                          persist=lambda c: saved.append(dict(c)))
        self.assertTrue(tc.status()["reachable"])
        tid = tc.add("magnet:?xt=urn:btih:aa")
        self.assertEqual(tid, "cloud-1")
        # 登录链依次发生
        keys = list(seen)
        self.assertIn("https://xluser-ssl.xunlei.com/xluser.core.login/v3/login", keys)
        self.assertIn("https://xluser-ssl.xunlei.com/v1/shield/captcha/init", keys)
        self.assertIn("https://xluser-ssl.xunlei.com/v1/auth/signin/token", keys)
        # 业务请求带 Bearer + X-Captcha-Token
        task_req = seen["https://api-pan.xunlei.com/drive/v1/task"][0]
        self.assertEqual(task_req.headers["authorization"], "Bearer at-1")
        self.assertEqual(task_req.headers["x-captcha-token"], "ct-1")
        # parent_folder_id/path 必须是解析出的真实下载目录（缺失任务不会开始下载）
        body = json.loads(task_req.content)
        self.assertEqual(body["params"]["parent_folder_id"], "cdir-1")
        self.assertEqual(body["params"]["parent_folder_path"], "/downloads/")
        # 令牌已持久化
        self.assertTrue(saved)
        self.assertEqual(saved[-1]["access_token"], "at-1")

    def test_token_refresh_on_error_code(self):
        seen = {}
        state = {"failed_once": False}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            seen.setdefault(url, []).append(request)
            if path.endswith("/auth/signin/token"):
                return httpx.Response(200, json={
                    "token_type": "Bearer", "access_token": "at-1",
                    "refresh_token": "rt-1", "user_id": "u1"})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct-1"})
            if path.endswith("/xluser.core.login/v3/login"):
                return httpx.Response(200, json={"sessionID": "sess-1"})
            if path.endswith("/auth/token"):
                return httpx.Response(200, json={
                    "token_type": "Bearer", "access_token": "at-2",
                    "refresh_token": "rt-2", "user_id": "u1"})
            if path.endswith("/drive/v1/resource/list"):
                if not state["failed_once"]:
                    state["failed_once"] = True
                    return httpx.Response(200, json={"error_code": 10, "error": "token expired"})
                return httpx.Response(200, json={
                    "list": {"resources": [{"name": "movie", "file_size": 5}]}})
            return httpx.Response(200, json={})

        conf = self._conf()
        conf["access_token"] = "at-1"
        conf["refresh_token"] = "rt-1"
        tc = ThunderCloud(_client(handler), conf)
        r = tc.parse("magnet:?xt=urn:btih:aa")
        self.assertEqual(r.name, "movie")
        self.assertIn("https://xluser-ssl.xunlei.com/v1/auth/token", seen)
        self.assertEqual(conf["access_token"], "at-2")

    def test_not_configured(self):
        tc = ThunderCloud(_client(lambda r: httpx.Response(200, json={})), {})
        st = tc.status()
        self.assertFalse(st["configured"])
        with self.assertRaises(BackendError):
            tc.add("https://x/a")


class TestThunderCloudFixes(unittest.TestCase):
    """登录链修复回归（2026-11-05 用户实测登录失败）：
    review_panel 透出验证数据、信用密钥一次性语义、
    人机令牌刷新 action 全路径、错误判定、忽略代理环境变量。"""

    def _conf(self):
        return {"login_name": "13800000000", "password": "pw",
                "device_id": "c" * 32, "target": "remote-1"}

    def test_review_panel_gives_verify_data(self):
        saved = []

        def handler(request):
            return httpx.Response(200, json={
                "error": "review_panel", "reviewurl": "https://verify.x/step",
                "creditkey": "ck-9"})

        tc = ThunderCloud(_client(handler), self._conf(),
                          persist=lambda c: saved.append(dict(c)))
        st = tc.status()
        self.assertFalse(st["reachable"])
        self.assertIn("https://verify.x/step&deviceid=" + device_sign("c" * 32),
                      st["detail"])
        self.assertIn("creditkey=ck-9", st["detail"])
        # 信任密钥自动保存，供完成验证后重试登录携带
        self.assertEqual(saved[-1]["creditkey"], "ck-9")

    def test_creditkey_sent_then_cleared(self):
        seen = {}

        def handler(request):
            path = request.url.path
            seen.setdefault(path, []).append(request)
            if path.endswith("/xluser.core.login/v3/login"):
                return httpx.Response(200, json={"sessionID": "sess-1"})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct-1"})
            if path.endswith("/auth/signin/token"):
                return httpx.Response(200, json={
                    "token_type": "Bearer", "access_token": "at-1",
                    "refresh_token": "rt-1", "user_id": "u1"})
            return httpx.Response(200, json={})

        conf = self._conf()
        conf["creditkey"] = "ck-old"
        tc = ThunderCloud(_client(handler), conf)
        tc.login()
        body = json.loads(seen["/xluser.core.login/v3/login"][0].content)
        self.assertEqual(body["creditkey"], "ck-old")   # 重试登录时携带
        self.assertEqual(conf["creditkey"], "")         # 成功后清空已消费密钥

    def test_captcha_refresh_action_full_path(self):
        seen = {}
        state = {"n": 0}

        def handler(request):
            path = request.url.path
            seen.setdefault(path, []).append(request)
            if path.endswith("/drive/v1/resource/list"):
                state["n"] += 1
                if state["n"] == 1:
                    return httpx.Response(200, json={"error_code": 9,
                                                     "error": "captcha"})
                return httpx.Response(200, json={
                    "list": {"resources": [{"name": "movie", "file_size": 5}]}})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct-2"})
            return httpx.Response(200, json={})

        conf = self._conf()
        conf.update({"access_token": "at-1", "refresh_token": "rt-1",
                     "user_id": "u1"})
        tc = ThunderCloud(_client(handler), conf)
        r = tc.parse("magnet:?xt=urn:btih:aa")
        self.assertEqual(r.name, "movie")
        init_body = json.loads(seen["/v1/shield/captcha/init"][-1].content)
        # action 必须是完整 URL 路径（含 /drive/v1 前缀）
        self.assertEqual(init_body["action"], "POST:/drive/v1/resource/list")
        # 登录后刷新走 user_id + captcha_sign 签名元数据
        self.assertEqual(init_body["meta"]["user_id"], "u1")
        self.assertIn("captcha_sign", init_body["meta"])

    def test_captcha_refresh_without_userid_keeps_signed_meta(self):
        """"登录后"刷人机令牌必须走签名 meta，
        user_id 为空也不能退化成账号形态 meta——服务端按形态区分签发场景，
        形态不对时照发 token 但 drive/v1 不认，陷入 error_code=9 循环
        （0.1.9 用户实测「验证码无效（error_code=9）」的原因）。"""
        seen = {}
        state = {"n": 0}

        def handler(request):
            path = request.url.path
            seen.setdefault(path, []).append(request)
            if path.endswith("/drive/v1/resource/list"):
                state["n"] += 1
                if state["n"] == 1:
                    return httpx.Response(200, json={"error_code": 9,
                                                     "error": "captcha"})
                return httpx.Response(200, json={
                    "list": {"resources": [{"name": "movie", "file_size": 5}]}})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct-2"})
            return httpx.Response(200, json={})

        conf = self._conf()
        conf.update({"access_token": "at-1", "refresh_token": "rt-1"})  # 无 user_id
        tc = ThunderCloud(_client(handler), conf)
        r = tc.parse("magnet:?xt=urn:btih:aa")
        self.assertEqual(r.name, "movie")
        meta = json.loads(seen["/v1/shield/captcha/init"][-1].content)["meta"]
        self.assertEqual(
            sorted(meta),
            sorted(["client_version", "package_name", "user_id",
                    "timestamp", "captcha_sign"]))
        self.assertEqual(meta["user_id"], "")          # 空值也要带键
        self.assertNotIn("phone_number", meta)          # 不许退化成账号形态
        self.assertNotIn("username", meta)
        self.assertNotIn("email", meta)

    def test_core_login_stores_userid(self):
        """v3 登录响应的 userID 是登录后刷人机令牌 meta 的首要来源。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/xluser.core.login/v3/login"):
                return httpx.Response(200, json={"sessionID": "sess-1",
                                                 "userID": "u-42"})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct-1"})
            if path.endswith("/auth/signin/token"):
                return httpx.Response(200, json={
                    "token_type": "Bearer", "access_token": "at-1",
                    "refresh_token": "rt-1"})
            return httpx.Response(200, json={})

        conf = self._conf()
        tc = ThunderCloud(_client(handler), conf)
        tc.login()
        self.assertEqual(conf["user_id"], "u-42")

    def test_apply_token_userid_fallback_sub(self):
        """signin/token 的 user_id 缺失时退 sub，再退 v3 已存值。"""
        conf = self._conf()
        tc = ThunderCloud(_client(lambda r: httpx.Response(200, json={})), conf)
        tc._apply_token({"access_token": "at", "sub": "sub-9"})
        self.assertEqual(conf["user_id"], "sub-9")
        tc._apply_token({"access_token": "at", "user_id": "u-2", "sub": "sub-9"})
        self.assertEqual(conf["user_id"], "u-2")
        tc._apply_token({"access_token": "at"})
        self.assertEqual(conf["user_id"], "u-2")        # 保留已存值

    def test_captcha_error9_final_message_distinguishes_sms(self):
        """"验证码无效（error_code=9）"是人机令牌校验失败、不是短信码输错，
        终局文案必须说清（0.1.9 用户被误导重输短信码）。"""
        def handler(request):
            if request.url.path.endswith("/drive/v1/resource/list"):
                return httpx.Response(200, json={"error_code": 9,
                                                 "error": "captcha"})
            return httpx.Response(200, json={"captcha_token": "ct"})

        conf = self._conf()
        conf.update({"access_token": "at-1", "user_id": "u1"})
        tc = ThunderCloud(_client(handler), conf)
        with self.assertRaises(BackendError) as cm:
            tc.parse("magnet:?xt=urn:btih:aa")
        msg = str(cm.exception)
        self.assertIn("人机令牌", msg)
        self.assertIn("不是短信验证码输错", msg)

    def test_list_tasks_paginates_all_pages(self):
        """任务列表自动翻页（0.1.12 用户实测「列表显示不全」）：
        drive/v1 /tasks 按 page_token 分页，只拉首页会漏任务。"""
        seen = []

        def handler(request):
            if request.url.path.endswith("/tasks"):
                if request.url.params.get("type") == "user#runner":
                    # 设备发现（_target_of 在线校验）——不计入翻页序列
                    return httpx.Response(200, json={"tasks": [
                        {"id": "r1", "name": "dev",
                         "params": {"target": "remote-1"}}]})
                seen.append(str(request.url))
                if not request.url.params.get("page_token", ""):
                    return httpx.Response(200, json={
                        "tasks": [{"id": "t1", "name": "a",
                                   "params": {"target": "remote-1"}}],
                        "next_page_token": "p2"})
                return httpx.Response(200, json={
                    "tasks": [{"id": "t2", "name": "b",
                               "params": {"target": "remote-1"}}],
                    "next_page_token": ""})
            return httpx.Response(200, json={})

        conf = self._conf()
        conf["access_token"] = "at-1"
        tc = ThunderCloud(_client(handler), conf)
        tasks = tc.list_tasks()
        self.assertEqual(len(tasks), 2)          # 两页合并，不漏任务
        self.assertEqual(len(seen), 2)
        self.assertIn("page_token=p2", seen[1])  # 第二页带翻页游标

    def test_error_semantics_standard(self):
        from backends.thunder_cloud import _is_error
        self.assertFalse(_is_error({}))
        self.assertFalse(_is_error({"error": "success", "error_code": 0}))
        self.assertTrue(_is_error({"error_code": 10}))
        self.assertTrue(_is_error({"error_description": "bad"}))

    def test_real_login_error_shape(self):
        """真实登录错误（2026-11-05 探针实录）：errorCode 是字符串、
        errorDesc 是中文文案——错误识别与展示都必须对得上。"""
        from backends.thunder_cloud import _err_code, _err_text, _is_error
        data = {"error": "userinfo_expired", "errorCode": "13",
                "errorDesc": "身份信息已失效，请重新输入帐号密码[13]",
                "error_description": "Login info expired. Please log in again"}

        def handler(request):
            return httpx.Response(200, json=data)

        self.assertTrue(_is_error(data))
        self.assertEqual(_err_code(data), 13)
        text = _err_text("迅雷登录失败", data)
        self.assertIn("身份信息已失效", text)      # 中文文案优先
        self.assertIn("error_code=13", text)
        tc = ThunderCloud(_client(handler), self._conf())
        st = tc.status()
        self.assertFalse(st["reachable"])
        self.assertIn("身份信息已失效", st["detail"])

    def test_manager_client_ignores_proxy_env(self):
        import downloader
        mgr = downloader.DownloadManager({})
        self.assertFalse(mgr.client.trust_env)


class TestReviewSms(unittest.TestCase):
    """风控短信验证（review_panel）：面板内直调 sendsms/checksms。
    背景（2026-11-05 用户实测）：reviewurl 是迅雷 App 内嵌页
    ，独立浏览器打开白屏，只能走接口完成验证。"""

    REVIEW_URL = ("https://i.xunlei.com/xlcaptcha/verify.html"
                  "?smsEvent=34&creditkey=ck-1&mobile=189%2A%2A%2A%2A0690"
                  "&event=login3&appid=40"
                  "&appName=ANDROID-com.xunlei.downloadprovider"
                  "&clientVersion=8.31.0.9726&userID=u-7&deviceid=div101.abc")

    def _conf(self):
        return {"login_name": "13800000000", "password": "pw",
                "device_id": "c" * 32, "target": "remote-1",
                "creditkey": "ck-1", "review_url": self.REVIEW_URL}

    def test_review_state_masks_mobile(self):
        tc = ThunderCloud(_client(lambda r: httpx.Response(200, json={})),
                          self._conf())
        st = tc.review_state()
        self.assertTrue(st["active"])
        self.assertEqual(st["mobile"], "189****0690")   # parse_qs 解码 %2A
        self.assertEqual(st["creditkey"], "ck-1")       # 供 UI 自动填入表单
        idle = ThunderCloud(_client(lambda r: httpx.Response(200, json={})),
                            {"login_name": "x"})
        self.assertFalse(idle.review_state()["active"])

    @staticmethod
    def _params(request) -> dict:
        """sendsms/上报是 GET+JSONP：参数在查询串。"""
        return dict(request.url.params)

    @staticmethod
    def _form(request) -> dict:
        """checksms 是 multipart 表单 POST：解析 name="k" 后的表单值。"""
        text = request.content.decode("utf-8", "replace")
        return {m.group(1): m.group(2)
                for m in re.finditer(r'name="([^"]+)"\r\n\r\n(.*?)\r\n--',
                                     text, re.S)}

    def test_send_sms_get_jsonp_shape_and_token(self):
        """传输形状：sendsms 走 GET+JSONP+csrf_token，
        浏览器 UA；URL 的 deviceid 改名 devicesign（原 deviceid 不再发送）。"""
        seen = {}

        def handler(request):
            seen["request"] = request
            return httpx.Response(200, json={"errorCode": "0", "token": "sms-tok"})

        conf = self._conf()
        tc = ThunderCloud(_client(handler), conf)
        r = tc.review_send_sms()
        self.assertTrue(r["ok"])
        self.assertEqual(r["mobile"], "189****0690")
        self.assertEqual(conf["review_token"], "sms-tok")
        req = seen["request"]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.url.path, "/xluser.core.login/v3/sendsms")
        self.assertEqual(req.headers["user-agent"], REVIEW_UA)   # 网页同形 UA
        p = self._params(req)
        self.assertEqual(p["smsEvent"], "34")
        self.assertEqual(p["creditkey"], "ck-1")
        self.assertEqual(p["appid"], "40")
        self.assertEqual(p["userID"], "u-7")
        self.assertEqual(p["devicesign"], "div101.abc")   # URL 的 deviceid 改名
        self.assertNotIn("deviceid", p)                    # 原 deviceid 不再发送
        self.assertEqual(p["csrf_token"], hashlib.md5(b"c" * 32).hexdigest())

    def test_send_sms_jsonp_wrapper_and_cookie_channel(self):
        """结果双通道：JSONP 包裹体（callback({...})）与纯 Set-Cookie 都能读。"""
        def handler(request):
            return httpx.Response(
                200, content=b'cb({"errorCode":"0","token":"t-jsonp"})',
                headers={"content-type": "text/javascript"})

        conf = self._conf()
        tc = ThunderCloud(_client(handler), conf)
        self.assertEqual(conf.get("review_token", ""), "")
        r = tc.review_send_sms()
        self.assertTrue(r["ok"])
        self.assertEqual(conf["review_token"], "t-jsonp")

        def cookie_only(request):
            return httpx.Response(200, text="not-json", headers=[
                ("set-cookie", "errorCode=0; path=/"),
                ("set-cookie", "token=t-cookie; path=/"),
                ("set-cookie", "creditkey=ck-new; path=/"),
            ])

        conf2 = self._conf()
        tc2 = ThunderCloud(_client(cookie_only), conf2)
        r2 = tc2.review_send_sms()
        self.assertTrue(r2["ok"])
        self.assertEqual(conf2["review_token"], "t-cookie")
        self.assertEqual(conf2["review_key"], "ck-new")   # 页面续期

    def test_send_sms_error_shape(self):
        """真实错误形状（探针实录）：errorCode 字符串键 + 中文 errorDesc 优先。"""
        def handler(request):
            return httpx.Response(200, json={
                "error": "too_many_request", "errorCode": "1002",
                "errorDesc": "请求过于频繁，请稍后再试[1002]",
                "error_description": "Too many requests"})

        tc = ThunderCloud(_client(handler), self._conf())
        with self.assertRaises(BackendError) as ctx:
            tc.review_send_sms()
        self.assertIn("请求过于频繁", str(ctx.exception))

    def test_send_sms_prefers_url_creditkey(self):
        """sendsms/checksms 发验证链接里的 creditkey（URL 参数为准）；顶层 creditkey 是登录重试用的，
        两者可能不是同一个值，不能拿顶层值覆盖。"""
        seen = {}

        def handler(request):
            seen["request"] = request
            return httpx.Response(200, json={"errorCode": "0", "token": "t"})

        conf = self._conf()
        conf["creditkey"] = "ck-conf"      # 与验证链接里的 ck-1 不同
        tc = ThunderCloud(_client(handler), conf)
        tc.review_send_sms()
        self.assertEqual(self._params(seen["request"])["creditkey"], "ck-1")

    def test_send_sms_expired_renews_key_then_retries(self):
        """13 且响应续期了 creditkey：换新键原地重试（页面行为），
        无需重登。"""
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path.endswith("/sendsms"):
                if not any(c.url.path.endswith("/sendsms") for c in calls[:-1]):
                    return httpx.Response(200, json={
                        "error": "userinfo_expired", "errorCode": "13",
                        "creditkey": "ck-renew"})
                return httpx.Response(200, json={"errorCode": "0", "token": "t2"})
            return httpx.Response(200, json={"error": "review_panel",
                                             "reviewurl": "https://i.xunlei.com/x",
                                             "creditkey": "x"})

        tc = ThunderCloud(_client(handler), self._conf())
        r = tc.review_send_sms()
        self.assertTrue(r["ok"])                       # 一次点击出结果
        keys = [self._params(c)["creditkey"]
                for c in calls if c.url.path.endswith("/sendsms")]
        self.assertEqual(keys, ["ck-1", "ck-renew"])   # 重试换续期后的键
        self.assertFalse(any(c.url.path.endswith("/v3/login") for c in calls))

    def test_send_sms_expired_retries_after_refresh(self):
        """13 无续期键：自动重登刷新验证会话后原地重试成功
       （用户实测 2026-11-05：点获取验证码报 13）。"""
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path.endswith("/sendsms"):
                if not any(c.url.path.endswith("/sendsms") for c in calls[:-1]):
                    return httpx.Response(200, json={
                        "error": "userinfo_expired", "errorCode": "13",
                        "errorDesc": "身份信息已失效，请重新输入帐号密码[13]"})
                return httpx.Response(200, json={"errorCode": "0", "token": "sms-tok"})
            return httpx.Response(200, json={
                "error": "review_panel",
                "reviewurl": ("https://i.xunlei.com/x?creditkey=ck-new"
                              "&mobile=189%2A%2A%2A%2A0690&appid=40"
                              "&appName=a&userID=u-7&deviceid=div101.z"),
                "creditkey": "ck-top-new"})

        conf = self._conf()
        tc = ThunderCloud(_client(handler), conf)
        r = tc.review_send_sms()
        self.assertTrue(r["ok"])                                  # 一次点击出结果
        self.assertEqual(conf["review_token"], "sms-tok")
        self.assertIn("creditkey=ck-new", conf["review_url"])      # 会话已刷新
        self.assertEqual(conf["creditkey"], "ck-top-new")
        keys = [self._params(c)["creditkey"]
                for c in calls if c.url.path.endswith("/sendsms")]
        self.assertEqual(keys, ["ck-1", "ck-new"])   # 重试用新会话的键

    def test_send_sms_expired_still_rejected(self):
        """刷新后的新会话仍被拒：明确报「仍被迅雷拒绝」（协议/风控问题）。"""
        def handler(request):
            if request.url.path.endswith("/sendsms"):
                return httpx.Response(200, json={
                    "error": "userinfo_expired", "errorCode": "13",
                    "errorDesc": "身份信息已失效，请重新输入帐号密码[13]"})
            return httpx.Response(200, json={
                "error": "review_panel",
                "reviewurl": ("https://i.xunlei.com/x?creditkey=ck-new"
                              "&appid=40&appName=a&deviceid=div101.z"),
                "creditkey": "ck-top-new"})

        conf = self._conf()
        tc = ThunderCloud(_client(handler), conf)
        with self.assertRaises(BackendError) as ctx:
            tc.review_send_sms()
        self.assertIn("仍被迅雷拒绝", str(ctx.exception))
        self.assertIn("creditkey=ck-new", conf["review_url"])   # 刷新确实发生了

    def test_check_sms_expired_refreshes_then_prompts_new_code(self):
        """check 遇 13：自动刷新会话并提示重新获取短信（短信令牌随旧会话失效）。"""
        def handler(request):
            if request.url.path.endswith("/v3/checksms"):
                return httpx.Response(200, json={
                    "error": "userinfo_expired", "errorCode": "13",
                    "errorDesc": "身份信息已失效，请重新输入帐号密码[13]"})
            return httpx.Response(200, json={
                "error": "review_panel",
                "reviewurl": ("https://i.xunlei.com/x?creditkey=ck-new"
                              "&appid=40&appName=a&userID=u-7&deviceid=div101.z"),
                "creditkey": "ck-top-new"})

        conf = self._conf()
        conf["review_token"] = "sms-tok"
        tc = ThunderCloud(_client(handler), conf)
        with self.assertRaises(BackendError) as ctx:
            tc.review_check_sms("123456")
        self.assertIn("已自动刷新", str(ctx.exception))
        self.assertIn("获取验证码", str(ctx.exception))
        self.assertIn("creditkey=ck-new", conf["review_url"])

    def test_check_sms_cookie_error_name_triggers_recovery(self):
        """checksms 结果只在 Set-Cookie：error cookie 给的是错误名
        （userinfo_expired）而非数字码，也要触发会话恢复。"""
        def handler(request):
            if request.url.path.endswith("/v3/checksms"):
                return httpx.Response(200, text="err", headers=[
                    ("set-cookie", "error=userinfo_expired; path=/"),
                    ("set-cookie", "blogresult=13; path=/"),
                ])
            return httpx.Response(200, json={
                "error": "review_panel",
                "reviewurl": ("https://i.xunlei.com/x?creditkey=ck-new"
                              "&appid=40&appName=a&userID=u-7&deviceid=div101.z"),
                "creditkey": "ck-top-new"})

        conf = self._conf()
        conf["review_token"] = "sms-tok"
        tc = ThunderCloud(_client(handler), conf)
        with self.assertRaises(BackendError) as ctx:
            tc.review_check_sms("123456")
        self.assertIn("已自动刷新", str(ctx.exception))
        self.assertIn("creditkey=ck-new", conf["review_url"])

    def test_check_sms_multipart_then_report_renews_creditkey(self):
        """checksms 走 multipart 表单 POST（跨域表单），成功后上报 /credit/v1/report，响应 code=200 的续期密钥优先。"""
        seen = []

        def handler(request):
            seen.append(request)
            if "/v3/checksms" in request.url.path:
                return httpx.Response(200, json={
                    "blogresult": "0", "creditkey": "ck-2", "securitytoken": "st"})
            return httpx.Response(200, json={"code": 200, "creditkey": "ck-3"})

        conf = self._conf()
        conf["review_token"] = "sms-tok"
        saved = []
        tc = ThunderCloud(_client(handler), conf, persist=lambda c: saved.append(dict(c)))
        r = tc.review_check_sms("123456")
        self.assertTrue(r["ok"])
        self.assertEqual(r["creditkey"], "ck-3")     # 上报续期值优先
        self.assertEqual(conf["creditkey"], "ck-3")   # 续期信任密钥
        self.assertEqual(conf["review_url"], "")      # 清掉待验证标记
        self.assertFalse(tc.review_state()["active"])
        self.assertEqual(saved[-1]["creditkey"], "ck-3")
        checksms = [q for q in seen if "/v3/checksms" in q.url.path][0]
        self.assertEqual(checksms.method, "POST")
        self.assertTrue(checksms.headers["content-type"]
                        .startswith("multipart/form-data"))
        form = self._form(checksms)
        self.assertEqual(form["token"], "sms-tok")
        self.assertEqual(form["smsCode"], "123456")
        self.assertEqual(form["creditkey"], "ck-1")
        report = [q for q in seen if "/credit/v1/report" in q.url.path][0]
        self.assertEqual(report.method, "GET")
        rp = self._params(report)
        self.assertEqual(rp["type"], "sms")
        self.assertEqual(rp["scene"], "34")
        self.assertEqual(rp["data"], "st")
        self.assertEqual(rp["csrf_token"], hashlib.md5(b"c" * 32).hexdigest())

    def test_check_sms_cookie_channel_success(self):
        """checksms 成功结果（blogresult/securitytoken/creditkey）可能只在
        Set-Cookie 里（跨域 iframe 页面读不到响应体），cookie 通道要能读到。"""
        def handler(request):
            return httpx.Response(200, text="not-json", headers=[
                ("set-cookie", "blogresult=0; path=/"),
                ("set-cookie", "securitytoken=st9; path=/"),
                ("set-cookie", "creditkey=ck-c; path=/"),
                ("set-cookie", "error=; path=/"),   # 删除型，不留在罐里
            ])

        conf = self._conf()
        conf["review_token"] = "sms-tok"
        tc = ThunderCloud(_client(handler), conf)
        r = tc.review_check_sms("123456")
        self.assertTrue(r["ok"])
        self.assertEqual(conf["creditkey"], "ck-c")   # 无上报续期时用响应里的
        self.assertNotIn("blogresult", conf.get("review_cookies", {}))
        self.assertNotIn("error", conf.get("review_cookies", {}))

    def test_check_sms_rejects_bad_code(self):
        def handler(request):
            return httpx.Response(200, json={
                "error": "invalid_sms_code", "errorCode": "1002",
                "errorDesc": "验证码错误"})

        conf = self._conf()
        conf["review_token"] = "sms-tok"
        tc = ThunderCloud(_client(handler), conf)
        with self.assertRaises(BackendError):
            tc.review_check_sms("000000")
        self.assertTrue(tc.review_state()["active"])  # 失败保持待验证

    def test_requires_review_state(self):
        tc = ThunderCloud(_client(lambda r: httpx.Response(200, json={})),
                          {"login_name": "x"})
        with self.assertRaises(BackendError):
            tc.review_send_sms()
        with self.assertRaises(BackendError):
            tc.review_check_sms("123456")

    def test_login_success_clears_review(self):
        def handler(request):
            path = request.url.path
            if path.endswith("/v3/login"):
                return httpx.Response(200, json={"sessionID": "s"})
            if path.endswith("/shield/captcha/init"):
                return httpx.Response(200, json={"captcha_token": "ct"})
            return httpx.Response(200, json={
                "token_type": "Bearer", "access_token": "at",
                "refresh_token": "rt", "user_id": "u"})

        conf = self._conf()
        conf["review_token"] = "sms-tok"
        conf["review_key"] = "ck-x"
        conf["review_cookies"] = {"token": "t"}
        tc = ThunderCloud(_client(handler), conf)
        tc.login()
        self.assertEqual(conf["review_url"], "")
        self.assertEqual(conf["review_token"], "")
        self.assertEqual(conf["creditkey"], "")
        self.assertEqual(conf["review_key"], "")
        self.assertEqual(conf["review_cookies"], {})


class TestCloudDeviceApi(unittest.TestCase):
    """云端设备专属 API（0.1.30）：api-pan 的 /drive/v1/files 只服务云盘空间，远程设备（user#runner 空间）的目录表/建目录/设备配置必须走 INNER_API 发现的设备专属域名。"""

    def _tc(self, handler):
        conf = {"login_name": "u", "password": "p", "device_id": "c" * 32,
                "access_token": "at-1", "target": "remote-1"}
        return ThunderCloud(_client(handler), conf)

    def _mk(self, handler):
        # 解析缓存是 DriveCore 基类机制，用基类实例直接验证
        return DriveCore(_client(handler), "http://engine/drive/v1",
                         lambda: {"h": "1"})

    def test_relay_full_shape_and_no_apipan(self):
        """目录表走设备 API 完整形状：plugin 凭据、
        device_space=、filters 三条件、with=…HistoryDownloadPath、with_audit、
        order=TYPE_DESC 全齐，且绝不打 api-pan /files。"""
        seen, unexpected = {}, []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                seen["inner"] = dict(request.url.params)
                return httpx.Response(200, json={
                    "link": "https://relay.example/p1/"
                            "?plugin_app_id=pid-1&plugin_app_token=ptok-1"})
            if host == "relay.example" and path.endswith("/drive/v1/files") \
                    and request.method == "GET":
                seen["files_path"] = path
                seen["files"] = dict(request.url.params)
                seen["with_multi"] = request.url.params.get_list("with")
                return httpx.Response(200, json={"files": [
                    {"id": "d1", "name": "downloads", "kind": "drive#folder",
                     "params": {"RealPath": "/downloads/"}}]})
            unexpected.append(f"{request.method} {host}{path}")
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        files = tc.core._folders("remote-1", "")
        self.assertEqual([f["id"] for f in files], ["d1"])
        self.assertEqual(unexpected, [])        # 未打 api-pan /files
        self.assertEqual(seen["inner"], {"space": "remote-1"})
        self.assertEqual(seen["files_path"], "/p1/drive/v1/files")
        p = seen["files"]
        self.assertEqual(p["space"], "remote-1")
        self.assertEqual(p["device_space"], "")
        self.assertEqual(p["parent_id"], "")
        self.assertEqual(p["plugin_app_id"], "pid-1")
        self.assertEqual(p["plugin_app_token"], "ptok-1")
        self.assertEqual(p["with_audit"], "true")
        self.assertEqual(p["order"], "TYPE_DESC")
        self.assertEqual(p["page_token"], "")
        self.assertEqual(seen["with_multi"],
                         ["withCategoryDiskMountPath",
                          "withCategoryHistoryDownloadPath"])
        self.assertEqual(json.loads(p["filters"]), {
            "trashed": {"eq": False},
            "phase": {"eq": "PHASE_TYPE_COMPLETE"},
            "kind": {"eq": "drive#folder"}})

    def test_relay_links_variant_cached_and_slash_norm(self):
        """INNER_API 的 links[].url 变体可解析；link 无尾斜杠时 base 自动补
        （SDK ApiDomain+路径 是字符串拼接）；发现结果按 space 缓存。"""
        calls, paths = [], []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                calls.append(1)
                return httpx.Response(200, json={"links": [{
                    "url": "https://relay.example/p2"
                           "?plugin_app_id=pid-2&plugin_app_token=ptok-2"}]})
            if host == "relay.example":
                paths.append(path)
                return httpx.Response(200, json={"files": [
                    {"id": "d1", "name": "downloads", "kind": "drive#folder"}]})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        tc.core._folders("remote-1", "")
        tc.core._folders("remote-1", "x")
        self.assertEqual(len(calls), 1)          # 发现缓存：只查一次 INNER_API
        self.assertEqual(paths, ["/p2/drive/v1/files"] * 2)

    def test_relay_failure_refreshes_discovery(self):
        """设备 API 首轮全挂 → 强制重发现重试一轮（凭据过期自愈），仍失败才回退。"""
        state = {"inner": 0, "files": 0}

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                state["inner"] += 1
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/?plugin_app_id=i&plugin_app_token=t"})
            if host == "relay.example" and path.endswith("/drive/v1/files"):
                state["files"] += 1
                if state["files"] <= 2:          # 第一轮两个形状都 502
                    return httpx.Response(502, text="bad gateway")
                return httpx.Response(200, json={"files": [
                    {"id": "d1", "name": "downloads", "kind": "drive#folder"}]})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        files = tc.core._folders("remote-1", "")
        self.assertEqual([f["id"] for f in files], ["d1"])
        self.assertEqual(state["inner"], 2)      # 第二轮重新发现
        self.assertEqual(state["files"], 3)      # 2 失败形状 + 1 重试成功

    def test_inner_api_fails_falls_back_apipan(self):
        """INNER_API 不可达/无 link → 回退 api-pan 形状阶梯（兼容未来变化）。"""
        seen = []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                return httpx.Response(200, json={})      # 无 link
            if host == "api-pan.xunlei.com" and path.endswith("/drive/v1/files") \
                    and request.method == "GET":
                seen.append(dict(request.url.params))
                return httpx.Response(200, json={"files": [
                    {"id": "d1", "name": "downloads", "kind": "drive#folder"}]})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        files = tc.core._folders("remote-1", "")
        self.assertEqual([f["id"] for f in files], ["d1"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["space"], "remote-1")

    def test_relay_and_apipan_all_fail_aggregate_error(self):
        """两路全挂 → 聚合错误：设备API 发现错误 + api-pan 各形状错误都保留。
        0.1.31：api-pan 回退阶梯裁剪为标准/极简 2 形状。"""
        def handler(request):
            return httpx.Response(502, text="bad gateway")

        tc = self._tc(handler)
        with self.assertRaises(BackendError) as ctx:
            tc.core._folders("remote-1", "")
        msg = str(ctx.exception)
        self.assertIn("设备API发现", msg)
        self.assertIn("api-pan", msg)
        self.assertIn("502", msg)
        for label in ("标准形状", "极简形状"):
            self.assertIn(label, msg)

    def test_relay_shapes_all_fail_no_apipan_fallback(self):
        """0.1.31：设备 API 发现成功但 relay 形状全挂 → 直接抛聚合错误，
        不跑 api-pan 阶梯（api-pan 对设备空间不可用，回退纯属空耗预算）。"""
        apipan_hits = []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/"
                            "?plugin_app_id=i&plugin_app_token=t"})
            if host == "relay.example" and path.endswith("/drive/v1/files"):
                return httpx.Response(502, text="relay down")
            if host == "api-pan.xunlei.com":
                apipan_hits.append(path)
                return httpx.Response(200, json={"files": [
                    {"id": "d1", "name": "downloads", "kind": "drive#folder"}]})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        with self.assertRaises(BackendError) as ctx:
            tc.core._folders("remote-1", "")
        self.assertIn("未回退 api-pan", str(ctx.exception))
        self.assertIn("relay down", str(ctx.exception))
        self.assertEqual(apipan_hits, [])        # api-pan 一次都没打

    def test_resolve_cache_hits_within_ttl(self):
        """0.1.31：同 (base_url, space, directory) 解析结果 120s 内缓存——
        批量派发同目录只解析一次（此前每条链接重跑整条目录链）。"""
        hits = []

        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if path.endswith("/files") and request.method == "GET":
                hits.append(dict(request.url.params))
                return httpx.Response(200, json={"files": [
                    {"id": "dir-1", "name": "downloads",
                     "params": {"RealPath": "/downloads/"}}]})
            if path.endswith("/files") and request.method == "POST":
                return httpx.Response(200, json={"file": {"id": "made-x"}})
            return httpx.Response(200, json={"task": {"id": "t-x"}})

        core = self._mk(handler)
        self.assertEqual(core.resolve_dest_folder("sp", "电影"),
                         ("made-x", "/downloads/电影/"))
        n_first = len(hits)
        # 第二次同参数解析：命中缓存，目录表不再被查
        self.assertEqual(core.resolve_dest_folder("sp", "电影"),
                         ("made-x", "/downloads/电影/"))
        self.assertEqual(len(hits), n_first)

    def test_resolve_cache_isolated_by_client(self):
        """缓存按 client 身份隔离：每个请求新建 DownloadManager/client，
        不同 client 不共享缓存（防跨请求串味）。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/device/config"):
                return httpx.Response(200, json={})
            if path.endswith("/files") and request.method == "GET":
                return httpx.Response(200, json={"files": [
                    {"id": "only", "name": "x", "params": {"RealPath": "/x/"}}]})
            return httpx.Response(200, json={"task": {"id": "t-x"}})

        core1 = self._mk(handler)
        core2 = self._mk(handler)              # 不同 client
        core1.resolve_dest_folder("sp", "")
        core2.resolve_dest_folder("sp", "")    # 不该命中 core1 的缓存
        self.assertIsNot(core1.client, core2.client)

    def test_resolve_end_to_end_via_relay_walk_and_create(self):
        """端到端：relay device/config 提示 + 目录表逐段解析 + 缺级 relay 自动
        建目录（带 plugin 凭据），全程不打 api-pan 目录接口。"""
        made, config_hits, unexpected = [], [], []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/"
                            "?plugin_app_id=pid&plugin_app_token=ptok"})
            if host == "relay.example" and path.endswith("/device/config"):
                config_hits.append(host)
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if host == "relay.example" and path.endswith("/drive/v1/files") \
                    and request.method == "GET":
                pid = request.url.params.get("parent_id", "")
                if pid == "":
                    return httpx.Response(200, json={"files": [
                        {"id": "dir-1", "name": "downloads", "kind": "drive#folder",
                         "params": {"RealPath": "/downloads/"}}]})
                return httpx.Response(200, json={"files": [
                    {"id": "sub-1", "name": "电影", "kind": "drive#folder",
                     "params": {"RealPath": "/downloads/电影/"}}]})
            if host == "relay.example" and path.endswith("/drive/v1/files") \
                    and request.method == "POST":
                made.append({"body": json.loads(request.content),
                             "params": dict(request.url.params)})
                return httpx.Response(200, json={"file": {"id": "made-1"}})
            unexpected.append(f"{request.method} {host}{path}")
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        self.assertEqual(tc.core.resolve_dest_folder("remote-1", "电影/2025"),
                         ("made-1", "/downloads/电影/2025/"))
        self.assertEqual(config_hits, ["relay.example"])   # 提示走设备 API
        self.assertEqual(unexpected, [])
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["body"], {
            "parent_id": "sub-1", "name": "2025",
            "space": "remote-1", "kind": "drive#folder"})
        self.assertEqual(made[0]["params"]["plugin_app_id"], "pid")
        self.assertEqual(made[0]["params"]["plugin_app_token"], "ptok")

    def test_make_folder_relay_post_fails_raises_no_apipan(self):
        """0.1.31：发现成功但 relay 建目录失败 → 直接抛错，不回退 api-pan
        （api-pan 对设备空间建目录同样 502，回退纯属浪费响应预算）。"""
        seen = []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/"
                            "?plugin_app_id=i&plugin_app_token=t"})
            if host == "relay.example" and path.endswith("/drive/v1/files"):
                return httpx.Response(200, json={"error_code": 403,
                                                 "error": "denied"})
            if host == "api-pan.xunlei.com" and request.method == "POST":
                seen.append(json.loads(request.content))
                return httpx.Response(200, json={"file": {"id": "ap-1"}})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        with self.assertRaises(BackendError):
            tc.core._make_folder("remote-1", "p1", "电影")
        self.assertEqual(seen, [])           # api-pan 未被触碰

    def test_make_folder_falls_back_apipan_when_discovery_fails(self):
        """设备 API 发现失败（INNER_API 不可达）→ 建目录回退 api-pan。"""
        seen = []

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                return httpx.Response(502, text="bad gateway")
            if host == "api-pan.xunlei.com" and request.method == "POST":
                seen.append(json.loads(request.content))
                return httpx.Response(200, json={"file": {"id": "ap-1"}})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        self.assertEqual(tc.core._make_folder("remote-1", "p1", "电影"),
                         {"id": "ap-1", "name": "电影", "params": {"RealPath": ""}})
        self.assertEqual(seen, [{"parent_id": "p1", "name": "电影",
                                 "space": "remote-1", "kind": "drive#folder"}])

    def test_create_task_still_posts_apipan_after_relay_resolve(self):
        """回归：目录解析走设备 API，但建任务仍 POST api-pan /drive/v1/task，parent_folder_id 来自 relay 解析结果。"""
        captured = {}

        def handler(request):
            path, host = request.url.path, request.url.host
            if path.endswith("/apps/INNER_API"):
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/"
                            "?plugin_app_id=i&plugin_app_token=t"})
            if host == "relay.example" and path.endswith("/device/config"):
                return httpx.Response(200, json={"download_paths": ["/downloads"]})
            if host == "relay.example" and path.endswith("/drive/v1/files"):
                return httpx.Response(200, json={"files": [
                    {"id": "dir-1", "name": "downloads", "kind": "drive#folder",
                     "params": {"RealPath": "/downloads/"}}]})
            if host == "api-pan.xunlei.com" and path.endswith("/drive/v1/task") \
                    and request.method == "POST":
                captured["host"], captured["path"] = host, path
                captured.update(json.loads(request.content))
                return httpx.Response(200, json={"task": {"id": "cloud-9"}})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        tid = tc.core.create_task("https://x/a",
                                  ParsedResource(name="a", size=1), "remote-1")
        self.assertEqual(tid, "cloud-9")
        self.assertEqual(captured["host"], "api-pan.xunlei.com")
        self.assertEqual(captured["path"], "/drive/v1/task")
        self.assertEqual(captured["params"]["parent_folder_id"], "dir-1")
        self.assertEqual(captured["params"]["parent_folder_path"], "/downloads/")


class TestDeviceTimeoutAndHarvest(unittest.TestCase):
    """0.1.32：error_code=2「远程调用失败」=迅雷服务端调用设备超时（社区同报案例）。GET 幂等重试、错误透明化（指引+脱敏证据）、
    历史任务反查兜底保根 id。"""

    TIMEOUT_BODY = {
        "error": "external_unknown_err", "error_code": 2,
        "error_description": "远程调用失败",
        "error_details": [
            {"@type": "type.googleapis.com/google.rpc.DebugInfo",
             "detail": ('Get "http://k8s.xsl/drive-reverse-proxy/v1/proxy/'
                        '584201392/7abd3182d399f7bdda199550d8babede/x": '
                        'net/http: request canceled (Client.Timeout exceeded '
                        'while awaiting headers)')},
            {"@type": "type.googleapis.com/google.rpc.LocalizedMessage",
             "locale": "zh-cn", "message": "远程调用失败"}]}

    def _tc(self, handler):
        conf = {"login_name": "u", "password": "p", "device_id": "c" * 32,
                "access_token": "at-1", "target": "remote-1"}
        return ThunderCloud(_client(handler), conf)

    @staticmethod
    def _core(handler):
        return DriveCore(_client(handler), "http://engine/drive/v1",
                         lambda: {"h": "1"})

    def test_get_retries_on_device_timeout_then_succeeds(self):
        """GET 幂等重试：前 2 次设备超时、第 3 次成功（瞬态自愈）。"""
        calls = []

        def handler(request):
            if request.url.path.endswith("/apps/INNER_API"):
                calls.append(1)
                if len(calls) < 3:
                    return httpx.Response(200, json=self.TIMEOUT_BODY)
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/"
                            "?plugin_app_id=i&plugin_app_token=t"})
            return httpx.Response(200, json={})

        tc = self._tc(handler)
        with mock.patch("time.sleep", lambda _s: None):
            api = tc.core._relay_api("remote-1")
        self.assertEqual(api["base"], "https://relay.example/p/")
        self.assertEqual(len(calls), 3)

    def test_post_not_retried_on_device_timeout(self):
        """POST（建目录）非幂等不重试；报错带可操作指引。"""
        posts = []

        def handler(request):
            if request.url.path.endswith("/apps/INNER_API"):
                return httpx.Response(200, json={
                    "link": "https://relay.example/p/"
                            "?plugin_app_id=i&plugin_app_token=t"})
            if request.method == "POST":
                posts.append(1)
                return httpx.Response(200, json=self.TIMEOUT_BODY)
            return httpx.Response(200, json={"files": []})

        tc = self._tc(handler)
        with mock.patch("time.sleep", lambda _s: None):
            with self.assertRaises(BackendError) as ctx:
                tc.core._make_folder("remote-1", "p0", "sub")
        self.assertEqual(len(posts), 1)
        msg = str(ctx.exception)
        self.assertIn("设备端响应超时", msg)
        self.assertIn("error_code=2", msg)

    def test_device_timeout_message_guidance_and_masked_hint(self):
        """错误文案含人读指引 + 脱敏证据（哈希/uid 掩码，链路形态保留）。"""
        def handler(request):
            return httpx.Response(200, json=self.TIMEOUT_BODY)

        tc = self._tc(handler)
        with mock.patch("time.sleep", lambda _s: None):
            with self.assertRaises(BackendError) as ctx:
                tc.core._relay_api("remote-1", refresh=True)
        msg = str(ctx.exception)
        self.assertIn("设备端响应超时", msg)
        self.assertIn("迅雷远程插件", msg)
        self.assertIn("证据", msg)
        self.assertIn("<hash>", msg)
        self.assertIn("<id>", msg)
        self.assertNotIn("7abd3182d399f7bdda199550d8babede", msg)
        self.assertNotIn("584201392", msg)

    def test_harvest_root_prefers_entry_with_path(self):
        """历史任务反查：优先采用带 parent_folder_path 的条目。"""
        def handler(request):
            if request.url.path.endswith("/tasks"):
                return httpx.Response(200, json={"tasks": [
                    {"name": "t1", "params": {"parent_folder_id": "f-no-path"}},
                    {"name": "t2", "params": {"parent_folder_id": "f-2",
                                              "parent_folder_path": "/downloads/"}},
                    {"name": "runner", "params": {"target": "remote-1"}}]})
            return httpx.Response(200, json={})

        self.assertEqual(self._core(handler)._harvest_root("remote-1"),
                         ("f-2", "/downloads/"))

    def test_harvest_root_none_without_params(self):
        """无历史任务带 parent_folder_id → None（不误用 target 等杂参）。"""
        def handler(request):
            return httpx.Response(200, json={"tasks": [
                {"name": "r", "params": {"target": "remote-1"}}]})

        self.assertIsNone(self._core(handler)._harvest_root("remote-1"))

    def test_resolve_uses_harvested_root_when_folders_unavailable(self):
        """目录表全挂（错误体）时：下载根解析反查历史任务兜底，
        空目录直接返回反查到的 (id, path)。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(200, json=self.TIMEOUT_BODY)
            if path.endswith("/tasks"):
                return httpx.Response(200, json={"tasks": [
                    {"name": "t", "params": {
                        "parent_folder_id": "f-h",
                        "parent_folder_path": "/downloads/"}}]})
            return httpx.Response(200, json={})

        self.assertEqual(self._core(handler).resolve_dest_folder("remote-1", ""),
                         ("f-h", "/downloads/"))

    def test_resolve_harvested_root_degrades_subdir_to_path(self):
        """反查兜底下子目录仍不可解析（插件没恢复）→ 保留根 id + 完整目标
        路径交引擎尽力落盘（阶梯降级铁律）。"""
        def handler(request):
            path = request.url.path
            if path.endswith("/files"):
                return httpx.Response(200, json=self.TIMEOUT_BODY)
            if path.endswith("/tasks"):
                return httpx.Response(200, json={"tasks": [
                    {"name": "t", "params": {
                        "parent_folder_id": "f-h",
                        "parent_folder_path": "/downloads/"}}]})
            return httpx.Response(200, json={})

        pid, path = self._core(handler).resolve_dest_folder("remote-1", "movies")
        self.assertEqual(pid, "f-h")
        self.assertEqual(path, "/downloads/movies/")


if __name__ == "__main__":
    unittest.main()
