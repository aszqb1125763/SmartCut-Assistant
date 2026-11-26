# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import memory_config
from task_memory import ROLES, TaskMemory, migrate_root


class MemoryConfigTests(unittest.TestCase):
    def test_smartcut_home_and_default_roots_are_isolated(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": first}, clear=False):
                self.assertEqual(memory_config.app_home(), os.path.abspath(first))
                root1 = memory_config.resolve_root()
                self.assertTrue(root1.startswith(os.path.abspath(first)))
                self.assertTrue(root1.endswith(os.path.join("工作目录", "导演记忆")))
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": second}, clear=False):
                root2 = memory_config.resolve_root()
            self.assertNotEqual(root1, root2)

    def test_config_merge_preserves_unknown_keys_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as home:
            path = memory_config.config_path(home)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"future_key": {"keep": True}, "checkpoint_seconds": 90}, fh)
            saved = memory_config.save_config(
                {"journal_flush_ms": 25, "enabled": False}, path=path)
            self.assertEqual(saved["future_key"], {"keep": True})
            self.assertEqual(saved["journal_flush_ms"], 100)
            self.assertFalse(saved["enabled"])
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk, saved)
            leftovers = [p for p in os.listdir(os.path.dirname(path))
                         if p.startswith(".director_memory.")]
            self.assertEqual(leftovers, [])

    def test_rejects_build_and_internal_memory_roots(self):
        with tempfile.TemporaryDirectory() as home:
            for bad in (os.path.join(home, "build", "memory"),
                        os.path.join(home, "_internal", "memory"),
                        os.path.join(home, "dist", "memory")):
                with self.assertRaises(ValueError):
                    memory_config.resolve_root({"root": bad}, home=home)


class TaskMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.store = TaskMemory(root=self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_schema_task_roles_artifacts_and_recovery_data(self):
        expected = {
            "tasks", "role_status", "jobs", "artifacts", "handoffs",
            "rework_orders", "events", "checkpoints",
        }
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertTrue(expected.issubset(names))
        self.assertEqual(mode.lower(), "wal")

        task_id = self.store.create_task(
            {"sources": ["F:/video/a.mp4"]}, mode="gameplay", level="medium")
        task = self.store.get_task(task_id)
        self.assertEqual(task["source_manifest"], {"sources": ["F:/video/a.mp4"]})
        self.assertEqual(task["status"], "pending")
        self.assertTrue(Path(self.store.task_dir(task_id), "roles", "director").is_dir())
        for role in ROLES:
            self.assertEqual(self.store.role_state(task_id, role)["status"], "pending")

        self.store.set_task_status(task_id, "running", "writer")
        self.store.update_role_status(
            task_id, "writer", "running", progress=0.25, current_unit="chapter-1")
        self.assertEqual(self.store.role_state(task_id, "writer")["row_version"], 2)

        artifact_file = Path(self.store.task_dir(task_id), "artifacts", "script-v1.json")
        artifact_file.write_text('{"title":"test"}', encoding="utf-8")
        artifact_id = self.store.register_artifact(
            task_id, "writer", "script", str(artifact_file), activate=True)
        self.assertEqual(self.store.get_task(task_id)["active_script_id"], artifact_id)
        self.assertEqual(len(self.store.get_artifact(artifact_id)["sha256"]), 64)

        checkpoint_id = self.store.save_checkpoint(
            task_id, "writer", {"chapter": 1}, artifact_id=artifact_id)
        checkpoint = self.store.latest_checkpoint(task_id, "writer")
        self.assertEqual(checkpoint["checkpoint_id"], checkpoint_id)
        self.assertEqual(checkpoint["state"], {"chapter": 1})
        self.assertTrue(os.path.isfile(checkpoint["path"]))

        job_id = self.store.enqueue_job(
            task_id, "editor", "chapter-1", dependencies=["writer"],
            input_artifact_id=artifact_id, resources={"cpu": 1})
        self.store.update_job(job_id, "completed", output_artifact_id=artifact_id,
                              increment_attempt=True)
        handoff = self.store.record_handoff(
            task_id, "writer", "editor", output_artifact_id=artifact_id,
            summary="script ready", required_actions=["initial cut"])
        self.assertTrue(handoff.startswith("HANDOFF-"))
        order = self.store.create_rework_order(
            task_id, "director", "editor", "extend", "sentence boundary",
            target_artifact_id=artifact_id, target_unit_id="chapter-1")
        self.store.resolve_rework_order(order, artifact_id)
        self.assertGreaterEqual(len(self.store.events(task_id)), 10)
        self.assertIn(task_id, {row["task_id"] for row in self.store.list_unfinished_tasks()})

    def test_workflow_memory_protocol_can_resume_completed_stage(self):
        task_id = self.store.create_task(
            {"source": "F:/video/game.mp4"}, mode="gameplay", level="heavy")
        self.store.record_stage_state(
            task_id, "writer", "started",
            input_artifact_ids=["ART-index"],
            job={"dependencies": ["analyze"], "resources": {"cpu": 1}},
        )
        self.store.record_stage_state(
            task_id, "writer", "completed",
            input_artifact_ids=["ART-index"], output_artifact_id="ART-script",
            payload={"sections": 6}, metadata={"model": "local"},
            job={"dependencies": ["analyze"], "resources": {"cpu": 1}},
        )
        state = self.store.get_stage_state(task_id, "writer")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["output_artifact_id"], "ART-script")
        self.assertEqual(state["payload"], {"sections": 6})
        self.assertEqual(state["metadata"], {"model": "local"})
        self.assertEqual(self.store.role_state(task_id, "writer")["status"], "completed")
        self.assertEqual(self.store.get_task(task_id)["current_stage"], "writer")

        self.store.close()
        self.store = TaskMemory(root=self.root)
        resumed = self.store.get_stage_state(task_id, "writer")
        self.assertEqual(resumed["output_artifact_id"], "ART-script")
        self.assertEqual(resumed["payload"], {"sections": 6})

    def test_resumed_director_revises_artifact_and_closes_rework_orders(self):
        task_id = self.store.create_task(
            {"source": "F:/video/game.mp4"}, mode="gameplay", level="medium")
        first = Path(self.store.task_dir(task_id), "artifacts", "review-v1.json")
        second = Path(self.store.task_dir(task_id), "artifacts", "review-v2.json")
        first.write_text('{"passed":false}', encoding="utf-8")
        second.write_text('{"passed":true}', encoding="utf-8")
        artifact_id = self.store.register_artifact(
            task_id, "director", "review", str(first),
            artifact_id=task_id + ".REVIEW-V2", activate=True)
        first_row = self.store.get_artifact(artifact_id)
        order_a = self.store.create_rework_order(
            task_id, "director", "boundary", "expand", "tail")
        order_b = self.store.create_rework_order(
            task_id, "director", "editor", "add_previous", "sentence")

        self.store.revise_artifact(
            artifact_id, str(second), sha256=None, activate=True)
        revised = self.store.get_artifact(artifact_id)
        self.assertEqual(first_row["version"] + 1, revised["version"])
        self.assertEqual(os.path.abspath(second), revised["path"])
        self.assertNotEqual(first_row["sha256"], revised["sha256"])
        self.assertEqual(2, len(self.store.list_rework_orders(task_id, "open")))
        self.assertEqual(2, self.store.resolve_open_rework_orders(task_id, artifact_id))
        self.assertEqual([], self.store.list_rework_orders(task_id, "open"))
        resolved = {row["order_id"]: row for row in
                    self.store.list_rework_orders(task_id, "resolved")}
        self.assertEqual(artifact_id, resolved[order_a]["resolved_by_artifact_id"])
        self.assertEqual(artifact_id, resolved[order_b]["resolved_by_artifact_id"])

    def test_serial_workflow_uses_sqlite_backend_and_resumes(self):
        from workflow_engine import SerialExecutor, StageResult, TaskSpec

        task_id = self.store.create_task(
            {"source": "F:/video/game.mp4"}, mode="gameplay", level="medium")
        calls = []

        def handler(ctx):
            calls.append(ctx.stage.name)
            return StageResult(
                artifact_id="ART-%s" % ctx.stage.name,
                payload={"stage": ctx.stage.name},
                metadata={"role": ctx.stage.role},
            )

        handlers = {name: handler for name in (
            "analyze", "writer", "editor", "boundary", "director",
            "transition", "renderer", "finalize")}
        executor = SerialExecutor(handlers, memory=self.store)
        result = executor.run(TaskSpec(task_id, "F:/video/game.mp4"))
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.store.get_task(task_id)["status"], "completed")
        self.assertEqual(len(calls), 8)
        calls.clear()
        resumed = executor.run(TaskSpec(task_id, "F:/video/game.mp4"))
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(calls, [])

    def test_batch_commit_and_unclean_shutdown_recovery(self):
        running = self.store.create_task({"source": "a.mp4"}, status="running")
        completed = self.store.create_task({"source": "b.mp4"}, status="completed")
        self.store.update_role_status(running, "analyst", "running", progress=0.5)
        with self.store.batch():
            self.store.append_event(running, "analysis.chunk", {"index": 1}, "analyst")
            self.store.append_event(running, "analysis.chunk", {"index": 2}, "analyst")
        changed = self.store.mark_running_interrupted()
        self.assertEqual(changed, [running])
        self.assertEqual(self.store.get_task(running)["status"], "interrupted")
        self.assertEqual(self.store.role_state(running, "analyst")["status"], "interrupted")
        unfinished = {row["task_id"] for row in self.store.list_unfinished_tasks()}
        self.assertIn(running, unfinished)
        self.assertNotIn(completed, unfinished)

    def test_validation_rejects_unknown_mode_role_and_progress(self):
        with self.assertRaises(ValueError):
            self.store.create_task({}, mode="not-a-mode")
        task_id = self.store.create_task({}, mode="general")
        with self.assertRaises(ValueError):
            self.store.update_role_status(task_id, "camera", "running")
        with self.assertRaises(ValueError):
            self.store.update_role_status(task_id, "editor", "running", progress=1.1)
        with self.assertRaises(ValueError):
            self.store.record_stage_state(task_id, "unknown", "started")

    def test_integrity_backup_and_atomic_root_migration(self):
        task_id = self.store.create_task({"source": "a.mp4"}, status="running")
        self.store.save_checkpoint(task_id, "analyst", {"offset": 12.5})
        check = self.store.integrity_check()
        self.assertTrue(check["ok"])
        with tempfile.TemporaryDirectory() as parent:
            backup = os.path.join(parent, "memory-backup")
            self.store.backup_to(backup)
            self.assertTrue(os.path.isfile(os.path.join(backup, "backup_manifest.json")))
            with TaskMemory(root=backup) as restored:
                self.assertEqual(restored.get_task(task_id)["status"], "running")
                self.assertEqual(restored.latest_checkpoint(task_id)["state"], {"offset": 12.5})
            nested = os.path.join(self.root, "nested-backup")
            with self.assertRaises(ValueError):
                self.store.backup_to(nested)

        self.store.close()
        with tempfile.TemporaryDirectory() as parent:
            migrated = os.path.join(parent, "migrated")
            migrate_root(self.root, migrated)
            with TaskMemory(root=migrated) as restored:
                self.assertIsNotNone(restored.get_task(task_id))
        self.store = TaskMemory(root=self.root)


if __name__ == "__main__":
    unittest.main()
