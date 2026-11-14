#!/usr/bin/env python3
"""更新介绍渲染回归（0.1.41.4）。

事故表现：更新介绍文本只占左侧约一半宽、右侧大片空白——看似
"没自适应宽度"，实为 _renderNotes 把 CHANGELOG 源文件的硬换行
（50~60 字一行）当块边界逐行渲染，文本列停在源换行处。

契约：续行必须并入当前 <li>/<p>（markdown 段落语义），由 CSS 按
容器宽度自适应折行；软换行拼接只在中英/中数边界补一个空格，
全角标点边界（"、"+"「"）直接相接。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _extract_fn(js: str, name: str) -> str:
    m = re.search(r"function %s\([^)]*\) \{[\s\S]*?\n\}" % name, js)
    return m.group(0) if m else ""


@unittest.skipUnless(shutil.which("node"), "无 node 运行时")
class TestRenderNotesJoin(unittest.TestCase):
    def _render(self, md_js_literal: str) -> str:
        js = (ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        fn = _extract_fn(js, "_renderNotes")
        self.assertTrue(fn, "未找到 _renderNotes")
        harness = (
            "const esc = (s) => String(s)"
            ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');\n"
            + fn + "\n"
            + "console.log(_renderNotes(" + md_js_literal + "));\n"
        )
        r = subprocess.run([shutil.which("node"), "-e", harness],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_continuation_lines_join_into_li(self):
        """列表项的续行并入同一 <li>，不再各自成块。"""
        html = self._render(r'"- **甲**：全空、\n  「测试」报错。根因链：\n'
                            r'  生命周期脚本以 root 跑\n\n- **乙**：第二项\n  续行B"')
        self.assertIn("<li><b>甲</b>：全空、「测试」报错。根因链："
                      "生命周期脚本以 root 跑</li>", html)
        self.assertIn("<li><b>乙</b>：第二项续行B</li>", html)
        self.assertNotIn("<p>", html)          # 续行绝不能变成独立段落

    def test_soft_join_spacing_rules(self):
        """中英/中数边界补空格；全角标点与纯中文边界直接相接。"""
        html = self._render(r'"- 写 state\n  后自动修复\n  config.json/\n'
                            r'  锁文件\n\nhello\nworld"')
        self.assertIn("写 state 后自动修复", html)   # latin→CJK 边界补空格
        self.assertIn("config.json/锁文件", html)   # 标点边界不补
        self.assertIn("hello world", html)          # ASCII→ASCII 补空格

    def test_blank_line_separates_blocks(self):
        """空行仍是块边界：段落/列表正常分块。"""
        html = self._render(r'"第一段\n\n- 列表项\n\n第二段"')
        self.assertIn("<p>第一段</p>", html)
        self.assertIn("<ul><li>列表项</li></ul>", html)
        self.assertIn("<p>第二段</p>", html)


if __name__ == "__main__":
    unittest.main()
