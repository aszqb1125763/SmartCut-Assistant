# -*- coding: utf-8 -*-
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

import ui_main_v2


class _Signal:
    def connect(self, callback):
        pass


class _FakeWorker:
    instances = []
    fail_start = False

    def __init__(self, src, **kwargs):
        self.src = src
        self.kwargs = kwargs
        self.log = _Signal()
        self.task_started = _Signal()
        self.progress = _Signal()
        self.done = _Signal()
        self.failed = _Signal()
        self.cancelled = _Signal()
        self.started = False
        type(self).instances.append(self)

    def start(self):
        if type(self).fail_start:
            raise RuntimeError("thread start failed")
        self.started = True

    def isRunning(self):
        return False


class DirectCutStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _FakeWorker.instances = []
        _FakeWorker.fail_start = False
        self._selfcheck_patch = mock.patch.object(
            ui_main_v2.QTimer, "singleShot", staticmethod(lambda *_args: None))
        self._selfcheck_patch.start()
        self.window = ui_main_v2.MainWindowV2()
        self.window.src = "example.mp4"
        self.window.index_dir = "example-index"
        self.window.btn_direct.setEnabled(True)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(self.window, QEvent.DeferredDelete)
        self.app.processEvents()
        self._selfcheck_patch.stop()

    def test_button_click_does_not_pass_qt_checked_bool_as_request(self):
        with mock.patch.object(ui_main_v2, "CutWorker", _FakeWorker):
            self.window.btn_direct.click()

        self.assertEqual(1, len(_FakeWorker.instances))
        self.assertTrue(_FakeWorker.instances[0].started)
        self.assertEqual("example.mp4", self.window._active_cut_request["src"])
        self.assertFalse(self.window.btn_direct.isEnabled())

    def test_direct_cut_ignores_legacy_bool_argument(self):
        with mock.patch.object(ui_main_v2, "CutWorker", _FakeWorker):
            self.window.direct_cut(False)

        self.assertEqual(1, len(_FakeWorker.instances))
        self.assertTrue(_FakeWorker.instances[0].started)
        self.assertEqual("example.mp4", self.window._active_cut_request["src"])

    def test_worker_construction_failure_restores_idle_state(self):
        def fail_constructor(*args, **kwargs):
            raise RuntimeError("worker construction failed")

        with mock.patch.object(ui_main_v2, "CutWorker", fail_constructor):
            self.window.direct_cut()

        self.assertTrue(self.window.btn_direct.isEnabled())
        self.assertTrue(self.window.status.work.isHidden())
        self.assertIsNone(self.window._worker)
        self.assertIsNone(self.window._active_cut_request)
        self.assertEqual("剪辑未能启动", self.window.notice.title.text())

    def test_worker_start_failure_restores_idle_state(self):
        _FakeWorker.fail_start = True
        with mock.patch.object(ui_main_v2, "CutWorker", _FakeWorker):
            self.window.direct_cut()

        self.assertTrue(self.window.btn_direct.isEnabled())
        self.assertTrue(self.window.status.work.isHidden())
        self.assertIsNone(self.window._worker)
        self.assertIsNone(self.window._active_cut_request)
        self.assertEqual("剪辑未能启动", self.window.notice.title.text())


if __name__ == "__main__":
    unittest.main()
