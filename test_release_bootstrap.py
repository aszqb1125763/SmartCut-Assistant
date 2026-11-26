# -*- coding: utf-8 -*-
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import make_release


class ReleaseBootstrapTests(unittest.TestCase):
    def test_default_release_main_builds_current_source_without_binaries(self):
        source_root = Path(make_release.BASE)
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(make_release, "DIST", temp), \
                    mock.patch.object(sys, "argv", ["make_release.py"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, make_release.main())

            archive = Path(temp) / (
                "%s_v%s.zip" % (make_release.NAME, make_release.RELEASE_VERSION))
            self.assertTrue(archive.is_file())
            prefix = make_release.NAME + "/"
            with zipfile.ZipFile(archive) as package:
                names = set(package.namelist())
                for relative in (
                    "brains/__init__.py",
                    "brains/audio_brain.py",
                    "conf/decision.json",
                    "conf/coords_matrix.json",
                    "conf/spec_manifest.json",
                    "conf/editor_model.json",
                    "pr_bridge/jsx/host.jsx",
                    "pr_workflow.py",
                    "pr_engine.py",
                ):
                    member = prefix + relative
                    self.assertIn(member, names)
                    self.assertEqual((source_root / relative).read_bytes(),
                                     package.read(member), relative)
                binary_members = [
                    name for name in names
                    if os.path.splitext(name)[1].lower()
                    in (".exe", ".dll", ".pyd", ".so", ".dylib")
                ]
                self.assertEqual([], binary_members)

    def test_release_manifest_ships_one_click_pr_plugin_payload(self):
        for filename in ("安装并启动.bat", "安装到指定目录.bat", "启动SmartCut.bat",
                         "安装PR插件.bat", "install_pr_bridge.ps1"):
            self.assertIn(filename, make_release.PLAIN)
        for relative in (
            "brains/__init__.py",
            "brains/audio_brain.py",
            "pr_bridge/CSXS/manifest.xml",
            "pr_bridge/js/main.js",
            "pr_bridge/jsx/host.jsx",
            "docs/安装指南.md",
            "docs/Premiere插件.md",
        ):
            self.assertIn(relative, make_release.REQUIRED_FILES)

    def test_release_pins_runtime_and_excludes_retired_plugin_binary(self):
        root = Path(__file__).resolve().parent
        self.assertIn("constraints-runtime.txt", make_release.PLAIN)
        constraints = (root / "constraints-runtime.txt").read_text(encoding="utf-8")
        self.assertIn("faster-whisper==", constraints)
        self.assertIn("PySide6==", constraints)
        source = (root / "make_release.py").read_text(encoding="utf-8")
        self.assertIn('excluded_dirs={"bin"}', source)

    def test_source_bootstrap_does_not_require_binary_manifest_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "LICENSE").write_text("MIT license text " * 4, encoding="utf-8")
            (root / "requirements.lock").write_text("example==1.0\n", encoding="utf-8")
            (root / "SBOM.cdx.json").write_text(json.dumps({
                "bomFormat": "CycloneDX",
                "components": [{"name": "example", "version": "1.0"}],
            }), encoding="utf-8")
            (root / "LICENSES").mkdir()
            (root / "LICENSES" / "manifest.json").write_text(json.dumps({
                "complete": False,
                "components": [{"name": "example", "license_files": []}],
                "missing": ["binary redistribution material"],
            }), encoding="utf-8")

            self.assertEqual([], make_release.publication_metadata_issues(base=temp))
            binary_issues = make_release.publication_metadata_issues(
                base=temp, with_app_bundle=True)
            self.assertTrue(any("complete=true" in issue for issue in binary_issues))


if __name__ == "__main__":
    unittest.main()
