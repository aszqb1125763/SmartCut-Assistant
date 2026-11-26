# -*- coding: utf-8 -*-
"""回执与交付闸的测试（E03 两处标准、棘轮阈值）。

不需要素材：用假的小脑（返回固定探针结果）和假报告。
"""
from __future__ import annotations

import unittest

import coords
import coords_report


class StubCalc:
    """假小脑：只实现回执用到的两个探针。"""

    def __init__(self, energy=1.0, tone=50.0, shape=2.0, tail=None,
                 pause=False):
        self.energy = energy
        self.tone = tone
        self.shape = shape
        self.pause = pause
        self.tail = tail or {"decay_ms": 140, "shape": "natural",
                             "grade": "ok", "tail_chopped": False}

    def probe_seam(self, at):
        grade = coords.combine(coords.energy_grade(self.energy),
                               coords.tone_grade(self.tone),
                               coords.shape_grade(self.shape))
        return {"values": {"energy_gap_db": self.energy,
                           "tone_gap_hz": self.tone,
                           "shape_rms_db": self.shape},
                "grade": grade, "row": "M01", "severity": "none",
                "compares": [], "pause_edge": self.pause,
                "pause_note": "pause_edge" if self.pause else None,
                "tail_ms": self.tail.get("decay_ms"),
                "tail_shape": self.tail.get("shape"),
                "tail_grade": self.tail.get("grade"),
                "tail_chopped": self.tail.get("tail_chopped", False)}


class SeamRowTest(unittest.TestCase):
    def test_jump_is_max_of_energy_and_shape(self):
        row = coords_report.seam_row(StubCalc(energy=3.0, shape=9.0), 10.0,
                                     p90_db=10.0)
        self.assertEqual(9.0, row["jump_db"])
        self.assertEqual("ok", row["relative_grade"])

    def test_relative_over_marks_row(self):
        row = coords_report.seam_row(StubCalc(energy=14.0, shape=2.0), 10.0,
                                     p90_db=10.0)
        self.assertTrue(row["relative_over"])
        self.assertEqual("over", row["relative_grade"])


class VerdictTest(unittest.TestCase):
    def _report(self, **summary):
        base = {"total": 10, "over_ratio": 0.0, "gray_ratio": 0.0, "p0": 0,
                "relative": {"over_ratio": 0.0}}
        base.update(summary)
        return {"summary": base}

    def test_clean_passes(self):
        self.assertTrue(coords_report.verdict(self._report())["passed"])

    def test_p0_blocks(self):
        got = coords_report.verdict(self._report(p0=1), max_p0=0)
        self.assertFalse(got["passed"])
        self.assertIn("P0", got["reason"])

    def test_relative_over_blocks(self):
        got = coords_report.verdict(
            self._report(relative={"over_ratio": 0.6}),
            max_relative_over=0.3)
        self.assertFalse(got["passed"])
        self.assertIn("相对超标", got["reason"])

    def test_strict_over_does_not_block(self):
        """成片是混音，严格口径只记不拦（E03 出片前用 P90 宽松线）。"""
        got = coords_report.verdict(
            self._report(over_ratio=0.9, relative={"over_ratio": 0.0}),
            max_relative_over=0.3, max_p0=0)
        self.assertTrue(got["passed"], got)

    def test_gray_blocks_too_much(self):
        got = coords_report.verdict(
            self._report(gray_ratio=0.8), max_gray=0.3)
        self.assertFalse(got["passed"])
        self.assertIn("看不清", got["reason"])


if __name__ == "__main__":
    unittest.main()
