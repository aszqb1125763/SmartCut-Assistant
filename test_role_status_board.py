# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ui_role_status import RoleStatusBoard, learning_view, role_view


class _FakeStore:
    def __init__(self):
        self.closed = False
        self.task = {
            "task_id": "TASK-status-test",
            "status": "running",
            "current_stage": "director",
            "updated_at": "2026-11-22T10:00:00Z",
        }
        self.states = {
            "director": {
                "status": "running", "progress": 0.625,
                "output_artifact_id": "REVIEW-V2",
            },
            "editor": {
                "status": "failed", "progress": 0.5,
                "error_text": "镜头边界不完整",
            },
        }

    def get_task(self, task_id):
        return self.task if task_id == self.task["task_id"] else None

    def list_unfinished_tasks(self):
        return [self.task]

    def role_state(self, task_id, role):
        return self.states.get(role, {"status": "pending", "progress": 0.0})

    def close(self):
        self.closed = True


class RoleStatusBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _board(self, enabled=True, auto_start=True):
        parent = QWidget()
        store = _FakeStore()
        board = RoleStatusBoard(
            parent,
            store_factory=lambda config: store,
            config_loader=lambda: {"enabled": enabled},
            poll_ms=100,
            auto_start=auto_start,
        )
        return parent, board, store

    def test_embedded_board_is_not_a_window(self):
        parent, board, _ = self._board()
        try:
            self.assertFalse(board.isWindow())
        finally:
            board.shutdown()
            parent.deleteLater()

    def test_status_mapping_and_task_memory_rendering(self):
        parent, board, _ = self._board(auto_start=False)
        try:
            mapped = role_view("director", {"status": "running", "progress": 0.625})
            self.assertEqual("工作中", mapped["status_label"])
            self.assertEqual("63%", mapped["progress_text"])
            board.bind_task("TASK-status-test")
            self.assertIn("导演", board.summary.text())
            self.assertEqual("工作中", board.rows["director"].status.text())
            self.assertIn("错误：镜头边界不完整", board.rows["editor"].detail.text())
        finally:
            board.shutdown()
            parent.deleteLater()

    def test_timer_starts_and_shutdown_closes_store(self):
        parent, board, store = self._board(auto_start=True)
        self.assertTrue(board.timer.isActive())
        board.shutdown()
        self.assertFalse(board.timer.isActive())
        self.assertTrue(store.closed)
        parent.deleteLater()

    def test_explicit_task_binding_survives_creation_race(self):
        parent, board, _ = self._board(auto_start=False)
        try:
            board.bind_task("TASK-not-created-yet")
            self.assertEqual("TASK-not-created-yet", board.task_id)
            self.assertEqual("等待任务状态建立…", board.summary.text())
        finally:
            board.shutdown()
            parent.deleteLater()

    def test_disabled_memory_degrades_without_opening_store(self):
        parent = QWidget()
        calls = []
        board = RoleStatusBoard(
            parent,
            store_factory=lambda config: calls.append(config),
            config_loader=lambda: {"enabled": False},
            auto_start=False,
        )
        try:
            board.refresh()
            self.assertEqual([], calls)
            self.assertEqual("任务记忆未启用", board.summary.text())
        finally:
            board.shutdown()
            parent.deleteLater()

    def test_learning_view_reads_director_debrief(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "director-plan.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "evidence": {"director": {"debrief": {
                        "issues_discovered": 5,
                        "issues_resolved": 4,
                        "remaining_serious": 1,
                        "lessons": [
                            {"result": "candidate", "rule": "扩边后复听"},
                            {"result": "task_only", "rule": "本次加转场"},
                        ],
                    }}},
                }, handle, ensure_ascii=False)
            learned = learning_view(path)
        self.assertTrue(learned["visible"])
        self.assertIn("发现 5", learned["text"])
        self.assertIn("候选经验 1", learned["text"])
        self.assertEqual(2, learned["lessons"])


if __name__ == "__main__":
    unittest.main()
