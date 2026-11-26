# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

import source_timeline


class SourceTimelineTests(unittest.TestCase):
    def test_out_of_order_text_is_sorted_by_absolute_time(self):
        segments = [
            [6.0, 7.0, "后说"],
            [1.0, 2.5, "先说"],
        ]
        events = source_timeline.build_events(
            segments, duration=9.0, audio_tags=["后轨", "先轨"])
        speech = [row for row in events if row["kind"] == "speech"]
        self.assertEqual(["U0001", "U0002"],
                         [row["event_id"] for row in speech])
        self.assertEqual([1.0, 6.0], [row["start"] for row in speech])
        self.assertEqual(["先说", "后说"], [row["text"] for row in speech])
        self.assertEqual(["先轨", "后轨"], [row["audio"] for row in speech])
        self.assertEqual([1, 0], [row["source_index"] for row in speech])

    def test_gaps_and_overlaps_are_explicit_chronology(self):
        segments = [
            [1.0, 4.0, "主句"],
            [2.5, 3.5, "重叠句"],
            [6.0, 7.0, "下一句"],
        ]
        events = source_timeline.build_events(segments, duration=8.0)
        speech = [row for row in events if row["kind"] == "speech"]
        gaps = [row for row in events if row["kind"] == "gap"]
        self.assertEqual(1.5, speech[0]["next_overlap"])
        self.assertEqual(1.5, speech[1]["prev_overlap"])
        self.assertEqual(0.0, speech[1]["safe_after"])
        self.assertTrue(any(row["start"] == 4.0 and row["end"] == 6.0
                            for row in gaps))
        self.assertTrue(all("不等于静音" in row["audio"] for row in gaps))

    def test_revision_changes_when_a_source_boundary_moves(self):
        first = source_timeline.build_events([[1.0, 2.0, "甲"]], duration=3.0)
        second = source_timeline.build_events([[1.0, 2.1, "甲"]], duration=3.0)
        self.assertNotEqual(source_timeline.revision(first),
                            source_timeline.revision(second))

    def test_revision_changes_when_transcript_is_corrected(self):
        first = source_timeline.build_events([[1.0, 2.0, "错字"]], duration=3.0)
        second = source_timeline.build_events([[1.0, 2.0, "正字"]], duration=3.0)
        self.assertNotEqual(source_timeline.revision(first),
                            source_timeline.revision(second))

    def test_revision_uses_normalised_transcript_text(self):
        first = source_timeline.build_events(
            [[1.0, 2.0, "  Ａ  计划  "]], duration=3.0)
        second = source_timeline.build_events(
            [[1.0, 2.0, "A 计划"]], duration=3.0)
        self.assertEqual(source_timeline.revision(first),
                         source_timeline.revision(second))


if __name__ == "__main__":
    unittest.main()

