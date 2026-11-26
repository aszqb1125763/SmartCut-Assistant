# -*- coding: utf-8 -*-
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import installer
import llm_config
import pr_bridge_installer as bridge


class OneClickInstallerTests(unittest.TestCase):
    def test_packaged_script_command_uses_exe_self_bootstrap(self):
        with mock.patch.object(installer, "FROZEN_APP", True), \
                mock.patch.object(sys, "executable", r"C:\\SmartCut\\智能剪辑助手.exe"):
            self.assertEqual(
                [r"C:\\SmartCut\\智能剪辑助手.exe", "--run-script",
                 r"C:\\SmartCut\\fetch_model.py", "large-v3-turbo"],
                installer.script_command(
                    r"C:\\SmartCut\\fetch_model.py", "large-v3-turbo"),
            )

    def test_default_local_model_is_single_8b_tier(self):
        self.assertEqual("smart", llm_config.active_tier(llm_config.DEFAULTS))
        self.assertTrue(all(value == "smart"
                            for value in llm_config.DEFAULTS["tasks"].values()))
        self.assertFalse(llm_config.DEFAULTS["escalate"])

    def test_asr_model_follows_settings_and_defaults_to_accurate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "conf").mkdir()
            self.assertEqual("large-v3", installer.selected_asr_model(root))
            (root / "conf" / "settings.json").write_text(
                json.dumps({"model": "large-v3-turbo"}), encoding="utf-8")
            self.assertEqual("large-v3-turbo", installer.selected_asr_model(root))

    def test_llm_ready_checks_selected_model_not_any_gguf(self):
        with mock.patch.object(llm_config, "model_path", return_value=r"C:\\models\\selected-8b.gguf"), \
                mock.patch.object(installer.os.path, "isfile",
                                  side_effect=lambda path: str(path).endswith("llama-server.exe")), \
                mock.patch.object(installer.os.path, "getsize", return_value=900 * 1024 * 1024):
            self.assertFalse(installer.llm_ready())

    def test_premiere_plugin_auto_installs_to_current_user_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "pr_bridge"
            source.mkdir()
            target = bridge.ExtensionTarget(root / "CEP" / "extensions", "user", False)
            result = bridge.InstallResult(
                target.root / bridge.EXTENSION_ID, None, "1.2.0", 7, ())
            with mock.patch.object(bridge, "extension_source", return_value=source), \
                    mock.patch.object(bridge, "discover_premiere", return_value=[
                        bridge.PremiereInstall("Adobe Premiere Pro 2026", root / "Premiere")]), \
                    mock.patch.object(bridge, "discover_targets", return_value=[target]), \
                    mock.patch.object(bridge, "inspect_target", return_value={"matches": False}), \
                    mock.patch.object(bridge, "install_extension", return_value=result) as install, \
                    mock.patch.object(installer, "INSTALL_PREMIERE", True), \
                    mock.patch.object(installer, "rule"), mock.patch.object(installer, "log"):
                self.assertTrue(installer.install_premiere_plugin_best_effort())
            install.assert_called_once_with(source, target.root)

    def test_premiere_plugin_failure_does_not_fail_main_install(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "pr_bridge"
            source.mkdir()
            with mock.patch.object(bridge, "extension_source", return_value=source), \
                    mock.patch.object(bridge, "discover_premiere", side_effect=OSError("registry")), \
                    mock.patch.object(installer, "INSTALL_PREMIERE", True), \
                    mock.patch.object(installer, "rule"), mock.patch.object(installer, "log"):
                self.assertFalse(installer.install_premiere_plugin_best_effort())

    def test_premiere_plugin_preinstalls_even_before_premiere_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "pr_bridge"
            source.mkdir()
            target = bridge.ExtensionTarget(root / "CEP" / "extensions", "user", False)
            result = bridge.InstallResult(
                target.root / bridge.EXTENSION_ID, None, "1.2.0", 7, ())
            with mock.patch.object(bridge, "extension_source", return_value=source), \
                    mock.patch.object(bridge, "discover_premiere", return_value=[]), \
                    mock.patch.object(bridge, "discover_targets", return_value=[target]), \
                    mock.patch.object(bridge, "inspect_target", return_value={"matches": False}), \
                    mock.patch.object(bridge, "install_extension", return_value=result) as install, \
                    mock.patch.object(installer, "INSTALL_PREMIERE", True), \
                    mock.patch.object(installer, "rule"), mock.patch.object(installer, "log"):
                self.assertTrue(installer.install_premiere_plugin_best_effort())
            install.assert_called_once_with(source, target.root)

    def test_entrypoints_default_to_auto_install_and_launch(self):
        root = Path(__file__).resolve().parent
        batch = (root / "安装并启动.bat").read_text(encoding="utf-8")
        setup = (root / "setup_windows.ps1").read_text(encoding="utf-8")
        choose = (root / "安装到指定目录.bat").read_text(encoding="utf-8")
        directory_installer = (root / "安装器.py").read_text(encoding="utf-8")
        self.assertIn('智能剪辑助手.exe" --run-script', batch)
        self.assertIn("[switch]$NoPremierePlugin", setup)
        self.assertIn("'--run-script'", setup)
        self.assertIn("3.12.10", setup)
        self.assertIn("Python Software Foundation", setup)
        self.assertIn("$process.ExitCode", setup)
        self.assertIn("-ChooseInstallDirectory", choose)
        self.assertIn('"--run-script", inst, "--no-launch"', directory_installer)
        self.assertNotIn("$argsList += '--no-launch'", setup)


if __name__ == "__main__":
    unittest.main()
