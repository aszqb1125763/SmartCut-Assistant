# -*- coding: utf-8 -*-
"""Render seam checks share selected-track PCM and release it on cancellation."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

import numpy as np

import audio_evidence_cache
import autocut


class RenderSeamAudioCacheTests(unittest.TestCase):
    def setUp(self):
        self.conf = copy.deepcopy(autocut.DEFAULT_CONF)
        self.conf["transition"].update({
            "audio_glue": 0.15, "chapter_card": 0.3,
            "chapter_card_gap": 20.0,
        })
        self.conf["render"]["seam_tone_align"] = True
        self.work = [(0.0, 2.0), (3.0, 5.0), (6.0, 8.0)]
        self.seams = {0: {"kind": "cut"}, 1: {"kind": "cut"}}

    def cache(self):
        cache = mock.Mock()
        cache.prepare_track.return_value = {"reused": True}
        return cache

    def process(self):
        process = mock.Mock()
        process.returncode = 0
        process.poll.return_value = 0
        process.communicate.return_value = (
            np.zeros(4800, dtype="<i2").tobytes(), b"")
        return process

    def test_all_seams_use_one_selected_track_index_without_window_decodes(self):
        cache = self.cache()
        sample_rate = 16000
        times = np.arange(8 * sample_rate, dtype=np.float32) / sample_rate
        samples = 0.3 * np.sin(2 * np.pi * 400 * times)
        middle = (times >= 3.0) & (times < 5.0)
        samples[middle] = 0.05 * np.sin(2 * np.pi * 3000 * times[middle])

        def window(track, start, end):
            self.assertEqual(3, track)
            return samples[int(start * sample_rate):int(round(end * sample_rate))]

        cache.review_samples.side_effect = window
        logs = []
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache) as factory, \
                mock.patch.object(autocut.subprocess, "Popen",
                                  side_effect=AssertionError(
                                      "cached seam windows must not decode")):
            glue, cards, tone = autocut._render_seam_prechecks(
                "source.mp4", self.work, self.seams, self.conf, [3, 1],
                "ffmpeg", on_log=logs.append)

        factory.assert_called_once_with(
            "source.mp4", [3], sample_rate=16000,
            ffmpeg="ffmpeg", should_cancel=None)
        cache.prepare_track.assert_called_once_with(3)
        self.assertEqual(8, cache.review_samples.call_count)
        self.assertEqual({}, glue)
        self.assertEqual({0: 0.3, 1: 0.3}, cards)
        self.assertEqual({0, 1, 2}, set(tone))
        self.assertLess(tone[1][0], 0.0)
        self.assertLess(tone[1][1], 0.0)
        self.assertTrue(any("复用共享 PCM" in line for line in logs))
        cache.close.assert_called_once_with()

    def test_failed_index_is_attempted_once_then_all_windows_keep_selected_track(self):
        cache = self.cache()
        cache.prepare_track.side_effect = OSError("cache is unavailable")
        logs = []
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache) as factory, \
                mock.patch.object(autocut.subprocess, "Popen",
                                  side_effect=lambda *args, **kwargs: self.process()) as popen:
            result = autocut._render_seam_prechecks(
                "source.mp4", self.work, self.seams, self.conf, [3],
                "ffmpeg", on_log=logs.append)

        self.assertEqual(({}, {}, {}), result)
        factory.assert_called_once()
        cache.prepare_track.assert_called_once_with(3)
        cache.close.assert_called_once_with()
        self.assertEqual(8, popen.call_count)
        for call in popen.call_args_list:
            command = call.args[0]
            self.assertEqual("0:a:3", command[command.index("-map") + 1])
            self.assertEqual(autocut._NO_WIN, call.kwargs["creationflags"])
        self.assertEqual(1, sum("共享 PCM 不可用" in line for line in logs))

    def test_read_failure_closes_cache_and_does_not_rebuild_it_per_window(self):
        cache = self.cache()
        cache.review_samples.side_effect = OSError("index read failed")
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache) as factory, \
                mock.patch.object(autocut.subprocess, "Popen",
                                  side_effect=lambda *args, **kwargs: self.process()) as popen:
            with autocut._SeamAudioWindows(
                    "source.mp4", 2, "ffmpeg", on_log=lambda _line: None) as audio:
                audio.read(1.0, 0.3)
                audio.read(3.0, 0.3)

        factory.assert_called_once()
        cache.prepare_track.assert_called_once_with(2)
        cache.review_samples.assert_called_once_with(2, 1.0, 1.3)
        cache.close.assert_called_once_with()
        self.assertEqual(2, popen.call_count)

    def test_cancelled_cache_build_closes_and_does_not_fallback(self):
        cache = self.cache()
        cache.prepare_track.side_effect = audio_evidence_cache.AudioEvidenceCancelled()
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache), \
                mock.patch.object(autocut.subprocess, "Popen") as popen:
            with self.assertRaises(autocut.RenderCancelled):
                autocut._render_seam_prechecks(
                    "source.mp4", self.work, self.seams, self.conf, [2],
                    "ffmpeg", on_log=lambda _line: None)
        cache.close.assert_called_once_with()
        popen.assert_not_called()

    def test_cancelled_window_read_propagates_and_closes(self):
        cache = self.cache()
        cancelled = False

        def read_window(*args):
            nonlocal cancelled
            cancelled = True
            return np.zeros(4800, dtype=np.float32)

        cache.review_samples.side_effect = read_window
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache), \
                mock.patch.object(autocut.subprocess, "Popen") as popen:
            with self.assertRaises(autocut.RenderCancelled):
                autocut._render_seam_prechecks(
                    "source.mp4", self.work, self.seams, self.conf, [2],
                    "ffmpeg", cancel=lambda: cancelled,
                    on_log=lambda _line: None)
        cache.close.assert_called_once_with()
        self.assertEqual(1, cache.review_samples.call_count)
        popen.assert_not_called()

    def test_external_failure_also_closes_index(self):
        cache = self.cache()
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache):
            with self.assertRaisesRegex(ValueError, "precheck failed"):
                with autocut._SeamAudioWindows(
                        "source.mp4", 2, "ffmpeg", on_log=lambda _line: None):
                    raise ValueError("precheck failed")
        cache.close.assert_called_once_with()

    def test_cancelled_fallback_stops_decoder_process(self):
        cache = self.cache()
        cache.prepare_track.side_effect = OSError("cache is unavailable")
        cancelled = False
        process = mock.Mock()
        process.poll.return_value = None

        def communicate(timeout=None):
            nonlocal cancelled
            if timeout == 0.1:
                cancelled = True
                raise autocut.subprocess.TimeoutExpired("ffmpeg", timeout)
            return b"", b""

        process.communicate.side_effect = communicate
        with mock.patch.object(audio_evidence_cache, "AudioEvidenceCache",
                               return_value=cache), \
                mock.patch.object(autocut.subprocess, "Popen", return_value=process):
            with self.assertRaises(autocut.RenderCancelled):
                with autocut._SeamAudioWindows(
                        "source.mp4", 2, "ffmpeg", cancel=lambda: cancelled,
                        on_log=lambda _line: None) as audio:
                    audio.read(1.0, 0.3)
        process.terminate.assert_called_once_with()
        cache.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
