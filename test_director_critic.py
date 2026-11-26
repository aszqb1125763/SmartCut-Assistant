# -*- coding: utf-8 -*-
import unittest

import numpy as np

import director_critic
import evaluate_director_critic


def sample_data():
    texts = [
        "大家好欢迎来到这期实况",
        "今天我们的目标是造一台自动机器",
        "首先把材料放进工作台",
        "因为这个配方需要先通电所以要接线",
        "不对不对刚才说错了我们重来",
        "接下来安装管道并开始测试",
        "机器终于完成了现在可以使用",
        "接下来安装管道并开始测试",
        "然后把产物送到箱子里面",
        "整个自动化系统已经搞定了",
        "这期就先到这里感谢观看",
        "下期我们继续升级再见",
    ]
    segments = [[i * 4.0, i * 4.0 + 3.0, text] for i, text in enumerate(texts)]
    return {
        "duration": 48.0,
        "segments": segments,
        "script": {"sections": [
            {"title": "搭建", "start": 0.0, "end": 24.0},
            {"title": "测试与收尾", "start": 24.0, "end": 48.0},
        ]},
    }


class DirectorCriticTests(unittest.TestCase):
    def test_stage_roles_cover_gameplay_story(self):
        labels = director_critic.classify_stages(sample_data())
        self.assertEqual(labels[0], "hook")
        self.assertEqual(labels[1], "goal")
        self.assertEqual(labels[3], "explain")
        self.assertEqual(labels[6], "result")
        self.assertEqual(labels[10], "closing")

    def test_feedback_rebuild_improves_global_review(self):
        data = sample_data()
        mask = [False, False, True, False, True, True,
                False, True, True, False, False, False]
        spans = director_critic.spans_from_mask(data, mask)
        scores = [0.82, 0.88, 0.72, 0.80, 0.18, 0.70,
                  0.91, 0.55, 0.68, 0.90, 0.84, 0.82]
        rebuilt, report = director_critic.rebuild(
            data, spans, scores, target_seconds=sum(b - a for a, b in spans),
            max_rounds=2)
        self.assertTrue(report["improved"])
        self.assertGreater(report["final"]["score"], report["initial"]["score"])
        self.assertGreaterEqual(report["final"]["stages"]["goal"]["selected"], 1)
        self.assertGreaterEqual(report["final"]["stages"]["result"]["selected"], 1)
        self.assertGreaterEqual(report["final"]["stages"]["closing"]["selected"], 1)
        rebuilt_seconds = sum(b - a for a, b in rebuilt)
        if rebuilt_seconds > report["target_seconds"] * 1.12:
            rebuilt_mask = director_critic.mask_from_spans(data, rebuilt)
            protected = director_critic.genre.chapter_story_chain_indices(data)
            labels = director_critic.classify_stages(data)
            protected.update(i for i, label in enumerate(labels)
                             if label in ("hook", "goal", "result", "closing"))
            self.assertFalse([i for i, keep in enumerate(rebuilt_mask)
                              if keep and i not in protected])

    def test_duplicate_and_retake_are_actionable(self):
        data = sample_data()
        spans = director_critic.spans_from_mask(data, [True] * len(data["segments"]))
        report = director_critic.review(data, spans, [0.6] * len(data["segments"]))
        kinds = {item["type"] for item in report["issues"]}
        self.assertIn("duplicate", kinds)
        self.assertIn("retake", kinds)
        actionable = [item for item in report["issues"]
                      if item["type"] in ("duplicate", "retake")]
        self.assertTrue(all(item["event_ids"] for item in actionable))
        self.assertTrue(all(item["coordinate_space"] == "source"
                            for item in actionable))
        self.assertTrue(all(len(item["interval"]) == 2
                            for item in actionable))
        self.assertEqual("smartcut.source-events.v1",
                         report["timeline_schema"])
        self.assertTrue(report["source_timeline_revision"])

    def test_rejected_short_sentence_is_not_bridged_back(self):
        data = {"duration": 3.0, "segments": [
            [0.0, 1.0, "保留"], [1.0, 2.0, "删除废话"], [2.0, 3.0, "保留结果"],
        ]}
        spans = director_critic.spans_from_mask(data, [True, False, True])
        self.assertEqual(spans, [(0.0, 1.0), (2.0, 3.0)])
        self.assertEqual(director_critic.mask_from_spans(data, spans),
                         [True, False, True])

    def test_long_time_gap_is_not_an_actionable_boundary(self):
        data = {"duration": 11.0, "segments": [
            [0.0, 1.0, "先准备材料"], [10.0, 11.0, "然后继续安装"],
        ]}
        report = director_critic.review(data, [(0.0, 1.0), (10.0, 11.0)],
                                        [0.8, 0.8])
        kinds = {item["type"] for item in report["issues"]}
        self.assertNotIn("abrupt_start", kinds)
        self.assertEqual(1.0, report["continuity_score"])

    def test_missing_story_roles_cannot_pass(self):
        data = {"duration": 8.0, "segments": [
            [0.0, 3.0, "放一个方块"], [4.0, 8.0, "再放一个方块"],
        ]}
        report = director_critic.review(data, [(0.0, 3.0), (4.0, 8.0)], [0.8, 0.8])
        kinds = {item["type"] for item in report["issues"]}
        self.assertIn("missing_source_stage", kinds)
        self.assertFalse(report["passed"])
        self.assertLess(report["stage_score"], 1.0)

    def test_pairwise_candidate_uses_target_ratio(self):
        case = {"base": np.asarray([[0.0], [1.0], [2.0], [3.0]])}
        model = {"kind": "pairwise", "feature_key": "base",
                 "mean": [0.0], "std": [1.0], "weights": [1.0],
                 "target_ratio": 0.5}
        scores = evaluate_director_critic._sentence_scores(case, model)
        self.assertEqual((scores >= 0.5).tolist(), [False, False, True, True])

    def test_out_of_order_transcript_fails_closed(self):
        data = {"duration": 8.0, "segments": [
            [4.0, 5.0, "后发生"], [1.0, 2.0, "先发生"],
        ]}
        report = director_critic.review(
            data, [(1.0, 2.0), (4.0, 5.0)], [0.8, 0.8])
        self.assertFalse(report["timeline_monotonic"])
        self.assertFalse(report["passed"])
        issue = next(row for row in report["issues"]
                     if row["type"] == "timeline_out_of_order")
        self.assertEqual("critical", issue["severity"])
        self.assertEqual(["U0001", "U0002"], issue["event_ids"])


if __name__ == "__main__":
    unittest.main()
