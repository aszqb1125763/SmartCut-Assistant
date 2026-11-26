# -*- coding: utf-8 -*-
"""Regression tests for Windows CMD launchers and release encoding."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import make_release


ROOT = Path(__file__).resolve().parent
LAUNCHERS = (
    "安装并启动.bat",
    "安装到指定目录.bat",
    "安装PR插件.bat",
    "启动（排错用）.bat",
    "启动SmartCut.bat",
)
REQUIRED_STUBS = (
    "product_info.py",
    "requirements.txt",
    "constraints-runtime.txt",
    "app_main.py",
    "启动SmartCut.bat",
    "brains/__init__.py",
    "conf/decision.json",
    "conf/coords_matrix.json",
    "conf/spec_manifest.json",
    "conf/editor_model.json",
)


class WindowsLauncherTests(unittest.TestCase):
    def test_all_launchers_are_cmd_safe_utf8_crlf(self):
        for filename in LAUNCHERS:
            with self.subTest(filename=filename):
                data = (ROOT / filename).read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                self.assertTrue(data.startswith(b"@echo off\r\n"))
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
                text = data.decode("utf-8")
                self.assertNotIn('cd /d "%~dp0"', text.lower())
                self.assertIn('pushd "%~dp0"', text.lower())
                self.assertIn("popd", text.lower())
                prefix = text.split("chcp 65001", 1)[0]
                self.assertTrue(prefix.isascii())

    def test_release_normalizer_removes_bom_and_writes_crlf(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "入口.bat"
            path.write_bytes(b"\xef\xbb\xbf@echo off\nrem test\n")
            make_release.normalize_windows_batch(path)
            self.assertEqual(b"@echo off\r\nrem test\r\n", path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows CMD test")
    def test_main_launcher_runs_from_chinese_path(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "SmartCut 中文 空格 (A&B)"
            package.mkdir()
            shutil.copy2(ROOT / "安装并启动.bat", package / "安装并启动.bat")
            (package / "installer.py").write_text(
                "print('SMARTCUT_BATCH_OK')\n", encoding="utf-8")
            for relative in REQUIRED_STUBS:
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            command = 'cmd.exe /d /s /c ""%s" --help"' % (
                package / "安装并启动.bat")
            # The application installs a global no-console wrapper in some
            # earlier tests.  Exercise CMD exactly as Explorer would launch it,
            # without inheriting CREATE_NO_WINDOW from that unrelated wrapper.
            popen_type = subprocess.Popen
            if popen_type.__module__ == "no_console":
                popen_type = popen_type.__mro__[1]
            process = popen_type(
                command,
                cwd=os.environ.get("WINDIR", r"C:\Windows"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )
            output, _ = process.communicate(timeout=30)
            display = output.decode("utf-8", errors="replace")
            self.assertEqual(0, process.returncode, display)
            self.assertIn("SMARTCUT_BATCH_OK", display)
            self.assertNotIn("not recognized", display)


if __name__ == "__main__":
    unittest.main()
