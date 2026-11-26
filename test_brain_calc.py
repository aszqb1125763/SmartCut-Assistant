# -*- coding: utf-8 -*-
"""计算器（小脑门面）的测试。

只用合成数据：不需要素材、不需要模型、毫秒级。
"""
from __future__ import annotations

import unittest

import numpy as np

import brain
import coords
import decision
from brains.base import AudioSource, FrameSource, WordSource

SR = 16000


def tone(seconds, amp=0.5, freq=440.0):
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class FakeAudio(AudioSource):
    """跳过 ffmpeg：直接给一段合成音频。"""

    def __init__(self, audio, track=0):
        # This fixture never invokes an external decoder.  Pass an explicit
        # sentinel so clean source checkouts do not need bundled FFmpeg merely
        # to construct synthetic PCM.
        super().__init__("synthetic", track=track, ffmpeg="ffmpeg-test")
        self._audio = audio


class FakeFrames(FrameSource):
    """Return deterministic in-memory frames without spawning FFmpeg."""

    def __init__(self):
        super().__init__("synthetic", ffmpeg="ffmpeg-test")

    def frames(self, start=0.0, end=None, fps=2.0, width=360, limit=240):
        end = float(end) if end is not None else float(start) + 1.0
        shape = (32, 48, 3)
        return [float(start), end], [
            np.zeros(shape, dtype=np.uint8),
            np.full(shape, 64, dtype=np.uint8),
        ]


class FakeCalculator(brain.Calculator):
    """把合成音频塞进"音频那一包"，词表塞进"词那一包"。"""

    def __init__(self, segments, words=None):
        chunks = [np.zeros(int(d * SR), dtype=np.float32) if amp <= 0
                  else tone(d, amp) for d, amp in segments]
        audio = np.concatenate(chunks) if chunks else np.zeros(0)
        word_source = WordSource("")
        word_source.set_words(list(words or []))
        super().__init__("synthetic", ffmpeg="ffmpeg-test", sources={
            "audio": FakeAudio(audio), "words": word_source,
            "frames": FakeFrames()})


class ProbePairTest(unittest.TestCase):
    def test_matched_pair_is_ok(self):
        calc = FakeCalculator([(1.0, 0.5), (0.5, 0.0), (1.0, 0.5)])
        got = calc.probe_pair(1.0, 1.5)
        self.assertEqual(["E03"], got["clause"])
        # 小脑只报数据：左右通道 + 差值；档位由决策层给
        self.assertIn("channels", got)
        self.assertIn("silent", got["channels"]["left"])
        verdict = decision.judge(got)
        self.assertEqual("ok", verdict["grade"], verdict)
        self.assertIn(verdict["row"], ("M01", "M16", "M17", None))

    def test_quiet_after_cut_is_not_ok(self):
        calc = FakeCalculator([(1.0, 0.5), (0.5, 0.5), (1.0, 0.05)])
        got = calc.probe_pair(1.0, 1.5)
        self.assertGreater(got["values"]["energy_gap_db"], 15.0)
        self.assertGreater(got["channels"]["left"]["db"],
                           got["channels"]["right"]["db"])
        self.assertEqual("over", decision.judge(got)["grade"])

    def test_seam_is_pair_at_same_point(self):
        calc = FakeCalculator([(1.0, 0.5)])
        got = calc.probe_seam(0.5)
        self.assertEqual("pair", got["probe"])
        self.assertEqual(0.0, got["removed_s"])


class ProbeTailTest(unittest.TestCase):
    def test_natural_decay_is_long(self):
        pieces = [(0.4, 0.5), (0.1, 0.25), (0.1, 0.12), (0.1, 0.06),
                  (0.1, 0.03), (0.2, 0.015)]
        calc = FakeCalculator(pieces)
        got = calc.probe_tail(1.0, span=0.9)
        self.assertEqual("natural", got["shape"], got)
        self.assertGreaterEqual(got["decay_ms"], coords.TAIL_NATURAL_MS)

    def test_hard_break_is_short(self):
        calc = FakeCalculator([(1.0, 0.5), (1.0, 0.0)])
        got = calc.probe_tail(1.0)
        self.assertEqual("hard", got["shape"], got)
        self.assertLess(got["decay_ms"], coords.TAIL_HARD_MS)
        self.assertEqual("P0", got["severity"])

    def test_quiet_region_tail_is_silent(self):
        calc = FakeCalculator([(1.0, 0.5), (1.0, 0.0)])
        got = calc.probe_tail(1.9, span=0.5)
        self.assertEqual("silent", got["shape"], got)
        self.assertEqual("ok", got["grade"])


class ProbeRegionTest(unittest.TestCase):
    def test_long_silence_is_hollow(self):
        calc = FakeCalculator([(1.0, 0.5), (3.0, 0.0), (1.0, 0.5)])
        got = calc.probe_region(1.0, 4.0)
        self.assertEqual("over", got["grade"], got)
        self.assertEqual("M03", got["row"])
        self.assertGreater(got["silence_ratio"], 0.9)

    def test_speech_region_is_fine(self):
        calc = FakeCalculator([(2.0, 0.5)])
        got = calc.probe_region(0.2, 1.8)
        self.assertEqual("ok", got["grade"], got)
        self.assertLess(got["silence_ratio"], 0.1)


class CutSafetyTest(unittest.TestCase):
    WORDS = [{"start": 1.0, "end": 1.5, "word": "第一句"}]

    def test_cut_inside_word(self):
        calc = FakeCalculator([(2.0, 0.5)], words=self.WORDS)
        got = calc.check_cut_safety(1.2)
        self.assertTrue(got["inside_word"])
        self.assertEqual("over", got["grade"])

    def test_cut_between_words(self):
        calc = FakeCalculator([(2.0, 0.5)], words=self.WORDS)
        got = calc.check_cut_safety(1.6)
        self.assertFalse(got["inside_word"])
        self.assertEqual("ok", got["grade"])


class VoicedNoWordTest(unittest.TestCase):
    def test_voiced_but_no_words_is_flagged(self):
        calc = FakeCalculator([(2.0, 0.5)], words=[])
        got = calc.probe_voiced(0.2, 1.8)
        self.assertTrue(got["voiced_no_word"])
        self.assertEqual("M15", got["row"])

    def test_voiced_with_words_is_fine(self):
        calc = FakeCalculator([(2.0, 0.5)],
                              words=[{"start": 0.5, "end": 1.0, "word": "x"}])
        got = calc.probe_voiced(0.2, 1.8)
        self.assertFalse(got["voiced_no_word"])


class SingleDecodeTest(unittest.TestCase):
    def test_audio_decoded_once(self):
        """解码只做一次——以前同一段音频被解两次，白扔几十秒。"""

        class Counting(AudioSource):
            def __init__(self):
                super().__init__("synthetic", ffmpeg="ffmpeg-test")
                self.calls = 0

            @property
            def audio(self):
                if self._audio is None:
                    self.calls += 1
                    self._audio = tone(3.0)
                return self._audio

        source = Counting()
        calc = brain.Calculator("synthetic", ffmpeg="ffmpeg-test",
                                sources={"audio": source})
        for at in (0.5, 0.8, 1.2):
            calc.probe_seam(at)
        self.assertEqual(1, source.calls, "音频被重复解码了 %d 次" % source.calls)


if __name__ == "__main__":
    unittest.main()
