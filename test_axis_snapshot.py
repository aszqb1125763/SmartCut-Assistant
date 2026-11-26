# -*- coding: utf-8 -*-
"""三轴快照测试：文字（是什么+多久）+ 能量（形态）+ 统一时间轴。

子枫 2026-11-24：
    「能量轴数据量巨大还要自己计算，现在大模型只用知道三个数据：
      文字是什么、在时间轴上持续多久、能量是怎么样的。
      因为时间轴全程同步，所以能量轴天生自带时间轴信息。」
"""
from __future__ import annotations

import unittest

import model_gateway as gateway
from test_brain_calc import FakeCalculator

WORDS = [
    {"start": 0.0, "end": 0.5, "word": "我们"},
    {"start": 0.5, "end": 0.9, "word": "开始"},
    {"start": 2.0, "end": 2.5, "word": "然后"},
]


def rising_falling():
    """0–1s 渐强、1–2s 渐弱、2–3s 静音。"""
    import numpy as np
    sr = 16000
    t = np.arange(int(3 * sr), dtype=np.float32) / sr
    gain = np.ones_like(t)
    gain[t < 1.0] = t[t < 1.0]
    gain[(t >= 1.0) & (t < 2.0)] = 2.0 - t[(t >= 1.0) & (t < 2.0)]
    gain[t >= 2.0] = 0.0
    return (0.4 * gain * np.sin(2 * np.pi * 300 * t)).astype(np.float32)


class EnergyShapeTest(unittest.TestCase):
    def test_has_segments_with_time(self):
        calc = FakeCalculator([(3.0, 0.0)])   # 占位，下面替换音频
        calc._sources["audio"]._audio = rising_falling()
        got = calc.energy_shape(0.0, 3.0)
        self.assertEqual("energy_shape", got["probe"])
        self.assertIn("segments", got)
        self.assertGreaterEqual(len(got["segments"]), 2)
        for row in got["segments"]:
            self.assertIn(row["kind"], ("rise", "fall", "flat", "silence"))
            self.assertLessEqual(row["t0"], row["t1"])
            self.assertGreater(row["dur_s"], 0)

    def test_peak_valley_and_span(self):
        calc = FakeCalculator([(3.0, 0.0)])
        calc._sources["audio"]._audio = rising_falling()
        got = calc.energy_shape(0.0, 3.0)
        self.assertGreater(got["peak_db"], got["valley_db"])
        self.assertAlmostEqual(3.0, got["duration_s"], 2)
        self.assertTrue(0 <= got["peak_at"] <= 3.0)

    def test_silence_region_labelled(self):
        calc = FakeCalculator([(3.0, 0.0)])
        calc._sources["audio"]._audio = rising_falling()
        got = calc.energy_shape(2.1, 2.9)
        self.assertEqual("silence", got["segments"][0]["kind"])


class TextSpanTest(unittest.TestCase):
    def test_duration_and_cps(self):
        calc = FakeCalculator([(3.0, 0.5)], words=WORDS)
        got = calc.text_span(0.0, 3.0)
        self.assertEqual("text_span", got["probe"])
        self.assertGreaterEqual(len(got["sentences"]), 1)
        first = got["sentences"][0]
        self.assertGreater(first["duration_s"], 0)
        self.assertGreater(first["chars"], 0)
        self.assertGreater(first["cps"], 0)


class AxisSnapshotTest(unittest.TestCase):
    def test_three_axes_present(self):
        calc = FakeCalculator([(3.0, 0.0)], words=WORDS)
        calc._sources["audio"]._audio = rising_falling()
        snap = calc.axis_snapshot(0.0, 3.0)
        self.assertEqual("source", snap["timeline"])
        self.assertIn("text", snap)
        self.assertIn("energy", snap)
        self.assertEqual("text_span", snap["text"]["probe"])
        self.assertEqual("energy_shape", snap["energy"]["probe"])

    def test_no_raw_arrays(self):
        calc = FakeCalculator([(3.0, 0.0)], words=WORDS)
        calc._sources["audio"]._audio = rising_falling()
        gateway.assert_no_raw(calc.axis_snapshot(0.0, 3.0))


class GatewayAxisTest(unittest.TestCase):
    def test_axis_look(self):
        calc = FakeCalculator([(3.0, 0.0)], words=WORDS)
        calc._sources["audio"]._audio = rising_falling()
        got = gateway.see(calc, {"look": "axis", "start": 0.0, "end": 3.0})
        self.assertTrue(got["ok"])
        self.assertIn("energy", got["result"])
        gateway.assert_no_raw(got)

    def test_energy_look(self):
        calc = FakeCalculator([(3.0, 0.0)])
        calc._sources["audio"]._audio = rising_falling()
        got = gateway.see(calc, {"look": "energy", "start": 0.0, "end": 3.0})
        self.assertEqual("energy_shape", got["result"]["probe"])


if __name__ == "__main__":
    unittest.main()
