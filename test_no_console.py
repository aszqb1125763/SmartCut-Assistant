# -*- coding: utf-8 -*-
"""Exercise hidden processes with the installed production subprocess wrapper."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import no_console
import winproc


@unittest.skipUnless(os.name == "nt", "Windows process creation flags")
class HiddenWindowsProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the product wrapper active; loading a fresh subprocess module
        # would hide exactly the regression these integration checks cover.
        no_console.install()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="smartcut-子进程-")
        self.addCleanup(self.directory.cleanup)
        self.marker = Path(self.directory.name) / "带空格 marker.txt"

    def powershell_command(self, shell=None):
        shell = shell or shutil.which("powershell.exe")
        if not shell:
            self.skipTest("Windows PowerShell not found")
        path = str(self.marker).replace("'", "''")
        script = (
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "[IO.File]::WriteAllText('" + path + "','标记已写入',[Text.UTF8Encoding]::new($false));"
            "[Console]::WriteLine('脚本已执行');"
            "[Console]::Error.WriteLine('诊断输出');"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]

    def assert_powershell_executed(self, result):
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("脚本已执行", result.stdout)
        self.assertIn("诊断输出", result.stderr)
        self.assertTrue(self.marker.is_file(), "exit 0 alone does not prove script execution")
        self.assertEqual("标记已写入", self.marker.read_text(encoding="utf-8"))

    def test_powershell_run_writes_marker_and_captures_both_streams(self):
        result = subprocess.run(self.powershell_command(), capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assert_powershell_executed(result)

    def test_powershell_popen_clears_explicit_inherited_detached_flag(self):
        command = self.powershell_command()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", creationflags=no_console.PROCESS_FLAGS) as process:
            stdout, stderr = process.communicate(timeout=15)
            self.assert_powershell_executed(
                subprocess.CompletedProcess(command, process.returncode, stdout, stderr))

    def test_powershell_through_explicit_hidden_helper_executes(self):
        result = winproc.run_hidden(self.powershell_command(), capture_output=True,
                                   text=True, encoding="utf-8", timeout=15)
        self.assert_powershell_executed(result)

    def test_production_installer_command_runner_executes_powershell(self):
        import installer
        result = installer.run(self.powershell_command())
        self.assert_powershell_executed(result)

    def test_powershell_hidden_popen_executes(self):
        command = self.powershell_command()
        with winproc.popen_hidden(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8") as process:
            stdout, stderr = process.communicate(timeout=15)
            self.assert_powershell_executed(
                subprocess.CompletedProcess(command, process.returncode, stdout, stderr))

    def test_pwsh_executes_when_available(self):
        shell = shutil.which("pwsh.exe")
        if not shell:
            self.skipTest("PowerShell 7 not installed")
        result = subprocess.run(self.powershell_command(shell), capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assert_powershell_executed(result)

    def test_ordinary_python_retains_output_and_nonzero_exit_code(self):
        script = (
            "import pathlib, sys;"
            "pathlib.Path(sys.argv[1]).write_text('ordinary-child', encoding='utf-8');"
            "print('ordinary-stdout');print('ordinary-stderr',file=sys.stderr);sys.exit(7)"
        )
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.marker)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(7, result.returncode)
        self.assertEqual("ordinary-stdout", result.stdout.strip())
        self.assertEqual("ordinary-stderr", result.stderr.strip())
        self.assertEqual("ordinary-child", self.marker.read_text(encoding="utf-8"))

    def test_global_policy_never_detaches_ordinary_python(self):
        options = no_console.hidden_options(
            [sys.executable, "-c", "pass"],
            {"creationflags": no_console.DETACHED_PROCESS}, detached=None)
        self.assertTrue(options["creationflags"] & no_console.CREATE_NO_WINDOW)
        self.assertEqual(0, options["creationflags"] & no_console.DETACHED_PROCESS)

        explicit = no_console.hidden_options(
            [sys.executable, "-c", "pass"], {}, detached=True)
        self.assertTrue(explicit["creationflags"] & no_console.DETACHED_PROCESS)

    def test_cmd_and_implicit_shell_execute(self):
        cmd = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not cmd:
            self.skipTest("cmd.exe not found")
        direct = subprocess.run([cmd, "/d", "/c", "echo smartcut-direct-cmd"],
                                capture_output=True, text=True, timeout=15)
        implicit = subprocess.run("echo smartcut-implicit-cmd", shell=True,
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(0, direct.returncode, direct.stderr)
        self.assertIn("smartcut-direct-cmd", direct.stdout)
        self.assertEqual(0, implicit.returncode, implicit.stderr)
        self.assertIn("smartcut-implicit-cmd", implicit.stdout)

    def test_shell_flag_policy_and_existing_options(self):
        extra_flag = subprocess.CREATE_NEW_PROCESS_GROUP
        for command, options in (
            ([r"C:\Windows\System32\WindowsPowerShell\v1.0\PowerShell.EXE"], {}),
            (r'"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile', {}),
            (["cmd.exe", "/c", "exit"], {}),
            (["alias", "-NoProfile"], {"executable": r"C:\Tools\pwsh.exe"}),
            ("echo value", {"shell": True}),
            ([r"C:\Tools\启动.cmd"], {}),
        ):
            with self.subTest(command=command):
                options["creationflags"] = extra_flag | no_console.PROCESS_FLAGS
                result = no_console.hidden_options(command, options)
                self.assertEqual(0, result["creationflags"] & no_console.DETACHED_PROCESS)
                self.assertTrue(result["creationflags"] & no_console.CREATE_NO_WINDOW)
                self.assertTrue(result["creationflags"] & extra_flag)
                self.assertEqual(0, result["startupinfo"].wShowWindow)
                self.assertTrue(result["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
                self.assertTrue(options["creationflags"] & no_console.DETACHED_PROCESS)

    def test_media_tools_keep_existing_detached_silent_policy(self):
        for executable in ("ffmpeg.exe", "ffprobe.exe", "llama-server.exe"):
            options = no_console.hidden_options([executable, "-version"])
            self.assertEqual(no_console.PROCESS_FLAGS,
                             options["creationflags"] & no_console.PROCESS_FLAGS)

    def test_real_ffmpeg_output_still_reaches_parent(self):
        ffmpeg = Path(__file__).resolve().parent / "bin" / "ffmpeg.exe"
        if not ffmpeg.is_file():
            self.skipTest("FFmpeg not bundled")
        result = subprocess.run([str(ffmpeg), "-version"], capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("ffmpeg version", result.stdout)


if __name__ == "__main__":
    unittest.main()
