# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import unittest

import audiofeat_bounds as bounds
import subtitles


def _data():
    evidence = [
        {
            "segment_id": "s000001", "start": 1.0, "end": 2.0,
            "source_audio_track": 1, "source_audio_tracks": [1],
            "words": [{"text": "甲", "start": 1.05, "end": 1.95}],
        },
        {
            "segment_id": "s000002", "start": 2.2, "end": 3.0,
            "source_audio_track": 1, "source_audio_tracks": [1],
            "words": [{"text": "乙", "start": 2.25, "end": 2.95}],
        },
    ]
    data = {
        "source_identity": {"size": 123, "mtime_ns": 456, "fingerprint": "abc"},
        "audio_stream_used": 1,
        "asr_profile": {"version": 8},
        "segments": [[1.0, 2.0, "甲"], [2.2, 3.0, "乙"]],
        "segment_evidence": evidence,
        "segments_refined": [[0.95, 2.05], [2.15, 3.05]],
        "segments_refined_version": bounds.CAPTION_REFINEMENT_VERSION,
    }
    data["segments_refined_metadata"] = bounds.metadata_for_index(data)
    return data


class RefinedSubtitleValidationTests(unittest.TestCase):
    def test_valid_current_refinement_is_used(self):
        rows = subtitles.get_segments(_data())
        self.assertEqual((0.95, 2.05, "甲"), rows[0])
        self.assertEqual((2.15, 3.05, "乙"), rows[1])

    def test_old_v1_and_each_provenance_mismatch_fall_back(self):
        base = _data()
        cases = []
        old = copy.deepcopy(base)
        old["segments_refined_version"] = 1
        cases.append(old)
        for key, value in (
                ("audio_stream_used", 0),
                ("source_identity", {"size": 999}),
                ("asr_profile_version", 6),
                ("segment_id_hash", "bad"),
                ("segment_timeline_hash", "bad"),
                ("segment_count", 999)):
            item = copy.deepcopy(base)
            if key in item["segments_refined_metadata"]:
                item["segments_refined_metadata"][key] = value
            else:
                item[key] = value
            cases.append(item)
        for item in cases:
            with self.subTest(item=item):
                self.assertEqual((1.0, 2.0, "甲"),
                                 subtitles.get_segments(item)[0])

    def test_count_overlap_and_word_clipping_are_rejected(self):
        count = _data()
        count["segments_refined"] = count["segments_refined"][:1]
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(count)[0])

        overlap = _data()
        overlap["segments_refined"] = [[0.95, 2.25], [2.15, 3.05]]
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(overlap)[0])

        clipped = _data()
        clipped["segments_refined"] = [[1.10, 2.05], [2.15, 3.05]]
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(clipped)[0])

    def test_segment_id_order_change_invalidates_refinement(self):
        data = _data()
        data["segment_evidence"][0]["segment_id"] = "s999999"
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(data)[0])

    def test_precise_evidence_baseline_allows_full_quarter_second_move(self):
        data = _data()
        data["segments"][0][0] = 1.0
        data["segment_evidence"][0]["start"] = 0.995
        data["segment_evidence"][0]["words"][0]["start"] = 1.05
        data["segments_refined"][0][0] = 0.745
        data["segments_refined_metadata"] = bounds.metadata_for_index(data)
        self.assertEqual((0.745, 2.05, "甲"), subtitles.get_segments(data)[0])

    def test_timeline_change_with_same_segment_id_invalidates_refinement(self):
        data = _data()
        data["segment_evidence"][0]["words"][0]["end"] = 1.90
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(data)[0])

    def test_missing_words_are_valid_only_when_edge_is_unchanged(self):
        data = _data()
        data["segment_evidence"][0]["words"] = []
        data["segments_refined"][0] = [1.0, 2.0]
        data["segments_refined_metadata"] = bounds.metadata_for_index(data)
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(data)[0])
        data["segments_refined"][0] = [0.99, 2.0]
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(data)[0])

    def test_unchanged_raw_edge_may_equal_first_or_last_word(self):
        data = _data()
        data["segment_evidence"][0]["words"] = [
            {"text": "甲", "start": 1.0, "end": 2.0}]
        data["segments_refined"][0] = [1.0, 2.0]
        data["segments_refined_metadata"] = bounds.metadata_for_index(data)
        self.assertEqual((1.0, 2.0, "甲"), subtitles.get_segments(data)[0])


if __name__ == "__main__":
    unittest.main()
