# -*- coding: utf-8 -*-
"""文字小脑测试（B02/B03/B06/B08/B11 初版）。"""
from __future__ import annotations

import unittest

from brains.base import WordSource
from brains.text_brain import TextBrain


def words(*rows):
    """rows: (start, end, text)"""
    return [{"start": float(a), "end": float(b), "word": text}
            for a, b, text in rows]


class SentenceTest(unittest.TestCase):
    def test_split_by_gap(self):
        src = WordSource("")
        src.set_words(words((0.0, 0.3, "我们"), (0.3, 0.6, "开始"),
                            (1.2, 1.5, "然后"), (1.5, 1.8, "结束")))
        brain = TextBrain(words=src)
        got = brain.sentences(gap=0.4)
        self.assertEqual(2, len(got))
        self.assertEqual("我们开始", got[0]["text"])
        self.assertEqual(1.2, got[1]["start"])


class EdgeGradeTest(unittest.TestCase):
    def setUp(self):
        src = WordSource("")
        src.set_words(words(
            (0.0, 0.5, "最下面"),
            (0.5, 1.0, "这一层"),
            (1.0, 1.5, "是"),
            (1.5, 2.0, "沙粒"),
            (3.0, 3.5, "新的"),
        ))
        self.brain = TextBrain(words=src)

    def test_inside_word_is_d(self):
        self.assertEqual("D", self.brain.edge_grade(0.25)["grade"])

    def test_long_gap_is_sentence_end(self):
        got = self.brain.edge_grade(2.0)
        self.assertEqual("S", got["grade"], got)
        self.assertEqual("sentence_end", got["reason"])

    def test_short_gap_next_to_function_word_is_c(self):
        # 0.5 处：前后紧挨（gap 0），且后一个词是"这"（虚词）
        got = self.brain.edge_grade(0.5)
        self.assertIn(got["grade"], ("C", "D"), got)

    def test_plain_boundary_is_b_or_a(self):
        got = self.brain.edge_grade(1.5)
        self.assertIn(got["grade"], ("A", "B", "C"), got)

    def test_safe_edges_only_wanted(self):
        edges = self.brain.safe_edges(0.0, 4.0, wanted=("S",))
        self.assertTrue(edges)
        for row in edges:
            self.assertEqual("S", row["grade"])


class MarkTest(unittest.TestCase):
    def test_marks(self):
        src = WordSource("")
        src.set_words(words((0, 1, "因为")))
        brain = TextBrain(words=src)
        got = brain.mark("因为所以")
        self.assertIn("因为", got["logic_hits"])
        self.assertEqual(4, got["length"])


class ConceptTest(unittest.TestCase):
    def test_core_concept_counts(self):
        rows = []
        for k in range(6):
            rows.append((k * 1.0, k * 1.0 + 0.4, "金块"))
            rows.append((k * 1.0 + 0.5, k * 1.0 + 0.9, "处理"))
        src = WordSource("")
        src.set_words(words(*rows))
        brain = TextBrain(words=src)
        got = {row["concept"]: row for row in brain.concept_counts()}
        self.assertIn("金块", got)
        self.assertEqual("core", got["金块"]["grade"])


if __name__ == "__main__":
    unittest.main()
