# -*- coding: utf-8 -*-
"""Fast offline checks for product metadata, docs and installer packaging."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import installer
import product_info


ROOT = Path(__file__).resolve().parent


def _load_target_installer():
    spec = importlib.util.spec_from_file_location(
        "smartcut_target_installer", ROOT / "安装器.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_target_package(root, module, packaged=True):
    files = list(module.REQUIRED_PROGRAM_FILES)
    if packaged:
        files += ["智能剪辑助手.exe", "_internal/python314.dll",
                  "_internal/base_library.zip", "_internal/ctranslate2/models/data.bin"]
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("new:" + relative).encode("utf-8"))


class ProductReleaseTests(unittest.TestCase):
    def test_product_metadata_is_canonical(self):
        self.assertEqual("SmartCut 智能剪辑助手", product_info.PRODUCT_NAME)
        self.assertEqual("SmartCutAssistant", product_info.PRODUCT_ID)
        self.assertEqual("1.1.5", product_info.VERSION)
        self.assertIn("瓜子神教大主教", product_info.COPYRIGHT)
        self.assertIn("不无聊的子枫哥", product_info.COPYRIGHT)
        self.assertNotIn("DeepSeek", product_info.COPYRIGHT)
        self.assertNotIn("Codex", product_info.COPYRIGHT)

    def test_required_docs_and_tutorial_targets_exist(self):
        required = (
            "README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md",
            "RELEASE_CHECKLIST.md", "docs/安装指南.md", "docs/Premiere插件.md",
            "docs/API模型.md", "docs/字幕与工程.md", "docs/故障排查.md",
            "docs/开发与构建.md",
        )
        for relative in required:
            self.assertTrue((ROOT / relative).is_file(), relative)

        for relative in ("README.md", "NOTICE.md", "docs/开发与构建.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)", text):
                if re.match(r"^[a-z]+://", target, re.I):
                    continue
                self.assertTrue((ROOT / relative).parent.joinpath(target).resolve().exists(),
                                "%s -> %s" % (relative, target))

    def test_ui_has_real_tutorial_and_subtitle_actions(self):
        cards = (ROOT / "cards_ui.py").read_text(encoding="utf-8")
        autoedit = (ROOT / "ui_autoedit.py").read_text(encoding="utf-8")
        self.assertNotIn("self._stub(", cards)
        self.assertNotIn("def _stub(", cards)
        self.assertIn('self.quick_sub("srt")', cards)
        self.assertIn('self.quick_sub("ass")', cards)
        self.assertNotIn("使用说明.txt", autoedit)
        self.assertNotIn("待改记录.md", autoedit)

    def test_runtime_requirements_cover_imported_optional_features(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("opencc-python-reimplemented", "rapidocr-onnxruntime",
                        "onnxruntime", "pillow", "pyclipper", "shapely"):
            self.assertIn(package, text)
        self.assertNotIn("pyinstaller", text)
        self.assertIn("pyinstaller", (ROOT / "requirements-dev.txt").read_text(
            encoding="utf-8").lower())

    def test_missing_command_returns_diagnostic_instead_of_crashing(self):
        result = installer.run(["__smartcut_missing_command_1b9f2c__"])
        self.assertEqual(127, result.returncode)
        self.assertTrue(result.stderr)

    def test_no_python_launcher_or_path_candidate_is_handled(self):
        missing = subprocess.CompletedProcess([], 127, "", "not found")
        with mock.patch.object(installer, "run", return_value=missing), \
                mock.patch.object(installer, "python_is_usable", return_value=None), \
                mock.patch.object(installer.glob, "glob", return_value=[]), \
                mock.patch.object(installer.shutil, "which", return_value=None):
            self.assertEqual((None, "未找到"), installer.find_base_python())

    def test_missing_winget_gives_manual_install_hint(self):
        with mock.patch.object(installer.subprocess, "run", side_effect=FileNotFoundError("winget")), \
                mock.patch.object(installer, "log") as log:
            self.assertIsNone(installer.install_python_312())
        output = "\n".join(str(call.args[0]) for call in log.call_args_list if call.args)
        self.assertIn("https://www.python.org/downloads/", output)

    def test_requirements_change_invalidates_install_stamp(self):
        with tempfile.TemporaryDirectory() as temp:
            req = Path(temp) / "requirements.txt"
            stamp = Path(temp) / "requirements.sha256"
            req.write_text("numpy>=1.26\n", encoding="utf-8")
            with mock.patch.object(installer, "REQ_FILE", str(req)), \
                    mock.patch.object(installer, "DEPS_STAMP", str(stamp)), \
                    mock.patch.object(installer, "deps_ok", return_value=True):
                self.assertFalse(installer.dependencies_current())
                stamp.write_text(installer.requirements_digest(), encoding="ascii")
                self.assertTrue(installer.dependencies_current())
                req.write_text("numpy>=1.26\nopencc-python-reimplemented\n", encoding="utf-8")
                self.assertFalse(installer.dependencies_current())

    def test_repeated_dependency_setup_skips_network(self):
        with mock.patch.object(installer.os.path, "isfile", return_value=True), \
                mock.patch.object(installer, "dependencies_current", return_value=True), \
                mock.patch.object(installer, "run") as run, \
                mock.patch.object(installer, "log"):
            self.assertTrue(installer.ensure_deps())
            run.assert_not_called()

    def test_missing_requirements_stops_before_pip(self):
        with mock.patch.object(installer.os.path, "isfile", return_value=False), \
                mock.patch.object(installer, "run") as run, \
                mock.patch.object(installer, "log"):
            self.assertFalse(installer.ensure_deps())
            run.assert_not_called()

    def test_target_installer_skips_private_and_generated_files(self):
        module = _load_target_installer()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("app_main.py", "conf/autocut.json", "conf/settings.json",
                             "conf/llm.json", "conf/bgm.json", "conf/director_memory.json",
                             "conf/editor_model_private.json", "data/task.json",
                             "工作目录/private.txt", "_诊断/error.txt",
                             "智能剪辑助手.exe", "_internal/python314.dll",
                             "_internal/base_library.zip", "_internal/ctranslate2/models/data.bin",
                             "_internal_旧备份/runtime.dll", "一次备份/old.py", "docs/help.md"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            files = {Path(item).as_posix() for item in module.scan_package(
                str(root), with_bin=False, with_runtime=False, with_models=False)}
            self.assertIn("app_main.py", files)
            self.assertIn("conf/autocut.json", files)
            self.assertIn("docs/help.md", files)
            self.assertIn("智能剪辑助手.exe", files)
            self.assertIn("_internal/python314.dll", files)
            self.assertIn("_internal/base_library.zip", files)
            self.assertIn("_internal/ctranslate2/models/data.bin", files)
            self.assertNotIn("conf/settings.json", files)
            self.assertNotIn("conf/llm.json", files)
            self.assertNotIn("conf/bgm.json", files)
            self.assertNotIn("conf/director_memory.json", files)
            self.assertNotIn("conf/editor_model_private.json", files)
            self.assertNotIn("data/task.json", files)
            self.assertFalse(any("工作目录" in item or "备份" in item
                                 or "_诊断" in item
                                 for item in files))

    def test_source_package_does_not_copy_orphan_internal(self):
        module = _load_target_installer()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            _make_target_package(source, module, packaged=False)
            (source / "_internal").mkdir()
            (source / "_internal/old.dll").write_bytes(b"orphan")
            self.assertFalse(any(Path(item).parts[0] == "_internal"
                                 for item in module.scan_package(str(source))))

    def test_incomplete_executable_bundle_is_rejected_before_target_creation(self):
        module = _load_target_installer()
        for missing in ("_internal", "_internal/python314.dll",
                        "_internal/base_library.zip", "智能剪辑助手.exe"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                source, target = Path(temp, "package"), Path(temp, "installed")
                _make_target_package(source, module)
                path = source / missing
                if path.is_dir():
                    shutil.rmtree(path)
                elif missing.endswith(".exe"):
                    path.write_bytes(b"")
                else:
                    path.unlink()
                with mock.patch.object(module, "BASE", str(source)):
                    result = module.install(str(target), do_setup=False,
                                            progress=lambda *_: None)
                self.assertEqual(1, result)
                self.assertFalse(target.exists())

    def test_executable_upgrade_replaces_whole_runtime_and_preserves_old_build(self):
        module = _load_target_installer()
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp, "package"), Path(temp, "installed")
            _make_target_package(source, module)
            (target / "_internal").mkdir(parents=True)
            (target / "_internal/stale.dll").write_bytes(b"old-runtime")
            (target / "SmartCut 智能剪辑助手.exe").write_bytes(b"old-exe")
            (target / "conf").mkdir()
            (target / "conf/settings.json").write_text('{"private": true}', encoding="utf-8")
            with mock.patch.object(module, "BASE", str(source)), \
                    mock.patch.object(module.subprocess, "Popen") as launch:
                result = module.install(str(target), do_setup=False, force=True,
                                        with_runtime=False, launch=True,
                                        progress=lambda *_: None)
            self.assertEqual(0, result)
            self.assertEqual((source / "智能剪辑助手.exe").read_bytes(),
                             (target / "智能剪辑助手.exe").read_bytes())
            self.assertFalse((target / "SmartCut 智能剪辑助手.exe").exists())
            self.assertFalse((target / "_internal/stale.dll").exists())
            self.assertTrue((target / "_internal/ctranslate2/models/data.bin").is_file())
            self.assertIn('"private": true', (target / "conf/settings.json").read_text(encoding="utf-8"))
            backups = list(target.glob("_安装备份_*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(b"old-runtime", (backups[0] / "_internal/stale.dll").read_bytes())
            self.assertEqual(b"old-exe", (backups[0] / "SmartCut 智能剪辑助手.exe").read_bytes())
            launch.assert_called_once_with([str(target / "智能剪辑助手.exe")], cwd=str(target))

    def test_copy_failure_returns_failure_and_keeps_old_runtime(self):
        module = _load_target_installer()
        for failed_file in ("python314.dll", "README.md"):
            with self.subTest(failed_file=failed_file), tempfile.TemporaryDirectory() as temp:
                source, target = Path(temp, "package"), Path(temp, "installed")
                _make_target_package(source, module)
                (target / "_internal").mkdir(parents=True)
                (target / "_internal/old.dll").write_bytes(b"old-runtime")
                (target / "智能剪辑助手.exe").write_bytes(b"old-exe")
                original_copy = shutil.copy2

                def fail_copy(src, dst, *args, **kwargs):
                    if Path(src).name == failed_file:
                        raise PermissionError("simulated locked destination")
                    return original_copy(src, dst, *args, **kwargs)

                messages = []
                with mock.patch.object(module, "BASE", str(source)), \
                        mock.patch.object(module.shutil, "copy2", side_effect=fail_copy):
                    result = module.install(str(target), do_setup=False, force=True,
                                            progress=messages.append)
                self.assertEqual(1, result)
                self.assertFalse(any("安装完成：" in line for line in messages))
                self.assertEqual(b"old-exe", (target / "智能剪辑助手.exe").read_bytes())
                self.assertEqual(b"old-runtime", (target / "_internal/old.dll").read_bytes())
                self.assertFalse((target / "_internal/python314.dll").exists())

    def test_replacement_failure_restores_matching_old_exe_and_internal(self):
        module = _load_target_installer()
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp, "package"), Path(temp, "installed")
            _make_target_package(source, module)
            (target / "_internal").mkdir(parents=True)
            (target / "_internal/old.dll").write_bytes(b"old-runtime")
            (target / "智能剪辑助手.exe").write_bytes(b"old-exe")
            original_replace = os.replace

            def fail_activation(src, dst):
                if "_安装暂存_" in str(src) and Path(src).name == "智能剪辑助手.exe":
                    raise PermissionError("simulated activation failure")
                return original_replace(src, dst)

            with mock.patch.object(module, "BASE", str(source)), \
                    mock.patch.object(module.os, "replace", side_effect=fail_activation):
                result = module.install(str(target), do_setup=False, force=True,
                                        progress=lambda *_: None)
            self.assertEqual(1, result)
            self.assertEqual(b"old-exe", (target / "智能剪辑助手.exe").read_bytes())
            self.assertEqual(b"old-runtime", (target / "_internal/old.dll").read_bytes())
            self.assertFalse((target / "_internal/python314.dll").exists())

    def test_source_upgrade_retires_stale_executable_in_recoverable_backup(self):
        module = _load_target_installer()
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp, "package"), Path(temp, "installed")
            _make_target_package(source, module, packaged=False)
            (target / "_internal").mkdir(parents=True)
            (target / "_internal/old.dll").write_bytes(b"old-runtime")
            (target / "智能剪辑助手.exe").write_bytes(b"old-exe")
            with mock.patch.object(module, "BASE", str(source)):
                result = module.install(str(target), do_setup=False, force=True,
                                        progress=lambda *_: None)
            self.assertEqual(0, result)
            self.assertTrue((target / "app_main.py").is_file())
            self.assertFalse((target / "智能剪辑助手.exe").exists())
            self.assertFalse((target / "_internal").exists())
            backup = next(target.glob("_安装备份_*"))
            self.assertEqual(b"old-exe", (backup / "智能剪辑助手.exe").read_bytes())

    def test_incomplete_package_does_not_create_target(self):
        module = _load_target_installer()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "installation"
            with mock.patch.object(module, "BASE", temp):
                result = module.install(str(target), progress=lambda *_: None)
            self.assertEqual(1, result)
            self.assertFalse(target.exists())

    def test_release_manifest_contains_docs_and_clean_settings(self):
        import make_release
        self.assertEqual(product_info.PRODUCT_ID, make_release.NAME)
        self.assertEqual(product_info.VERSION, make_release.RELEASE_VERSION)
        clean_settings = json.loads(make_release.CLEAN_CONF["conf/settings.json"])
        self.assertEqual("large-v3", clean_settings["model"])
        for relative in make_release.REQUIRED_FILES:
            self.assertTrue((ROOT / relative).is_file(), relative)
        for relative in (
            "README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md",
            "RELEASE_CHECKLIST.md", "RELEASE_NOTES.md", "CHANGELOG.md", "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md", "SECURITY.md", "OPEN_SOURCE_DECISIONS.md",
            "requirements.txt", "setup_windows.ps1",
        ):
            self.assertIn(relative, make_release.INCLUDE)
        # Once the owners choose a project license, the same release script
        # must ship it automatically without another packaging change.
        self.assertIn("LICENSE", make_release.PLAIN)
        self.assertIn("SBOM.cdx.json", make_release.PLAIN)
        self.assertIn("requirements.lock", make_release.PLAIN)
        self.assertIn("build_tools/generate_compliance.py",
                      make_release.REQUIRED_FILES)
        self.assertEqual(
            tuple("%02d_%s.txt" % item for item in (
                (1, "模组名"), (2, "机器设备"), (3, "材料化学"),
                (4, "游戏机制"), (5, "口头语"), (6, "化学元素"),
                (7, "单位与符号"), (8, "物理工程"),
                (9, "电学与电子"), (10, "计算机与信息"),
            )),
            make_release.PUBLIC_KB_FILES,
        )
        for private in ("mods_terms.json", "mods_hint.txt", "术语_全量.txt",
                        "术语_键值.json", "术语_统计.txt"):
            self.assertNotIn(private, make_release.PUBLIC_KB_FILES)
        for private in ("conf/settings.json", "conf/llm.json"):
            self.assertNotIn(private, make_release.CONF_COPY)
        for required_runtime in (
            "conf/editor_model.json", "conf/decision.json", "conf/coords_matrix.json",
            "conf/spec_manifest.json", "conf/asr_terms.txt", "conf/subtitle_styles.json",
        ):
            self.assertIn(required_runtime, make_release.CONF_COPY)

    def test_public_release_gates_require_legal_metadata(self):
        import make_release
        with tempfile.TemporaryDirectory() as temp:
            issues = make_release.publication_metadata_issues(base=temp)
            self.assertTrue(any("LICENSE" in issue for issue in issues))
            self.assertTrue(any("SBOM" in issue for issue in issues))
            self.assertTrue(any("requirements.lock" in issue for issue in issues))
            self.assertTrue(any("LICENSES/manifest.json" in issue for issue in issues))

            root = Path(temp)
            (root / "LICENSE").write_text("approved licence text " * 4,
                                           encoding="utf-8")
            (root / "LICENSES").mkdir()
            (root / "LICENSES/example.txt").write_text(
                "example dependency licence", encoding="utf-8")
            (root / "LICENSES/manifest.json").write_text(
                '{"complete":true,"components":[{"name":"example",'
                '"license_files":["LICENSES/example.txt"]}]}',
                encoding="utf-8")
            (root / "SBOM.cdx.json").write_text(
                '{"bomFormat":"CycloneDX","specVersion":"1.6",'
                '"components":[{"name":"example","version":"1"}]}',
                encoding="utf-8")
            (root / "requirements.lock").write_text(
                "example==1.0  # installed-metadata-sha256=test\n",
                encoding="utf-8")
            self.assertEqual(
                [], make_release.publication_metadata_issues(base=temp))
            model_issues = make_release.publication_metadata_issues(
                base=temp, with_models=True)
            self.assertTrue(any("LICENSES/models/MANIFEST.json" in issue
                                for issue in model_issues))
            bin_issues = make_release.publication_metadata_issues(
                base=temp, with_bin=True)
            self.assertTrue(any("LICENSES/bin/MANIFEST.json" in issue
                                for issue in bin_issues))

    def test_release_privacy_scan_blocks_private_paths_and_real_keys(self):
        import make_release
        fake_openai_key = "".join((
            "s", "k", "-", "abcdefghijklmnopqrstuvwxyz012345"))
        findings = make_release.audit_text_for_private_data(
            'mods=E:\\\\CurseForge\\\\Instances\\\\private\n'
            'token=' + fake_openai_key, "kb/private.txt")
        self.assertTrue(any("绝对路径" in finding for finding in findings))
        self.assertTrue(any("密钥" in finding for finding in findings))
        safe = make_release.audit_text_for_private_data(
            r"示例：D:\\录像 --out D:\\索引；系统位于 C:\\Windows",
            "docs/example.md")
        self.assertEqual([], safe)

    def test_failed_release_artifacts_are_quarantined(self):
        import make_release
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            stage = dist / make_release.NAME
            stage.mkdir()
            (stage / "partial.txt").write_text("partial", encoding="utf-8")
            archive = dist / ("SmartCutAssistant_v%s.zip" % product_info.VERSION)
            checksum = Path(str(archive) + ".sha256")
            archive.write_bytes(b"old")
            checksum.write_text("old", encoding="ascii")
            blocked = make_release.quarantine_release_artifacts({
                "stage": str(stage), "zip": str(archive),
                "sha256": str(checksum),
            }, reason="test", dist=str(dist))
            self.assertIsNotNone(blocked)
            self.assertFalse(stage.exists())
            self.assertFalse(archive.exists())
            self.assertFalse(checksum.exists())
            moved = Path(blocked)
            self.assertTrue((moved / make_release.NAME / "partial.txt").is_file())
            self.assertTrue((moved / archive.name).is_file())
            self.assertTrue((moved / checksum.name).is_file())

    def test_release_writes_verifiable_sha256_sidecar(self):
        import hashlib
        import make_release
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / ("SmartCutAssistant_v%s.zip" % product_info.VERSION)
            archive.write_bytes(b"smartcut-release")
            checksum = Path(make_release.write_sha256(str(archive)))
            expected = hashlib.sha256(b"smartcut-release").hexdigest()
            self.assertEqual(
                "%s  %s\n" % (expected, archive.name),
                checksum.read_text(encoding="ascii"),
            )

    def test_windows_version_matches_product_info(self):
        text = (ROOT / "build_tools/version_info.txt").read_text(encoding="utf-8")
        self.assertIn(product_info.VERSION + ".0", text)
        self.assertIn(product_info.PRODUCT_NAME, text)
        self.assertIn(product_info.COPYRIGHT, text)

    def test_pyinstaller_does_not_bundle_all_of_pyside(self):
        source = (ROOT / "build_tools/build_app.py").read_text(encoding="utf-8")
        self.assertNotIn('"--collect-submodules", "PySide6"', source)
        for module in ("PySide6.QtCore", "PySide6.QtGui",
                       "PySide6.QtWidgets", "PySide6.QtMultimedia",
                       "PySide6.QtMultimediaWidgets"):
            self.assertIn('"--hidden-import", "%s"' % module, source)
        self.assertIn("def _check_qt_payload", source)
        self.assertIn("def _prune_unused_qt_payload", source)
        self.assertIn("Qt6WebEngineCore.dll", source)
        self.assertIn("qtvirtualkeyboardplugin.dll", source)
        self.assertIn("qpdf.dll", source)

    @unittest.skipUnless(os.name == "nt", "PowerShell bootstrap is Windows-only")
    def test_bootstrap_with_empty_path_and_no_python_is_non_destructive(self):
        powershell = shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell not installed")
        setup = str(ROOT / "setup_windows.ps1").replace("'", "''")
        code = (
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "function Get-Command { param($Name) return $null };"
            "function Test-Path { param($LiteralPath,$PathType) "
            "return [bool]($LiteralPath -match "
            "'(installer.py|product_info.py|requirements.txt|constraints-runtime.txt|"
            "app_main.py|启动SmartCut.bat|__init__.py|decision.json|coords_matrix.json|"
            "spec_manifest.json|editor_model.json)$') };"
            "$env:PATH=''; & '" + setup + "' -Check; exit $LASTEXITCODE"
        )
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", code],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("https://www.python.org/downloads/windows/", result.stdout)
        self.assertIn("没有安装 Python", result.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell bootstrap is Windows-only")
    def test_bootstrap_falls_back_when_py_lacks_312(self):
        powershell = shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell not installed")
        setup = str(ROOT / "setup_windows.ps1").replace("'", "''")
        watched = [ROOT / name for name in (".venv", "bin", "models")]
        before = [path.exists() for path in watched]
        with tempfile.TemporaryDirectory() as temp:
            tools = Path(temp)
            (tools / "py.cmd").write_text(
                "@echo off\n"
                ">&2 echo No suitable Python runtime found\n"
                "exit /b 103\n",
                encoding="utf-8",
            )
            (tools / "python.cmd").write_text(
                "@echo off\n"
                "if \"%~1\"==\"-c\" exit /b 0\n"
                "echo FAKE_PYTHON_FALLBACK_OK\n"
                "exit /b 0\n",
                encoding="utf-8",
            )
            fake_path = str(tools).replace("'", "''")
            code = (
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
                "$env:PATH='" + fake_path + "';"
                "& '" + setup + "' -Check; exit $LASTEXITCODE"
            )
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", code],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=20,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("FAKE_PYTHON_FALLBACK_OK", result.stdout)
        self.assertNotIn("NativeCommandError", result.stderr)
        self.assertEqual(before, [path.exists() for path in watched])

    @unittest.skipUnless(os.name == "nt", "PowerShell bootstrap is Windows-only")
    def test_bootstrap_incomplete_package_stops_before_python_lookup(self):
        powershell = shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell not installed")
        setup = str(ROOT / "setup_windows.ps1").replace("'", "''")
        code = (
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "function Test-Path { param($LiteralPath,$PathType) return $false };"
            "function Get-Command { throw 'must not look up commands for incomplete package' };"
            "& '" + setup + "' -Check; exit $LASTEXITCODE"
        )
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", code],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("安装包不完整", result.stdout)
        self.assertNotIn("must not look up", result.stderr)

    def test_removed_noop_cli_options(self):
        source = (ROOT / "autocut.py").read_text(encoding="utf-8")
        self.assertNotIn('add_argument("--ratio"', source)
        self.assertNotIn('add_argument("--target-min"', source)
        self.assertIn('add_argument("--keep-percent"', source)


if __name__ == "__main__":
    unittest.main()
