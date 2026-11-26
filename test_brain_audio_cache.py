# -*- coding: utf-8 -*-
"""Audio brains reuse the persistent PCM prepared by ingest/rendered QC."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

import audio_evidence_cache
import index as indexer
import rendered_output_qc
from brains.base import AudioSource


class BrainAudioCacheTests(unittest.TestCase):
    def cache(self, sample_count=4, sample_rate=4):
        cache = mock.Mock()
        cache.prepare_track.return_value = {
            "track": 3, "sample_count": sample_count,
            "sample_rate": sample_rate,
            "duration": sample_count / sample_rate,
            "key": "shared-pcm-key", "reused": True,
        }
        cache.review_samples.return_value = np.array(
            [-1.0, -0.5, 0.0, 32767.0 / 32768.0], dtype=np.float32)
        return cache

    def test_duration_uses_metadata_and_audio_reads_only_selected_track_once(self):
        cache = self.cache()
        source = AudioSource("source.mp4", track=3, sr=4, ffmpeg="ffmpeg")
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache) as factory:
            self.assertEqual(1.0, source.duration())
            cache.review_samples.assert_not_called()
            first = source.audio
            self.assertIs(first, source.audio)
            self.assertEqual(np.float32, first.dtype)
            np.testing.assert_array_equal(first, cache.review_samples.return_value)
            self.assertEqual("shared-pcm-key", source.last_cache_audit["cache_key"])
            self.assertTrue(source.last_cache_audit["reused"])
            source.close()
            source.close()
        factory.assert_called_once_with(
            "source.mp4", [3], sample_rate=4, ffmpeg="ffmpeg", should_cancel=None)
        cache.prepare_track.assert_called_once_with(3)
        cache.review_samples.assert_called_once_with(3, 0.0, 1.0)
        cache.close.assert_called_once_with()
        self.assertIsNone(source._audio)

    def test_close_allows_new_cache_instance_on_later_read(self):
        first, second = self.cache(), self.cache()
        source = AudioSource("source.mp4", track=3, sr=4, ffmpeg="ffmpeg")
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               side_effect=[first, second]) as factory:
            expected = source.audio.copy()
            source.close()
            np.testing.assert_array_equal(expected, source.audio)
            source.close()
        self.assertEqual(2, factory.call_count)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_index_failure_is_propagated_and_closed_without_empty_audio_fallback(self):
        cache = self.cache()
        cache.prepare_track.side_effect = audio_evidence_cache.AudioEvidenceError(
            "decoder failed")
        source = AudioSource("source.mp4", track=3, sr=4, ffmpeg="ffmpeg")
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache), \
                mock.patch("brains.base.subprocess.run") as legacy_decode:
            with self.assertRaisesRegex(audio_evidence_cache.AudioEvidenceError,
                                        "decoder failed"):
                _ = source.audio
        self.assertIsNone(source._audio)
        self.assertIn("decoder failed", source.last_cache_audit["error"])
        cache.close.assert_called_once_with()
        legacy_decode.assert_not_called()

    def test_window_failure_or_incomplete_pcm_is_propagated_and_closed(self):
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                cache = self.cache()
                if incomplete:
                    cache.review_samples.return_value = np.zeros(0, dtype=np.float32)
                else:
                    cache.review_samples.side_effect = OSError("PCM read failed")
                source = AudioSource("source.mp4", track=3, sr=4, ffmpeg="ffmpeg")
                with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                                       return_value=cache):
                    with self.assertRaises((RuntimeError, OSError)):
                        _ = source.audio
                self.assertIsNone(source._audio)
                self.assertIn("error", source.last_cache_audit)
                cache.close.assert_called_once_with()

    def test_cancellation_before_prepare_does_not_create_cache(self):
        source = AudioSource("source.mp4", ffmpeg="ffmpeg",
                             should_cancel=lambda: True)
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache") as factory:
            with self.assertRaises(audio_evidence_cache.AudioEvidenceCancelled):
                source.duration()
        factory.assert_not_called()

    def test_cancellation_during_prepare_or_read_closes_cache(self):
        for phase in ("prepare", "read"):
            with self.subTest(phase=phase):
                cache = self.cache()
                cancelled = False

                def cancel_after_prepare(_track):
                    nonlocal cancelled
                    cancelled = phase == "prepare"
                    return {"duration": 1.0, "sample_count": 4,
                            "key": "key", "reused": True}

                def cancel_after_read(*_args):
                    nonlocal cancelled
                    cancelled = True
                    return np.zeros(4, dtype=np.float32)

                cache.prepare_track.side_effect = cancel_after_prepare
                cache.review_samples.side_effect = cancel_after_read
                source = AudioSource("source.mp4", track=3, sr=4, ffmpeg="ffmpeg",
                                     should_cancel=lambda: cancelled)
                with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                                       return_value=cache):
                    with self.assertRaises(audio_evidence_cache.AudioEvidenceCancelled):
                        _ = source.audio
                cache.close.assert_called_once_with()
                self.assertIsNone(source._audio)

    def test_default_ffmpeg_uses_packaged_application_resolver(self):
        with mock.patch.object(indexer, "find_ffmpeg",
                               return_value="packaged/bin/ffmpeg.exe") as resolver:
            source = AudioSource("source.mp4")
        self.assertEqual("packaged/bin/ffmpeg.exe", source.ffmpeg)
        resolver.assert_called_once_with()

    def test_preloaded_audio_keeps_existing_synthetic_source_contract(self):
        source = AudioSource("synthetic", ffmpeg="ffmpeg", sr=4)
        source._audio = np.zeros(8, dtype=np.float32)
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache") as factory:
            self.assertEqual(2.0, source.duration())
            self.assertEqual(8, source.audio.size)
        factory.assert_not_called()


class BrainAudioRealCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = indexer.find_ffmpeg()
        except FileNotFoundError:
            raise unittest.SkipTest("bundled FFmpeg is unavailable")
        if not os.path.isfile(cls.ffmpeg):
            raise unittest.SkipTest("bundled FFmpeg is unavailable")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="brain-pcm-")
        self.source = os.path.join(self.folder.name, "two_tracks.mkv")
        self.cache_dir = os.path.join(self.folder.name, "audio_evidence")
        done = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.35",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=0.35",
            "-map", "0:a", "-map", "1:a", "-c:a", "pcm_s16le", self.source,
        ], capture_output=True, creationflags=audio_evidence_cache.CREATE_NO_WINDOW)
        self.assertEqual(0, done.returncode, done.stderr.decode("utf-8", "replace"))

    def tearDown(self):
        self.folder.cleanup()

    def legacy_samples(self, track):
        done = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", self.source, "-map", "0:a:%d" % track,
            "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ], capture_output=True, creationflags=audio_evidence_cache.CREATE_NO_WINDOW)
        self.assertEqual(0, done.returncode, done.stderr.decode("utf-8", "replace"))
        return np.frombuffer(done.stdout, dtype="<i2").astype(np.float32) / 32768.0

    def test_real_cold_and_warm_sources_decode_once_with_identical_waveform(self):
        expected = self.legacy_samples(1)
        with mock.patch.object(audio_evidence_cache, "_default_cache_dir",
                               return_value=self.cache_dir):
            original_popen = audio_evidence_cache.subprocess.Popen
            with mock.patch.object(audio_evidence_cache.subprocess, "Popen",
                                   wraps=original_popen) as decode:
                cold = AudioSource(self.source, track=1, ffmpeg=self.ffmpeg)
                try:
                    self.assertEqual(expected.size / 16000.0, cold.duration())
                    self.assertIsNone(cold._audio)
                    np.testing.assert_array_equal(expected, cold.audio)
                    cold_key = cold.last_cache_audit["cache_key"]
                    self.assertFalse(cold.last_cache_audit["reused"])
                finally:
                    cold.close()
                self.assertEqual(1, decode.call_count)
                command = decode.call_args.args[0]
                self.assertEqual("0:a:1", command[command.index("-map") + 1])
                warm = AudioSource(self.source, track=1, ffmpeg=self.ffmpeg)
                try:
                    np.testing.assert_array_equal(expected, warm.audio)
                    self.assertTrue(warm.last_cache_audit["reused"])
                    self.assertEqual(cold_key, warm.last_cache_audit["cache_key"])
                finally:
                    warm.close()
                self.assertEqual(1, decode.call_count)

    def test_real_rendered_qc_cache_is_reused_by_brain_without_decoder(self):
        expected = self.legacy_samples(1)
        with mock.patch.object(audio_evidence_cache, "_default_cache_dir",
                               return_value=self.cache_dir):
            cache = rendered_output_qc._open_shared_audio_cache(
                self.source, 2, ffmpeg=self.ffmpeg, sample_rate=16000)
            try:
                entry = cache.prepare_track(1)
            finally:
                cache.close()
            with mock.patch.object(audio_evidence_cache.subprocess, "Popen",
                                   side_effect=AssertionError(
                                       "QC PCM must prevent another decoder")):
                source = AudioSource(self.source, track=1, ffmpeg=self.ffmpeg)
                try:
                    self.assertEqual(expected.size / 16000.0, source.duration())
                    np.testing.assert_array_equal(expected, source.audio)
                    self.assertEqual(entry["key"], source.last_cache_audit["cache_key"])
                    self.assertTrue(source.last_cache_audit["reused"])
                finally:
                    source.close()


if __name__ == "__main__":
    unittest.main()
