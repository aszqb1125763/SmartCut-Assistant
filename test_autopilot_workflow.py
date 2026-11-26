# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

import autocut
import autopilot
import workflow_engine


def fake_handlers(calls, fail_at=None):
    handlers = {}
    for stage in workflow_engine.STAGE_ORDER:
        def run(ctx, stage=stage):
            calls.append(stage)
            if stage == fail_at:
                raise ValueError("bad stage")
            payload = {"stage": stage}
            if stage == "analyze":
                payload["index_dir"] = "cached-index"
            if stage == "finalize":
                payload.update({"out": "result.mp4", "task_id": ctx.task.task_id})
            return workflow_engine.StageResult(
                "%s.%s" % (ctx.task.task_id, stage.upper()), payload)
        handlers[stage] = run
    return handlers


class AutopilotWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "source.mp4")
        with open(self.src, "wb") as fh:
            fh.write(b"test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_auto_keeps_run_one_result_contract(self):
        calls = []
        result = autopilot.run_one(
            self.src, output_dir=self.tmp.name, task_id="TASK-FULL",
            stage_handlers=fake_handlers(calls), on_log=lambda _m: None)
        self.assertEqual(list(workflow_engine.STAGE_ORDER), calls)
        self.assertEqual("result.mp4", result["out"])
        self.assertEqual("TASK-FULL", result["task_id"])

    def test_analysis_only_stops_before_story_and_render(self):
        calls = []
        result = autopilot.run_one(
            self.src, output_dir=self.tmp.name, task_id="TASK-ANALYZE",
            workflow=workflow_engine.ANALYSIS_ONLY,
            stage_handlers=fake_handlers(calls), on_log=lambda _m: None)
        self.assertEqual(["analyze"], calls)
        self.assertEqual(workflow_engine.ANALYSIS_ONLY, result["workflow"])
        self.assertEqual("cached-index", result["index_dir"])

    def test_premiere_edit_stops_after_director_timeline_is_final(self):
        calls = []
        handlers = fake_handlers(calls)

        def transition(ctx):
            calls.append("transition")
            return workflow_engine.StageResult(
                "TASK-PR.TRANSITION",
                {"index_dir": "cached-index", "spans": [[1.0, 4.0]],
                 "plan": {"final_render_spans": [[1.25, 3.75]]},
                 "director_multimodal_audit": {"passed": True}})

        handlers["transition"] = transition
        result = autopilot.run_one(
            self.src, output_dir=self.tmp.name, task_id="TASK-PR",
            workflow=workflow_engine.PREMIERE_EDIT,
            stage_handlers=handlers, on_log=lambda _m: None)
        self.assertEqual(list(workflow_engine.STAGE_ORDER[:6]), calls)
        self.assertEqual(workflow_engine.PREMIERE_EDIT, result["workflow"])
        self.assertEqual([[1.25, 3.75]], result["spans"])
        self.assertTrue(result["director_passed"])

    def test_cancel_maps_to_existing_render_cancelled_exception(self):
        with self.assertRaises(autocut.RenderCancelled):
            autopilot.run_one(
                self.src, output_dir=self.tmp.name, task_id="TASK-CANCEL",
                stage_handlers=fake_handlers([]), should_cancel=lambda: True,
                on_log=lambda _m: None)

    def test_resume_skips_completed_idempotent_roles(self):
        memory = workflow_engine.InMemoryWorkflowMemory()
        signature = autopilot._run_signature(
            self.src, os.path.join(self.tmp.name, "source_索引"),
            "gameplay", "medium", None, True, False, None,
            workflow_engine.FULL_AUTO)
        memory.record_stage_state(
            "TASK-RESUME", "analyze", "completed",
            output_artifact_id="TASK-RESUME.INDEX-V1",
            payload={"index_dir": "cached-index"},
            metadata={"run_signature": signature})
        memory.record_stage_state(
            "TASK-RESUME", "writer", "completed",
            output_artifact_id="TASK-RESUME.SCRIPT-V2", payload={},
            metadata={"run_signature": signature})
        calls = []
        result = autopilot.run_one(
            self.src, output_dir=self.tmp.name, task_id="TASK-RESUME",
            memory=memory, resume=True, stage_handlers=fake_handlers(calls),
            on_log=lambda _m: None)
        self.assertEqual(list(workflow_engine.STAGE_ORDER[2:]), calls)
        self.assertEqual("result.mp4", result["out"])

    def test_stage_failure_reports_role_name(self):
        with self.assertRaisesRegex(RuntimeError, "boundary.*bad stage"):
            autopilot.run_one(
                self.src, output_dir=self.tmp.name, task_id="TASK-FAIL",
                stage_handlers=fake_handlers([], fail_at="boundary"),
                on_log=lambda _m: None)

    def test_boundary_closure_freezes_waveform_snapped_revision(self):
        class FakeCache:
            @staticmethod
            def activity_intervals(track, start, end, kind="speech"):
                return [{"start": 1.12, "end": 1.86,
                         "confidence": 0.95, "track": str(track),
                         "kind": kind, "source": "energy"}]

        run = autopilot._WorkflowRun.__new__(autopilot._WorkflowRun)
        run.director_conf = {}
        run.should_cancel = None
        run._sink = lambda _message: None
        run.selected_tracks = lambda _data: [0]
        run.speech_tracks = lambda _data, _selected=None: [0]
        run.ensure_audio_cache = lambda _data: FakeCache()
        logs = []
        modifications = []
        spans, closure = run.close_timeline_boundaries(
            {"duration": 4.0, "fps": 25.0, "segments": []},
            [(1.0, 2.0)],
            [{"start": 1.15, "end": 1.80, "text": "一句话"}],
            logs, modifications)

        self.assertEqual([(1.08, 1.96)], spans)
        self.assertEqual(1, closure["waveform_snaps"])
        self.assertEqual(0, closure["breath_pads"])
        self.assertEqual(run.timeline_revision(spans),
                         closure["timeline_revision"])
        self.assertTrue(any(row.get("source") == "waveform_edge_boundary"
                            for row in modifications))

    def test_boundary_closure_keeps_revision_when_audio_cache_fails(self):
        run = autopilot._WorkflowRun.__new__(autopilot._WorkflowRun)
        run.director_conf = {}
        run.should_cancel = None
        run._sink = lambda _message: None
        run.selected_tracks = lambda _data: [0]
        run.speech_tracks = lambda _data, _selected=None: [0]

        def fail_cache(_data):
            raise RuntimeError("cache unavailable")

        run.ensure_audio_cache = fail_cache
        original = [(1.0, 2.0)]
        spans, closure = run.close_timeline_boundaries(
            {"duration": 4.0, "fps": 25.0, "segments": []},
            original, [], [], [])
        self.assertEqual(original, spans)
        self.assertEqual(run.timeline_revision(original),
                         closure["timeline_revision"])
        self.assertIn("cache unavailable", closure["waveform_error"])

    def test_boundary_closure_protects_word_before_sentence_decision(self):
        run = autopilot._WorkflowRun.__new__(autopilot._WorkflowRun)
        run.director_conf = {"waveform_edge_snap_enabled": False}
        run.should_cancel = None
        run._sink = lambda _message: None
        spans, closure = run.close_timeline_boundaries(
            {"duration": 4.0, "fps": 25.0,
             "segments": [(1.0, 2.0, "完整句子")]},
            [(1.25, 1.40)],
            [{"start": 1.05, "end": 1.45, "text": "短词"}],
            [], [])
        self.assertEqual([(1.0, 2.0)], spans)
        self.assertGreaterEqual(closure["word_snaps"], 1)

    def test_resume_after_director_failure_starts_from_director(self):
        memory = workflow_engine.InMemoryWorkflowMemory()
        first_calls = []
        with self.assertRaisesRegex(RuntimeError, "director.*bad stage"):
            autopilot.run_one(
                self.src, output_dir=self.tmp.name, task_id="TASK-DIRECTOR-RESUME",
                memory=memory, resume=True,
                stage_handlers=fake_handlers(first_calls, fail_at="director"),
                on_log=lambda _m: None)
        self.assertEqual(list(workflow_engine.STAGE_ORDER[:5]), first_calls)

        resumed_calls = []
        result = autopilot.run_one(
            self.src, output_dir=self.tmp.name, task_id="TASK-DIRECTOR-RESUME",
            memory=memory, resume=True,
            stage_handlers=fake_handlers(resumed_calls), on_log=lambda _m: None)
        self.assertEqual(list(workflow_engine.STAGE_ORDER[4:]), resumed_calls)
        self.assertEqual("TASK-DIRECTOR-RESUME", result["task_id"])


if __name__ == "__main__":
    unittest.main()
