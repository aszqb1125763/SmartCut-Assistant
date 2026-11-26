# -*- coding: utf-8 -*-
"""句级收口：刀口落在句子中间时，按覆盖率补全整句或去掉半句碎片。

和词级收口的分工：
  · 词级（word_safe_spans）保证「每个字的音完整」；
  · 句级（sentence_safe_spans）保证「这句话说完」。
实测一条素材：字级修完 0 处，句级还有 11 处半句，成片复审一处没报。
"""
from __future__ import annotations

import unittest

import autopilot


class SentenceSafeSpansTests(unittest.TestCase):
    def fix(self, spans, sentences, **kwargs):
        return autopilot._WorkflowRun.sentence_safe_spans(
            spans, sentences, **kwargs)

    def test_half_kept_sentence_is_completed(self):
        # 句子 10-12 秒，段只到 11.5（保留 75%）→ 补全整句。
        spans, stats = self.fix([(0.0, 11.5)], [(10.0, 12.0)])
        self.assertEqual(stats["expanded"], 1)
        self.assertAlmostEqual(spans[0][1], 12.0, places=6)

    def test_short_fragment_is_dropped(self):
        # 句子 10-12 秒，段只到 10.4（保留 20%）→ 认作想删，去掉碎片。
        spans, stats = self.fix([(0.0, 10.4)], [(10.0, 12.0)])
        self.assertEqual(stats["dropped"], 1)
        self.assertLessEqual(spans[0][1], 10.0 + 1e-6)

    def test_whole_sentence_is_untouched(self):
        spans, stats = self.fix([(0.0, 20.0)], [(10.0, 12.0)])
        self.assertEqual((stats["expanded"], stats["dropped"]), (0, 0))
        self.assertEqual(spans, [(0.0, 20.0)])

    def test_sentence_not_kept_at_all_is_untouched(self):
        spans, stats = self.fix([(0.0, 5.0)], [(10.0, 12.0)])
        self.assertEqual((stats["expanded"], stats["dropped"]), (0, 0))
        self.assertEqual(spans, [(0.0, 5.0)])

    def test_two_halves_join_into_one(self):
        # 同一句被劈成前后两段（各 40%），合起来超过阈值 → 补全并连成一段。
        spans, stats = self.fix([(0.0, 10.8), (11.2, 20.0)], [(10.0, 12.0)])
        self.assertEqual(stats["expanded"], 1)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0][0], 0.0, places=6)
        self.assertAlmostEqual(spans[0][1], 20.0, places=6)

    def test_duration_cap(self):
        spans, _ = self.fix([(0.0, 11.5)], [(10.0, 12.0)], duration=11.8)
        self.assertLessEqual(spans[0][1], 11.8 + 1e-9)

    def test_empty_inputs(self):
        self.assertEqual(self.fix([], [(1.0, 2.0)])[0], [])
        self.assertEqual(self.fix([(1.0, 2.0)], [])[0], [(1.0, 2.0)])

    def test_real_case_eleven_half_sentences(self):
        # 2026-11-22 这条素材：11 处半句，补全 7 句、去掉 3 处碎片。
        spans = [
            (17.350, 59.330), (76.140, 129.760), (152.317, 175.950),
            (195.900, 266.400), (267.600, 319.330), (321.870, 327.140),
            (337.300, 347.350), (354.150, 397.130), (397.400, 443.600),
            (456.880, 483.330), (485.120, 512.210), (516.770, 570.780),
            (593.600, 670.350), (674.350, 688.270), (727.570, 775.970),
            (778.900, 800.130), (804.420, 897.280), (901.170, 913.630),
            (939.220, 951.630),
        ]
        sentences = [
            (58.930, 60.530), (76.140, 77.900), (129.220, 130.440),
            (174.890, 177.130), (263.940, 268.880), (326.820, 327.980),
            (336.280, 338.080), (456.880, 458.540), (511.170, 512.430),
            (686.710, 688.450), (774.390, 776.310), (778.900, 780.500),
            (804.420, 806.240),
        ]
        fixed, stats = self.fix(spans, sentences, duration=951.75)
        self.assertGreaterEqual(stats["expanded"], 6)
        # 补全之后，每句话在时间线里要么完整、要么完全不在。
        for start, end in sentences:
            kept = sum(max(0.0, min(end, right) - max(start, left))
                       for left, right in fixed)
            self.assertTrue(
                kept <= 0.03 or abs(kept - (end - start)) <= 0.03,
                "句子 %.2f-%.2f 只保留了 %.3f/%.3f 秒"
                % (start, end, kept, end - start))


if __name__ == "__main__":
    unittest.main()
