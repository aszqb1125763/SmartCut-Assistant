# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import numpy as np

import audio_evidence_cache
import index


class FakeAudioCache:
    def __init__(self, values, sample_rate=16000):
        self.values = values
        self.sample_rate = sample_rate
        self.calls = []

    def entry(self, track):
        return {"track": track,
                "duration": len(self.values[track]) / float(self.sample_rate)}

    def review_samples(self, track, start, end):
        self.calls.append((int(track), float(start), float(end)))
        first = max(0, int(float(start) * self.sample_rate))
        last = min(len(self.values[track]), int(np.ceil(
            float(end) * self.sample_rate)))
        return np.asarray(self.values[track][first:last], dtype=np.float32)


class IndexAudioCacheTests(unittest.TestCase):
    def _tracks(self):
        return [{"ordinal": 0, "label": "mic"},
                {"ordinal": 1, "label": "game"}]

    def _audio(self):
        sr = 16000
        speech = np.concatenate([
            np.zeros(sr // 2, dtype=np.float32),
            np.full(sr // 2, 0.25, dtype=np.float32),
            np.zeros(sr // 2, dtype=np.float32),
            np.full(sr // 2, 0.25, dtype=np.float32),
        ])
        dense = np.full(sr * 2, 0.2, dtype=np.float32)
        return {0: speech, 1: dense}

    def test_probe_tracks_reads_shared_pcm_without_wav_decode(self):
        cache = FakeAudioCache(self._audio())
        with mock.patch.object(index, "extract_audio_wav",
                               side_effect=AssertionError("must not decode")):
            rows = index.probe_tracks(
                "unused.mp4", [0, 1], max_seconds=2,
                audio_cache=cache)
        self.assertEqual("speech", rows[0]["kind"])
        self.assertEqual("dense", rows[1]["kind"])
        self.assertEqual([0, 1], [row[0] for row in cache.calls])

    def test_stream_picker_reads_shared_pcm_without_temp_files(self):
        cache = FakeAudioCache(self._audio())
        with mock.patch.object(index, "extract_audio_wav",
                               side_effect=AssertionError("must not decode")), \
                mock.patch.object(index.tempfile, "mkstemp",
                                  side_effect=AssertionError("must not write wav")):
            chosen = index.pick_audio_stream(
                "unused.mp4", self._tracks(), sample_seconds=2,
                audio_cache=cache)
        self.assertEqual(0, chosen)
        self.assertEqual([0, 1], [row[0] for row in cache.calls])

    def test_probe_cancellation_is_not_swallowed_as_unknown_track(self):
        class CancelledCache(FakeAudioCache):
            def entry(self, track):
                raise audio_evidence_cache.AudioEvidenceCancelled("cancel")

        cache = CancelledCache(self._audio())
        with self.assertRaises(audio_evidence_cache.AudioEvidenceCancelled):
            index.probe_tracks("unused.mp4", [0], audio_cache=cache)


if __name__ == "__main__":
    unittest.main()
