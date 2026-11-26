# -*- coding: utf-8 -*-
"""小脑中立性测试：它只是被动的计算机，中立、绝对。

子枫 2026-11-24：
    「小脑不需要有思考能力，他就是被动的计算机，中立绝对的。」

守四条：
    1. 同一输入，两次输出**完全一致**（绝对，不含随机/时间）；
    2. 小脑源码不许出现随机、当前时间、环境变量这类"外部影响"；
    3. 候选排序**不许自己发明权重**（没有 score 这种加权分）；
    4. 小脑不读任何"偏好"配置（只认 coords 里的阈值）。
"""
from __future__ import annotations

import json
import os
import re
import unittest

from test_brain_calc import FakeCalculator

BASE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BASE, "brains")
PLUGIN_FILES = ("audio_brain.py", "speech_brain.py", "picture_brain.py",
                "text_brain.py")

#: 会让结果"看运气"的东西
FORBIDDEN = (r"\brandom\b", r"time\.time\(", r"datetime\.now",
             r"datetime\.today", r"os\.environ", r"uuid",
             r"np\.random", r"numpy\.random")


def _same(first, second):
    return json.dumps(first, sort_keys=True, ensure_ascii=False) == \
        json.dumps(second, sort_keys=True, ensure_ascii=False)


class DeterminismTest(unittest.TestCase):
    def test_probe_is_deterministic(self):
        calc = FakeCalculator([(1.0, 0.5), (0.4, 0.0), (1.0, 0.5)])
        first = calc.probe_seam(1.2)
        second = calc.probe_seam(1.2)
        self.assertTrue(_same(first, second), "同一输入两次结果不同")

    def test_tail_is_deterministic(self):
        calc = FakeCalculator([(1.0, 0.5), (1.0, 0.0)])
        self.assertTrue(_same(calc.probe_tail(1.0), calc.probe_tail(1.0)))

    def test_new_calculator_same_result(self):
        """换一个计算器实例，结果也必须一样（不靠进程状态）。"""
        segments = [(1.0, 0.5), (0.4, 0.0), (1.0, 0.5)]
        self.assertTrue(_same(FakeCalculator(segments).probe_seam(1.2),
                              FakeCalculator(segments).probe_seam(1.2)))


class NoHiddenStateTest(unittest.TestCase):
    def test_no_random_or_clock(self):
        offenders = []
        for name in PLUGIN_FILES:
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                text = handle.read()
            for pattern in FORBIDDEN:
                if re.search(pattern, text):
                    offenders.append("%s ← %s" % (name, pattern))
        self.assertEqual([], offenders,
                         "小脑不许受随机/时间/环境影响：%s" % offenders)

    def test_no_preference_config(self):
        offenders = []
        for name in PLUGIN_FILES:
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                text = handle.read()
            if re.search(r"conf[/\\].*json", text) or "load_conf" in text:
                offenders.append(name)
        self.assertEqual([], offenders,
                         "小脑不许读偏好配置（阈值只能在 coords）：%s" % offenders)


class NoInventedWeightsTest(unittest.TestCase):
    def test_candidates_have_no_weighted_score(self):
        calc = FakeCalculator([(2.0, 0.5)])
        rows = calc.candidates(0.0, 1.9, top=3)
        for row in rows:
            self.assertNotIn("score", row,
                             "候选里不许出现自拟加权分（中立绝对）")
            self.assertIn("worst_axis", row)
            self.assertGreaterEqual(row["worst_axis"], 0.0)

    def test_worst_axis_is_explained_by_axes(self):
        calc = FakeCalculator([(2.0, 0.5)])
        for row in calc.candidates(0.0, 1.9, top=3):
            expected = max(abs(row["d_energy"]) / 5.0,
                           abs(row["d_centroid"]) / 500.0,
                           abs(row["d_shape"]) / 5.0)
            self.assertAlmostEqual(expected, row["worst_axis"], 2)


if __name__ == "__main__":
    unittest.main()
