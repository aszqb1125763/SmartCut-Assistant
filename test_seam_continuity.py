# -*- coding: utf-8 -*-
"""接缝体检：声音连续性 + 画面连续性。

子枫定的金标准（2026-11-22）：
  观众看不到原片，只在意「割裂感」。割裂感首先来自视听信号不连续，
  语义跳跃反而能被视听连续盖住。所以评分以视听为主。

关键判据（都是踩过坑才定下来的）：
  · 硬切 = 跨接缝能量骤降（不是"接缝前有衰减"）
  · 自然停顿 = 声音掉了（或本来就平稳）**且**背景声仍连续
  · 画面用几何平均，"某一项彻底不符"必须被放大
"""
from __future__ import annotations

import unittest

import numpy as np

import seam_continuity as seam

SR = 16000


def tone(seconds, freq=440.0, amp=0.2, sr=SR):
    t = np.arange(int(seconds * sr)) / float(sr)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds, amp=0.02, sr=SR, seed=7):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(seconds * sr))).astype(np.float32)


class CrossSeamTests(unittest.TestCase):
    def test_step_down_is_detected(self):
        audio = np.concatenate([tone(0.5, amp=0.5),
                                np.zeros(int(0.5 * SR), dtype=np.float32)])
        self.assertGreater(seam.cross_seam_drop_db(audio, int(0.5 * SR)), 20.0)

    def test_steady_tone_has_no_drop(self):
        audio = tone(1.0)
        self.assertLess(seam.cross_seam_drop_db(audio, int(0.5 * SR)), 3.0)


class AudioSeamTests(unittest.TestCase):
    def test_same_continuous_tone_scores_high(self):
        audio = tone(2.0)
        score, detail = seam.audio_seam(audio, int(1.0 * SR))
        self.assertGreater(score, 0.8)
        self.assertTrue(detail["natural_pause"])

    def test_hard_cut_to_silence_is_flagged(self):
        # 说话中直接切到静音：跨接缝能量骤降，背景也没了 → 必须判割裂。
        audio = np.concatenate([tone(1.0, amp=0.4),
                                np.zeros(int(0.5 * SR), dtype=np.float32)])
        score, detail = seam.audio_seam(audio, int(1.0 * SR))
        self.assertLess(score, 0.5)
        self.assertGreater(detail["cross_drop_db"], seam.HARD_CUT_DROP)
        self.assertFalse(detail["natural_pause"])

    def test_natural_fade_with_steady_background_is_excused(self):
        # 人声渐弱、背景音乐一直响 = 说完一句话，不算割裂。
        ramp = np.linspace(1.0, 0.0, int(1.0 * SR)).astype(np.float32) ** 2
        voice = tone(1.0, freq=1000.0, amp=0.30) * ramp
        background = tone(1.6, freq=200.0, amp=0.05)
        audio = np.concatenate([voice + background[:int(1.0 * SR)],
                                background[:int(0.5 * SR)]])
        score, detail = seam.audio_seam(audio, int(1.0 * SR))
        self.assertTrue(detail["natural_pause"])
        self.assertGreater(score, 0.6)

    def test_voice_fade_but_background_cut_still_penalized(self):
        # 实测 100.1 秒那处：人声渐弱，但背景声断了 18dB → 仍然算割裂。
        ramp = np.linspace(1.0, 0.0, int(1.0 * SR)).astype(np.float32) ** 2
        voice = tone(1.0, freq=1000.0, amp=0.30) * ramp
        background = tone(1.6, freq=200.0, amp=0.08)
        audio = np.concatenate([voice + background[:int(1.0 * SR)],
                                noise(0.5, amp=0.0004)])
        score, detail = seam.audio_seam(audio, int(1.0 * SR))
        self.assertFalse(detail["natural_pause"])
        self.assertLess(score, 0.6)

    def test_tail_drop_detects_step(self):
        # 接缝前 100ms 内音量骤降（该衰减却没衰减，直接掉）。
        audio = np.concatenate([
            tone(0.1, amp=0.5),
            tone(0.1, amp=0.5) * np.linspace(1.0, 0.0,
                                             int(0.1 * SR)).astype(np.float32),
            np.zeros(int(0.2 * SR), dtype=np.float32)])
        self.assertGreater(seam.tail_drop_db(audio, int(0.2 * SR)), 6.0)

    def test_tail_drop_small_for_steady_tone(self):
        audio = tone(0.4)
        self.assertLess(seam.tail_drop_db(audio, int(0.2 * SR)), 3.0)


