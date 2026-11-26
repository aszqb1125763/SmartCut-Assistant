# -*- coding: utf-8 -*-
import os
import sqlite3
import tempfile
import unittest

from director_memory_store import DirectorMemoryStore


PLAN_A = "a" * 64
PLAN_B = "b" * 64


class DirectorMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "candidate_promotion": "validated_only",
            "cross_mode_sharing": "facts_only",
        }
        self.store = DirectorMemoryStore(root=self.temp.name, config=self.config)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, **overrides):
        values = {
            "mode": "gameplay",
            "content_type": "game_recording",
            "role": "director",
            "category": "boundary_rule",
            "summary": "短首字前保留预卷",
            "content": {"lead_ms": 180},
            "confidence": 0.92,
            "evidence_refs": ["audio:0", "word:s1.w1"],
            "source_task_id": "TASK-001",
            "source_plan_sha256": PLAN_A,
            "scope_type": "project",
            "scope_key": "project-one",
            "passed": True,
            "auto_learning_eligible": True,
        }
        values.update(overrides)
        return self.store.record_observation(**values)

    def test_root_and_database_are_separate_from_task_memory(self):
        self.assertTrue(self.store.db_path.endswith(
            os.path.join("long_term", "director_memory.sqlite")))
        self.assertTrue(os.path.isfile(self.store.db_path))
        tables = {row[0] for row in self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("memories", tables)
        self.assertIn("memory_applications", tables)
        self.assertNotIn("tasks", tables)

    def test_dedupe_merges_evidence_and_validation_counts(self):
        memory_id = self.record()
        duplicate = self.record(
            confidence=0.80, evidence_refs=["visual:v1"],
            source_task_id="TASK-002", source_plan_sha256=PLAN_B,
            passed=False, auto_learning_eligible=False)
        self.assertEqual(memory_id, duplicate)
        row = self.store.get(memory_id)
        self.assertEqual(2, row["observation_count"])
        self.assertEqual(2, row["validation_count"])
        self.assertEqual(1, row["success_count"])
        self.assertEqual(1, row["failure_count"])
        self.assertEqual(["audio:0", "visual:v1", "word:s1.w1"],
                         row["evidence_refs"])
        self.assertEqual(2, len(self.store.sources(memory_id)))

    def test_auto_promotion_requires_passed_and_eligible(self):
        candidate = self.record(
            summary="证据不足不能激活", content={"rule": 1},
            passed=False, auto_learning_eligible=True)
        self.assertEqual("candidate", self.store.get(candidate)["lifecycle"])
        validated = self.record(
            summary="通过证据进入验证层", content={"rule": 2})
        self.assertEqual("validated", self.store.get(validated)["lifecycle"])
        active = self.record(
            summary="自动策略可激活", content={"rule": 3},
            promotion_policy="automatic")
        self.assertEqual("active", self.store.get(active)["lifecycle"])
        with self.assertRaises(PermissionError):
            self.store.transition_lifecycle(
                candidate, "validated", passed=True,
                auto_learning_eligible=False)
        self.store.transition_lifecycle(candidate, "validated", manual=True)
        self.assertEqual("validated", self.store.get(candidate)["lifecycle"])

    def test_replayed_observation_is_idempotent(self):
        memory_id = self.record(
            passed=None, auto_learning_eligible=False,
            promotion_policy="manual")
        replay = self.record(
            passed=None, auto_learning_eligible=False,
            promotion_policy="manual")
        self.assertEqual(memory_id, replay)
        row = self.store.get(memory_id)
        self.assertEqual(1, row["observation_count"])
        self.assertEqual(0, row["validation_count"])
        self.assertEqual(1, len(self.store.sources(memory_id)))

    def test_final_output_rewards_promote_only_after_independent_tasks(self):
        memory_id = self.record(
            passed=None, auto_learning_eligible=False,
            promotion_policy="manual")
        first = self.store.reinforce(
            [memory_id], source_task_id="TASK-R1", success=True,
            source_plan_sha256=PLAN_A, metadata={"audio_guard_sec": 0.06})[0]
        self.assertEqual("candidate", first["lifecycle"])
        second = self.store.reinforce(
            [memory_id], source_task_id="TASK-R2", success=True,
            source_plan_sha256=PLAN_B, metadata={"audio_guard_sec": 0.08})[0]
        self.assertEqual("validated", second["lifecycle"])
        third = self.store.reinforce(
            [memory_id], source_task_id="TASK-R3", success=True,
            source_plan_sha256="c" * 64)[0]
        self.assertEqual("active", third["lifecycle"])
        self.assertEqual(3, third["success_count"])
        self.assertEqual(3, len(self.store.applications(memory_id)))

    def test_reinforcement_is_idempotent_and_resume_can_replace_outcome(self):
        memory_id = self.record(
            passed=None, auto_learning_eligible=False,
            promotion_policy="manual")
        self.store.reinforce(
            [memory_id], source_task_id="TASK-R1", success=False,
            source_plan_sha256=PLAN_A)
        self.store.reinforce(
            [memory_id], source_task_id="TASK-R1", success=False,
            source_plan_sha256=PLAN_A)
        failed = self.store.get(memory_id)
        self.assertEqual(0, failed["success_count"])
        self.assertEqual(1, failed["failure_count"])
        self.store.reinforce(
            [memory_id], source_task_id="TASK-R1", success=True,
            source_plan_sha256=PLAN_A)
        resumed = self.store.get(memory_id)
        self.assertEqual(1, resumed["success_count"])
        self.assertEqual(0, resumed["failure_count"])
        self.assertEqual("success", self.store.applications(memory_id)[0]["outcome"])

    def test_repeated_failures_remove_bad_memory_from_queries(self):
        memory_id = self.record(promotion_policy="automatic")
        self.assertEqual("active", self.store.get(memory_id)["lifecycle"])
        self.store.reinforce(
            [memory_id], source_task_id="TASK-F1", success=False,
            source_plan_sha256=PLAN_A)
        self.store.reinforce(
            [memory_id], source_task_id="TASK-F2", success=False,
            source_plan_sha256=PLAN_B)
        row = self.store.get(memory_id)
        self.assertEqual("deprecated", row["lifecycle"])
        queried = self.store.query(
            mode="gameplay", content_type="game_recording",
            lifecycles=("active", "validated"), project_id="project-one")
        self.assertNotIn(memory_id, {item["memory_id"] for item in queried})

    def test_mode_isolation_and_controlled_cross_mode_sharing(self):
        style = self.record(
            category="style_habit", summary="游戏模式保留主播口头禅",
            content={"keep": "catchphrase"}, scope_type="global", scope_key="",
            promotion_policy="automatic")
        fact = self.record(
            category="fact", summary="语音首字不可截断",
            content={"rule": "protect_first_word"}, scope_type="global", scope_key="",
            promotion_policy="automatic")
        same_mode = self.store.query(
            mode="gameplay", content_type="game_recording",
            lifecycles=("active",), cross_mode_sharing="none")
        self.assertEqual({style, fact}, {row["memory_id"] for row in same_mode})
        other_none = self.store.query(
            mode="podcast", content_type="game_recording",
            lifecycles=("active",), cross_mode_sharing="none")
        self.assertEqual([], other_none)
        other_shared = self.store.query(
            mode="podcast", content_type="game_recording",
            lifecycles=("active",), cross_mode_sharing="facts_only")
        self.assertEqual([fact], [row["memory_id"] for row in other_shared])

    def test_project_game_global_scopes_do_not_leak(self):
        global_id = self.record(
            summary="全局规则", content={"scope": "global"},
            scope_type="global", scope_key="", promotion_policy="automatic")
        game_id = self.record(
            summary="某游戏规则", content={"scope": "game"},
            scope_type="game", scope_key="wormgate", promotion_policy="automatic")
        project_id = self.record(
            summary="某项目规则", content={"scope": "project"},
            scope_type="project", scope_key="episode-1", promotion_policy="automatic")
        rows = self.store.query(
            mode="gameplay", content_type="game_recording",
            lifecycles=("active",), game_id="wormgate", project_id="episode-1")
        self.assertEqual({global_id, game_id, project_id},
                         {row["memory_id"] for row in rows})
        isolated = self.store.query(
            mode="gameplay", content_type="game_recording",
            lifecycles=("active",), game_id="other", project_id="other")
        self.assertEqual({global_id}, {row["memory_id"] for row in isolated})

    def test_relations_connect_roles_without_merging_partitions(self):
        writer = self.record(
            role="writer", category="hard_rule", summary="剧本必须有结果",
            content={"stage": "result"}, scope_type="global", scope_key="",
            promotion_policy="automatic")
        boundary = self.record(
            role="boundary", category="boundary_rule", summary="结果句保留尾音",
            content={"tail_ms": 220}, scope_type="global", scope_key="",
            promotion_policy="automatic")
        self.store.add_relation(writer, boundary, "requires", weight=0.95,
                                metadata={"reason": "result completeness"})
        related = self.store.related(writer, incoming=False)
        self.assertEqual(1, len(related))
        self.assertEqual(boundary, related[0]["memory"]["memory_id"])
        self.assertEqual("boundary", related[0]["memory"]["role"])
        self.assertEqual("result completeness", related[0]["metadata"]["reason"])

    def test_transaction_rollback_integrity_and_consistent_backup(self):
        memory_id = self.record(promotion_policy="automatic")
        before = self.store.get(memory_id)["summary"]
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store._conn.execute(
                    "UPDATE memories SET summary='should rollback' WHERE memory_id=?",
                    (memory_id,))
                raise RuntimeError("simulated interruption")
        self.assertEqual(before, self.store.get(memory_id)["summary"])
        self.assertTrue(self.store.integrity_check()["ok"])
        backup = self.store.backup()
        self.assertTrue(os.path.isfile(backup))
        copy = sqlite3.connect(backup)
        try:
            self.assertEqual("ok", copy.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual(1, copy.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_id=?", (memory_id,)).fetchone()[0])
        finally:
            copy.close()


if __name__ == "__main__":
    unittest.main()
