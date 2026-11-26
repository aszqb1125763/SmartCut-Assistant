# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import pr_bridge_installer as installer


ROOT = Path(__file__).resolve().parent


class PremiereInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        for filename, content in {
            "CSXS/manifest.xml": '<ExtensionManifest ExtensionBundleVersion="0.5.0" />',
            "index.html": "面板",
            "js/main.js": "var version = 5;",
            "js/CSInterface.js": "function CSInterface() {}",
            "jsx/host.jsx": "function smartcut() {}",
            "NOTICE.md": "作者署名",
        }.items():
            path = self.source / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.target_root = self.base / "custom CEP" / "extensions"

    def install(self, **kwargs):
        return installer.install_extension(
            self.source, self.target_root, configure_debug=False, **kwargs)

    def make_old_install(self):
        target = self.target_root / installer.EXTENSION_ID
        shutil.copytree(self.source, target)
        (target / "js" / "main.js").write_text("old plugin", encoding="utf-8")
        return target

    def test_discovers_user_and_both_system_cep_roots(self):
        rows = installer.discover_targets({
            "APPDATA": str(self.base / "roaming"),
            "CommonProgramFiles": str(self.base / "common"),
            "CommonProgramFiles(x86)": str(self.base / "common x86"),
        })
        self.assertEqual(3, len(rows))
        self.assertEqual(self.base / "roaming" / "Adobe" / "CEP" / "extensions",
                         rows[0].root)
        self.assertFalse(rows[0].system)
        self.assertTrue(rows[1].system)
        self.assertTrue(rows[2].system)

    def test_discovers_multiple_premiere_versions_without_duplicates(self):
        program_files = self.base / "program files"
        for name in ("Adobe Premiere Pro 2020", "Adobe Premiere Pro 2024",
                     "Adobe Premiere Pro 2026", "Adobe Photoshop 2026"):
            (program_files / "Adobe" / name).mkdir(parents=True)
        rows = installer.discover_premiere(program_roots=[program_files, program_files])
        self.assertEqual(["Adobe Premiere Pro 2020", "Adobe Premiere Pro 2024",
                          "Adobe Premiere Pro 2026"], [row.name for row in rows])

    def test_custom_target_is_installed_and_verified(self):
        result = self.install()
        self.assertEqual(self.target_root / installer.EXTENSION_ID, result.target)
        self.assertIsNone(result.backup)
        self.assertEqual("0.5.0", result.version)
        self.assertEqual(6, result.file_count)
        self.assertEqual(installer.tree_hashes(self.source), installer.tree_hashes(result.target))
        info = installer.inspect_target(self.target_root, self.source)
        self.assertTrue(info["installed"])
        self.assertTrue(info["matches"])
        self.assertEqual("0.5.0", info["version"])

    def test_premiere_custom_registry_location_is_included_once(self):
        folder = self.base / "custom adobe" / "Adobe Premiere Pro 2026"
        folder.mkdir(parents=True)
        row = installer.PremiereInstall("Adobe Premiere Pro 2026", folder)
        found = installer.discover_premiere(program_roots=[], registered=[row, row])
        self.assertEqual([row], found)

    def test_updates_only_target_plugin_and_preserves_old_files_in_backup(self):
        target = self.make_old_install()
        old_hashes = installer.tree_hashes(target)
        other = self.target_root / "unrelated.plugin"
        other.mkdir()
        (other / "keep.txt").write_text("do not touch", encoding="utf-8")
        result = self.install()
        self.assertIsNotNone(result.backup)
        self.assertEqual(old_hashes, installer.tree_hashes(result.backup))
        self.assertNotIn(self.target_root, result.backup.parents)
        self.assertEqual("do not touch", (other / "keep.txt").read_text(encoding="utf-8"))
        self.assertEqual(installer.tree_hashes(self.source), installer.tree_hashes(target))
        self.assertEqual([], list(self.target_root.glob(".smartcut-*")))

    def test_copy_failure_preserves_existing_install(self):
        target = self.make_old_install()
        old = installer.tree_hashes(target)
        with mock.patch.object(installer.shutil, "copytree", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.install()
        self.assertEqual(old, installer.tree_hashes(target))
        self.assertEqual([], list(self.target_root.glob(".smartcut-*")))

    def test_staging_hash_failure_preserves_existing_install(self):
        target = self.make_old_install()
        old = installer.tree_hashes(target)
        real_hashes = installer.tree_hashes

        def hashes(path):
            if Path(path).name.startswith(".smartcut-stage-"):
                return {"corrupt": "hash"}
            return real_hashes(path)

        with mock.patch.object(installer, "tree_hashes", side_effect=hashes):
            with self.assertRaisesRegex(RuntimeError, "哈希校验失败"):
                self.install()
        self.assertEqual(old, real_hashes(target))

    def test_publish_permission_failure_restores_old_install(self):
        target = self.make_old_install()
        old = installer.tree_hashes(target)
        real_replace = installer.os.replace

        def replace(source, destination):
            if Path(source).name.startswith(".smartcut-stage-"):
                raise PermissionError("publish denied")
            return real_replace(source, destination)

        with mock.patch.object(installer.os, "replace", side_effect=replace):
            with self.assertRaises(PermissionError):
                self.install()
        self.assertEqual(old, installer.tree_hashes(target))
        self.assertEqual([], list(self.target_root.glob(".smartcut-*")))

    def test_final_hash_failure_restores_old_install(self):
        target = self.make_old_install()
        old = installer.tree_hashes(target)
        real_hashes = installer.tree_hashes

        def hashes(path):
            if Path(path) == target:
                return {"corrupt": "hash"}
            return real_hashes(path)

        with mock.patch.object(installer, "tree_hashes", side_effect=hashes):
            with self.assertRaisesRegex(RuntimeError, "哈希校验失败"):
                self.install()
        self.assertEqual(old, real_hashes(target))

    def test_registry_problem_keeps_verified_files_and_returns_warning(self):
        result = installer.install_extension(
            self.source, self.target_root,
            registry_writer=mock.Mock(side_effect=PermissionError("HKCU denied")))
        self.assertTrue(result.target.is_dir())
        self.assertIn("调试开关设置失败", result.warnings[0])

    @unittest.skipUnless(os.name == "nt", "Windows HKCU registry")
    def test_debug_mode_writes_only_current_user_csxs8_to_12(self):
        import winreg
        with mock.patch.object(winreg, "CreateKeyEx") as create, \
                mock.patch.object(winreg, "SetValueEx") as set_value:
            self.assertEqual([], installer.enable_debug_mode())
        self.assertEqual(5, create.call_count)
        self.assertEqual([r"Software\Adobe\CSXS.%d" % version
                          for version in range(8, 13)],
                         [call.args[1] for call in create.call_args_list])
        self.assertTrue(all(call.args[0] == winreg.HKEY_CURRENT_USER
                            for call in create.call_args_list))
        self.assertTrue(all(call.args[1:] == ("PlayerDebugMode", 0, winreg.REG_SZ, "1")
                            for call in set_value.call_args_list))

    def test_invalid_source_and_recursive_destination_are_rejected(self):
        with self.assertRaises(FileNotFoundError):
            installer.install_extension(self.base / "missing", self.target_root)
        with self.assertRaises(ValueError):
            installer.install_extension(self.source, self.source / "nested")
        with self.assertRaises(ValueError):
            installer.install_extension(self.source, "")
        with self.assertRaises(ValueError):
            installer.install_extension(self.source, Path(self.target_root.anchor))
        self.assertFalse(self.target_root.exists())

    def test_existing_install_lock_is_not_removed_or_overwritten(self):
        self.target_root.mkdir(parents=True)
        lock = self.target_root / ".smartcut-install.lock"
        lock.write_text("another installer", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "另一项插件安装"):
            self.install()
        self.assertEqual("another installer", lock.read_text(encoding="utf-8"))

    def test_missing_csinterface_rejects_incomplete_plugin_before_install(self):
        (self.source / "js" / "CSInterface.js").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "CSInterface"):
            self.install()
        self.assertFalse(self.target_root.exists())

    def test_missing_or_broken_manifest_is_reported_by_inspection(self):
        self.assertFalse(installer.inspect_target(self.target_root)["installed"])
        target = self.make_old_install()
        (target / "CSXS" / "manifest.xml").write_text("broken", encoding="utf-8")
        self.assertTrue(installer.inspect_target(self.target_root)["error"])

    def test_manifest_covers_pr2020_and_uses_cep9_runtime(self):
        manifest = ET.parse(ROOT / "pr_bridge" / "CSXS" / "manifest.xml").getroot()
        host = manifest.find("./ExecutionEnvironment/HostList/Host")
        runtime = manifest.find("./ExecutionEnvironment/RequiredRuntimeList/RequiredRuntime")
        self.assertEqual("[14.0,99.9]", host.get("Version"))
        self.assertEqual("9.0", runtime.get("Version"))
        self.assertEqual("0.5.1", manifest.get("ExtensionBundleVersion"))

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell fallback")
    def test_powershell_custom_target_installs_and_backs_up_without_registry_writes(self):
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not shell:
            self.skipTest("PowerShell not installed")
        target = self.make_old_install()
        old = installer.tree_hashes(target)
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(ROOT / "install_pr_bridge.ps1"),
                   "-TargetRoot", str(self.target_root), "-SkipDebug"]
        run = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertEqual(installer.tree_hashes(ROOT / "pr_bridge"),
                         installer.tree_hashes(target), run.stdout + run.stderr)
        backups = list((self.target_root.parent / "SmartCut-CEP备份" / installer.EXTENSION_ID).iterdir())
        self.assertEqual(1, len(backups))
        self.assertEqual(old, installer.tree_hashes(backups[0]))
        self.assertEqual([], list(self.target_root.glob(".smartcut-*")))

    @unittest.skipUnless(os.name == "nt", "Windows short-path handling")
    def test_powershell_accepts_windows_short_user_path(self):
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not shell:
            self.skipTest("PowerShell not installed")
        short_root = Path(self.temp.name) / "short CEP" / "extensions"
        run = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / "install_pr_bridge.ps1"), "-TargetRoot", str(short_root),
             "-SkipDebug"], capture_output=True, timeout=30)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertEqual(installer.tree_hashes(ROOT / "pr_bridge"),
                         installer.tree_hashes(short_root / installer.EXTENSION_ID),
                         run.stdout.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
