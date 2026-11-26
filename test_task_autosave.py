# -*- coding: utf-8 -*-
import os
import tempfile
import time
import unittest
from unittest import mock

import task_memory
from task_autosave import AutosaveError, TaskAutosave


class TaskAutosaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def _backend_with_task(self, task_id="TASK-autosave-test"):
        backend = task_memory.TaskMemory(root=self.root)
        backend.create_task(
            {"sources": ["sample.mp4"]}, task_id=task_id,
            mode="gameplay", level="medium")
        return backend, task_id

    @staticmethod
    def _events_of_type(backend, task_id, event_type):
        return [row for row in backend.events(task_id)
                if row.get("event_type") == event_type]

    def test_batches_multiple_operations_into_one_database_write(self):
        backend, task_id = self._backend_with_task()
        original = backend.apply_buffered_operations
        with mock.patch.object(
                backend, "apply_buffered_operations", wraps=original) as apply_mock:
            autosave = TaskAutosave(
                backend, journal_flush_ms=5000, checkpoint_seconds=3600)
            autosave.append_event(task_id, "progress.tick", {"value": 1})
            autosave.append_event(task_id, "progress.tick", {"value": 2})
            autosave.set_task_status(task_id, "running", current_stage="analyze")
            autosave.flush()
            stats = autosave.stats()
            self.assertEqual(1, apply_mock.call_count)
            self.assertEqual(3, stats["spool_appends"])
            self.assertEqual(1, stats["batches"])
            self.assertEqual(3, stats["operations_applied"])
            self.assertEqual(0, stats["pending"])
            autosave.close()
        backend.close()

    def test_terminal_stage_state_flushes_immediately(self):
        backend, task_id = self._backend_with_task()
        autosave = TaskAutosave(
            backend, journal_flush_ms=10000, checkpoint_seconds=3600)
        autosave.record_stage_state(
            task_id, "analyze", "completed",
            output_artifact_id="TASK-autosave-test.INDEX-V2",
            payload={"index_dir": "index"}, metadata={"neutral": True})
        state = backend.get_stage_state(task_id, "analyze")
        self.assertIsNotNone(state)
        self.assertEqual("completed", state["status"])
        self.assertEqual(0, autosave.stats()["pending"])
        autosave.close()
        backend.close()

    def test_unclean_shutdown_replays_disk_spool(self):
        backend, task_id = self._backend_with_task()
        first = TaskAutosave(
            backend, journal_flush_ms=10000, checkpoint_seconds=3600,
            start=False)
        first.append_event(task_id, "recover.me", {"step": 1})
        self.assertTrue(os.path.getsize(first.spool_path) > 0)
        backend.close()  # simulate process loss before the writer starts

        reopened = task_memory.TaskMemory(root=self.root)
        second = TaskAutosave(
            reopened, journal_flush_ms=100, checkpoint_seconds=3600)
        self.assertEqual(0, second.replay_pending())
        self.assertEqual(1, len(self._events_of_type(reopened, task_id, "recover.me")))
        self.assertGreaterEqual(second.stats()["replayed"], 1)
        second.close()
        reopened.close()

    def test_replay_is_idempotent_after_database_commit_before_spool_cleanup(self):
        backend, task_id = self._backend_with_task()
        first = TaskAutosave(
            backend, journal_flush_ms=10000, checkpoint_seconds=3600,
            start=False)
        first.append_event(task_id, "commit.then.crash", {"value": 7})
        operation = list(first._pending)
        report = backend.apply_buffered_operations(operation)
        self.assertEqual(1, len(report["applied"]))
        backend.flush()
        backend.close()  # spool still contains the already committed operation

        reopened = task_memory.TaskMemory(root=self.root)
        second = TaskAutosave(
            reopened, journal_flush_ms=100, checkpoint_seconds=3600)
        second.replay_pending()
        rows = self._events_of_type(reopened, task_id, "commit.then.crash")
        self.assertEqual(1, len(rows))
        self.assertEqual(1, second.stats()["operations_skipped"])
        second.close()
        reopened.close()

    def test_periodic_checkpoint_captures_latest_role_state(self):
        backend, task_id = self._backend_with_task()
        autosave = TaskAutosave(
            backend, journal_flush_ms=100, checkpoint_seconds=3600)
        autosave.record_stage_state(task_id, "writer", "started", job={})
        autosave.flush()
        autosave._save_periodic_checkpoints(force=True)
        checkpoint = backend.latest_checkpoint(task_id, "writer")
        self.assertIsNotNone(checkpoint)
        self.assertEqual("started", checkpoint["state"]["autosave"]["status"])
        self.assertEqual(1, autosave.stats()["checkpoints"])
        autosave.close()
        backend.close()

    def test_database_error_is_queryable_and_close_stops_writer(self):
        backend, task_id = self._backend_with_task()
        autosave = TaskAutosave(
            backend, journal_flush_ms=5000, checkpoint_seconds=3600)
        autosave.append_event(task_id, "will.fail", {})
        with mock.patch.object(
                backend, "apply_buffered_operations",
                side_effect=OSError("simulated disk failure")):
            with self.assertRaises(AutosaveError):
                autosave.flush(timeout=2)
            error = autosave.last_error()
            self.assertEqual("database_batch", error["phase"])
            self.assertIn("simulated disk failure", error["message"])

        # Once storage is available again the same spooled operation can finish.
        autosave.flush(timeout=2)
        autosave.close()
        self.assertFalse(autosave.stats()["writer_alive"])
        backend.close()

    def test_task_memory_operation_marker_is_transactionally_idempotent(self):
        backend, task_id = self._backend_with_task()
        operation = {
            "operation_id": "ASOP-fixed-idempotency-test",
            "method": "append_event",
            "args": [task_id, "once.only"],
            "kwargs": {"payload": {"ok": True}},
        }
        first = backend.apply_buffered_operations([operation])
        second = backend.apply_buffered_operations([operation])
        self.assertEqual([operation["operation_id"]], first["applied"])
        self.assertEqual([operation["operation_id"]], second["skipped"])
        self.assertEqual(1, len(self._events_of_type(backend, task_id, "once.only")))
        backend.close()


if __name__ == "__main__":
    unittest.main()
