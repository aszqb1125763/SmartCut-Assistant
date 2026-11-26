# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import rendered_output_qc as qc


class _FakeAudioCache:
    def __init__(self, stream_count, samples=None, prepare_error=None):
        self.sample_rate = 16000
        self.stream_count = int(stream_count)
        self.samples = (np.asarray(samples, dtype=np.float32)
                        if samples is not None
                        else np.full(16000, 0.02, dtype=np.float32))
        self.prepare_error = prepare_error
        self.prepare_calls = 0
        self.review_calls = []
        self.closed = False

    def prepare(self):
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return {
            track: {"key": "fake-%d" % track, "duration": 4.0,
                    "reused": track % 2 == 0}
            for track in range(self.stream_count)
        }

    def review_samples(self, track, start, end):
        self.review_calls.append((int(track), float(start), float(end)))
        return self.samples.copy()

    def close(self):
        self.closed = True


class RenderedOutputQCTests(unittest.TestCase):
    def test_nodes_cover_start_end_seams_and_late_timeline(self):
        timeline = [(float(i * 10), float(i * 20), 8.0) for i in range(10)]
        rows = qc.select_review_nodes(100.0, timeline=timeline, max_nodes=8)
        self.assertEqual("output_start", rows[0]["kind"])
        self.assertEqual("output_end", rows[-1]["kind"])
        self.assertLess(rows[-1]["sample_times"][-1], 100.0)
        seams = [row for row in rows if row["kind"] == "edit_boundary"]
        self.assertGreaterEqual(len(seams), 2)
        self.assertGreater(max(row["time"] for row in seams), 70.0)

    def test_word_partial_coverage_is_a_render_veto(self):
        timeline = [(0.0, 1.10, 0.90)]
        words = [{"id": "w1", "word": "我", "start": 1.0, "end": 1.25,
                  "probability": 0.96, "required": True}]
        rows = qc.find_word_boundary_cuts(timeline, words)
        self.assertEqual(1, len(rows))
        self.assertEqual("render_word_boundary_cut", rows[0]["type"])
        self.assertTrue(rows[0]["veto"])

    def test_finish_gate_does_not_relax_real_word_cut(self):
        finding = qc.find_word_boundary_cuts(
            [(0.0, 1.10, 0.90)],
            [{"id": "w1", "word": "我", "start": 1.0, "end": 1.25,
              "probability": 0.96, "required": True}])[0]
        report = qc._finish_report(
            "out.mp4", {"duration": 1.0},
            [{"actual_audio_inspected": True}],
            [{"id": "visual_001", "issues": []}], [finding],
            1.0, [0], nodes=[])
        self.assertFalse(report["passed"])
        self.assertTrue(report["vetoed"])
        self.assertEqual(1, report["veto_count"])

    def test_complete_word_coverage_has_no_veto(self):
        timeline = [(0.0, 0.80, 0.80)]
        words = [{"id": "w1", "word": "完成", "start": 1.0, "end": 1.25,
                  "probability": 0.96, "required": True}]
        self.assertEqual([], qc.find_word_boundary_cuts(timeline, words))

    def test_float_edge_that_touches_word_boundary_is_not_a_cut(self):
        # 刀口收到字边界上时，浮点误差会留下 1e-14 秒的"重叠"。
        # 字其实是完整的，不能判成切字（2026-11-22 实测误报过一次）。
        edge = 152.317 + 22.573
        timeline = [(0.0, 152.317, 22.573)]
        words = [{"id": "w1", "word": "完", "start": edge, "end": edge + 0.5,
                  "probability": 0.88, "required": True}]
        self.assertEqual([], qc.find_word_boundary_cuts(timeline, words))

    def test_tiny_press_below_five_milliseconds_is_ignored(self):
        timeline = [(0.0, 1.0, 1.001)]
        words = [{"id": "w1", "word": "我", "start": 1.001, "end": 1.30,
                  "probability": 0.9, "required": True}]
        self.assertEqual([], qc.find_word_boundary_cuts(timeline, words))

    def test_half_sentence_is_reported_for_joint_director_review(self):
        # 句子 10~12 秒，成片只保留了 10~10.5（25%）→ 话说一半。
        timeline = [(0.0, 10.0, 0.5)]
        segments = [[10.0, 12.0, "它是有两层吧"]]
        rows = qc.find_half_sentences(timeline, segments)
        self.assertEqual(1, len(rows))
        self.assertEqual("major", rows[0]["severity"])
        self.assertFalse(rows[0]["veto"])

    def test_whole_sentence_kept_is_not_half(self):
        timeline = [(0.0, 10.0, 5.0)]
        segments = [[10.0, 15.0, "完整的一句话"]]
        self.assertEqual([], qc.find_half_sentences(timeline, segments))

    def test_sentence_removed_entirely_is_not_half(self):
        # 整句都没留，那是删除，不是"话说一半"。
        timeline = [(0.0, 10.0, 5.0)]
        segments = [[20.0, 22.0, "被删掉的一句"]]
        self.assertEqual([], qc.find_half_sentences(timeline, segments))

    def test_sentence_split_across_two_spans_is_not_half(self):
        # 两段各取一半、合起来是整句 → 内容完整，不算半句。
        timeline = [(0.0, 10.0, 1.0), (1.0, 11.0, 1.0)]
        segments = [[10.0, 12.0, "跨两段的一句"]]
        self.assertEqual([], qc.find_half_sentences(timeline, segments))

    def test_half_sentence_reports_render_time(self):
        timeline = [(0.0, 10.0, 0.5)]
        segments = [[10.0, 12.0, "它是有两层吧"]]
        rows = qc.find_half_sentences(timeline, segments)
        self.assertEqual(1, len(rows))
        self.assertEqual([0.0, 0.4], rows[0]["suggested_interval"])

    def test_actual_audio_window_detects_level_jump_and_clipping(self):
        sr = 16000
        left = np.full(sr // 2, 0.01, dtype=np.float32)
        right = np.ones(sr // 2, dtype=np.float32)
        samples = np.concatenate([left, right])
        node = {"id": "render_node_003", "kind": "edit_boundary", "time": 0.5}
        report = qc.analyze_rendered_audio_window(samples, sr, node, 0.0)
        kinds = {row["type"] for row in report["findings"]}
        self.assertIn("render_audio_level_jump", kinds)
        self.assertIn("digital_clipping", kinds)

    def test_review_passes_clean_actual_output_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            output = os.path.join(temp, "out.mp4")
            with open(output, "wb") as handle:
                handle.write(b"video")
            probe = {
                "duration": 4.0, "size": 5,
                "video_streams": [{"type": "video", "duration": 4.0}],
                "audio_streams": [{"type": "audio", "duration": 4.0}],
                "streams": [],
            }
            audio = np.full(16000, 0.02, dtype=np.float32)
            cache = _FakeAudioCache(1, audio)
            visual = [{"id": "render_node_001", "kind": "timeline_probe",
                       "sample_times": [0.0, 0.2, 0.4], "issues": []}]
            with mock.patch.object(qc, "probe_media", return_value=probe), \
                    mock.patch.object(qc, "_open_shared_audio_cache",
                                      return_value=cache), \
                    mock.patch.object(qc, "_extract_audio_window_samples",
                                      side_effect=AssertionError(
                                          "node loop reopened the media")), \
                    mock.patch.object(qc, "_review_visual_nodes", return_value=visual):
                report = qc.review_rendered_output(
                    output, timeline=[(0.0, 0.0, 4.0)], expected_duration=4.0,
                    words=[], output_dir=os.path.join(temp, "qc"), max_nodes=4)
            self.assertTrue(report["passed"])
            self.assertEqual(1, cache.prepare_calls)
            self.assertTrue(cache.closed)
            self.assertEqual("ready", report["audio_cache"]["status"])
            self.assertEqual("available", report["evidence_status"]["audio"])
            self.assertEqual("available", report["evidence_status"]["visual"])

    def test_every_rendered_audio_stream_is_inspected(self):
        with tempfile.TemporaryDirectory() as temp:
            output = os.path.join(temp, "out.mp4")
            with open(output, "wb") as handle:
                handle.write(b"video")
            probe = {
                "duration": 4.0, "size": 5,
                "video_streams": [{"type": "video", "duration": 4.0}],
                "audio_streams": [
                    {"index": 1, "type": "audio", "duration": 4.0},
                    {"index": 2, "type": "audio", "duration": 4.0},
                ],
                "streams": [],
            }
            audio = np.full(16000, 0.02, dtype=np.float32)
            cache = _FakeAudioCache(2, audio)
            visual = [{"id": "render_node_001", "kind": "timeline_probe",
                       "sample_times": [0.0, 0.2, 0.4], "issues": []}]
            with mock.patch.object(qc, "probe_media", return_value=probe), \
                    mock.patch.object(qc, "_open_shared_audio_cache",
                                      return_value=cache), \
                    mock.patch.object(qc, "_extract_audio_window_samples",
                                      side_effect=AssertionError(
                                          "node loop reopened the media")), \
                    mock.patch.object(qc, "_review_visual_nodes", return_value=visual):
                report = qc.review_rendered_output(
                    output, timeline=[(0.0, 0.0, 4.0)], expected_duration=4.0,
                    words=[], output_dir=os.path.join(temp, "qc"), max_nodes=2)
            self.assertTrue(report["passed"])
            self.assertEqual(1, cache.prepare_calls)
            self.assertTrue(cache.closed)
            self.assertEqual({0, 1}, {
                row["audio_stream_ordinal"] for row in report["audio_reports"]})
            self.assertEqual({0, 1}, {
                track for track, _start, _end in cache.review_calls})

    def test_missing_required_render_evidence_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            output = os.path.join(temp, "out.mp4")
            with open(output, "wb") as handle:
                handle.write(b"video")
            probe = {
                "duration": 4.0, "size": 5,
                "video_streams": [{"type": "video", "duration": 4.0}],
                "audio_streams": [{"index": 1, "type": "audio", "duration": 4.0}],
                "streams": [],
            }
            cache = _FakeAudioCache(
                1, prepare_error=RuntimeError("decode failed"))
            with mock.patch.object(qc, "probe_media", return_value=probe), \
                    mock.patch.object(qc, "_open_shared_audio_cache",
                                      return_value=cache), \
                    mock.patch.object(qc, "_review_visual_nodes", return_value=[]):
                report = qc.review_rendered_output(
                    output, timeline=[(0.0, 0.0, 4.0)], expected_duration=4.0,
                    output_dir=os.path.join(temp, "qc"), max_nodes=2)
            self.assertFalse(report["passed"])
            self.assertFalse(report["required_evidence_available"])
            self.assertEqual("failed", report["audio_cache"]["status"])

    def test_duration_mismatch_and_missing_audio_block_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = os.path.join(temp, "out.mp4")
            with open(output, "wb") as handle:
                handle.write(b"video")
            probe = {
                "duration": 7.0, "size": 5,
                "video_streams": [{"type": "video", "duration": 7.0}],
                "audio_streams": [], "streams": [],
            }
            with mock.patch.object(qc, "probe_media", return_value=probe), \
                    mock.patch.object(qc, "_review_visual_nodes", return_value=[]):
                report = qc.review_rendered_output(
                    output, timeline=[(0.0, 0.0, 4.0)], expected_duration=4.0,
                    output_dir=os.path.join(temp, "qc"), max_nodes=4)
            kinds = {row["type"] for row in report["findings"]}
            self.assertIn("render_audio_missing", kinds)
            self.assertIn("render_duration_mismatch", kinds)
            self.assertFalse(report["passed"])

    def test_cancel_is_not_converted_to_a_review_finding(self):
        with tempfile.TemporaryDirectory() as temp:
            output = os.path.join(temp, "out.mp4")
            with open(output, "wb") as handle:
                handle.write(b"video")
            with self.assertRaises(qc.RenderedOutputCancelled):
                qc.review_rendered_output(output, should_cancel=lambda: True)


if __name__ == "__main__":
    unittest.main()
