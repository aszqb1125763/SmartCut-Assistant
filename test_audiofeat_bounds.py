# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

import audiofeat_bounds as bounds


def _evidence(start, end, track=1, segment_id="s000001"):
    return {
        "start": start, "end": end, "segment_id": segment_id,
        "source_audio_track": track, "source_audio_tracks": [track],
        "words": [
            {"text": "开", "start": start + 0.05, "end": start + 0.25},
            {"text": "口", "start": end - 0.25, "end": end - 0.05},
        ],
    }


class CaptionWaveformRefinementTests(unittest.TestCase):
    def test_local_edges_move_within_limit_without_changing_cue_count(self):
        audio = np.zeros(16000 * 4, dtype=np.float32)
        t = np.arange(int(1.20 * 16000), dtype=np.float32) / 16000.0
        audio[int(0.90 * 16000):int(2.10 * 16000)] = 0.2 * np.sin(
            2.0 * np.pi * 220.0 * t)
        segments = [(1.0, 2.0, "开口")]
        refined = bounds.refine_caption_segments(
            audio, segments, [_evidence(1.0, 2.0)], 1)
        self.assertEqual(1, len(refined))
        self.assertGreaterEqual(refined[0][0], 0.75)
        self.assertLessEqual(abs(refined[0][0] - 1.0), 0.25)
        self.assertLessEqual(abs(refined[0][1] - 2.0), 0.25)
        self.assertLessEqual(refined[0][0], 1.045)
        self.assertGreaterEqual(refined[0][1], 1.955)

    def test_continuous_activity_has_no_observed_edge_and_stays_put(self):
        audio = np.full(16000 * 3, 0.2, dtype=np.float32)
        segments = [(1.0, 2.0, "持续")]
        self.assertEqual(
            [(1.0, 2.0)],
            bounds.refine_caption_segments(
                audio, segments, [_evidence(1.0, 2.0)], 1))

    def test_wrong_track_and_missing_words_are_conservative(self):
        audio = np.zeros(16000 * 3, dtype=np.float32)
        segments = [(1.0, 2.0, "原文")]
        wrong = _evidence(1.0, 2.0, track=0)
        self.assertEqual(
            [(1.0, 2.0)],
            bounds.refine_caption_segments(audio, segments, [wrong], 1))
        missing = dict(wrong, source_audio_track=1,
                       source_audio_tracks=[1], words=[])
        self.assertEqual(
            [(1.0, 2.0)],
            bounds.refine_caption_segments(audio, segments, [missing], 1))

    def test_candidate_beyond_maximum_shift_is_rejected(self):
        audio = np.zeros(16000 * 3, dtype=np.float32)
        segments = [(1.0, 2.0, "原文")]
        fake = [{"start": 0.70, "end": 2.30, "confidence": 1.0}]
        with mock.patch.object(bounds, "_local_activity", return_value=fake):
            refined = bounds.refine_caption_segments(
                audio, segments, [_evidence(1.0, 2.0)], 1)
        self.assertEqual([(1.0, 2.0)], refined)

    def test_new_overlap_reverts_touching_edges(self):
        audio = np.zeros(16000 * 3, dtype=np.float32)
        segments = [(1.0, 1.5, "甲"), (1.55, 2.0, "乙")]
        evidence = [_evidence(1.0, 1.5, segment_id="s000001"),
                    _evidence(1.55, 2.0, segment_id="s000002")]

        def fake_activity(_audio, start, end, _sr):
            midpoint = (start + end) / 2.0
            if abs(midpoint - 1.5) < 0.02:
                return [{"start": 1.30, "end": 1.58, "confidence": 1.0}]
            if abs(midpoint - 1.55) < 0.02:
                return [{"start": 1.48, "end": 1.75, "confidence": 1.0}]
            return []

        with mock.patch.object(bounds, "_local_activity", side_effect=fake_activity):
            refined = bounds.refine_caption_segments(
                audio, segments, evidence, 1)
        self.assertEqual(2, len(refined))
        self.assertLessEqual(refined[0][1], refined[1][0])
        self.assertEqual(1.5, refined[0][1])
        self.assertEqual(1.55, refined[1][0])

    def test_serialization_keeps_submillisecond_word_guard_precision(self):
        audio = np.zeros(16000 * 3, dtype=np.float32)
        segments = [(1.0, 2.0, "精度")]
        evidence = [_evidence(1.0, 2.0)]
        evidence[0]["words"][-1]["end"] = 1.9904
        activity = [{"start": 0.95, "end": 1.9354, "confidence": 1.0}]
        with mock.patch.object(bounds, "_local_activity", return_value=activity):
            refined = bounds.refine_caption_segments(
                audio, segments, evidence, 1)
        self.assertEqual(1.9954, refined[0][1])
        data = {
            "source_identity": {"size": 1},
            "audio_stream_used": 1,
            "asr_profile": {"version": 8},
            "segments": [[1.0, 2.0, "精度"]],
            "segment_evidence": evidence,
            "segments_refined": [list(refined[0])],
            "segments_refined_version": bounds.CAPTION_REFINEMENT_VERSION,
        }
        data["segments_refined_metadata"] = bounds.metadata_for_index(data)
        self.assertEqual(refined, bounds.validated_refined_segments(data))


class RefineIndexTests(unittest.TestCase):
    def test_refine_index_extracts_recorded_track_and_writes_current_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "index.json")
            data = {
                "source": str(Path(folder, "source.mp4")),
                "source_identity": {"size": 10, "mtime_ns": 20},
                "audio_stream_used": 1,
                "asr_profile": {"version": 8},
                "segments": [[1.0, 2.0, "字幕"]],
                "segment_evidence": [_evidence(0.995, 2.004)],
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            calls = []
            seen_segments = []

            def fake_extract(_source, _wav, stream):
                calls.append(stream)
                return True

            def fake_refine(_audio, segments, _evidence_rows, _track, sr):
                seen_segments.extend(segments)
                self.assertEqual(16000, sr)
                return [(0.95, 2.05)]

            with mock.patch("index.extract_audio_wav", side_effect=fake_extract), \
                    mock.patch("index.read_wav_mono",
                               return_value=np.zeros(16000 * 3, dtype=np.float32)), \
                    mock.patch.object(bounds, "refine_caption_segments",
                                      side_effect=fake_refine):
                self.assertEqual(0, bounds.refine_index(str(path), on_status=lambda _m: None))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([1], calls)
            self.assertEqual((0.995, 2.004, "字幕"), seen_segments[0])
            self.assertEqual(bounds.CAPTION_REFINEMENT_VERSION,
                             saved["segments_refined_version"])
            self.assertEqual([[0.95, 2.05]], saved["segments_refined"])
            self.assertEqual(1,
                             saved["segments_refined_metadata"]["audio_stream_used"])
            self.assertEqual(bounds.CAPTION_REFINEMENT_ALGORITHM,
                             saved["segments_refined_metadata"]["algorithm"])
            self.assertTrue(
                saved["segments_refined_metadata"]["segment_timeline_hash"])


if __name__ == "__main__":
    unittest.main()
