# -*- coding: utf-8 -*-
"""Regression tests for auditable licence supplements and native gaps."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from build_tools import generate_compliance


class ComplianceOverrideTests(unittest.TestCase):
    def test_default_distribution_scope_excludes_unverified_binary_bundle(self):
        scope = generate_compliance.DEFAULT_DISTRIBUTION_SCOPE
        self.assertEqual("source-bootstrap", scope["mode"])
        self.assertIn("智能剪辑助手.exe", scope["excluded"])
        self.assertIn("_internal/", scope["excluded"])
        self.assertIn("complete=true", scope["offline_binary_release_gate"])

    def test_version_pinned_override_requires_matching_file_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            licences = root / "LICENSES"
            text = licences / "supplemental" / "demo" / "LICENSE"
            text.parent.mkdir(parents=True)
            text.write_text("official text\n", encoding="utf-8")
            relative = text.relative_to(root).as_posix()
            digest = hashlib.sha256(text.read_bytes()).hexdigest()
            (licences / "python_overrides.json").write_text(json.dumps({
                "components": [{
                    "name": "Demo_Package",
                    "version": "1.2.3",
                    "source_url": "https://example.invalid/demo-1.2.3.tar.gz",
                    "source_sha256": "a" * 64,
                    "license_files": [relative],
                    "file_sha256": {relative: digest},
                }],
            }), encoding="utf-8")
            missing = []
            rows = generate_compliance.load_python_overrides(
                root, licences, missing)
            self.assertFalse(missing)
            self.assertIn(("demo-package", "1.2.3"), rows)

            text.write_text("tampered\n", encoding="utf-8")
            missing = []
            rows = generate_compliance.load_python_overrides(
                root, licences, missing)
            self.assertFalse(rows)
            self.assertIn("SHA256", missing[0]["reason"])

    def test_incomplete_native_component_remains_explicit_without_fake_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            licences = root / "LICENSES"
            licences.mkdir()
            (licences / "manual_components.json").write_text(json.dumps({
                "components": [{
                    "name": "Unresolved native runtime",
                    "version": "build-dependent",
                    "complete": False,
                    "reason": "exact redistributable terms pending",
                    "license_files": [],
                }],
            }), encoding="utf-8")
            missing = []
            rows = generate_compliance.load_manual_components(
                root, licences, missing)
            self.assertEqual(1, len(rows))
            self.assertEqual([], rows[0]["license_files"])
            self.assertEqual("exact redistributable terms pending",
                             missing[0]["reason"])


if __name__ == "__main__":
    unittest.main()
