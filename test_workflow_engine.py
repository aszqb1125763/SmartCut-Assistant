# -*- coding: utf-8 -*-
import threading
import time
import unittest

from workflow_engine import (
    ANALYSIS_ONLY,
    FULL_AUTO,
    STAGE_ORDER,
    InMemoryWorkflowMemory,
    SerialExecutor,
    StageResult,
    TaskSpec,
)


def handlers(calls, fail_at=None, cancel_at=None, cancel_flag=None):
    result = {}
    for stage in STAGE_ORDER:
        def run(ctx, stage=stage):
            calls.append(stage)
            if cancel_at == stage and cancel_flag is not None:
                cancel_flag[0] = True
                ctx.cancel_check()
            if fail_at == stage:
                raise RuntimeError("broken %s" % stage)
            return StageResult("%s-v1" % stage, {"stage": stage})
        result[stage] = run
    return result


class WorkflowEngineTests(unittest.TestCase):
    def test_full_auto_runs_role_graph_in_order(self):
        calls = []
        memory = InMemoryWorkflowMemory()
        result = SerialExecutor(handlers(calls), memory).run(
            TaskSpec("task-1", "source.mp4", FULL_AUTO))

        self.assertEqual("completed", result.status)
        self.assertEqual(list(STAGE_ORDER), calls)
        self.assertEqual(STAGE_ORDER, result.completed_stages)
        self.assertEqual("finalize-v1", result.outputs["finalize"].artifact_id)
        for stage in STAGE_ORDER:
            statuses = [e["status"] for e in memory.events if e["stage"] == stage]
            self.assertEqual(["started", "completed"], statuses)
        writer_started = next(e for e in memory.events
                              if e["stage"] == "writer" and e["status"] == "started")
        self.assertEqual(["analyze-v1"], writer_started["input_artifact_ids"])
        self.assertEqual(["analyze"], writer_started["job"]["dependencies"])
        self.assertIn("resources", writer_started["job"])

    def test_analysis_only_stops_after_neutral_analysis(self):
        calls = []
        result = SerialExecutor(handlers(calls)).run(
            TaskSpec("task-analysis", "source.mp4", ANALYSIS_ONLY))

        self.assertEqual("completed", result.status)
        self.assertEqual(["analyze"], calls)
        self.assertEqual(("analyze",), result.completed_stages)

    def test_failure_blocks_every_downstream_stage(self):
        calls = []
        memory = InMemoryWorkflowMemory()
        result = SerialExecutor(handlers(calls, fail_at="boundary"), memory).run(
            TaskSpec("task-fail", "source.mp4"))

        self.assertEqual("failed", result.status)
        self.assertEqual("boundary", result.current_stage)
        self.assertEqual(["analyze", "writer", "editor", "boundary"], calls)
        self.assertEqual(("analyze", "writer", "editor"), result.completed_stages)
        self.assertNotIn("director", calls)
        self.assertEqual("failed", memory.get_stage_state(
            "task-fail", "boundary")["status"])

    def test_cancel_records_current_role_and_stops(self):
        calls = []
        cancel_flag = [False]
        memory = InMemoryWorkflowMemory()
        result = SerialExecutor(
            handlers(calls, cancel_at="editor", cancel_flag=cancel_flag),
            memory,
        ).run(TaskSpec("task-cancel", "source.mp4"),
              should_cancel=lambda: cancel_flag[0])

        self.assertEqual("cancelled", result.status)
        self.assertEqual("editor", result.current_stage)
        self.assertEqual(["analyze", "writer", "editor"], calls)
        self.assertEqual(("analyze", "writer"), result.completed_stages)
        self.assertEqual("cancelled", memory.get_stage_state(
            "task-cancel", "editor")["status"])

    def test_resume_skips_completed_idempotent_nodes(self):
        memory = InMemoryWorkflowMemory()
        memory.record_stage_state(
            "task-resume", "analyze", "completed",
            output_artifact_id="index-v7", payload={"cached": True})
        memory.record_stage_state(
            "task-resume", "writer", "completed",
            output_artifact_id="script-v3", payload={"cached": True})
        calls = []
        result = SerialExecutor(handlers(calls), memory).run(
            TaskSpec("task-resume", "source.mp4"))

        self.assertEqual("completed", result.status)
        self.assertEqual(list(STAGE_ORDER[2:]), calls)
        self.assertEqual("index-v7", result.outputs["analyze"].artifact_id)
        self.assertEqual("script-v3", result.outputs["writer"].artifact_id)
        editor_started = next(e for e in memory.events
                              if e["stage"] == "editor" and e["status"] == "started")
        self.assertEqual(["script-v3"], editor_started["input_artifact_ids"])

    def test_changed_run_signature_invalidates_completed_stages(self):
        memory = InMemoryWorkflowMemory()
        calls = []

        def handler(ctx):
            calls.append((ctx.task.metadata.get("run_signature"), ctx.stage.name))
            return StageResult(payload={"stage": ctx.stage.name})

        stage_handlers = {name: handler for name in STAGE_ORDER}
        executor = SerialExecutor(stage_handlers, memory)
        first = TaskSpec("task-signature", "source.mp4",
                         metadata={"run_signature": "settings-a"})
        second = TaskSpec("task-signature", "source.mp4",
                          metadata={"run_signature": "settings-b"})
        self.assertEqual("completed", executor.run(first).status)
        calls.clear()
        self.assertEqual("completed", executor.run(first).status)
        self.assertEqual([], calls)
        self.assertEqual("completed", executor.run(second).status)
        self.assertEqual(len(STAGE_ORDER), len(calls))
        self.assertTrue(all(signature == "settings-b" for signature, _stage in calls))

    def test_validation_rejects_invalid_workflow_and_level(self):
        with self.assertRaises(ValueError):
            TaskSpec("x", "source.mp4", "unknown")
        with self.assertRaises(ValueError):
            TaskSpec("x", "source.mp4", level="maximum")

    def test_executor_serializes_concurrent_tasks(self):
        gate = threading.Lock()
        counts = {"active": 0, "maximum": 0}

        def analyze(_ctx):
            with gate:
                counts["active"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
            time.sleep(0.03)
            with gate:
                counts["active"] -= 1
            return "index"

        executor = SerialExecutor({"analyze": analyze})
        threads = [
            threading.Thread(
                target=executor.run,
                args=(TaskSpec("parallel-%d" % i, "source.mp4", ANALYSIS_ONLY),),
            )
            for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, counts["maximum"])


if __name__ == "__main__":
    unittest.main()
