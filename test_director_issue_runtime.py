# -*- coding: utf-8 -*-
"""Director issue lifecycle through the production workflow helpers."""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import autopilot
import director_multimodal
import edit_plan
import workflow_engine


class ReviewCache:
    sample_rate = 1000

    def __init__(self):
        self.reads = []

    def review_samples(self, track, start, end):
        self.reads.append((track, start, end))
        return np.zeros(max(1, int((end - start) * self.sample_rate)), dtype=np.float32)


class DirectorIssueRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source.mp4")
        with open(self.source, "wb") as handle:
            handle.write(b"fixture")
        self.idx = os.path.join(self.tmp.name, "index")
        self.logs = []
        self.events = []
        self.runtime = autopilot._WorkflowRun(
            self.source, "medium", True, self.idx, self.logs.append,
            False, [0, 1], self.tmp.name, False, None, None,
            "gameplay", "TASK-ISSUE-RUNTIME", on_progress=self.events.append)
        self.runtime.director_conf.update({
            "auto_learn": False, "audio_review_enabled": True,
            "director_audio_rework_rounds": 1,
            "enabled": False,
        })
        self.data = {
            "source": self.source, "duration": 100.0, "duration_exact": 100.0,
            "fps": 30.0, "audio_streams": [{"ordinal": 0}, {"ordinal": 1}],
            "audio_stream_used": 0, "segments": [[0.0, 100.0, "对白范围"]],
        }

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def _report(self, spans, index=0, track=0, side="end", problem=True,
                kind=None, confidence=0.95, words=True, unstable="a"):
        anchors = self.runtime.audio_boundary_anchors(spans)
        kind = kind or "speech_cut_" + side
        start, end = spans[index]
        suggested = [start - 0.2, start + 0.24] if side == "start" else [end - 0.24, end + 0.2]
        refs = ["s000001.w0001"] if words else []
        findings = []
        if problem:
            findings.append({
                "id": "transient-" + unstable, "type": kind, "modality": "audio",
                "severity": "major" if kind == "important_audio_tail" else "critical",
                "veto": kind != "important_audio_tail", "confidence": confidence,
                "message": "检查声音边界", "action": "expand_" + side,
                "suggested_interval": suggested, "boundary_word_refs": refs,
                "evidence_refs": ["audio:%s" % track] + refs,
            })
        return {
            "actual_audio_inspected": True, "score": 65.0 if findings else 100.0,
            "track_id": str(track), "span_index": index, "review_kind": "cut_" + side,
            "anchor_key": anchors[index]["cut_" + side], "word_refs": refs,
            "boundary_time": start if side == "start" else end,
            "findings": findings,
        }

    def _persistent_review(self, kind="speech_cut_end", confidence=0.95, words=True):
        calls = []

        def review(_data, spans, _words):
            calls.append(list(spans))
            return [], [self._report(spans, kind=kind, confidence=confidence,
                                     words=words, unstable=str(len(calls)))]

        self.runtime.audio_review = mock.Mock(side_effect=review)
        return calls

    def _checkpoint(self):
        with open(os.path.join(self.idx, "director_issues", "TASK-ISSUE-RUNTIME.json"),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def test_boundary_expansion_keeps_both_original_edge_identities(self):
        first = copy.deepcopy(self.runtime.audio_boundary_anchors([(2.0, 3.0), (5.0, 7.0)]))
        changed = self.runtime.audio_boundary_anchors([(1.8, 3.2), (4.8, 7.2)])
        self.assertEqual(first, changed)

    def test_insertion_and_merge_preserve_surviving_source_edges(self):
        first = copy.deepcopy(self.runtime.audio_boundary_anchors([(2.0, 3.0), (5.0, 7.0)]))
        inserted = self.runtime.audio_boundary_anchors([(0.5, 1.0), (2.0, 3.0), (5.0, 7.0)])
        self.assertEqual(first, inserted[1:])
        merged = self.runtime.audio_boundary_anchors([(0.5, 1.0), (2.0, 7.0)])
        self.assertEqual(first[0]["cut_start"], merged[1]["cut_start"])
        self.assertEqual(first[1]["cut_end"], merged[1]["cut_end"])

    def test_repeated_audio_finding_uses_source_identity_not_interval_or_model_id(self):
        spans = [(2.0, 3.0)]
        first = self.runtime.observe_audio_issues([self._report(spans)], spans)[0]["issue_id"]
        for index in range(1, 21):
            spans = [(2.0, 3.0 + index * 0.1)]
            current = self.runtime.observe_audio_issues(
                [self._report(spans, unstable=str(index))], spans)
            self.assertEqual(first, current[0]["issue_id"])
        self.assertEqual({"total": 1, "resolved": 0, "remaining": 1, "reopened": 0},
                         self.runtime.issue_ledger().counts())

    def test_complete_reinspection_resolves_a_and_keeps_b_for_rework(self):
        spans = [(2.0, 3.0), (5.0, 7.0)]
        first = self.runtime.observe_audio_issues([
            self._report(spans, 0), self._report(spans, 1)], spans)
        self.runtime.observe_audio_issues([
            self._report(spans, 0, problem=False), self._report(spans, 1)], spans)
        counts = self.runtime.report_director_issues(message="仍有一个真实问题", force=True)
        self.assertEqual((2, 1, 1), tuple(counts[key] for key in ("total", "resolved", "remaining")))
        self.assertEqual([first[1]["issue_id"]], [
            row["issue_id"] for row in self.runtime.issue_ledger().pending("audio")])
        self.assertEqual(1, self.events[-1]["remaining"])

    def test_audio_issue_identity_separates_tracks_and_cut_sides(self):
        spans = [(2.0, 3.0)]
        reports = [self._report(spans, track=track, side=side)
                   for track in (0, 1) for side in ("start", "end")]
        observed = self.runtime.observe_audio_issues(reports, spans)
        self.assertEqual(4, len({row["issue_id"] for row in observed}))
        self.assertEqual(4, self.runtime.issue_ledger().counts()["remaining"])

    def test_audio_review_reuses_unchanged_edges_and_rechecks_only_moved_edge(self):
        cache = ReviewCache()
        self.runtime.ensure_audio_cache = lambda _data: cache
        clean = {"actual_audio_inspected": True, "score": 100.0, "findings": [], "word_refs": []}
        with mock.patch("director_multimodal.analyze_audio_span",
                        side_effect=lambda *args, **kwargs: copy.deepcopy(clean)) as analyzer:
            self.runtime.audio_review(self.data, [(2.0, 5.0)], [])
            self.assertEqual(4, analyzer.call_count)
            self.runtime.audio_review(self.data, [(2.0, 5.0)], [])
            self.assertEqual(4, analyzer.call_count)
            self.assertEqual({"inspected": 0, "reused": 4, "reports": 4},
                             self.runtime._director_audio_review_stats)
            self.runtime.audio_review(self.data, [(2.0, 5.25)], [])
            self.assertEqual(6, analyzer.call_count)
            self.assertEqual({"inspected": 2, "reused": 2, "reports": 4},
                             self.runtime._director_audio_review_stats)
        self.assertEqual(6, len(cache.reads))

    def test_cached_edge_evidence_gets_current_lineage_after_span_insertion(self):
        cache = ReviewCache()
        self.runtime.ensure_audio_cache = lambda _data: cache
        clean = {"actual_audio_inspected": True, "score": 100.0, "findings": [], "word_refs": []}
        with mock.patch("director_multimodal.analyze_audio_span",
                        side_effect=lambda *args, **kwargs: copy.deepcopy(clean)) as analyzer:
            _, original = self.runtime.audio_review(self.data, [(2.0, 5.0)], [])
            original_anchors = {report["review_kind"]: report["anchor_key"] for report in original}
            _, inserted = self.runtime.audio_review(self.data, [(0.5, 1.0), (2.0, 5.0)], [])
            old_edge_reports = [report for report in inserted if report["span_index"] == 1]
            self.assertEqual(4, len(old_edge_reports))
            self.assertTrue(all(report["anchor_key"] == original_anchors[report["review_kind"]]
                                for report in old_edge_reports))
            self.assertEqual(8, analyzer.call_count)
            self.assertEqual(4, self.runtime._director_audio_review_stats["reused"])

    def test_missing_extra_window_cannot_close_its_uninspected_previous_issue(self):
        self.runtime.tracks = [0]
        data = {**self.data, "segments": []}
        cache = ReviewCache()
        self.runtime.ensure_audio_cache = lambda _data: cache

        def analyze(_samples, _sample_rate, target, **kwargs):
            findings = []
            if kwargs.get("check_content"):
                findings = [{"id": "content-silence", "type": "unexpected_silence",
                             "modality": "audio", "severity": "major", "veto": False,
                             "confidence": 0.9, "action": "inspect_track_or_span",
                             "suggested_interval": list(target)}]
            return {"actual_audio_inspected": True, "score": 85.0 if findings else 100.0,
                    "findings": findings, "word_refs": []}

        spans = [(2.0, 5.0)]
        with mock.patch("director_multimodal.analyze_audio_span", side_effect=analyze):
            windows, first = self.runtime.audio_review(data, spans, [])
            self.assertEqual(3, len(first))
            self.runtime.observe_audio_issues(first, spans)
            self.assertEqual(1, self.runtime.issue_ledger().counts()["remaining"])
            extra = next(window for window in windows if window["kind"] == "no_dialogue_event")
            self.runtime._director_audio_review_cache.clear()
            ordinary_read = cache.review_samples

            def missing_content(track, start, end):
                if abs(start - extra["interval"][0]) < 1e-6:
                    return np.zeros(0, dtype=np.float32)
                return ordinary_read(track, start, end)

            cache.review_samples = missing_content
            _, second = self.runtime.audio_review(data, spans, [])
            self.assertEqual(2, len(second))
            self.runtime.observe_audio_issues(second, spans)
        self.assertEqual(1, self.runtime.issue_ledger().counts()["remaining"])

    def test_low_confidence_waveform_budget_is_one_across_rework_calls(self):
        self._persistent_review(confidence=0.72, words=False)
        spans = [(2.0, 3.0)]
        applied_rounds = []
        for _ in range(4):
            result = self.runtime.direct_audio_rework(self.data, spans, [])
            spans = result[0]
            applied_rounds.append(result[4])
        self.assertEqual([1, 0, 0, 0], applied_rounds)
        self.assertEqual([1], list(self.runtime._director_audio_attempts.values()))
        self.assertEqual(1, self.runtime.issue_ledger().counts()["total"])

    def test_game_tail_budget_is_two_across_rework_calls(self):
        self._persistent_review(kind="important_audio_tail", words=False)
        spans = [(2.0, 3.0)]
        applied_rounds = []
        for _ in range(4):
            result = self.runtime.direct_audio_rework(self.data, spans, [])
            spans = result[0]
            applied_rounds.append(result[4])
        self.assertEqual(1, applied_rounds[0])
        self.assertLessEqual(sum(applied_rounds), 2)
        self.assertEqual([0, 0], applied_rounds[-2:])
        self.assertLessEqual(max(self.runtime._director_audio_attempts.values()), 2)
        self.assertEqual(1, self.runtime.issue_ledger().counts()["total"])

    def test_handled_waveform_does_not_reopen_from_unchanged_raw_evidence(self):
        self._persistent_review(confidence=0.72, words=False)
        first = self.runtime.direct_audio_rework(self.data, [(2.0, 3.0)], [])
        self.assertEqual(1, self.runtime.issue_ledger().counts()["resolved"])
        before = self.runtime.issue_ledger().counts()["reopened"]
        event_start = len(self.events)
        second = self.runtime.direct_audio_rework(self.data, first[0], [])
        self.assertEqual(0, second[4])
        self.assertEqual(before, self.runtime.issue_ledger().counts()["reopened"])
        self.assertTrue(all(row["remaining"] == 0 for row in self.events[event_start:]))

    def test_word_supported_issue_stays_pending_after_three_attempts(self):
        self._persistent_review()
        spans = [(2.0, 3.0)]
        applied_rounds = []
        for _ in range(5):
            result = self.runtime.direct_audio_rework(self.data, spans, [])
            spans = result[0]
            applied_rounds.append(result[4])
        self.assertEqual([1, 1, 1, 0, 0], applied_rounds)
        self.assertEqual([3], list(self.runtime._director_audio_attempts.values()))
        self.assertEqual({"total": 1, "resolved": 0, "remaining": 1, "reopened": 0},
                         self.runtime.issue_ledger().counts())
        self.assertEqual(1, self._checkpoint()["remaining"])

    def test_failed_reinspection_preserves_unverified_pending_issue(self):
        spans = [(2.0, 3.0)]
        report = self._report(spans)
        self.runtime.audio_review = mock.Mock(side_effect=[([], [report]), RuntimeError("listen failed")])
        result = self.runtime.direct_audio_rework(self.data, spans, [])
        self.assertIn("listen failed", result[3])
        self.assertEqual(1, len(self.runtime.issue_ledger().pending()))
        self.assertEqual(1, self._checkpoint()["remaining"])
        self.assertEqual(0, self.events[-1]["resolved"])

    def test_checkpoint_and_orders_close_only_verified_a_while_b_stays_pending(self):
        spans = [(2.0, 3.0), (5.0, 7.0)]
        memory = mock.Mock()
        memory.list_rework_orders.return_value = []
        memory.create_rework_order.side_effect = ["ORDER-A", "ORDER-B"]
        self.runtime.memory = memory
        first = self.runtime.observe_audio_issues([
            self._report(spans, 0), self._report(spans, 1)], spans)
        self.runtime.report_director_issues(force=True)
        self.runtime.observe_audio_issues([
            self._report(spans, 0, problem=False), self._report(spans, 1)], spans)
        self.runtime.report_director_issues(message="审核尚未通过", force=True)
        saved = self._checkpoint()
        self.assertEqual((2, 1, 1), tuple(saved[key] for key in ("total", "resolved", "remaining")))
        self.assertEqual([first[1]["issue_id"]], [entry["issue_id"]
                         for entry in saved["entries"] if entry["status"] == "open"])
        memory.resolve_rework_order.assert_called_once_with("ORDER-A", self.runtime.artifact("director"))
        self.assertEqual(2, memory.create_rework_order.call_count)
        folder = os.path.join(self.idx, "director_issues")
        self.assertEqual(["TASK-ISSUE-RUNTIME.json"], os.listdir(folder))

    def test_atomic_checkpoint_failure_keeps_previous_valid_json(self):
        spans = [(2.0, 3.0)]
        self.runtime.observe_audio_issues([self._report(spans)], spans)
        self.runtime.report_director_issues(force=True)
        original = self._checkpoint()
        self.runtime.observe_audio_issues([self._report(spans, problem=False)], spans)
        with mock.patch("autopilot.os.replace", side_effect=PermissionError("replace interrupted")):
            self.runtime.report_director_issues(force=True)
        self.assertEqual(original, self._checkpoint())
        self.assertEqual(["TASK-ISSUE-RUNTIME.json"], os.listdir(os.path.join(self.idx, "director_issues")))
        self.assertTrue(any("台账保存失败" in message for message in self.logs))

    def test_recovered_open_orders_are_reused_and_resolved_by_issue_id(self):
        spans = [(2.0, 3.0)]
        finding = self.runtime.observe_audio_issues([self._report(spans)], spans)[0]
        memory = mock.Mock()
        memory.list_rework_orders.return_value = [{
            "operation": "director_issue_rework", "target_unit_id": finding["issue_id"],
            "order_id": "EXISTING-ORDER",
        }]
        self.runtime.memory = memory
        self.runtime.report_director_issues(force=True)
        memory.create_rework_order.assert_not_called()
        self.runtime.observe_audio_issues([self._report(spans, problem=False)], spans)
        self.runtime.report_director_issues(force=True)
        memory.resolve_rework_order.assert_called_once_with(
            "EXISTING-ORDER", self.runtime.artifact("director"))
        memory.list_rework_orders.assert_called_once_with("TASK-ISSUE-RUNTIME", status="open")

    def _run_capped_director(self, final_audio_failure=False):
        spans = [(2.0, 3.0)]
        self.data["analysis"] = {"times": [2.0, 3.0], "diffs": [0.1, 0.2], "runs": []}
        os.makedirs(self.idx, exist_ok=True)
        with open(os.path.join(self.idx, "index.json"), "w", encoding="utf-8") as handle:
            json.dump(self.data, handle)
        plan = edit_plan.build_plan(
            source={"path": self.source, "size": 7, "mtime_ns": 1,
                    "sha256": "a" * 64, "algorithm": "sha256"},
            mode="gameplay", level="medium", config={},
            semantic_spans=spans, versions=self.runtime.ARTIFACTS)
        previous = {"index_dir": self.idx, "spans": spans, "conf": {}, "logs": [], "plan": plan}
        context = workflow_engine.StageContext(
            workflow_engine.TaskSpec("TASK-ISSUE-RUNTIME", self.source),
            workflow_engine.STAGES["director"], (),
            {"boundary": workflow_engine.StageResult("BOUNDARY-FIXTURE", previous)}, lambda: False)
        clean_text = {"score": 100.0, "passed": True, "issues": [], "timeline_revision": "old-text"}
        text_audit = {"initial": clean_text, "final": clean_text, "history": []}
        seen_text = []
        seen_audio = []
        helper_cuts = []

        def text_review(_data, current, **kwargs):
            seen_text.append(list(current))
            return copy.deepcopy(clean_text)

        def no_text_changes(_data, current, report, **kwargs):
            return list(current), dict(report), {"added": [], "dropped": [], "reasons": []}, []

        def mutate_audio(_data, current, _words, **kwargs):
            helper_cuts.append(list(current))
            stale_report = self._report(current, unstable="stale-%s" % len(helper_cuts))
            stale_report["timeline_revision"] = self.runtime.timeline_revision(current)
            self.runtime.observe_audio_issues([stale_report], current)
            changed = [(a, b + 0.2) for a, b in current]
            return changed, [], [stale_report], None, 1, [], 1

        def fresh_audio(_data, current, _words):
            seen_audio.append(list(current))
            if final_audio_failure:
                raise RuntimeError("final audio unavailable")
            clean_report = self._report(current, problem=False)
            return [], [clean_report]

        self.runtime.director_conf["visual_review_enabled"] = False
        self.runtime.refine_boundaries = mock.Mock(side_effect=lambda _data, current, _seed:
                                                  (list(current), [], {}))
        self.runtime.finalize_cut_points = mock.Mock(side_effect=lambda _data, current, *_args:
                                                     (list(current), {}))
        self.runtime.close_timeline_boundaries = mock.Mock(side_effect=lambda _data, current, *_args:
            (list(current), {"timeline_revision": self.runtime.timeline_revision(current)}))
        self.runtime.direct_text_rework = mock.Mock(side_effect=no_text_changes)
        self.runtime.direct_audio_rework = mock.Mock(side_effect=mutate_audio)
        self.runtime.audio_review = mock.Mock(side_effect=fresh_audio)
        with mock.patch("director_critic.review_and_rebuild", return_value=(spans, text_audit)), \
                mock.patch("director_critic.review", side_effect=text_review), \
                mock.patch("director_multimodal.combine_review", wraps=director_multimodal.combine_review) as combined, \
                mock.patch.object(self.runtime, "require_director_approval",
                                  wraps=self.runtime.require_director_approval) as approval:
            if final_audio_failure:
                with mock.patch.object(self.runtime, "_strict_director", return_value=True), \
                        self.assertRaises(autopilot.DirectorReviewFailed):
                    self.runtime.director(context)
                payload = approval.call_args.args[0]
            else:
                payload = self.runtime.director(context).payload
        return payload, combined.call_args.kwargs, seen_text, seen_audio, helper_cuts

    def test_final_cycle_cap_rechecks_actual_cut_instead_of_relabeling_stale_reports(self):
        payload, combined, seen_text, seen_audio, helper_cuts = self._run_capped_director()
        final = [tuple(span) for span in payload["spans"]]
        revision = self.runtime.timeline_revision(final)
        self.assertEqual(4, len(helper_cuts))  # Initial pass plus all three final cycles.
        self.assertEqual([final], seen_audio)
        self.assertEqual(final, seen_text[-1])
        self.assertNotEqual(helper_cuts[-1], final)
        self.assertEqual(revision, combined["text_report"]["timeline_revision"])
        self.assertTrue(all(report["timeline_revision"] == revision for report in combined["audio_reports"]))
        self.assertEqual(revision, payload["director_audit"]["final"]["timeline_revision"])
        audit = payload["director_multimodal_audit"]
        self.assertTrue(audit["passed"])
        self.assertFalse(any(row["type"] in ("speech_cut_end", "timeline_revision_mismatch")
                             for row in audit["findings"]))
        self.assertEqual(0, audit["issue_ledger"]["remaining"])

    def test_failed_final_cap_reinspection_does_not_relabel_old_audio_or_mark_all_solved(self):
        payload, combined, seen_text, seen_audio, helper_cuts = self._run_capped_director(
            final_audio_failure=True)
        final = [tuple(span) for span in payload["spans"]]
        self.assertEqual(4, len(helper_cuts))
        self.assertEqual([final], seen_audio)
        self.assertEqual(final, seen_text[-1])
        self.assertEqual([], combined["audio_reports"])
        audit = payload["director_multimodal_audit"]
        self.assertFalse(audit["passed"])
        self.assertIn("final audio unavailable", audit["audio_error"])
        self.assertGreater(audit["issue_ledger"]["remaining"], 0)
        self.assertGreater(self.events[-1]["remaining"], 0)
        self.assertGreater(self._checkpoint()["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