class CombineTests(unittest.TestCase):
    def test_audio_weight_defaults_to_seventy_percent(self):
        self.assertAlmostEqual(0.7 * 0.2 + 0.3 * 0.8, seam.combine(0.2, 0.8),
                               places=6)

    def test_weight_is_clamped(self):
        # 权重夹到 [0,1]：1.0 只看声音，0.0 只看画面。
        self.assertAlmostEqual(0.5, seam.combine(0.5, 1.0, audio_weight=2.0),
                               places=6)
        self.assertAlmostEqual(1.0, seam.combine(0.5, 1.0, audio_weight=-1.0),
                               places=6)


class VisualSeamTests(unittest.TestCase):
    def test_same_frame_scores_one(self):
        frame = np.full((180, 320, 3), 120, dtype=np.uint8)
        score, _detail = seam.visual_seam(frame, frame.copy())
        self.assertGreater(score, 0.9)

    def test_brightness_and_structure_jump_is_penalized(self):
        # 户外亮画面 → 深色菜单：几何平均必须把它压到低分。
        bright = np.full((180, 320, 3), 210, dtype=np.uint8)
        dark = np.zeros((180, 320, 3), dtype=np.uint8)
        dark[:, :, 2] = 40
        score, detail = seam.visual_seam(bright, dark)
        self.assertLess(score, 0.5)
        self.assertGreater(detail["brightness_gap"], 100)

    def test_geometric_mean_punishes_one_bad_term(self):
        # 两项很好、一项极差：加权平均会得到 0.66，几何平均必须更低。
        base = np.full((180, 320, 3), 128, dtype=np.uint8)
        stripes = base.copy()
        stripes[::2] = 250
        score, _detail = seam.visual_seam(base, stripes)
        weighted = 0.4 * 0.6 + 0.4 * 0.6 + 0.2 * 0.1
        self.assertLess(score, weighted)


class BridgeTests(unittest.TestCase):
    def test_source_contiguous_segment_is_a_bridge(self):
        # 段2 的源起点正好接在段1 的源终点 = 叠化桥接段。
        stamps = [(0.0, 10.0, 5.0), (5.0, 15.0, 0.22), (5.22, 15.3, 8.0)]
        self.assertTrue(seam._is_bridge(stamps, 1))

    def test_real_jump_is_not_a_bridge(self):
        # 源区间断开 = 真剪辑点，要评分。
        stamps = [(0.0, 10.0, 5.0), (5.0, 30.0, 8.0)]
        self.assertFalse(seam._is_bridge(stamps, 1))

    def test_first_segment_is_never_a_bridge(self):
        stamps = [(0.0, 10.0, 5.0), (5.0, 15.0, 3.0)]
        self.assertFalse(seam._is_bridge(stamps, 0))


class SummaryTests(unittest.TestCase):
    def test_summary_counts_and_gate(self):
        rows = [{"at": 1.0, "total": 0.3}, {"at": 2.0, "total": 0.65},
                {"at": 3.0, "total": 0.9}]
        report = seam.summarize(rows, gate=0.6)
        self.assertEqual(1, report["bad"])
        self.assertEqual(1, report["warn"])
        self.assertEqual(1, report["ok"])
        self.assertFalse(report["passed"])

    def test_summary_passes_when_all_above_gate(self):
        rows = [{"at": 1.0, "total": 0.8}, {"at": 2.0, "total": 0.9}]
        report = seam.summarize(rows, gate=0.6)
        self.assertTrue(report["passed"])

    def test_summary_of_empty_timeline_passes(self):
        report = seam.summarize([], gate=0.6)
        self.assertEqual(0, report["count"])
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
