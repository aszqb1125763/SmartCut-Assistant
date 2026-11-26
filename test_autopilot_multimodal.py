# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

import autopilot
import edit_plan
import workflow_engine


class FakeCache:
    sample_rate = 1000

    def activity_intervals(self, track, start, end, kind="speech"):
        if track == 0:
            return [{"start": max(0.0, start + 0.05), "end": end - 0.05,
                     "confidence": 0.9, "track": "0", "kind": kind}]
        return [{"start": end - 0.25, "end": end,
                 "confidence": 0.85, "track": str(track), "kind": kind}]


class AutopilotMultimodalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source.mp4")
        with open(self.source, "wb") as handle:
            handle.write(b"source")
        self.idx = os.path.join(self.tmp.name, "index")
        os.makedirs(self.idx)
        self.data = {
            "source": self.source, "duration": 5.0, "duration_exact": 5.0,
            "fps": 30.0, "audio_streams": [{"ordinal": 0}, {"ordinal": 1}],
            "audio_stream_used": 0,
            "segments": [[1.0, 2.0, "我开始了"], [3.0, 4.0, "完成了"]],
            "segment_evidence": [{
                "segment_id": "s000001", "start": 1.0, "end": 2.0,
                "text": "我开始了", "source_audio_tracks": [0],
                "words": [{"text": "我", "start": 0.96, "end": 1.13,
                           "probability": 0.92}],
            }],
            "analysis": {"times": [0.9, 1.0, 2.0, 3.0],
                         "diffs": [0.1, 0.9, 0.2, 0.8],
                         "runs": [[0.8, 2.2]]},
            "script": {"sections": [{"start": 1.0, "end": 4.0,
                                       "title": "测试"}]},
            "scores": [0.8, 0.9], "semantic": [{}, {}],
        }
        self._write_data()
        self.runtime = autopilot._WorkflowRun(
            self.source, "medium", True, self.idx, lambda _message: None,
            False, [0, 1], self.tmp.name, False, None, None,
            "gameplay", "TASK-MM")
        self.runtime.director_conf["auto_learn"] = False
        self.runtime.director_conf["rendered_review_enabled"] = False

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def _write_data(self):
        with open(os.path.join(self.idx, "index.json"), "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False)

    def _context(self, stage, outputs):
        task = workflow_engine.TaskSpec("TASK-MM", self.source)
        return workflow_engine.StageContext(
            task, workflow_engine.STAGES[stage], (), outputs, lambda: False)

    def _plan(self, spans=((1.0, 2.0), (3.0, 4.0))):
        return edit_plan.build_plan(
            source={"path": self.source, "size": 6, "mtime_ns": 1,
                    "sha256": "a" * 64, "algorithm": "sha256"},
            mode="gameplay", level="medium", config={},
            semantic_spans=spans, versions=self.runtime.ARTIFACTS)

    def test_production_refiner_uses_words_primary_and_secondary_tracks(self):
        self.runtime.ensure_audio_cache = lambda _data: FakeCache()
        spans, details, summary = self.runtime.refine_boundaries(
            self.data, [(1.0, 2.0)], "fixed-seed")
        self.assertEqual(1, len(spans))
        self.assertLessEqual(spans[0][0], 0.96)
        self.assertGreater(summary["primary_activity"], 0)
        self.assertGreater(summary["secondary_activity"], 0)
        self.assertIn("word_core_protected", details[0]["reasons"])

    def test_director_reviews_all_rendered_tracks_by_default(self):
        self.runtime.tracks = None
        self.data["audio_stream_used"] = 0
        self.assertEqual([0, 1], self.runtime.selected_tracks(self.data))
        self.assertEqual([0], self.runtime.speech_tracks(self.data, [0, 1]))

    def test_director_respects_explicit_track_selection(self):
        self.runtime.tracks = [1]
        self.assertEqual([1], self.runtime.selected_tracks(self.data))

    def test_director_combines_text_audio_visual_and_writes_plan(self):
        plan = self._plan()
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0], [3.0, 4.0]],
            "conf": {}, "logs": [], "plan": plan,
            "task_seed": plan["task_seed"],
        }
        text_audit = {
            "initial": {"score": 90.0, "passed": True, "issues": []},
            "final": {"score": 90.0, "passed": True, "issues": []},
            "history": [],
        }
        audio = [{"actual_audio_inspected": True, "score": 96.0,
                  "findings": []}]
        visual = {"nodes": [{"id": "visual_001", "issues": [],
                              "sample_times": [1.0, 1.2, 1.4]}],
                  "node_count": 1, "score": 100.0, "passed": True}
        self.runtime.refine_boundaries = mock.Mock(
            return_value=([(1.0, 2.0), (3.0, 4.0)], [], {"words": 1}))
        self.runtime.audio_review = mock.Mock(
            return_value=([{"id": "review_001", "kind": "cut_start",
                            "time": 1.0, "interval": [0.2, 1.8]}], audio))
        with mock.patch("director_critic.review_and_rebuild",
                        return_value=([(1.0, 2.0), (3.0, 4.0)], text_audit)), \
                mock.patch("visual_critic.review_video", return_value=visual):
            result = self.runtime.director(self._context(
                "director", {"boundary": workflow_engine.StageResult(
                    "TASK-MM.BOUNDARY-V1", previous)}))
        payload = result.payload
        self.assertTrue(payload["director_multimodal_audit"]["passed"])
        self.assertEqual({"text": 0.5, "audio": 0.5},
                         payload["director_multimodal_audit"]["primary_weights"])
        self.assertTrue(os.path.isfile(payload["plan_path"]))
        self.assertNotEqual(plan["plan_sha256"], payload["plan_sha256"])

    def test_model_editor_timeline_is_not_rebuilt_by_director(self):
        self.runtime.model_editor = True
        plan = self._plan()
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0], [3.0, 4.0]],
            "conf": {}, "logs": [], "plan": plan,
            "task_seed": plan["task_seed"],
        }
        clean = {"score": 96.0, "passed": True, "issues": []}
        audio = [{"actual_audio_inspected": True, "score": 98.0,
                  "findings": []}]
        visual = {"nodes": [], "node_count": 0,
                  "score": 100.0, "passed": True}
        self.runtime.refine_boundaries = mock.Mock(
            side_effect=lambda _data, current, _seed:
            (list(current), [], {"words": 1}))
        self.runtime.audio_review = mock.Mock(return_value=([], audio))
        with mock.patch("director_critic.review_and_rebuild") as rebuild, \
                mock.patch("director_critic.review", return_value=clean), \
                mock.patch("visual_critic.review_video", return_value=visual):
            result = self.runtime.director(self._context(
                "director", {"boundary": workflow_engine.StageResult(
                    "TASK-MM.BOUNDARY-V1", previous)}))
        rebuild.assert_not_called()
        self.assertTrue(
            result.payload["director_audit"]["model_timeline_preserved"])

    def test_director_repeats_audio_rework_until_the_cut_is_clean(self):
        plan = self._plan(spans=((1.0, 2.0),))
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0]],
            "conf": {}, "logs": [], "plan": plan,
            "task_seed": plan["task_seed"],
        }
        text_audit = {
            "initial": {"score": 95.0, "passed": True, "issues": []},
            "final": {"score": 95.0, "passed": True, "issues": []},
            "history": [],
        }
        visual = {"nodes": [{"id": "visual_001", "issues": [],
                              "sample_times": [1.0, 1.5, 2.0]}],
                  "node_count": 1, "score": 100.0, "passed": True}

        def report(end, suggested=None):
            findings = []
            if suggested is not None:
                findings.append({
                    "id": "tail_%.1f" % suggested, "type": "speech_cut_end",
                    "modality": "audio", "severity": "critical", "veto": True,
                    "message": "尾音", "evidence_refs": ["audio:0"],
                    "suggested_interval": [end - 0.24, suggested],
                    "confidence": 0.95, "action": "expand_end", "learnable": False,
                })
            return [{"actual_audio_inspected": True, "score": 100.0 if not findings else 65.0,
                     "review_window_id": "review_001", "findings": findings}]

        self.runtime.director_conf["director_audio_rework_rounds"] = 3
        self.runtime.refine_boundaries = mock.Mock(
            side_effect=lambda _data, spans, _seed: (list(spans), [], {"words": 1}))
        self.runtime.audio_review = mock.Mock(side_effect=[
            ([], report(2.0, 2.2)),
            ([], report(2.2, 2.4)),
            ([], report(2.4, None)),
            ([], report(2.4, None)),
        ])
        with mock.patch("director_critic.review_and_rebuild",
                        return_value=([(1.0, 2.0)], text_audit)), \
                mock.patch("director_critic.review",
                           return_value=text_audit["final"]), \
                mock.patch("visual_critic.review_video", return_value=visual):
            result = self.runtime.director(self._context(
                "director", {"boundary": workflow_engine.StageResult(
                    "TASK-MM.BOUNDARY-V1", previous)}))
        self.assertTrue(result.payload["director_multimodal_audit"]["passed"])
        self.assertGreaterEqual(result.payload["spans"][0][1], 2.4)
        self.assertEqual(4, self.runtime.audio_review.call_count)

    def test_boundary_repair_targets_only_the_reported_cut(self):
        reports = [{
            "span_index": 1, "review_kind": "cut_end", "boundary_time": 4.0,
            "findings": [{
                "id": "tail", "type": "speech_cut_end", "severity": "critical",
                "action": "expand_end", "suggested_interval": [3.76, 4.35],
            }],
        }]
        repaired, changes = self.runtime.apply_boundary_findings(
            [(1.0, 2.0), (3.0, 4.0)], reports, fps=30.0, duration=5.0)
        self.assertEqual((1.0, 2.0), repaired[0])
        self.assertGreaterEqual(repaired[1][1], 4.35)
        self.assertEqual(1, len(changes))
        self.assertEqual(1, changes[0]["span_index"])

    def test_low_confidence_activity_becomes_transition_guidance(self):
        reports = [{
            "actual_audio_inspected": True, "score": 65.0, "word_refs": [],
            "findings": [{
                "id": "wave-only", "type": "speech_cut_end",
                "severity": "critical", "veto": True, "confidence": 0.72,
                "evidence_refs": ["audio:0"], "action": "expand_end",
                "suggested_interval": [1.8, 2.2], "learnable": False,
            }],
        }]
        settled = self.runtime.settle_audio_reports(reports, rework_rounds=1)
        finding = settled[0]["findings"][0]
        self.assertEqual("unconfirmed_boundary_activity", finding["type"])
        self.assertEqual("minor", finding["severity"])
        self.assertFalse(finding["veto"])
        self.assertEqual("transition_guard", finding["action"])

    def test_word_backed_boundary_remains_a_veto(self):
        reports = [{
            "actual_audio_inspected": True, "score": 65.0,
            "word_refs": ["s000001.w0001"],
            "findings": [{
                "id": "word-backed", "type": "speech_cut_end",
                "severity": "critical", "veto": True, "confidence": 0.72,
                "evidence_refs": ["audio:0", "s000001.w0001"],
                "action": "expand_end", "suggested_interval": [1.8, 2.2],
                "learnable": False,
            }],
        }]
        settled = self.runtime.settle_audio_reports(reports, rework_rounds=2)
        finding = settled[0]["findings"][0]
        self.assertEqual("speech_cut_end", finding["type"])
        self.assertEqual("critical", finding["severity"])
        self.assertTrue(finding["veto"])

    def test_hard_visual_jump_is_directed_to_transition(self):
        report = {"nodes": [{
            "id": "visual_001", "sample_times": [2.0, 3.0],
            "issues": [{"type": "hard_visual_jump", "severity": "major",
                        "action": "inspect_transition", "message": "jump"}],
        }]}
        prepared, directives = self.runtime.prepare_visual_report(report)
        issue = prepared["nodes"][0]["issues"][0]
        self.assertEqual("minor", issue["severity"])
        self.assertEqual("apply_transition", issue["action"])
        self.assertEqual("choose_safe_transition", directives[0]["operation"])

    def test_confirmed_no_dialogue_director_uses_audio_and_visual(self):
        self.data.update({"segments": [], "segment_evidence": [], "scores": [],
                          "semantic": [], "has_speech": False,
                          "asr_requested": True})
        self._write_data()
        plan = self._plan(spans=((0.0, 5.0),))
        previous = {
            "index_dir": self.idx, "spans": [[0.0, 5.0]],
            "conf": {}, "logs": [], "plan": plan,
            "task_seed": plan["task_seed"],
        }
        audio = [{"actual_audio_inspected": True, "score": 98.0, "findings": []}]
        visual = {"nodes": [{"id": "visual_001", "issues": [],
                              "sample_times": [0.5, 2.5, 4.5]}],
                  "node_count": 1, "score": 100.0, "passed": True}
        self.runtime.refine_boundaries = mock.Mock(
            return_value=([(0.0, 5.0)], [], {"words": 0}))
        self.runtime.audio_review = mock.Mock(return_value=([], audio))
        with mock.patch("director_critic.review_and_rebuild") as text_review, \
                mock.patch("visual_critic.review_video", return_value=visual):
            result = self.runtime.director(self._context(
                "director", {"boundary": workflow_engine.StageResult(
                    "TASK-MM.BOUNDARY-V1", previous)}))
        text_review.assert_not_called()
        audit = result.payload["director_multimodal_audit"]
        self.assertTrue(audit["passed"])
        self.assertEqual("not_applicable", audit["evidence_status"]["text"])

    def test_renderer_uses_final_plan_hash_as_default_bgm_seed(self):
        plan = edit_plan.extend_plan(self._plan(), final_render_spans=[(1.0, 2.0)])
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0]], "seams": [],
            "conf": {"render": {}}, "logs": [], "plan": plan,
            "plan_sha256": plan["plan_sha256"], "task_seed": plan["task_seed"],
            "director_multimodal_audit": {"status": "passed", "passed": True},
        }
        captured = {}

        def fake_render(_source, _spans, _seams, output, _conf, **kwargs):
            captured.update(kwargs["bgm_conf"])
            with open(output, "wb") as handle:
                handle.write(b"video")
            return {"minutes": 0.02, "timeline": []}

        with mock.patch.object(autopilot.autocut, "load_index", return_value=self.data), \
                mock.patch.object(autopilot.autocut, "render", side_effect=fake_render), \
                mock.patch.object(autopilot.bgm_mod, "load_conf", return_value={}):
            result = self.runtime.renderer(self._context(
                "renderer", {"transition": workflow_engine.StageResult(
                    "TASK-MM.TRANSITION-V2", previous)}))
        self.assertEqual(plan["plan_sha256"], captured["seed"])
        self.assertTrue(os.path.isfile(result.payload["out"]))

    def test_renderer_blocks_delivery_when_actual_output_review_fails(self):
        self.runtime._strict_director = mock.Mock(return_value=True)
        plan = edit_plan.extend_plan(self._plan(), final_render_spans=[(1.0, 2.0)])
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0]], "seams": [],
            "conf": {"render": {}}, "logs": [], "plan": plan,
            "plan_sha256": plan["plan_sha256"], "task_seed": plan["task_seed"],
            "director_multimodal_audit": {"status": "passed", "passed": True},
        }

        def fake_render(_source, _spans, _seams, output, _conf, **_kwargs):
            with open(output, "wb") as handle:
                handle.write(b"video")
            return {"minutes": 0.02, "timeline": [(0.0, 1.0, 1.0)]}

        failed = {
            "passed": False, "score": 40.0, "veto_count": 1,
            "major_issues": 0, "findings": [{
                "id": "render-fail", "modality": "audio", "severity": "critical",
                "veto": True, "type": "render_word_boundary_cut",
                "message": "实际成片截字", "suggested_interval": [0.0, 0.3],
            }],
        }
        self.runtime.director_conf["rendered_review_enabled"] = True
        with mock.patch.object(autopilot.autocut, "load_index", return_value=self.data), \
                mock.patch.object(autopilot.autocut, "render", side_effect=fake_render), \
                mock.patch.object(autopilot.bgm_mod, "load_conf", return_value={}), \
                mock.patch("rendered_output_qc.review_rendered_output",
                           return_value=failed):
            with self.assertRaises(autopilot.RenderedOutputReviewFailed):
                self.runtime.renderer(self._context(
                    "renderer", {"transition": workflow_engine.StageResult(
                        "TASK-MM.TRANSITION-V2", previous)}))
        reports = []
        for root, _dirs, files in os.walk(self.idx):
            reports.extend(os.path.join(root, name) for name in files
                           if name == "rendered_output_qc.json")
        self.assertTrue(reports)

    def test_bgm_modified_final_file_must_pass_a_second_delivery_review(self):
        self.runtime._strict_director = mock.Mock(return_value=True)
        plan = edit_plan.extend_plan(
            self._plan(), final_render_spans=[(1.0, 2.0)])
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0]], "seams": [],
            "conf": {"render": {}}, "logs": [], "plan": plan,
            "plan_sha256": plan["plan_sha256"],
            "task_seed": plan["task_seed"],
            "director_multimodal_audit": {
                "status": "passed", "passed": True},
        }

        def fake_render(_source, _spans, _seams, output, _conf, **_kwargs):
            with open(output, "wb") as handle:
                handle.write(b"video")
            return {
                "minutes": 0.02, "timeline": [(0.0, 1.0, 1.0)],
                "timeline_v2": {
                    "schema": "smartcut.sequence-timeline",
                    "schema_version": 2, "duration": 1.0,
                    "sequence_revision": "c" * 64, "pieces": [],
                },
                "chapter_ranges": [], "chapter_seconds": 0.0,
                "smooth_seams": [], "spans": [(1.0, 2.0)],
            }

        clean = {
            "passed": True, "score": 96.0, "veto_count": 0,
            "major_issues": 0, "findings": [],
            "sequence_revision": "c" * 64,
        }
        failed_after_bgm = {
            "passed": False, "score": 30.0, "veto_count": 1,
            "major_issues": 0, "findings": [{
                "id": "bgm-clip", "modality": "audio",
                "severity": "critical", "veto": True,
                "type": "digital_clipping", "message": "混音削波",
            }],
            "sequence_revision": "c" * 64,
        }
        self.runtime.director_conf.update({
            "rendered_review_enabled": True,
            "seam_review_enabled": False,
            "paragraph_review_enabled": False,
        })
        reinforcement = mock.Mock(return_value={
            "status": "penalized", "rewarded": 0, "penalized": 2,
            "memory_ids": ["MEM-1", "MEM-2"],
        })
        rework = mock.Mock(return_value=1)
        self.runtime.reinforce_director_learning = reinforcement
        self.runtime.record_director_rework = rework
        with mock.patch.object(autopilot.autocut, "load_index",
                               return_value=self.data), \
                mock.patch.object(autopilot.autocut, "render",
                                  side_effect=fake_render), \
                mock.patch.object(autopilot.bgm_mod, "load_conf",
                                  return_value={}), \
                mock.patch("rendered_output_qc.review_rendered_output",
                           side_effect=[clean, failed_after_bgm]) as review, \
                mock.patch("bgm.attach", return_value={
                    "track": "music.wav", "start": 0.0, "len": 1.0
                }) as attach, \
                mock.patch.object(
                    autopilot, "_atomic_write_json",
                    wraps=autopilot._atomic_write_json) as atomic_write:
            with self.assertRaises(autopilot.RenderedOutputReviewFailed):
                self.runtime.renderer(self._context(
                    "renderer", {"transition": workflow_engine.StageResult(
                        "TASK-MM.TRANSITION-V2", previous)}))
        self.assertEqual(2, review.call_count)
        attach.assert_called_once()
        reinforcement.assert_called_once()
        self.assertFalse(reinforcement.call_args.args[2])
        rework.assert_called_once()
        self.assertGreaterEqual(atomic_write.call_count, 2)

        report_path = os.path.join(
            self.idx, "rendered_output_qc", plan["plan_sha256"][:16],
            "rendered_output_qc.json")
        self.assertTrue(os.path.isfile(report_path))
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertFalse(report["passed"])
        self.assertTrue(report["delivery_blocked"])
        self.assertEqual("post_bgm", report["failure_phase"])
        self.assertEqual("penalized",
                         report["learning_reinforcement"]["status"])
        self.assertEqual(2, report["learning_reinforcement"]["penalized"])
        self.assertEqual("music.wav", report["bgm"]["track"])
        report_dir = os.path.dirname(report_path)
        self.assertFalse([name for name in os.listdir(report_dir)
                          if name.endswith(".tmp")])

    def test_atomic_qc_write_preserves_previous_report_on_failure(self):
        path = os.path.join(self.tmp.name, "qc", "report.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"status": "previous"}, handle)
        with mock.patch.object(autopilot.json, "dump",
                               side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                autopilot._atomic_write_json(path, {"status": "partial"})
        with open(path, encoding="utf-8") as handle:
            self.assertEqual({"status": "previous"}, json.load(handle))
        self.assertFalse([name for name in os.listdir(os.path.dirname(path))
                          if name.endswith(".tmp")])

    def test_transition_plan_is_registered_under_renderer_role(self):
        captured = {}

        class StrictMemory:
            @staticmethod
            def get_artifact(_artifact_id):
                return None

            @staticmethod
            def register_artifact(_task_id, role, _artifact_type, _path, **_kwargs):
                if role not in ("analyst", "writer", "editor", "boundary",
                                "director", "renderer"):
                    raise ValueError("unknown role")
                captured["role"] = role

        self.runtime.memory = StrictMemory()
        self.runtime.write_plan("transition", self._plan(), "render_input",
                                source_stage="director")
        self.assertEqual("renderer", captured["role"])

    def test_only_eligible_passed_review_is_written_to_long_term_memory(self):
        class FakeStore:
            def __init__(self):
                self.rows = []
                self.relations = []

            def record_observation(self, **kwargs):
                self.rows.append(kwargs)
                return "MEM-%02d" % len(self.rows)

            def add_relation(self, source, target, relation_type, **kwargs):
                self.relations.append((source, target, relation_type, kwargs))

            @staticmethod
            def flush():
                return None

        store = FakeStore()
        self.runtime._long_term_store = store
        self.runtime.director_conf.update({
            "auto_learn": True, "candidate_promotion": "validated_only",
            "director_audio_rework_rounds": 3,
        })
        audit = {
            "passed": True, "auto_learning_eligible": True,
            "scores": {"text": 92.0, "audio": 96.0},
            "primary_weights": {"text": 0.5, "audio": 0.5},
            "evidence_status": {"text": "available", "audio": "available",
                                "visual": "available"},
            "findings": [], "review_windows": [{"id": "review_001"}],
        }
        plan = self._plan()
        ids = self.runtime.record_director_learning(
            audit, plan, [{"source": "audio_director", "round": 1}])
        self.assertEqual(["MEM-01", "MEM-02"], ids)
        self.assertEqual(["director", "boundary"],
                         [row["role"] for row in store.rows])
        self.assertTrue(all(row["passed"] is None and
                            not row["auto_learning_eligible"] and
                            row["promotion_policy"] == "manual"
                            for row in store.rows))
        self.assertEqual("confirms", store.relations[0][2])

        store.rows.clear()
        audit["auto_learning_eligible"] = False
        self.assertEqual([], self.runtime.record_director_learning(audit, plan, []))
        self.assertEqual([], store.rows)
        ids = self.runtime.record_director_learning(
            audit, plan, [{"source": "audio_director", "round": 1,
                           "changes": [{"finding_id": "tail"}]}])
        self.assertEqual([], ids)
        self.assertEqual([], store.rows)

    def test_director_debrief_separates_validated_and_task_only_lessons(self):
        self.runtime._director_progress_state = {
            "total": 6, "resolved": 5, "fraction": 5 / 6,
        }
        audit = {
            "passed": True, "scores": {"final": 86.0},
            "findings": [{
                "modality": "visual", "severity": "minor", "veto": False,
                "type": "hard_visual_jump", "action": "apply_transition",
            }],
        }
        debrief = self.runtime.build_director_debrief(audit, [
            {"source": "audio_director", "changes": [{"finding_id": "a1"}]},
            {"source": "text_director", "added": [1], "dropped": []},
            {"source": "multimodal_director", "action": "apply_transition"},
        ])
        self.assertEqual("passed", debrief["outcome"])
        self.assertEqual(5, debrief["issues_resolved"])
        results = {(row["role"], row["result"]) for row in debrief["lessons"]}
        self.assertIn(("boundary", "candidate"), results)
        self.assertIn(("editor", "candidate"), results)
        self.assertIn(("renderer", "task_only"), results)

    def test_validated_boundary_memory_adds_only_bounded_rejected_cut_guard(self):
        self.runtime._learning_guidance = {
            "audio_guard_sec": 0.08,
            "roles": {"boundary": ["MEM-boundary"]},
        }
        guided = self.runtime.guide_audio_finding({
            "severity": "major", "action": "expand_start",
            "suggested_interval": [4.9, 5.0],
        }, 30.0)
        self.assertAlmostEqual(4.82, guided["suggested_interval"][0], places=6)
        self.assertEqual({"MEM-boundary"}, self.runtime._applied_memory_ids)
        untouched = self.runtime.guide_audio_finding({
            "severity": "minor", "action": "inspect",
            "suggested_interval": [4.9, 5.0],
        }, 30.0)
        self.assertEqual([4.9, 5.0], untouched["suggested_interval"])

    def test_final_review_reinforces_only_attributable_memories(self):
        class FakeStore:
            def __init__(self):
                self.calls = []
                self.rows = {
                    "MEM-director": {"memory_id": "MEM-director", "role": "director"},
                    "MEM-boundary": {"memory_id": "MEM-boundary", "role": "boundary"},
                    "MEM-editor": {"memory_id": "MEM-editor", "role": "editor"},
                }

            def get(self, memory_id):
                return self.rows.get(memory_id)

            def reinforce(self, memory_ids, **kwargs):
                self.calls.append((list(memory_ids), kwargs))
                return [self.rows[memory_id] for memory_id in memory_ids]

            @staticmethod
            def flush():
                return None

        store = FakeStore()
        self.runtime._long_term_store = store
        previous = {
            "plan_sha256": "a" * 64,
            "plan": {"director_modifications": []},
            "director_multimodal_audit": {
                "learning_candidate_ids": [
                    "MEM-director", "MEM-boundary", "MEM-editor"],
                "applied_memory_ids": [],
            },
        }
        result = self.runtime.reinforce_director_learning(previous, {
            "score": 42.0,
            "findings": [{"id": "f1", "modality": "audio",
                          "type": "render_word_boundary_cut"}],
        }, False)
        self.assertEqual(2, result["penalized"])
        self.assertEqual(["MEM-director", "MEM-boundary"], store.calls[0][0])

    def test_director_blocks_render_when_multimodal_review_fails(self):
        self.runtime._strict_director = mock.Mock(return_value=True)
        events = []
        self.runtime._progress_sink = events.append
        plan = self._plan()
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0], [3.0, 4.0]],
            "conf": {}, "logs": [], "plan": plan,
            "task_seed": plan["task_seed"],
        }
        text_audit = {
            "initial": {"score": 90.0, "passed": True, "issues": []},
            "final": {"score": 90.0, "passed": True, "issues": []},
            "history": [],
        }
        audio = [{"actual_audio_inspected": True, "score": 96.0,
                  "findings": []}]
        visual = {"nodes": [{
            "id": "visual_001", "time": 3.0,
            "sample_times": [2.0, 3.0, 4.0],
            "issues": [{"type": "black_or_blank", "severity": "major",
                        "message": "结果节点是黑屏", "action": "inspect_or_drop"}],
        }], "node_count": 1, "score": 20.0, "passed": False}
        self.runtime.refine_boundaries = mock.Mock(
            return_value=([(1.0, 2.0), (3.0, 4.0)], [], {"words": 1}))
        self.runtime.audio_review = mock.Mock(return_value=([], audio))
        with mock.patch("director_critic.review_and_rebuild",
                        return_value=([(1.0, 2.0), (3.0, 4.0)], text_audit)), \
                mock.patch("visual_critic.review_video", return_value=visual):
            with self.assertRaises(autopilot.DirectorReviewFailed):
                self.runtime.director(self._context(
                    "director", {"boundary": workflow_engine.StageResult(
                        "TASK-MM.BOUNDARY-V1", previous)}))
        with open(os.path.join(self.idx, "index.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertFalse(saved["director_multimodal_audit"]["passed"])
        self.assertTrue(events)
        self.assertGreater(events[-1]["remaining"], 0)
        self.assertLess(events[-1]["resolved"], events[-1]["total"])
        self.assertLess(events[-1]["fraction"], 1.0)

    def test_paired_text_boundary_rework_keeps_the_local_gap(self):
        import director_critic

        data = {
            "duration": 6.0, "duration_exact": 6.0, "fps": 30.0,
            "segments": [[0.0, 2.0, "先做这个"], [4.0, 6.0, "然后继续"]],
        }
        spans = [(0.0, 2.0), (4.0, 6.0)]
        report = director_critic.review(data, spans, [0.8, 0.8])
        issues = [row for row in report["issues"]
                  if row.get("type") in ("abrupt_start", "abrupt_end")]
        self.assertTrue(issues)
        repaired, changes = self.runtime.apply_text_findings(
            data, spans, issues, fps=30.0, duration=6.0)
        self.assertEqual([(0.0, 6.0)], repaired)
        self.assertEqual([0, 1], changes["added"])
        reviewed = director_critic.review(data, repaired, [0.8, 0.8])
        self.assertFalse(any(row.get("type") in ("abrupt_start", "abrupt_end")
                             for row in reviewed["issues"]))

    def test_director_progress_reports_current_snapshot_after_reopen(self):
        events = []
        self.runtime._progress_sink = events.append
        self.runtime._director_progress_state = {
            "total": 0, "resolved": 0, "fraction": 0.0,
        }
        self.runtime.director_progress(10, 8, 1, "首次复检")
        self.runtime.director_progress(10, 5, 2, "确认三个问题复发")
        self.runtime.director_progress(10, 10, 3, "全部复检通过")
        self.assertEqual(
            [(10, 8, 2), (10, 5, 5), (10, 10, 0)],
            [(row["total"], row["resolved"], row["remaining"])
             for row in events])
        self.assertEqual([0.8, 0.5, 1.0], [row["fraction"] for row in events])

    def test_director_progress_ui_displays_current_ratio_and_reopened_issues(self):
        from types import SimpleNamespace
        import ui_main_v2

        view = SimpleNamespace(
            status=mock.Mock(), _cut_progress_fraction=0.8,
            _cancel_cut=lambda: None)
        ui_main_v2.MainWindowV2._on_cut_progress(view, {
            "stage": "director", "total": 10, "resolved": 5,
            "remaining": 5, "fraction": 0.5, "round": 24,
            "reopened": 3, "message": "重新复听问题位置",
        })
        displayed = view.status.working.call_args.args
        self.assertEqual(0.5, displayed[3])
        self.assertEqual(0.5, view._cut_progress_fraction)
        self.assertIn("已解决 5 个", displayed[2])
        self.assertIn("剩余 5 个", displayed[2])
        self.assertIn("第 24 次局部修复", displayed[2])
        self.assertIn("复发 3 个", displayed[2])

    def test_audio_rework_only_retries_the_issue_still_present(self):
        def report(span_index, end, suggested):
            return {
                "actual_audio_inspected": True, "score": 65.0,
                "review_window_id": "review_%d" % span_index,
                "span_index": span_index, "review_kind": "cut_end",
                "boundary_time": end, "track_id": "0",
                "findings": [{
                    "id": "tail_%d" % span_index,
                    "type": "speech_cut_end", "modality": "audio",
                    "severity": "critical", "veto": True,
                    "confidence": 0.95, "action": "expand_end",
                    "message": "尾音需要保护", "evidence_refs": ["audio:0"],
                    "suggested_interval": [end - 0.24, suggested],
                }],
            }

        events = []
        self.runtime._progress_sink = events.append
        self.runtime.director_conf["director_audio_rework_rounds"] = 3
        self.runtime.audio_review = mock.Mock(side_effect=[
            ([], [report(0, 2.0, 2.2), report(1, 4.0, 4.2)]),
            ([], [report(1, 4.2, 4.4)]),
            ([], []),
        ])
        with mock.patch.object(
                self.runtime, "apply_boundary_findings",
                wraps=self.runtime.apply_boundary_findings) as apply:
            result = self.runtime.direct_audio_rework(
                self.data, [(1.0, 2.0), (3.0, 4.0)], [])
        self.assertEqual(2, apply.call_count)
        self.assertEqual(
            ["tail_0", "tail_1"],
            [finding["id"] for row in apply.call_args_list[0].args[1]
             for finding in row["findings"]])
        self.assertEqual(
            ["tail_1"],
            [finding["id"] for row in apply.call_args_list[1].args[1]
             for finding in row["findings"]])
        self.assertAlmostEqual(2.2, result[0][0][1], places=5)
        self.assertAlmostEqual(4.4, result[0][1][1], places=5)

    def test_transition_defensively_rejects_unapproved_review(self):
        self.runtime._strict_director = mock.Mock(return_value=True)
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0]], "conf": {},
            "director_multimodal_audit": {
                "status": "needs_rework", "passed": False,
                "scores": {"final": 70.0}, "veto_count": 1, "major_issues": 1,
            },
        }
        with self.assertRaises(autopilot.DirectorReviewFailed):
            self.runtime.transition(self._context(
                "transition", {"director": workflow_engine.StageResult(
                    "TASK-MM.REVIEW-V3", previous)}))

    def test_degraded_director_keeps_failed_audit_and_unresolved_findings(self):
        self.runtime._strict_director = mock.Mock(return_value=False)
        audit = {"status": "needs_rework", "passed": False,
                 "scores": {"final": 70}, "veto_count": 1, "major_issues": 1,
                 "findings": [{"id": "unresolved-cut", "veto": True}]}
        self.assertFalse(self.runtime.require_director_approval({
            "spans": [[1.0, 2.0]], "director_multimodal_audit": audit}))
        self.assertFalse(audit["passed"])
        self.assertEqual("unresolved-cut", audit["findings"][0]["id"])
        self.assertTrue(self.runtime._director_degraded)

    def test_degraded_render_keeps_failure_report_without_rewarding_failure(self):
        self.runtime._strict_director = mock.Mock(return_value=False)
        self.runtime.director_conf.update({
            "rendered_review_enabled": True, "seam_review_enabled": False,
            "paragraph_review_enabled": False,
        })
        self.runtime.use_bgm = False
        plan = edit_plan.extend_plan(self._plan(), final_render_spans=[(1.0, 2.0)])
        previous = {
            "index_dir": self.idx, "spans": [[1.0, 2.0]], "seams": [],
            "conf": {"render": {}}, "logs": [], "plan": plan,
            "plan_sha256": plan["plan_sha256"], "task_seed": plan["task_seed"],
            "director_multimodal_audit": {"status": "passed", "passed": True},
        }

        def fake_render(_source, _spans, _seams, output, _conf, **_kwargs):
            with open(output, "wb") as handle:
                handle.write(b"draft-video")
            return {"minutes": 0.02, "timeline": [(0.0, 1.0, 1.0)]}

        failed = {"passed": False, "score": 40.0, "veto_count": 1,
                  "major_issues": 0, "findings": [{"id": "failed-word-edge",
                      "modality": "audio", "veto": True, "severity": "critical"}]}
        reinforcement = mock.Mock(return_value={
            "status": "penalized", "rewarded": 0, "penalized": 1})
        self.runtime.reinforce_director_learning = reinforcement
        self.runtime.record_director_rework = mock.Mock(return_value=1)
        with mock.patch.object(autopilot.autocut, "load_index", return_value=self.data), \
                mock.patch.object(autopilot.autocut, "render", side_effect=fake_render), \
                mock.patch("rendered_output_qc.review_rendered_output", return_value=failed):
            result = self.runtime.renderer(self._context(
                "renderer", {"transition": workflow_engine.StageResult(
                    "TASK-MM.TRANSITION-V2", previous)}))
        self.assertTrue(os.path.isfile(result.payload["out"]))
        reinforcement.assert_called_once()
        self.assertFalse(reinforcement.call_args.args[2])
        with open(result.payload["rendered_output_qc_path"], encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertFalse(report["passed"])
        self.assertFalse(report["delivery_blocked"])
        self.assertTrue(report["degraded_delivered"])
        self.assertEqual("pre_bgm", report["failure_phase"])
        self.assertEqual("penalized", report["learning_reinforcement"]["status"])
        self.assertEqual("failed-word-edge", report["findings"][0]["id"])
        self.assertEqual(0, result.payload["learning_reinforcement"]["rewarded"])


if __name__ == "__main__":
    unittest.main()
