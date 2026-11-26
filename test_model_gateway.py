# -*- coding: utf-8 -*-
"""模型网关测试：本地模型只能"提请求"，不能"拿数据"。

守三条：
    1. 原始数据永远进不了模型（audio/frames/words 直接拒）；
    2. 白名单外的"看"也拒；
    3. 返回里不许出现数组/字节（模型只收结果）。
"""
from __future__ import annotations

import unittest

import model_gateway as gateway
from test_brain_calc import FakeCalculator

WORDS = [
    {"start": 0.5, "end": 1.0, "word": "我们"},
    {"start": 1.0, "end": 1.4, "word": "开始"},
    {"start": 2.4, "end": 2.8, "word": "然后"},
    {"start": 2.8, "end": 3.2, "word": "结束"},
]


def calc():
    return FakeCalculator([(4.0, 0.5)], words=WORDS)


class RawDeniedTest(unittest.TestCase):
    def test_audio_denied(self):
        with self.assertRaises(gateway.LookDenied):
            gateway.see(calc(), {"look": "audio"})

    def test_frames_denied(self):
        with self.assertRaises(gateway.LookDenied):
            gateway.see(calc(), {"look": "frames"})

    def test_words_denied(self):
        with self.assertRaises(gateway.LookDenied):
            gateway.see(calc(), {"look": "words"})

    def test_unknown_look_denied(self):
        with self.assertRaises(gateway.LookDenied):
            gateway.see(calc(), {"look": "everything"})


class RouteTest(unittest.TestCase):
    def _see(self, request):
        got = gateway.see(calc(), request)
        self.assertTrue(got["ok"])
        self.assertIn("result", got)
        gateway.assert_no_raw(got)
        return got["result"]

    def test_seam(self):
        result = self._see({"look": "seam", "at": 1.5})
        self.assertEqual("audio", result["brain"])
        self.assertIn("values", result)

    def test_color(self):
        result = self._see({"look": "color", "at": 1.5})
        self.assertIn(result["color"], ("green", "yellow", "red", "gray"))

    def test_picture(self):
        result = self._see({"look": "picture", "at": 1.5})
        self.assertEqual("picture", result["brain"])

    def test_text(self):
        result = self._see({"look": "text", "start": 0.0, "end": 4.0})
        self.assertEqual("text", result["brain"])
        self.assertGreaterEqual(len(result["sentences"]), 1)

    def test_edges(self):
        result = self._see({"look": "edges", "start": 0.0, "end": 4.0,
                            "wanted": ["S"]})
        for row in result["edges"]:
            self.assertEqual("S", row["grade"])

    def test_candidates(self):
        result = self._see({"look": "candidates", "gap": [1.4, 2.4],
                            "top": 3})
        self.assertIn("candidates", result)
        self.assertLessEqual(len(result["candidates"]), 3)

    def test_word(self):
        result = self._see({"look": "word", "at": 0.7})
        self.assertTrue(result["inside_word"])

    def test_missing_argument(self):
        with self.assertRaises(ValueError):
            gateway.see(calc(), {"look": "seam"})


if __name__ == "__main__":
    unittest.main()
