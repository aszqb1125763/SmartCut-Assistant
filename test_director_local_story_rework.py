# -*- coding: utf-8 -*-
"""Local story repairs must execute director findings without reopening safe cuts."""
import unittest
from unittest import mock

import autopilot
import director_critic


class DirectorLocalStoryReworkTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "duration": 1100.0, "duration_exact": 1100.0, "fps": 30.0,
            "segments": [
                [0.0, 3.0, "大家好今天开始这一期"],
                [10.0, 13.0, "今天的目标是制作传送门"],
                [40.0, 43.0, "接下来把方块放进去合成"],
                [100.0, 103.0, "终于制作完成了传送门"],
                [1000.0, 1003.0, "这期就到这里感谢观看"],
            ],
            "scores": [0.8, 0.9, 0.85, 0.99, 0.8],
            "script": {"sections": [{"start": 0.0, "end": 1100.0,
                                       "title": "传送门制作"}]},
        }

    @staticmethod
    def issue(action, indices, kind="story_chain_gap", severity="critical"):
        return {"type": kind, "severity": severity, "action": action,
                "indices": indices, "message": kind}

    @staticmethod
    def report(issues=(), score=90.0, story_score=1.0):
        return {"issues": list(issues), "score": score,
                "story_score": story_score, "chapter_score": story_score,
                "stage_score": 1.0, "continuity_score": 1.0,
                "relevance_score": 1.0}

    def test_story_chain_adds_only_named_source_sentences(self):
        result, changes = autopilot._WorkflowRun.apply_text_findings(
            self.data, [(0.0, 3.0)],
            [self.issue("add_story_chain", [1, 3])], duration=1100.0)
        self.assertEqual([(0.0, 3.0), (10.0, 13.0), (100.0, 103.0)], result)
        self.assertEqual([1, 3], changes["added"])
        self.assertEqual(9.0, sum(end - start for start, end in result))

    def test_stage_and_chapter_follow_existing_director_selection_semantics(self):
        for action in ("add_stage", "add_chapter"):
            with self.subTest(action=action):
                issue = self.issue(action, list(range(5)))
                mask = [False] * 5
                director_critic._apply_feedback(
                    self.data, mask, {"issues": [issue]}, self.data["scores"],
                    target_seconds=None)
                spans, _changes = autopilot._WorkflowRun.apply_text_findings(
                    self.data, [], [issue], duration=1100.0)
                self.assertEqual(mask, director_critic.mask_from_spans(self.data, spans))
                self.assertLessEqual(sum(end - start for start, end in spans), 9.0)

    def test_added_story_and_existing_spine_survive_conflicting_drop(self):
        with mock.patch("genre.chapter_story_chain_indices", return_value={2}):
            spans, changes = autopilot._WorkflowRun.apply_text_findings(
                self.data, [(40.0, 43.0)], [
                    self.issue("add_story_chain", [1]),
                    self.issue("drop", [1, 2], "offtopic", "major"),
                ], duration=1100.0)
        self.assertEqual([(10.0, 13.0), (40.0, 43.0)], spans)
        self.assertEqual([], changes["dropped"])

    def test_real_story_review_can_dispatch_missing_chain_and_recheck_it(self):
        initial_spans = [(0.0, 3.0), (10.0, 13.0), (1000.0, 1003.0)]
        observed = []
        with mock.patch("genre.chapter_story_chain_indices", return_value=set(range(5))):
            initial = director_critic.review(self.data, initial_spans)
            self.assertTrue(any(row["action"] == "add_story_chain"
                                for row in initial["issues"]))
            spans, final, changes, history = autopilot._WorkflowRun.direct_text_rework(
                self.data, initial_spans, initial, duration=1100.0,
                on_review=lambda report, cuts: observed.append((report, cuts)))
        self.assertTrue(final["passed"])
        self.assertEqual([2, 3], changes["added"])
        self.assertEqual(["add_story_chain"], [row["target"][1] for row in history])
        self.assertLessEqual(sum(end - start for start, end in spans), 15.0)
        self.assertEqual(2, len(observed))
        self.assertEqual(initial_spans, observed[0][1])
        self.assertEqual(spans, observed[-1][1])

    def test_local_repair_cannot_hide_growth_of_unactionable_serious_findings(self):
        issue = self.issue("add_story_chain", [1])
        initial = self.report([issue])
        candidate = self.report([
            self.issue("inspect", [], "unresolved_first", "major"),
            self.issue("trim_budget", [], "over_budget", "major"),
        ], score=95.0)
        observed = []
        with mock.patch("director_critic.review", return_value=candidate):
            spans, final, changes, history = autopilot._WorkflowRun.direct_text_rework(
                self.data, [(0.0, 3.0)], initial, duration=1100.0,
                on_review=lambda report, cuts: observed.append((report, cuts)))
        self.assertEqual([(0.0, 3.0)], spans)
        self.assertEqual(initial, final)
        self.assertEqual([], history)
        self.assertEqual([], changes["added"])
        self.assertEqual(1, len(observed), "rejected trial must not alter the live ledger")

    def test_same_issue_count_cannot_escalate_major_to_critical(self):
        initial = self.report([
            self.issue("add_story_chain", [1], severity="major")])
        candidate = self.report([
            self.issue("inspect", [], "new_critical", "critical")], score=95.0)
        with mock.patch("director_critic.review", return_value=candidate):
            result = autopilot._WorkflowRun.direct_text_rework(
                self.data, [(0.0, 3.0)], initial, duration=1100.0)
        self.assertEqual([], result[3])
        self.assertEqual(initial, result[1])

    def test_story_repair_precedes_unrelated_drop_and_cannot_be_dropped_later(self):
        add = self.issue("add_story_chain", [2])
        drop = self.issue("drop", [2], "offtopic", "major")
        initial = self.report([drop, add], story_score=0.5)
        after_add = self.report([drop], story_score=1.0)
        with mock.patch("genre.chapter_story_chain_indices", return_value=set()), \
                mock.patch("director_critic.review", return_value=after_add):
            spans, final, _changes, history = autopilot._WorkflowRun.direct_text_rework(
                self.data, [(0.0, 3.0)], initial, duration=1100.0)
        self.assertEqual(["add_story_chain"], [row["target"][1] for row in history])
        self.assertIn((40.0, 43.0), spans)
        self.assertEqual(after_add, final)


if __name__ == "__main__":
    unittest.main()
