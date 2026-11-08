#!/usr/bin/env python3
"""自诊断探针测试：_probe 三态（正常/异常/线程硬超时，0.1.32）+
设备插件逐台探活分类（0.1.33）。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import diag  # noqa: E402


class TestDiagProbe(unittest.TestCase):
    def test_probe_ok(self):
        r = diag._probe("步A", lambda: "一切正常")
        self.assertTrue(r["ok"])
        self.assertEqual(r["detail"], "一切正常")
        self.assertEqual(r["step"], "步A")
        self.assertIsInstance(r["ms"], int)

    def test_probe_error_captured(self):
        def boom():
            raise ValueError("炸了")
        r = diag._probe("步B", boom)
        self.assertFalse(r["ok"])
        self.assertIn("ValueError", r["detail"])
        self.assertIn("炸了", r["detail"])

    def test_probe_thread_hard_timeout(self):
        r = diag._probe("步C", lambda: time.sleep(3), timeout=0.2)
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["detail"])


class _FakeCore:
    """duck-type core：behavior[target] = "ok" 或 ("discover"|"request", msg)。"""

    def __init__(self, behavior):
        self.behavior = behavior

    def _relay_api(self, target):
        b = self.behavior.get(target, "ok")
        if isinstance(b, tuple) and b[0] == "discover":
            raise RuntimeError(b[1])
        return {"base": "https://relay.example/p/",
                "plugin_app_id": "i", "plugin_app_token": "t"}

    def _relay_params(self, api, target):
        return {"space": target}

    def _request(self, method, path, **kw):
        tgt = (kw.get("params") or {}).get("space", "")
        b = self.behavior.get(tgt, "ok")
        if isinstance(b, tuple) and b[0] == "request":
            raise RuntimeError(b[1])
        return {}


class TestPluginProbe(unittest.TestCase):
    """0.1.33 设备插件逐台探活：可达/不可达分类 + 派发目标标注 +
    单台问题 vs 全账号问题结论。"""

    def test_probe_one_plugin_classification(self):
        core = _FakeCore({
            "t-ok": "ok",
            "t-slow": ("request", "迅雷接口错误：设备端响应超时（error_code=2）"),
            "t-net": ("request", "网络错误（请检查 NAS 出网/DNS/代理设置）：timed out"),
            "t-err": ("request", "HTTP 404: nope"),
            "t-disc": ("discover", "INNER_API 响应缺少 link"),
        })
        self.assertEqual(diag._probe_one_plugin(core, "t-ok"),
                         (True, "插件响应正常"))
        ok, why = diag._probe_one_plugin(core, "t-slow")
        self.assertFalse(ok)
        self.assertIn("设备端超时", why)
        ok, why = diag._probe_one_plugin(core, "t-net")
        self.assertFalse(ok)
        self.assertIn("不可达", why)
        ok, why = diag._probe_one_plugin(core, "t-err")
        self.assertTrue(ok)                       # 有响应但报错：通道是活的
        self.assertIn("通道有响应", why)
        ok, why = diag._probe_one_plugin(core, "t-disc")
        self.assertFalse(ok)
        self.assertIn("设备API发现失败", why)

    def test_probe_devices_plugin_marks_target(self):
        core = _FakeCore({
            "t-a": "ok",
            "t-b": ("request", "迅雷接口错误：设备端响应超时（error_code=2）"),
            "t-c": "ok",
        })
        targets = [{"target": "t-a", "name": "设备A"},
                   {"target": "t-b", "name": "设备B"},
                   {"target": "t-c", "name": "设备C"}]
        out = diag.probe_devices_plugin(core, targets, "t-b")
        self.assertIn("可达 2/3", out)
        self.assertIn("设备B✗", out)
        self.assertIn("派发目标（t-b", out)
        self.assertIn("✗", out.split("派发目标")[1])
        self.assertIn("仅 1/3 台不可达", out)
        self.assertIn("目标设备单台插件问题", out)

    def test_probe_devices_plugin_majority_down(self):
        """0.1.34 结论按倒下比例措辞：多数不可达时报「非单台问题」并列出
        可达设备建议换目标（0.1.33 实测 1/10 可达被误报「其他设备正常」）。"""
        core = _FakeCore({
            "t-a": "ok",
            "t-b": ("request", "设备端响应超时（error_code=2）"),
            "t-c": ("request", "设备端响应超时（error_code=2）"),
            "t-d": ("request", "网络错误：连接失败"),
        })
        targets = [{"target": "t-a", "name": "设备A"},
                   {"target": "t-b", "name": "设备B"},
                   {"target": "t-c", "name": "设备C"},
                   {"target": "t-d", "name": "设备D"}]
        out = diag.probe_devices_plugin(core, targets, "t-b")
        self.assertIn("可达 1/4", out)
        self.assertIn("3/4 台均不可达", out)
        self.assertIn("非单台问题", out)
        self.assertIn("建议换其中一台为派发目标", out)
        self.assertIn("设备A✓", out)

    def test_probe_devices_plugin_all_down(self):
        core = _FakeCore({
            "t-a": ("request", "设备端响应超时（error_code=2）"),
            "t-b": ("request", "网络错误：连接失败"),
        })
        targets = [{"target": "t-a", "name": "设备A"},
                   {"target": "t-b", "name": "设备B"}]
        out = diag.probe_devices_plugin(core, targets, "t-a")
        self.assertIn("可达 0/2", out)
        self.assertIn("全部设备不可达", out)

    def test_probe_devices_plugin_no_targets(self):
        with self.assertRaises(RuntimeError):
            diag.probe_devices_plugin(_FakeCore({}), [], "")


class TestTransientReconcile(unittest.TestCase):
    """0.1.36 瞬态综合判定：「设备 API 发现」单独失败但同链路后续步骤
    成功 → 标瞬态不计异常（0.1.35 NAS 实测首步 3s 超时被放大成唯一 ✗）。"""

    @staticmethod
    def _steps(disc_ok, folders_ok, root_ok, disc_detail="设备端响应超时"):
        return [
            {"step": "设备 API 发现", "ok": disc_ok, "ms": 3047,
             "detail": disc_detail},
            {"step": "中继目录表", "ok": folders_ok, "ms": 999,
             "detail": "顶层目录 3 个"},
            {"step": "下载根解析", "ok": root_ok, "ms": 9353,
             "detail": "下载根 id=x"},
        ]

    def test_transient_marked_when_same_chain_recovered(self):
        steps = self._steps(False, True, True)
        diag._reconcile_transient(steps)
        disc = steps[0]
        self.assertTrue(disc["ok"])
        self.assertTrue(disc["transient"])
        self.assertIn("⚠ 瞬态", disc["detail"])
        self.assertIn("中继目录表", disc["detail"])
        self.assertIn("下载根解析", disc["detail"])

    def test_transient_marked_with_single_witness(self):
        steps = self._steps(False, True, False)
        diag._reconcile_transient(steps)
        self.assertTrue(steps[0]["ok"])
        self.assertTrue(steps[0]["transient"])
        self.assertIn("中继目录表", steps[0]["detail"])
        self.assertNotIn("下载根解析", steps[0]["detail"])

    def test_not_reconciled_when_chain_also_down(self):
        steps = self._steps(False, False, False)
        diag._reconcile_transient(steps)
        self.assertFalse(steps[0]["ok"])
        self.assertNotIn("transient", steps[0])

    def test_ok_step_untouched(self):
        steps = self._steps(True, True, True)
        diag._reconcile_transient(steps)
        self.assertNotIn("transient", steps[0])
        self.assertNotIn("瞬态", steps[0]["detail"])

    def test_idempotent(self):
        steps = self._steps(False, True, True)
        diag._reconcile_transient(steps)
        detail = steps[0]["detail"]
        diag._reconcile_transient(steps)          # 二次调用不得重复追加
        self.assertEqual(steps[0]["detail"], detail)


class TestPluginProbeOutput(unittest.TestCase):
    """0.1.36 探活输出：错误截短 + 同因合并 + 派发目标可达结论。"""

    def test_probe_one_plugin_error_truncated(self):
        core = _FakeCore({
            "t-disc": ("discover", "x" * 200),
            "t-net": ("request", "网络错误（请检查 NAS 出网/DNS/代理设置）"
                                "：" + "y" * 200),
        })
        ok, why = diag._probe_one_plugin(core, "t-disc")
        self.assertFalse(ok)
        self.assertLessEqual(len(why), 50)        # 前缀 + 截短到 36
        ok, why = diag._probe_one_plugin(core, "t-net")
        self.assertFalse(ok)
        self.assertLessEqual(len(why), 50)

    def test_probe_devices_plugin_same_cause_merged(self):
        cause = ("request", "迅雷接口错误：设备端响应超时（error_code=2）"
                            "：" + "z" * 120)
        core = _FakeCore({"t-a": "ok", "t-b": cause, "t-c": cause})
        targets = [{"target": "t-a", "name": "设备A"},
                   {"target": "t-b", "name": "电脑-许诺"},
                   {"target": "t-c", "name": "电脑-贰玖图文"}]
        out = diag.probe_devices_plugin(core, targets, "t-a")
        self.assertIn("2 台同因", out)            # 合并为一段
        self.assertIn("电脑-许诺✗、电脑-贰玖图文✗", out)
        self.assertLessEqual(len(out), 220)       # 不再刷屏

    def test_probe_devices_plugin_target_up_others_down(self):
        """派发目标可达 + 其余不可达 → 结论点明主链路可用不影响派发。"""
        core = _FakeCore({
            "t-a": "ok",
            "t-b": ("request", "设备端响应超时（error_code=2）"),
            "t-c": ("request", "设备端响应超时（error_code=2）"),
        })
        targets = [{"target": "t-a", "name": "群晖-xunlei"},
                   {"target": "t-b", "name": "电脑-许诺"},
                   {"target": "t-c", "name": "电脑-贰玖图文"}]
        out = diag.probe_devices_plugin(core, targets, "t-a")
        self.assertIn("结论：设备插件通道整体可用", out)
        self.assertIn("主链路可用", out)
        self.assertIn("不影响本插件派发", out)


if __name__ == "__main__":
    unittest.main()
