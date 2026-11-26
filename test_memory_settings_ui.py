# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent


class MemorySettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dispose(self, dialog):
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_memory_page_is_named_and_reads_independent_config(self):
        import ui_settings

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "conf"), exist_ok=True)
            with open(os.path.join(home, "conf", "director_memory.json"),
                      "w", encoding="utf-8") as fh:
                json.dump({"root": "memory-custom", "checkpoint_seconds": 180,
                           "future_key": "keep"}, fh)
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": home}, clear=False):
                dialog = ui_settings.SettingsDialogV2()
                try:
                    self.assertTrue(dialog.select_page("导演与记忆"))
                    self.assertEqual("导演与记忆", dialog.title.text())
                    self.assertEqual("memory-custom", dialog.mem_root_edit.text())
                    self.assertEqual(180, dialog.mem_checkpoint_sec.value())
                    self.assertTrue(dialog.mem_audio_review.isChecked())
                    self.assertTrue(dialog.mem_visual_review.isChecked())
                    self.assertTrue(dialog.mem_rendered_review.isChecked())
                    self.assertFalse(dialog.mem_visual_vlm.isChecked())
                finally:
                    self._dispose(dialog)

    def test_memory_updates_preserve_typed_values(self):
        import ui_settings

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "conf"), exist_ok=True)
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": home}, clear=False):
                dialog = ui_settings.SettingsDialogV2()
                try:
                    dialog.mem_enabled.setChecked(False)
                    dialog.mem_days.setValue(45)
                    dialog.mem_checkpoint_sec.setValue(150)
                    dialog.mem_audio_review.setChecked(True)
                    dialog.mem_audio_windows.setValue(36)
                    dialog.mem_visual_vlm.setChecked(True)
                    dialog.mem_rendered_review.setChecked(False)
                    dialog.mem_rendered_nodes.setValue(16)
                    dialog.mem_vlm_tier.setCurrentIndex(
                        dialog.mem_vlm_tier.findData("accurate"))
                    values = dialog._memory_updates()
                    self.assertIs(values["enabled"], False)
                    self.assertEqual(45, values["session_retention_days"])
                    self.assertEqual(150, values["checkpoint_seconds"])
                    self.assertIs(values["audio_review_enabled"], True)
                    self.assertEqual(36, values["director_max_review_windows"])
                    self.assertIs(values["visual_use_vlm"], True)
                    self.assertEqual("accurate", values["visual_vlm_tier"])
                    self.assertIs(values["rendered_review_enabled"], False)
                    self.assertEqual(16, values["rendered_review_max_nodes"])
                finally:
                    self._dispose(dialog)

    def test_legacy_default_audio_rounds_migrate_without_overriding_new_choice(self):
        import memory_config

        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "director_memory.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1,
                           "director_audio_rework_rounds": 8}, handle)
            migrated = memory_config.load_config(path=path)
            self.assertEqual(12, migrated["director_audio_rework_rounds"])
            self.assertEqual(2, migrated["schema_version"])
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 2,
                           "director_audio_rework_rounds": 8}, handle)
            explicit = memory_config.load_config(path=path)
            self.assertEqual(8, explicit["director_audio_rework_rounds"])


if __name__ == "__main__":
    unittest.main()
