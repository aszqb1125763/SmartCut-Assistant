# -*- coding: utf-8 -*-
"""模型剪辑师的入口、冻结路径与发布清单回归测试。"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.abspath(__file__))


def _tree(relative_path):
    path = os.path.join(BASE, relative_path)
    with open(path, encoding="utf-8") as handle:
        return path, ast.parse(handle.read(), filename=path)


def _run_one_calls(relative_path):
    _path, tree = _tree(relative_path)
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_one"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "autopilot"
    ]


class ModelEditorEntrypointTests(unittest.TestCase):
    def _assert_all_autopilot_calls_use_model_editor(self, relative_path):
        calls = _run_one_calls(relative_path)
        self.assertTrue(calls, "%s 没找到 autopilot.run_one 调用" % relative_path)
        for call in calls:
            keyword = next((item for item in call.keywords
                            if item.arg == "model_editor"), None)
            self.assertIsNotNone(
                keyword, "%s 的自动剪辑入口没有指定模型剪辑师" % relative_path)
            self.assertIsInstance(keyword.value, ast.Constant)
            self.assertIs(keyword.value.value, True)

    def test_queue_auto_cut_uses_model_editor(self):
        self._assert_all_autopilot_calls_use_model_editor("ui_queue.py")

    def test_legacy_card_auto_task_uses_model_editor(self):
        self._assert_all_autopilot_calls_use_model_editor("cards_ui.py")


class FrozenHomeTests(unittest.TestCase):
    MODULES = (
        "asr.py",
        "neutral_text.py",
        "three_axis.py",
        "time_sense.py",
        "utterance.py",
    )

    def test_smartcut_home_overrides_frozen_module_directory(self):
        wanted = os.path.join(BASE, "_test_install_home")
        for relative_path in self.MODULES:
            path, tree = _tree(relative_path)
            assignment = next(
                (node for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "BASE"
                         for target in node.targets)),
                None,
            )
            self.assertIsNotNone(assignment, "%s 没有 BASE" % relative_path)
            expression = ast.Expression(body=assignment.value)
            ast.fix_missing_locations(expression)
            from memory_config import app_home
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": wanted}):
                actual = eval(compile(expression, path, "eval"),
                              {"os": os, "__file__": path,
                               "app_home": app_home})
            self.assertEqual(wanted, actual, relative_path)

    def test_ui_and_dictionary_read_actual_frozen_installation_paths(self):
        for explicit_home in (False, True):
            with self.subTest(explicit_home=explicit_home), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / "selected-installation" if explicit_home else root / "executable"
                conf = home / "conf"
                conf.mkdir(parents=True)
                (conf / "settings.json").write_text(
                    json.dumps({"out_autocut": str(home / "chosen-output")}), encoding="utf-8")
                (conf / "asr_terms.txt").write_text("用户词典\n", encoding="utf-8")
                environment = dict(os.environ)
                environment.pop("SMARTCUT_HOME", None)
                environment["SMARTCUT_TEST_HOME"] = str(home)
                environment["SMARTCUT_TEST_EXE"] = str(root / "executable" / "SmartCut.exe")
                environment["QT_QPA_PLATFORM"] = "offscreen"
                if explicit_home:
                    environment["SMARTCUT_HOME"] = str(home)
                code = "\n".join([
                    "import os, sys",
                    "from pathlib import Path",
                    "sys.frozen = True",
                    "sys.executable = os.environ['SMARTCUT_TEST_EXE']",
                    "import autoedit_qt, ui_autoedit, terms",
                    "home = Path(os.environ['SMARTCUT_TEST_HOME'])",
                    "assert Path(autoedit_qt.BASE) == home",
                    "assert Path(ui_autoedit.BASE) == home",
                    "assert ui_autoedit._load_prefs()['out_dir'] == str(home / 'chosen-output')",
                    "assert terms.load(force=True)['terms'] == {'用户词典'}",
                    "assert Path(terms.dict_path()) == home / 'conf/asr_terms.txt'",
                    "print('portable-ui-paths-ok')",
                ])
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", "-c", code], cwd=BASE,
                    env=environment, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("portable-ui-paths-ok", result.stdout)


class PackagingManifestTests(unittest.TestCase):
    def test_build_script_and_spec_include_model_editor(self):
        _path, build_tree = _tree(os.path.join("build_tools", "build_app.py"))
        build_literals = {node.value for node in ast.walk(build_tree)
                          if isinstance(node, ast.Constant)
                          and isinstance(node.value, str)}
        self.assertIn("model_editor", build_literals)

        _path, spec_tree = _tree(os.path.join("build_tools", "智能剪辑助手.spec"))
        spec_literals = {node.value for node in ast.walk(spec_tree)
                         if isinstance(node, ast.Constant)
                         and isinstance(node.value, str)}
        self.assertIn("model_editor", spec_literals)

    def test_release_staleness_check_covers_new_editing_chain(self):
        _path, tree = _tree("make_release.py")
        literals = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)}
        for filename in (
                "model_editor.py", "autoedit.py", "ui_autoedit.py",
                "autoedit_qt.py", "ui_main_v2.py", "llm_bridge.py"):
            self.assertIn(filename, literals)


if __name__ == "__main__":
    unittest.main()
