# -*- coding: utf-8 -*-
"""刀口收口：落在字中间的切点必须被收到字边界上。

背景：导演返工（direct_text_rework / apply_text_findings）会移动刀口，
但不经过边界岗位的护词，成片因此把词切成半截。这里只验证收口函数本身：
它只微调端点，不改“留哪句、删哪句”，也不会把切字挪给下一个词。
"""
from __future__ import annotations

import unittest

import autopilot

TOL = 0.025   # 与成片复审一致的容差


def words_of(*rows):
    return [{"start": float(start), "end": float(end)}
            for start, end in rows]


def coverage(spans, start, end):
    return sum(max(0.0, min(end, right) - max(start, left))
               for left, right in spans)


class WordSafeSpansTests(unittest.TestCase):
    def snap(self, spans, words, **kwargs):
        return autopilot._WorkflowRun.word_safe_spans(
            spans, words_of(*words), **kwargs)

    def assert_fully_kept(self, spans, start, end):
        self.assertGreaterEqual(
            coverage(spans, start, end), (end - start) - TOL,
            "词 %.3f-%.3f 只保留了 %.3f 秒" % (start, end,
                                              coverage(spans, start, end)))

    def assert_not_kept(self, spans, start, end):
        self.assertLessEqual(coverage(spans, start, end), TOL,
                             "词 %.3f-%.3f 本不该保留却留了 %.3f 秒"
                             % (start, end, coverage(spans, start, end)))

    def test_cut_inside_word_moves_to_word_edge(self):
        # 刀口 59.183 落在“是” [59.09, 59.33] 内部：原样渲染只剩 0.093 秒。
        spans, changes = self.snap([(17.35, 59.183)], [(59.09, 59.33)])
        self.assertEqual(len(changes), 1)
        self.assert_fully_kept(spans, 59.09, 59.33)
        # 收到边界内侧，不会越过去切下一个词
        self.assertLessEqual(spans[0][1], 59.33 + 1e-9)

    def test_start_inside_word_moves_to_word_edge(self):
        # 起点 804.667 落在“你” [804.42, 804.78] 内部：原样渲染只剩半句。
        spans, changes = self.snap([(804.667, 897.283)], [(804.42, 804.78)])
        self.assertEqual(len(changes), 1)
        self.assert_fully_kept(spans, 804.42, 804.78)
        self.assertGreaterEqual(spans[0][0], 804.42 - 1e-9)

    def test_snap_does_not_bite_the_next_word(self):
        # 词挨着词（连续说话）。向外让 0.06 秒会切到“有”，收边界内侧不会。
        spans, changes = self.snap(
            [(0.0, 59.183)], [(59.09, 59.33), (59.33, 59.59)])
        self.assert_fully_kept(spans, 59.09, 59.33)
        self.assertNotEqual(coverage(spans, 59.33, 59.59), 0.06)
        self.assert_not_kept(spans, 59.33, 59.59)

    def test_cut_at_word_boundary_is_untouched(self):
        spans, changes = self.snap([(10.0, 20.0)], [(11.0, 12.0), (20.0, 21.0)])
        self.assertEqual(changes, [])
        self.assertEqual(spans, [(10.0, 20.0)])

    def test_short_overlap_within_tolerance_is_untouched(self):
        # 只切掉词尾 0.02 秒，小于复审容差，不算切字。
        spans, changes = self.snap([(10.0, 20.0)], [(19.0, 20.02)])
        self.assertEqual(changes, [])
        self.assertEqual(spans, [(10.0, 20.0)])

    def test_overlapping_words_take_outermost(self):
        spans, changes = self.snap([(5.0, 13.0)], [(10.0, 20.0), (12.0, 15.0)])
        self.assertEqual(len(changes), 1)
        self.assert_fully_kept(spans, 12.0, 15.0)
        self.assert_fully_kept(spans, 10.0, 20.0)

    def test_snapped_stay_ordered_and_non_overlapping(self):
        spans, _ = self.snap(
            [(0.0, 10.0), (10.10, 20.0)], [(9.8, 10.2), (19.9, 20.3)])
        for index in range(len(spans) - 1):
            self.assertLessEqual(spans[index][1], spans[index + 1][0] + 1e-9)

    def test_duration_cap(self):
        spans, _ = self.snap([(10.0, 20.0)], [(19.9, 20.5)], duration=20.0)
        self.assertLessEqual(spans[0][1], 20.0)

    def test_empty_inputs(self):
        self.assertEqual(self.snap([], [(1.0, 2.0)]), ([], []))
        self.assertEqual(self.snap([(1.0, 2.0)], []), ([(1.0, 2.0)], []))

    def test_no_word_is_partially_kept_after_snap(self):
        # 一条时间轴上有多个跨界词：收口后每个词要么完整保留、要么完全不保留。
        words = [(1.0, 1.4), (2.0, 2.5), (3.6, 4.2)]
        spans, changes = self.snap([(0.0, 2.2), (3.5, 5.0)], words)
        self.assertTrue(changes)
        for left, right in words:
            kept = coverage(spans, left, right)
            self.assertTrue(
                kept <= TOL or abs(kept - (right - left)) <= TOL,
                "词 %.2f-%.2f 被保留了 %.3f/%.3f 秒"
                % (left, right, kept, right - left))

    def test_real_case_13_truncations_all_cleared(self):
        # 2026-11-22 那条 33 分钟素材的真实切口：原文 13 处切字。
        spans = [
            (17.350, 59.183), (76.350, 129.733), (152.317, 175.533),
            (195.900, 266.333), (321.867, 326.983), (337.400, 347.350),
            (456.967, 483.333), (485.117, 511.983), (674.350, 688.233),
            (727.567, 775.883), (779.117, 800.133), (804.667, 897.283),
            (900.000, 951.600),
        ]
        words = [
            (59.090, 59.330), (76.140, 76.620), (129.420, 129.760),
            (175.390, 175.950), (266.180, 266.400), (326.820, 327.140),
            (337.300, 337.520), (456.880, 457.360), (511.970, 512.210),
            (688.090, 688.270), (775.070, 775.970), (778.900, 779.260),
            (804.420, 804.780),
        ]
        spans_out, changes = self.snap(spans, words)
        # 13 个跨界词分布在 12 个切点上（有一个段两端都要收）。
        self.assertEqual(len(changes), 12)
        for left, right in words:
            self.assert_fully_kept(spans_out, left, right)


if __name__ == "__main__":
    unittest.main()
