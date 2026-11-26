# -*- coding: utf-8 -*-
"""统一时间轴测试（所有小脑必须落在同一条源片时间轴上）。"""
from __future__ import annotations

import os
import tempfile
import unittest

from timeline import Timeline


def seam(at, grade="ok"):
    return {"at": at, "grade": grade}


class PushAndOrderTest(unittest.TestCase):
    def test_entries_sorted_by_time(self):
        tl = Timeline()
        tl.push("audio", "seam", seam(3.0))
        tl.push("text", "edge", {"at": 1.0, "grade": "S"})
        tl.push("picture", "jump", {"at": 2.0, "grade": "ok"})
        self.assertEqual([1.0, 2.0, 3.0], [row["t"] for row in tl.entries])

    def test_entry_carries_timeline_and_brain(self):
        tl = Timeline()
        row = tl.push("audio", "seam", seam(1.5))
        self.assertEqual("source", row["timeline"])
        self.assertEqual("audio", row["brain"])
        self.assertEqual(1.5, row["t"])

    def test_missing_time_rejected(self):
        tl = Timeline()
        with self.assertRaises(ValueError):
            tl.push("audio", "seam", {"grade": "ok"})


class SyncTest(unittest.TestCase):
    def test_snapshot_merges_brains_within_tolerance(self):
        tl = Timeline(tolerance=0.02)
        tl.push("audio", "seam", seam(10.0))
        tl.push("speech", "cut_safety", {"at": 10.005, "inside_word": False})
        tl.push("text", "edge", {"at": 10.015, "grade": "S"})
        snap = tl.snapshot(10.0)
        self.assertEqual(3, snap["count"])
        self.assertEqual({"audio", "speech", "text"}, set(snap["brains"]))
        self.assertTrue(snap["aligned"])

    def test_far_entries_do_not_merge(self):
        tl = Timeline(tolerance=0.02)
        tl.push("audio", "seam", seam(10.0))
        tl.push("picture", "jump", {"at": 10.5, "grade": "ok"})
        self.assertEqual(1, tl.snapshot(10.0)["count"])

    def test_is_aligned_flags_misaligned(self):
        tl = Timeline(tolerance=0.02)
        tl.push("audio", "seam", seam(10.0))
        tl.push("text", "edge", {"at": 10.5, "grade": "S"})
        # 10.0 处只有一个脑 → 视为对齐；10.5 处也只有一个
        self.assertTrue(tl.is_aligned(10.0))

    def test_span(self):
        tl = Timeline()
        for at in (1.0, 5.0, 9.0):
            tl.push("audio", "seam", seam(at))
        self.assertEqual(2, len(tl.span(4.0, 9.5)))


class SaveTest(unittest.TestCase):
    def test_save_roundtrip(self):
        tl = Timeline()
        tl.push("audio", "seam", seam(2.0))
        folder = tempfile.mkdtemp(prefix="tl_")
        path = tl.save(os.path.join(folder, "coords_timeline.json"))
        self.assertTrue(os.path.isfile(path))
        import json
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(1, data["count"])
        self.assertEqual("source", data["timeline"])


if __name__ == "__main__":
    unittest.main()
