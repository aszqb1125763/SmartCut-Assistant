# -*- coding: utf-8 -*-
import json
import unittest

import numpy as np

import boundary_refiner as br


class BoundaryRefinerTests(unittest.TestCase):
    def test_short_chinese_first_word_is_never_clipped(self):
        result = br.refine_span(
            {"start": 1.04, "end": 2.0, "confidence": 0.9},
            words=[{"text": "我", "start": 0.96, "end": 1.09, "probability": 0.94},
                   {"text": "来了", "start": 1.10, "end": 1.82, "probability": 0.91}],
            fps=25.0, duration=5.0)
        self.assertLessEqual(result["final_interval"][0], 0.96)
        self.assertGreaterEqual(result["final_interval"][1], 1.82)
        self.assertAlmostEqual(
            result["final_interval"][0] * 25.0,
            round(result["final_interval"][0] * 25.0), places=7)

    def test_breath_laughter_and_game_audio_tail_are_preserved(self):
        result = br.refine_span(
            (2.0, 3.0),
            words=[{"text": "好", "start": 2.05, "end": 2.8}],
            primary_activity=[{"start": 1.93, "end": 3.08, "kind": "speech"}],
            secondary_activity={
                "game": [{"start": 3.10, "end": 3.34, "kind": "game_event"}],
                "mic": [{"start": 3.35, "end": 3.51, "kind": "laughter"}],
            }, fps=30.0, duration=8.0)
        self.assertGreaterEqual(result["final_interval"][1], 3.51)
        self.assertIn("audio_event_tail_preserved", result["reasons"])
        self.assertIn("breath_laughter_or_emotion_preserved", result["reasons"])

    def test_no_silence_uses_conservative_search_window(self):
        result = br.refine_span(
            {"start": 5.0, "end": 6.0, "confidence": 0.9},
            primary_activity=[{"start": 0.0, "end": 20.0}],
            fps=20.0, duration=20.0)
        self.assertEqual(result["search_window"], result["final_interval"])
        self.assertIn("no_silence_conservative_fallback", result["reasons"])

    def test_adjacent_expansions_merge_deterministically(self):
        spans = [(1.0, 1.5), (1.62, 2.1)]
        activity = [{"start": 0.9, "end": 2.2}]
        result = br.refine_spans(spans, primary_activity=activity, fps=30.0, duration=5.0)
        self.assertEqual(1, len(result))
        self.assertEqual([0, 1], result[0]["source_span_indices"])
        self.assertIn("overlapping_refined_spans_merged", result[0]["reasons"])

    def test_multitrack_audio_can_extend_beyond_speech(self):
        result = br.refine_span(
            (4.0, 4.8),
            primary_activity=[{"start": 3.95, "end": 4.9, "track": "mic"}],
            secondary_activity={"game": [{"start": 4.95, "end": 5.4}]},
            fps=24.0, duration=10.0)
        self.assertGreaterEqual(result["final_interval"][1], 5.4)
        self.assertEqual("game", result["evidence"]["secondary_activity"][0]["track"])

    def test_continuous_game_audio_does_not_fill_search_window(self):
        baseline = br.refine_span((2.0, 3.0), fps=30.0, duration=8.0)
        result = br.refine_span(
            (2.0, 3.0),
            secondary_activity={"game": [
                {"start": 0.0, "end": 8.0, "kind": "game_event"},
            ]}, fps=30.0, duration=8.0)
        self.assertEqual(baseline["final_interval"], result["final_interval"])
        self.assertEqual([], result["evidence"]["secondary_activity"])
        self.assertIn(
            "continuous_or_distant_secondary_audio_ignored",
            result["reasons"])

    def test_short_game_event_near_tail_is_still_preserved(self):
        baseline = br.refine_span((2.0, 3.0), fps=30.0, duration=8.0)
        result = br.refine_span(
            (2.0, 3.0),
            secondary_activity={"game": [
                {"start": baseline["final_interval"][1] - 0.02,
                 "end": baseline["final_interval"][1] + 0.24,
                 "kind": "game_event"},
            ]}, fps=30.0, duration=8.0)
        self.assertGreater(
            result["final_interval"][1], baseline["final_interval"][1])
        self.assertEqual(1, len(result["evidence"]["secondary_activity"]))

    def test_continuous_secondary_audio_does_not_merge_nearby_keeps(self):
        result = br.refine_spans(
            [(1.0, 1.3), (2.1, 2.4)],
            secondary_activity={"game": [
                {"start": 0.0, "end": 4.0, "kind": "game_event"},
            ]}, fps=30.0, duration=4.0)
        self.assertEqual(2, len(result))

    def test_frame_alignment_rounds_start_back_and_end_forward(self):
        result = br.refine_span(
            (1.011, 1.977), words=[{"text": "句", "start": 1.011, "end": 1.977}],
            fps=25.0, duration=5.0)
        start, end = result["final_interval"]
        self.assertAlmostEqual(start * 25.0, round(start * 25.0), places=7)
        self.assertAlmostEqual(end * 25.0, round(end * 25.0), places=7)
        self.assertLessEqual(start, result["immutable_semantic_core"][0])
        self.assertGreaterEqual(end, result["immutable_semantic_core"][1])

    def test_low_confidence_keeps_more_than_high_confidence(self):
        low = br.refine_span({"start": 2.0, "end": 3.0, "confidence": 0.1}, fps=30.0)
        high = br.refine_span(
            {"start": 2.0, "end": 3.0, "confidence": 1.0},
            words=[{"text": "清楚", "start": 2.0, "end": 3.0, "confidence": 1.0}], fps=30.0)
        self.assertLess(low["final_interval"][0], high["final_interval"][0])
        self.assertGreater(low["final_interval"][1], high["final_interval"][1])

    def test_energy_detector_and_repeatability(self):
        sample_rate = 1000
        audio = np.zeros(3000, dtype=np.float32)
        audio[900:1700] = 0.6
        detected = br.detect_activity_intervals(audio, sample_rate)
        self.assertEqual(1, len(detected))
        args = dict(
            semantic_span=(1.0, 1.5), primary_activity=detected,
            visual_boundaries=[{"time": 0.8, "confidence": 0.8},
                               {"time": 1.9, "confidence": 0.8}],
            fps=30.0, duration=3.0, task_seed="fixed-plan")
        first = br.refine_span(**args)
        second = br.refine_span(**args)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_waveform_edge_snap_can_trim_silence_around_model_span(self):
        spans, changes = br.snap_spans_to_activity_edges(
            [(1.0, 2.0)],
            [{"start": 1.12, "end": 1.86, "confidence": 0.9,
              "track": "0"}],
            words=[{"start": 1.15, "end": 1.80, "text": "一句话"}],
            duration=4.0)
        self.assertEqual(1, len(changes))
        self.assertAlmostEqual(1.08, spans[0][0], places=6)
        self.assertAlmostEqual(1.93, spans[0][1], places=6)
        self.assertEqual("local_waveform_energy", changes[0]["source"])

    def test_waveform_edge_snap_never_clips_retained_word(self):
        spans, _changes = br.snap_spans_to_activity_edges(
            [(1.0, 2.0)],
            [{"start": 1.30, "end": 1.70, "confidence": 0.95}],
            words=[{"start": 1.10, "end": 1.90, "text": "低声尾音"}],
            duration=4.0)
        self.assertLessEqual(spans[0][0], 1.095)
        self.assertGreaterEqual(spans[0][1], 1.905)

    def test_waveform_edge_snap_uses_detected_pcm_onset_and_offset(self):
        sample_rate = 1000
        audio = np.zeros(3000, dtype=np.float32)
        audio[900:1700] = 0.6
        activity = br.detect_activity_intervals(audio, sample_rate)
        spans, changes = br.snap_spans_to_activity_edges(
            [(1.0, 1.5)], activity, duration=3.0,
            config=br.WaveformEdgeConfig(
                search_seconds=0.8, preroll_seconds=0.0,
                postroll_seconds=0.0))
        self.assertEqual(1, len(changes))
        self.assertLessEqual(spans[0][0], 0.91)
        self.assertGreaterEqual(spans[0][1], 1.69)

    def test_waveform_edge_snap_keeps_cut_when_probe_has_no_edge(self):
        spans, changes = br.snap_spans_to_activity_edges(
            [(2.0, 3.0)],
            [{"start": 1.2, "end": 3.8, "confidence": 0.9}],
            duration=5.0,
            config=br.WaveformEdgeConfig(search_seconds=0.8))
        self.assertEqual([(2.0, 3.0)], spans)
        self.assertEqual([], changes)

    def test_waveform_edge_snap_preserves_gap_between_retained_spans(self):
        spans, changes = br.snap_spans_to_activity_edges(
            [(1.0, 2.0), (2.3, 3.3)],
            [{"start": 1.95, "end": 2.35, "confidence": 0.95}],
            duration=5.0)
        self.assertEqual([(1.0, 2.0), (2.3, 3.3)], spans)
        self.assertEqual([], changes)

    def test_waveform_word_guard_never_creates_negative_start(self):
        spans, _changes = br.snap_spans_to_activity_edges(
            [(0.0, 0.5)],
            [{"start": 0.02, "end": 0.45, "confidence": 0.95}],
            words=[{"start": 0.0, "end": 0.4, "text": "开头"}],
            duration=1.0)
        self.assertEqual(0.0, spans[0][0])


if __name__ == "__main__":
    unittest.main()
