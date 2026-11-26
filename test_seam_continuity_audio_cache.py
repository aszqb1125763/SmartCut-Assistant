# -*- coding: utf-8 -*-
"""Shared output PCM reuse without changing seam decisions or cancellation."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import audio_evidence_cache as evidence
import rendered_output_qc
import seam_continuity as seam


class SeamAudioCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = os.path.join(self.tmp.name, "rendered.mp4")
        with open(self.source, "wb") as handle:
            handle.write(b"rendered output identity")
        self.cache_dir = os.path.join(self.tmp.name, "pcm")
        count = 8 * seam.SR
        times = np.arange(count) / float(seam.SR)
        rng = np.random.default_rng(410)
        signal = (0.22 * np.sin(2 * np.pi * 230 * times)
                  + 0.04 * rng.standard_normal(count))
        signal[3 * seam.SR:4 * seam.SR] *= 0.003
        self.raw = {
            0: np.asarray(signal * 32767, dtype="<i2"),
            1: np.zeros(count, dtype="<i2"),
        }
        self.decode_calls = []

        def decode(cache, track, output_path):
            self.decode_calls.append((track, cache.sample_rate))
            self.raw[track].tofile(output_path)

        self.addCleanup(mock.patch.stopall)
        mock.patch.object(evidence, "_default_cache_dir",
                          return_value=self.cache_dir).start()
        mock.patch.object(evidence.AudioEvidenceCache, "_decode_ffmpeg",
                          autospec=True, side_effect=decode).start()
        self.cap = mock.Mock()
        mock.patch("cv2.VideoCapture", return_value=self.cap).start()
        mock.patch.object(seam, "_frame_at", return_value=None).start()
        mock.patch.object(seam, "visual_seam",
                          return_value=(0.83, {"test_picture": True})).start()
        self.created = []
        original_open = seam._open_audio_cache

        def open_cache(*args, **kwargs):
            cache = original_open(*args, **kwargs)
            self.created.append(cache)
            return cache

        mock.patch.object(seam, "_open_audio_cache",
                          side_effect=open_cache).start()
        self.points = [1.237, 2.551, 4.092, 5.833]
        starts = [0.0] + self.points
        ends = self.points + [8.0]
        # Disconnected source intervals ensure these are genuine jump cuts.
        self.timeline = [(start, index * 30.0, end - start)
                         for index, (start, end) in enumerate(zip(starts, ends))]

    def test_cold_then_warm_reviews_decode_first_track_once(self):
        first = seam.score_seams(self.source, self.timeline)
        second = seam.score_seams(self.source, self.timeline)
        self.assertEqual(first, second)
        self.assertEqual([(0, seam.SR)], self.decode_calls)
        self.assertEqual([False, True],
                         [cache._entries[0]["reused"] for cache in self.created])
        self.assertTrue(all(cache._closed for cache in self.created))

    def test_reuses_rendered_qc_track_zero_key_without_decoding_again(self):
        with rendered_output_qc._open_shared_audio_cache(
                self.source, 2, ffmpeg="ffmpeg") as cache:
            entries = cache.prepare()
        self.assertNotEqual(entries[0]["key"], entries[1]["key"])
        rows = seam.score_seams(self.source, self.timeline)
        self.assertTrue(rows)
        self.assertEqual([(0, seam.SR), (1, seam.SR)], self.decode_calls)
        used = self.created[-1]
        self.assertEqual((0,), used.tracks)
        self.assertEqual(entries[0]["key"], used._entries[0]["key"])
        self.assertTrue(used._entries[0]["reused"])

    def test_local_windows_match_full_pcm_decisions_with_all_context(self):
        full = self.raw[0].astype(np.float32) / 32768.0
        real_read = evidence.AudioEvidenceCache.review_samples
        reads = []

        def read(cache, track, start, end):
            reads.append((start, end))
            return real_read(cache, track, start, end)

        with mock.patch.object(evidence.AudioEvidenceCache, "review_samples",
                               autospec=True, side_effect=read):
            for window in (0.04, seam.WINDOW, 0.79):
                with self.subTest(window=window):
                    reads.clear()
                    rows = seam.score_seams(self.source, self.timeline,
                                            window=window)
                    self.assertEqual(len(self.points), len(rows))
                    cursor = 0.0
                    for row, stamp in zip(rows, self.timeline):
                        cursor += stamp[2]
                        score, detail = seam.audio_seam(
                            full, int(cursor * seam.SR), window=window)
                        self.assertEqual(score, row["audio"])
                        self.assertEqual(detail, row["audio_detail"])
                        self.assertEqual(seam.combine(score, 0.83), row["total"])
                    self.assertEqual(len(rows), len(reads))
                    self.assertLessEqual(max(end - start for start, end in reads),
                                         2 * max(window, 0.20) + 1 / seam.SR)

    def test_load_audio_preserves_array_interface_and_reuses_pcm(self):
        first = seam.load_audio(self.source)
        second = seam.load_audio(self.source, ffmpeg="another_ffmpeg")
        np.testing.assert_array_equal(
            first, self.raw[0].astype(np.float32) / 32768.0)
        np.testing.assert_array_equal(first, second)
        self.assertEqual([(0, seam.SR)], self.decode_calls)
        self.assertTrue(all(cache._closed for cache in self.created))

    def test_smooth_seams_and_source_bridge_rules_are_unchanged(self):
        rows = seam.score_seams(self.source, self.timeline,
                                smooth_at=[self.points[0]])
        self.assertEqual([round(point, 2) for point in self.points[1:]],
                         [row["at"] for row in rows])
        bridge = [(0.0, 10.0, 2.0), (2.0, 12.0, 2.0)]
        self.assertEqual([], seam.score_seams(self.source, bridge))

    def test_cancel_before_work_propagates_without_decode(self):
        with self.assertRaises(evidence.AudioEvidenceCancelled):
            seam.score_seams(self.source, self.timeline,
                             should_cancel=lambda: True)
        with self.assertRaises(evidence.AudioEvidenceCancelled):
            seam.load_audio(self.source, should_cancel=lambda: True)
        self.assertEqual([], self.decode_calls)

    def test_cancel_during_review_does_not_return_partial_pass(self):
        state = {"cancel": False}

        def read_frame(*args):
            state["cancel"] = True
            return None

        with mock.patch.object(seam, "_frame_at", side_effect=read_frame):
            with self.assertRaises(evidence.AudioEvidenceCancelled):
                seam.score_seams(self.source, self.timeline,
                                 should_cancel=lambda: state["cancel"])
        self.assertTrue(self.created[-1]._closed)
        self.cap.release.assert_called_once()

    def test_decode_failure_propagates_and_closes_cache(self):
        failure = evidence.AudioEvidenceError("decode failed")
        with mock.patch.object(evidence.AudioEvidenceCache, "_decode_ffmpeg",
                               side_effect=failure):
            with self.assertRaisesRegex(evidence.AudioEvidenceError,
                                        "decode failed"):
                seam.score_seams(self.source, self.timeline)
        self.assertTrue(self.created[-1]._closed)
        self.cap.release.assert_not_called()

    def test_cancel_while_building_pcm_propagates_and_closes_cache(self):
        with mock.patch.object(evidence.AudioEvidenceCache, "_decode_ffmpeg",
                               side_effect=evidence.AudioEvidenceCancelled(
                                   "decode cancelled")):
            with self.assertRaisesRegex(evidence.AudioEvidenceCancelled,
                                        "decode cancelled"):
                seam.score_seams(self.source, self.timeline)
        self.assertTrue(self.created[-1]._closed)
        self.cap.release.assert_not_called()

    def test_empty_decode_is_an_error_instead_of_empty_pass(self):
        with mock.patch.object(evidence.AudioEvidenceCache, "_decode_ffmpeg",
                               return_value=None):
            with self.assertRaisesRegex(evidence.AudioEvidenceError, "empty"):
                seam.load_audio(self.source)
        self.assertTrue(self.created[-1]._closed)


if __name__ == "__main__":
    unittest.main()
