# -*- coding: utf-8 -*-
"""Regression tests for the Premiere-style shared PCM consumer path."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import autopilot
import silence_trim
import track_audit


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


def _frames(levels, sr=16000):
    step = int(sr * 0.02)
    return np.concatenate([
        np.full(step, float(level), dtype=np.float32) for level in levels
    ])


class SharedPcmConsumerTests(unittest.TestCase):
    def test_silence_trim_uses_supplied_single_track_pcm(self):
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

    def test_track_audit_reads_each_cached_track_once_without_mixing(self):
        speech = _frames([0.8] * 10 + [0.0] + [0.8] * 12)
        other = _frames([0.0] * 10 + [0.8] * 3 + [0.0] * 10)
        cache = _FakeCache({0: speech, 1: other})

        expected = []
        expected.extend(track_audit.audit_speech_track(
            "unused", "unused", 0, audio=speech, keep=[(0.0, 1.0)]))
        expected.extend(track_audit.audit_other_track(
            "unused", "unused", 1, audio=other))

        with mock.patch.object(
                track_audit, "_grab_track",
                side_effect=AssertionError("must not launch ffmpeg")):
            actual = track_audit.audit(
                "unused.mp4", "unused-ffmpeg",
                {"0": {"kind": "speech"}, "1": {"kind": "other"}},
                spans=[(0.0, 1.0)], audio_cache=cache)

        self.assertEqual(expected, actual)
        self.assertEqual([0, 1], cache.entry_calls)
        self.assertEqual([0, 1], [row[0] for row in cache.review_calls])
        for finding in actual:
            self.assertTrue({"track", "track_name", "kind", "problem",
                             "at", "duration", "severity", "suggest",
                             "confidence"}.issubset(finding))

    def test_cut_finalization_reuses_one_runtime_pcm_read(self):
        class _Server:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        samples = np.full(3 * 16000, 0.2, dtype=np.float32)
        cache = _FakeCache({0: samples})
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.mp4")
            open(source, "wb").close()
            run = autopilot._WorkflowRun(
                source, "medium", False, os.path.join(tmp, "index"),
                lambda _message: None, False, [0], tmp, False, None,
                lambda: False, "game", "shared-pcm-test")
            run.director_conf.update({
                "seam_pick_enabled": True,
                "silence_trim_enabled": True,
            })
            run.ensure_audio_cache = lambda _data: cache
            data = {
                "source": source, "duration": 3.0, "duration_exact": 3.0,
                "audio_streams": [{"ordinal": 0}], "speech_tracks": [0],
                "segments": [], "segment_evidence": [],
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
        self.assertEqual([0], cache.entry_calls)
        self.assertEqual([0], [row[0] for row in cache.review_calls])


if __name__ == "__main__":
    unittest.main()
