# -*- coding: utf-8 -*-
"""判据矩阵测试（M01–M15 是数据，不是 if）。"""
from __future__ import annotations

import os
import unittest

import coords

BASE = os.path.dirname(os.path.abspath(__file__))
MATRIX = os.path.join(BASE, "conf", "coords_matrix.json")


class MatrixShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = coords.matrix()
        cls.rows = cls.data.get("rows") or []

    def test_matrix_present(self):
        self.assertTrue(os.path.isfile(MATRIX), "缺 conf/coords_matrix.json")
        self.assertGreaterEqual(len(self.rows), 15, "判据矩阵少于 15 行")

    def test_ids_unique(self):
        ids = [row.get("id") for row in self.rows]
        self.assertEqual(len(ids), len(set(ids)), "矩阵行号重复")
        bad = [i for i in ids if not str(i).startswith("M")]
        self.assertEqual([], bad)

    def test_every_row_has_verdict_and_action(self):
        for row in self.rows:
            self.assertTrue(row.get("verdict"), row.get("id"))
            self.assertIn(row.get("severity"), ("P0", "P1", "P2", "none"),
                          row.get("id"))
            self.assertTrue(row.get("action"), row.get("id"))
            self.assertTrue(row.get("requires"), row.get("id"))

    def test_spec_version_matches(self):
        self.assertEqual(coords.SPEC_VERSION, self.data.get("spec_version"))


class MatrixJudgeTest(unittest.TestCase):
    def test_ok_row(self):
        got = coords.judge({"energy_gap_db": 1.0, "tone_gap_hz": 35.0,
                            "shape_rms_db": 4.7})
        self.assertEqual("M01", got["row"])
        self.assertTrue(got["passed"])

    def test_hard_break_is_p0(self):
        got = coords.judge({"tail_decay_ms": 12.0})
        self.assertEqual("M04", got["row"])
        self.assertEqual("P0", got["severity"])
        self.assertFalse(got["passed"])

    def test_most_severe_wins(self):
        # 同时满足 M01（都合格）和 M02（20ms 跌 18dB 且同句内）
        got = coords.judge({"energy_gap_db": 1.0, "tone_gap_hz": 30.0,
                            "shape_rms_db": 3.0, "cliff_db_20ms": 18.0,
                            "same_sentence": True})
        self.assertEqual("M02", got["row"])
        self.assertEqual("P0", got["severity"])

    def test_missing_keys_are_skipped_not_guessed(self):
        # 只给 tail_decay_ms，其余行缺键 → 不许乱判
        got = coords.judge({"tail_decay_ms": 5.0})
        self.assertEqual("M04", got["row"])
        # 完全缺输入时：passed 必须是 None（判不了 ≠ 通过）
        empty = coords.judge({})
        self.assertIsNone(empty["passed"])
        self.assertIsNone(empty["row"])

    def test_unknown_axis_never_matches(self):
        fake = [{"id": "MX", "requires": ["not_exist"], "when": [],
                 "verdict": "x", "severity": "P0", "action": "y"}]
        got = coords.judge({}, rows=fake)
        self.assertIsNone(got["row"], "缺键的行不许命中")
        self.assertIsNone(got["passed"])


if __name__ == "__main__":
    unittest.main()
