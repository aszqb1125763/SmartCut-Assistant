# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

import ai_api_settings


class AiApiSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dispose(self, dialog):
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_provider_catalog_has_common_services_and_stable_ids(self):
        expected = {
            "local", "openai_compatible", "openai", "deepseek",
            "dashscope", "siliconflow", "moonshot", "zhipu", "ollama",
            "anthropic", "gemini",
        }
        self.assertEqual(expected, ai_api_settings.PROVIDER_IDS)
        self.assertEqual("https://api.openai.com/v1",
                         ai_api_settings.default_base_url("openai"))
        self.assertEqual("http://127.0.0.1:11434/v1",
                         ai_api_settings.default_base_url("ollama"))

    def test_cards_settings_loader_preserves_api_timeout_type(self):
        import cards_ui

        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"llm_provider": "openai", "llm_api_timeout": 211,
                           "llm_api_key": "stored-secret"}, handle)
            with mock.patch.object(cards_ui, "SETTINGS_PATH", path):
                loaded = cards_ui.load_settings()
            self.assertEqual("openai", loaded["llm_provider"])
            self.assertEqual(211, loaded["llm_api_timeout"])
            self.assertEqual("stored-secret", loaded["llm_api_key"])

    def test_v2_defaults_local_and_persists_remote_fields_without_showing_key(self):
        import ui_settings

        secret = "test-secret-must-not-appear-in-labels"
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"SMARTCUT_HOME": home}, clear=False):
            dialog = ui_settings.SettingsDialogV2()
            try:
                self.assertEqual("local", dialog._w_llm_provider.currentData())
                self.assertFalse(dialog._w_llm_api_key.isEnabled())
                self.assertEqual(QLineEdit.Password,
                                 dialog._w_llm_api_key.echoMode())
                index = dialog._w_llm_provider.findData("deepseek")
                dialog._w_llm_provider.setCurrentIndex(index)
                self.assertEqual("https://api.deepseek.com/v1",
                                 dialog._w_llm_api_base_url.text())
                dialog._w_llm_api_key.setText(secret)
                dialog._w_llm_api_model.setText("deepseek-chat")
                dialog._w_llm_api_timeout.setValue(187)
                labels = "\n".join(label.text() for label in
                                   dialog.findChildren(QLabel))
                self.assertNotIn(secret, labels)
                dialog.accept()
                with open(os.path.join(home, "conf", "settings.json"),
                          encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.assertEqual("deepseek", saved["llm_provider"])
                self.assertEqual("https://api.deepseek.com/v1",
                                 saved["llm_api_base_url"])
                self.assertEqual(secret, saved["llm_api_key"])
                self.assertEqual("deepseek-chat", saved["llm_api_model"])
                self.assertEqual(187, saved["llm_api_timeout"])
            finally:
                self._dispose(dialog)

    def test_legacy_settings_dialog_uses_same_fields_and_presets(self):
        import cards_ui

        captured = {}

        def save(value):
            captured.update(value)
            return True

        with mock.patch.object(cards_ui, "load_settings",
                               return_value=dict(cards_ui.DEFAULT_SETTINGS)), \
                mock.patch.object(cards_ui, "save_settings", side_effect=save):
            dialog = cards_ui.SettingsDialog()
            try:
                self.assertEqual("local", dialog.llm_provider.currentData())
                self.assertEqual(QLineEdit.Password, dialog.llm_api_key.echoMode())
                dialog.llm_provider.setCurrentIndex(
                    dialog.llm_provider.findData("gemini"))
                self.assertEqual(
                    "https://generativelanguage.googleapis.com/v1beta/openai",
                    dialog.llm_api_base_url.text())
                dialog.llm_api_key.setText("hidden-key")
                dialog.llm_api_model.setText("gemini-2.5-flash")
                dialog.llm_api_timeout.setValue(95)
                dialog._save()
                self.assertEqual("gemini", captured["llm_provider"])
                self.assertEqual("hidden-key", captured["llm_api_key"])
                self.assertEqual("gemini-2.5-flash",
                                 captured["llm_api_model"])
                self.assertEqual(95, captured["llm_api_timeout"])
            finally:
                self._dispose(dialog)

    def test_main_window_status_is_redacted(self):
        import ui_main_v2

        secret = "status-bar-secret"
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "conf"), exist_ok=True)
            with open(os.path.join(home, "conf", "settings.json"),
                      "w", encoding="utf-8") as handle:
                json.dump({
                    "llm_provider": "deepseek",
                    "llm_api_base_url": "https://api.deepseek.com/v1",
                    "llm_api_key": secret,
                    "llm_api_model": "deepseek-chat",
                    "llm_api_timeout": 120,
                }, handle)
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": home}, clear=False):
                window = ui_main_v2.MainWindowV2()
                try:
                    self.assertIn("DeepSeek", window.lbl_env.text())
                    self.assertIn("deepseek-chat", window.lbl_env.text())
                    self.assertNotIn(secret, window.lbl_env.text())
                    self.assertNotIn(secret, window.lbl_env.toolTip())
                finally:
                    self._dispose(window)


if __name__ == "__main__":
    unittest.main()
