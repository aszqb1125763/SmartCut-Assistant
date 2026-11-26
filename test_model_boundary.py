# -*- coding: utf-8 -*-
"""模型侧边界测试：本地模型那条链**拿不到原始数据**。

子枫 2026-11-24：
    「本地大模型只接受对应数据，不允许接收任何原始数据，
      所有数据都要经过小脑计算输出。」

这条测试扫源码：凡是会调用本地模型的文件，不许 import 任何原始数据源
（AudioSource / WordSource / FrameSource / brains）。
模型只能通过 `model_gateway.see()` 拿小脑结果。
"""
from __future__ import annotations

import os
import re
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))

#: 会调用本地模型的模块（谁调模型谁受约束）
MODEL_FILES = ("longchain.py", "script_writer.py", "director.py",
               "proofread.py", "judge.py")

FORBIDDEN = (r"brains\.base", r"from\s+brains\s+import",
             r"AudioSource", r"WordSource", r"FrameSource",
             r"\bwave\b", r"\bcv2\b")


class ModelBoundaryTest(unittest.TestCase):
    def test_model_modules_have_no_raw_sources(self):
        offenders = []
        for name in MODEL_FILES:
            path = os.path.join(BASE, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for pattern in FORBIDDEN:
                if re.search(pattern, text):
                    offenders.append("%s ← %s" % (name, pattern))
        self.assertEqual([], offenders,
                         "会调模型的文件不许碰原始数据：%s" % offenders)

    def test_gateway_is_the_only_entry(self):
        """模型入口只有 model_gateway；别处不许自建"看"的转发。"""
        offenders = []
        for name in ("brain.py", "coords_report.py"):
            path = os.path.join(BASE, name)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if "def see(" in text:
                offenders.append(name)
        self.assertEqual([], offenders,
                         "只允许 model_gateway 定义 see()：%s" % offenders)

    def test_denied_list_covers_raw(self):
        import model_gateway as gateway
        for kind in ("audio", "frames", "words", "raw"):
            self.assertIn(kind, gateway.RAW_LOOKS)
        for kind in gateway.LOOKS:
            self.assertNotIn(kind, gateway.RAW_LOOKS)


if __name__ == "__main__":
    unittest.main()
