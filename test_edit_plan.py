# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest

import edit_plan


class EditPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source.mp4")
        with open(self.source, "wb") as fh:
            fh.write(b"source-bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        source = edit_plan.source_fingerprint(
            self.source, os.path.join(self.tmp.name, "cache"))
        return edit_plan.build_plan(
            source=source, mode="gameplay", level="medium",
            config={"render": {"fps": 30}, "threshold": 0.25},
            semantic_spans=[(1.1234567894, 4.0)],
            versions={"editor": "CUT-V2"})

    def test_same_input_has_same_plan_hash_and_seed(self):
        first = self._plan()
        second = self._plan()
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["task_seed"], second["task_seed"])

    def test_config_or_span_change_changes_identity(self):
        first = self._plan()
        changed = edit_plan.build_plan(
            source=first["source"], mode="gameplay", level="heavy",
            config={"render": {"fps": 30}}, semantic_spans=[(1.0, 4.0)])
        self.assertNotEqual(first["plan_sha256"], changed["plan_sha256"])

    def test_extend_preserves_history_fields_and_is_deterministic(self):
        first = self._plan()
        a = edit_plan.extend_plan(first, refined_spans=[(1.0, 4.2)])
        b = edit_plan.extend_plan(first, refined_spans=[(1.0, 4.2)])
        self.assertEqual(a, b)
        self.assertEqual(first["semantic_spans"], a["semantic_spans"])
        self.assertNotEqual(first["plan_sha256"], a["plan_sha256"])

    def test_artifact_is_content_addressed_and_not_rewritten(self):
        plan = self._plan()
        first = edit_plan.write_artifact(self.tmp.name, "editor", plan, "TASK-A")
        before = os.stat(first).st_mtime_ns
        second = edit_plan.write_artifact(self.tmp.name, "editor", plan, "TASK-B")
        self.assertEqual(first, second)
        self.assertEqual(before, os.stat(first).st_mtime_ns)
        with open(first, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(plan["plan_sha256"], saved["plan"]["plan_sha256"])

    def test_source_hash_cache_invalidates_when_file_changes(self):
        cache = os.path.join(self.tmp.name, "cache")
        first = edit_plan.source_fingerprint(self.source, cache)
        with open(self.source, "ab") as fh:
            fh.write(b"changed")
        second = edit_plan.source_fingerprint(self.source, cache)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["size"], second["size"])


if __name__ == "__main__":
    unittest.main()
