# -*- coding: utf-8 -*-
"""Integration guards for the final cut-boundary pipeline."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import autopilot
import seam_picker
import silence_trim


class _FakeCache:
    sample_rate = 16000

    def __init__(self, tracks):
        self.tracks = {int(key): np.asarray(value, dtype=np.float32)
                       for key, value in tracks.items()}
        self.entry_calls = []
        self.review_calls = []

    def entry(self, track):
        track = int(track)
        self.entry_calls.append(track)
        return {"track": track,
                "duration": self.tracks[track].size / float(self.sample_rate)}

    def review_samples(self, track, start, end):
        track = int(track)
        self.review_calls.append((track, float(start), float(end)))
        first = max(0, int(float(start) * self.sample_rate))
        last = min(self.tracks[track].size,
                   int(np.ceil(float(end) * self.sample_rate)))
        return self.tracks[track][first:last].copy()


class CutPipelineIntegrationTests(unittest.TestCase):
    def test_seam_decode_maps_requested_audio_track(self):
        completed = mock.Mock(stdout=b"\x00\x00" * 128)
        with mock.patch.object(seam_picker.subprocess, "run",
                               return_value=completed) as run:
            seam_picker.load_audio("source.mp4", "ffmpeg", track=3)
        command = run.call_args.args[0]
        self.assertIn("0:a:3", command)
        self.assertNotIn("0:a:0", command)

    def test_silence_trim_reuses_supplied_pcm(self):
        audio = np.zeros(3 * silence_trim.SR, dtype=np.float32)
        with mock.patch.object(
                silence_trim, "_track_audio",
                side_effect=AssertionError("must not launch ffmpeg")):
            spans, silences = silence_trim.trim(
                "unused.mp4", "unused-ffmpeg", [(0.0, 3.0)], 2,
                audio=audio, sample_rate=silence_trim.SR,
                min_silence=0.5, pad=0.1)
        self.assertTrue(silences)
        self.assertEqual([], spans)

    def test_finalizer_uses_selected_speech_track_and_one_pcm_read(self):
        class _Server:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        samples = np.full(3 * 16000, 0.2, dtype=np.float32)
        cache = _FakeCache({1: samples})
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.mp4")
            open(source, "wb").close()
            run = autopilot._WorkflowRun(
                source, "medium", False, os.path.join(tmp, "index"),
                lambda _message: None, False, [1], tmp, False, None,
                lambda: False, "game", "selected-track-test")
            run.director_conf.update({
                "seam_pick_enabled": True,
                "silence_trim_enabled": True,
            })
            run.ensure_audio_cache = lambda _data: cache
            data = {
                "source": source, "duration": 3.0, "duration_exact": 3.0,
                "fps": 30.0,
                "audio_streams": [{"ordinal": 0}, {"ordinal": 1}],
                "speech_tracks": [1],
                "segments": [],
                "segment_evidence": [
                    {"source_audio_tracks": [1], "start": 0.0, "end": 1.0},
                ],
            }
            with mock.patch("proofread.Server", _Server), \
                    mock.patch("seam_picker.pick_cuts", return_value=[]), \
                    mock.patch("seam_picker.load_audio",
                               side_effect=AssertionError(
                                   "runtime cache must prevent fallback decode")):
                spans, info = run.finalize_cut_points(
                    data, [(0.0, 1.0), (2.0, 3.0)], [])

        self.assertEqual([(0.0, 1.0), (2.0, 3.0)], spans)
        self.assertTrue(info["finalized"])
        self.assertEqual(1, info["speech_track"])
        self.assertEqual([1], cache.entry_calls)
        self.assertEqual([1], [row[0] for row in cache.review_calls])

    def test_dsp_candidate_cannot_cross_another_spoken_word(self):
        class _Server:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        samples = np.zeros(6 * 16000, dtype=np.float32)
        cache = _FakeCache({0: samples})
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.mp4")
            open(source, "wb").close()
            run = autopilot._WorkflowRun(
                source, "medium", False, os.path.join(tmp, "index"),
                lambda _message: None, False, [0], tmp, False, None,
                lambda: False, "game", "semantic-guard-test")
            run.director_conf.update({
                "seam_pick_enabled": True,
                "silence_trim_enabled": False,
            })
            run.ensure_audio_cache = lambda _data: cache
            data = {
                "source": source, "duration": 6.0, "duration_exact": 6.0,
                "fps": 30.0, "audio_streams": [{"ordinal": 0}],
                "segments": [[0.0, 2.0, "左句"], [2.2, 3.2, "不应跨过"],
                             [4.0, 6.0, "右句"]],
                "segment_evidence": [
                    {"segment_id": "s1", "source_audio_tracks": [0],
                     "start": 0.0, "end": 2.0, "words": []},
                    {"segment_id": "s2", "source_audio_tracks": [0],
                     "start": 2.2, "end": 3.2, "words": [
                         {"start": 2.3, "end": 2.8, "text": "不应跨过"},
                     ]},
                ],
            }
            candidate = [{"best": {
                "left_t": 3.0, "right_t": 4.0,
                "energy_gap": 1.0, "tone_gap_hz": 10.0,
                "shape_rms": 1.0,
            }}]
            with mock.patch("proofread.Server", _Server), \
                    mock.patch("seam_picker.pick_cuts", return_value=candidate):
                spans, info = run.finalize_cut_points(
                    data, [(0.0, 2.0), (4.0, 6.0)], [])

        self.assertAlmostEqual(2.0, spans[0][1], places=3)
        self.assertGreaterEqual(info["semantic_rejected"], 1)


if __name__ == "__main__":
    unittest.main()
