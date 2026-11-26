# -*- coding: utf-8 -*-
"""定性视图测试：模型只拿三个数据，不需要精确原始值。

子枫 2026-11-24：
    「本地模型只用做三个数据的决策就行，所以他不需要知道精确的原始数据。」

守四条：
    1. brief 里**不许出现 dB / Hz 数值**（只有定性标签与秒数）；
    2. 三个数据齐：文字是什么、多久、能量怎么样；
    3. 强弱分档跟阈值一致；
    4. 骤变/渐变由段长决定（可复现）。
"""
from __future__ import annotations

import unittest

import decision
import model_gateway as gateway
from test_axis_snapshot import rising_falling
from test_brain_calc import FakeCalculator

WORDS = [
    {"start": 0.0, "end": 0.5, "word": "我们"},
    {"start": 0.5, "end": 0.9, "word": "开始"},
]


def calc_with_audio():
    calc = FakeCalculator([(3.0, 0.0)], words=WORDS)
    calc._sources["audio"]._audio = rising_falling()
    return calc


class BriefShapeTest(unittest.TestCase):
    def test_has_three_data(self):
        got = decision.brief(calc_with_audio().axis_snapshot(0.0, 3.0))
        self.assertIn("text", got)
        self.assertIn("duration_s", got)
        self.assertIn("energy", got)
        self.assertIn("says", got["text"])
        self.assertIn("duration_s", got["text"])
        self.assertIn("shape", got["energy"])
        self.assertIn("level", got["energy"])
        self.assertIn("change", got["energy"])

    def test_no_precise_numbers(self):
        """定性视图里不许出现 dB / Hz / 斜率这种精确量。"""
        got = decision.brief(calc_with_audio().axis_snapshot(0.0, 3.0))
        flat = repr(got)
        for token in ("db", "dB", "hz", "Hz"):
            self.assertNotIn(token, flat,
                             "定性视图里出现了精确量：%s" % token)
        # 能量那一段只允许字符串标签
        for key, value in got["energy"].items():
            self.assertIsInstance(value, str, "energy.%s 不是标签" % key)

    def test_duration_matches_snapshot(self):
        snap = calc_with_audio().axis_snapshot(0.0, 3.0)
        got = decision.brief(snap)
        self.assertAlmostEqual(3.0, got["duration_s"], 2)


class LevelTest(unittest.TestCase):
    def _level(self, peak):
        snap = {"t0": 0, "t1": 1,
                "text": {"total_duration_s": 1, "sentences": []},
                "energy": {"peak_db": peak, "label": "flat", "segments": []}}
        return decision.brief(snap)["energy"]["level"]

    def test_levels(self):
        # 六档：极强 / 强 / 中上 / 中 / 弱 / 静音
        self.assertEqual("极强", self._level(-8))
        self.assertEqual("强", self._level(-15))
        self.assertEqual("中上", self._level(-24))
        self.assertEqual("中", self._level(-31))
        self.assertEqual("弱", self._level(-40))
        self.assertEqual("静音", self._level(-60))


class ChangeTest(unittest.TestCase):
    def _change(self, segments):
        snap = {"t0": 0, "t1": 1,
                "text": {"total_duration_s": 1, "sentences": []},
                "energy": {"peak_db": -20, "label": "x",
                           "segments": segments}}
        return decision.brief(snap)["energy"]["change"]

    def test_sudden_vs_gradual(self):
        # 骤变：又快又狠（≤80ms 且跌 ≥15dB）
        self.assertEqual("骤变", self._change(
            [{"kind": "fall", "dur_s": 0.04, "db0": -20, "db1": -45}]))
        # 快但只跌 4dB → 不算骤变
        self.assertEqual("渐变", self._change(
            [{"kind": "fall", "dur_s": 0.04, "db0": -20, "db1": -24}]))
        # 跌很多但用了 0.4 秒 → 渐变（拖音/收句）
        self.assertEqual("渐变", self._change(
            [{"kind": "fall", "dur_s": 0.40, "db0": -20, "db1": -50}]))


class GatewayBriefTest(unittest.TestCase):
    def test_brief_via_gateway(self):
        calc = calc_with_audio()
        got = gateway.see(calc, {"look": "brief", "start": 0.0, "end": 3.0})
        self.assertTrue(got["ok"])
        gateway.assert_no_raw(got)
        self.assertIn("energy", got["result"])


if __name__ == "__main__":
    unittest.main()
