# -*- coding: utf-8 -*-
"""接缝三轴的单侧口径测试（2026-11-24 目标函数 bug 的回归）。

背景：
    以前用"以切点为中心 ±50ms"的窗口量接缝，把**即将被删掉的音频**
    也算了进去；而观众听到的是"左段最后 100ms 接右段最前 100ms"。
    选择器优化了一个错的量，于是"选的时候说匹配、交付 35/42 超标"。

这组测试用合成音频把这件事钉死：
    左段很响、中间全静音、右段很响 → 单侧口径的接缝应当是"响接响"（≈0dB），
    中心窗口径会算出十几 dB 的假跳变。
"""
from __future__ import annotations

import unittest

import numpy as np

import seam_picker as spc

SR = 16000


def tone(seconds, amp=0.5, freq=440.0):
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def build(segments):
    """segments: [(时长, 振幅), ...] → 连续音频。"""
    chunks = [np.zeros(int(d * SR), dtype=np.float32) if amp <= 0
              else tone(d, amp) for d, amp in segments]
    return np.concatenate(chunks)


class SideWindowTest(unittest.TestCase):
    def test_side_db_only_looks_one_way(self):
        audio = build([(1.0, 0.5), (1.0, 0.0), (1.0, 0.5)])
        left = 1.0      # 左段出点：之前有声音，之后是静音
        right = 2.0     # 右段入点：之前是静音，之后有声音
        before = spc.side_db(audio, left, "before")
        after = spc.side_db(audio, left, "after")
        self.assertLess(after, before - 30, "出点之后应当是静音")
        # 单侧口径：响 → 响，差应很小
        axes = spc.seam_axes(audio, left, right)
        self.assertLess(axes["energy_db"], 1.5,
                        "单侧口径下 响→响 不该有能量差：%s" % axes)

    def test_centered_window_would_lie(self):
        """中心窗把**将被删掉的音频**也算进来——这就是当初 0/41 的来源。

        构造：左段很响一直到切点（切点之后还会被删掉），右段很轻。
        听众听到的是 响→轻（约 20dB 跳变），
        而中心窗在切点上取 ±50ms，右切点的窗口里还有一半是被删掉的响声，
        于是算出"两边差不多"（≈3dB），选择器就被骗了。
        """
        audio = build([(1.0, 0.5), (0.5, 0.5), (1.0, 0.05)])
        left, right = 1.0, 1.5
        centered = abs(spc.db_at(audio, left) - spc.db_at(audio, right))
        one_sided = spc.seam_axes(audio, left, right)["energy_db"]
        self.assertGreater(one_sided, 15.0, "单侧应看到真实跳变 %.1fdB" % one_sided)
        self.assertLess(centered, one_sided - 10,
                        "中心窗(%.1fdB) 应显著低估单侧(%.1fdB)"
                        % (centered, one_sided))

    def test_shape_uses_one_sided_curves(self):
        audio = build([(1.0, 0.5), (1.0, 0.0), (1.0, 0.5)])
        axes = spc.seam_axes(audio, 1.0, 2.0)
        self.assertLess(axes["shape_db"], 3.0, "响→响 的形状应当接近")

    def test_grade_comes_from_coords(self):
        audio = build([(1.0, 0.5), (1.0, 0.0), (1.0, 0.5)])
        axes = spc.seam_axes(audio, 1.0, 2.0)
        self.assertEqual("ok", axes["grade"])

    def test_silence_to_silence_is_flat(self):
        audio = build([(0.5, 0.0), (1.0, 0.0), (0.5, 0.0)])
        axes = spc.seam_axes(audio, 0.5, 1.5)
        self.assertEqual("ok", axes["grade"])


if __name__ == "__main__":
    unittest.main()
