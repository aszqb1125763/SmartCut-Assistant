# -*- coding: utf-8 -*-
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

import app_main
import winproc


class BackgroundProcessEncodingTests(unittest.TestCase):
    def test_script_cmd_for_pythonw_enables_utf8_mode(self):
        with mock.patch.object(winproc, "_venv_pythonw",
                               return_value=r"C:\runtime\pythonw.exe"):
            command = winproc.script_cmd("index.py", ["素材.mp4"])
        self.assertEqual(
            [r"C:\runtime\pythonw.exe", "-X", "utf8", "-u",
             "index.py", "素材.mp4"],
            command)

    def test_hidden_process_environment_defaults_to_utf8(self):
        caller_env = {"KEEP": "yes"}
        with mock.patch.object(winproc.os, "name", "posix"):
            options = winproc._hidden_kw({"env": caller_env})
        self.assertEqual("utf-8", options["env"]["PYTHONIOENCODING"])
        self.assertEqual("1", options["env"]["PYTHONUTF8"])
        self.assertEqual("yes", options["env"]["KEEP"])
        self.assertNotIn("PYTHONIOENCODING", caller_env)

    def test_hidden_process_keeps_explicit_encoding_override(self):
        with mock.patch.object(winproc.os, "name", "posix"):
            options = winproc._hidden_kw({
                "env": {"PYTHONIOENCODING": "utf-8:backslashreplace",
                        "PYTHONUTF8": "0"},
            })
        self.assertEqual("utf-8:backslashreplace",
                         options["env"]["PYTHONIOENCODING"])
        self.assertEqual("0", options["env"]["PYTHONUTF8"])

    def test_safe_stdio_reconfigures_existing_text_streams(self):
        output = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        error = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        with mock.patch.object(app_main.sys, "stdout", output), \
                mock.patch.object(app_main.sys, "stderr", error):
            app_main._safe_stdio()
            self.assertEqual("utf-8", output.encoding.lower())
            self.assertEqual("utf-8", error.encoding.lower())

    def test_hidden_python_process_emits_utf8_chinese(self):
        process = winproc.popen_hidden(
            [sys.executable, "-c", "print('索引完成：中文日志正常')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
        output, _ = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode)
        self.assertEqual("索引完成：中文日志正常", output.strip())


if __name__ == "__main__":
    unittest.main()
