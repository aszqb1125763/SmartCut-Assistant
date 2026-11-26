# -*- coding: utf-8 -*-
"""Keep configuration/model paths outside the PyInstaller runtime bundle."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import memory_config


# These tests emulate a frozen application in child interpreters.  On Windows,
# run each child without inheriting the parent's console: a new process group
# still shares that console and can receive its Ctrl+C broadcast, producing the
# opaque 0xC000013A exit before Python can print an assertion.  CREATE_NO_WINDOW
# keeps stdout/stderr pipes intact while isolating the child from console events.
_ISOLATED_PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class InstallationPathTests(unittest.TestCase):
    def test_frozen_fallback_uses_executable_directory(self):
        with tempfile.TemporaryDirectory() as home:
            executable = os.path.join(home, "SmartCut.exe")
            with mock.patch.dict(os.environ, {"SMARTCUT_HOME": ""}), \
                    mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", executable):
                self.assertEqual(os.path.abspath(home), memory_config.app_home())

    def test_explicit_home_wins_over_environment(self):
        with mock.patch.dict(os.environ, {"SMARTCUT_HOME": "wrong-home"}):
            self.assertEqual(os.path.abspath("chosen-home"),
                             memory_config.app_home("chosen-home"))

    def test_modules_read_installed_settings_and_models(self):
        # Import in an isolated process so cached module globals from other
        # tests cannot conceal a bundle-directory regression.
        with tempfile.TemporaryDirectory(prefix="smartcut-install-") as home:
            conf = Path(home, "conf")
            conf.mkdir()
            Path(conf, "settings.json").write_text(json.dumps({
                "llm_provider": "deepseek", "llm_api_model": "installed-model",
                "llm_api_key": "test-only",
            }), encoding="utf-8")
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("SMARTCUT_AI_")}
            environment["SMARTCUT_HOME"] = home
            environment["PYTHONUTF8"] = "1"
            code = "\n".join([
                "import os, sys",
                "sys.frozen = True",
                "import llm_config, llm_models, proofread, script_writer",
                "from ui.llm_bridge import LLM",
                "home = os.path.abspath(os.environ['SMARTCUT_HOME'])",
                "assert all(m.BASE == home for m in "
                "(llm_config, llm_models, proofread, script_writer))",
                "assert llm_config.api_settings()['model'] == 'installed-model'",
                "assert LLM().base == home",
                "assert LLM().api.enabled",
                "assert proofread.TERMS_PATH == os.path.join(home, 'conf', 'terms.txt')",
                "print('installed-paths-ok')",
            ])
            result = subprocess.run(
                [sys.executable, "-c", code], env=environment,
                cwd=str(Path(__file__).resolve().parent), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=_ISOLATED_PROCESS_FLAGS)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("installed-paths-ok", result.stdout)

    def test_remote_mode_does_not_show_local_model_warning(self):
        from cards_ui import _SafeCloseDialog
        with mock.patch("llm_config.api_settings", return_value={"enabled": True}):
            self.assertEqual("", _SafeCloseDialog._check_local_llm(object()))

    def test_index_and_cards_use_real_installed_settings_cache_and_logs(self):
        # Importing cards installs its fatal-error logger. A bundle-directory
        # regression must be caught through this side effect as well as paths.
        for explicit_environment in (False, True):
            with self.subTest(explicit_environment=explicit_environment), \
                    tempfile.TemporaryDirectory(prefix="smartcut-main-paths-") as temp:
                executable_home = Path(temp, "executable")
                executable_home.mkdir()
                home = Path(temp, "selected-installation") if explicit_environment else executable_home
                conf = home / "conf"
                conf.mkdir(parents=True)
                bundle = executable_home / "_internal"
                bundle.mkdir()
                (conf / "settings.json").write_text(json.dumps({
                    "llm_api_model": "installed-main-model",
                    "out_autocut": str(home / "chosen-output"),
                }), encoding="utf-8")
                environment = dict(os.environ)
                for name in ("SMARTCUT_HOME", "SC_WORK", "HF_HOME", "HF_HUB_CACHE",
                             "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE"):
                    environment.pop(name, None)
                environment["SMARTCUT_TEST_INSTALL"] = str(home)
                environment["SMARTCUT_TEST_EXE"] = str(executable_home / "SmartCut.exe")
                environment["SMARTCUT_TEST_BUNDLE"] = str(bundle)
                environment["QT_QPA_PLATFORM"] = "offscreen"
                environment["PYTHONUTF8"] = "1"
                if explicit_environment:
                    environment["SMARTCUT_HOME"] = str(home)
                code = textwrap.dedent("""\
                    import os, sys
                    from pathlib import Path
                    home = Path(os.environ['SMARTCUT_TEST_INSTALL'])
                    bundle = Path(os.environ['SMARTCUT_TEST_BUNDLE'])
                    sys.frozen = True
                    sys.executable = os.environ['SMARTCUT_TEST_EXE']
                    sys._MEIPASS = str(bundle)
                    import index, cards_ui
                    assert Path(index.BASE_DIR) == home, index.BASE_DIR
                    assert Path(cards_ui.SETTINGS_PATH) == home / 'conf/settings.json'
                    assert cards_ui.load_settings()['llm_api_model'] == 'installed-main-model'
                    assert cards_ui.load_settings()['out_autocut'] == str(home / 'chosen-output')
                    assert Path(cards_ui._app_home()) == home
                    assert Path(index.work_root()) == home / '工作目录'
                    assert Path(os.environ['HF_HOME']) == home / 'models/.hf'
                    assert Path(os.environ['HF_HUB_CACHE']) == home / 'models/.hf/hub'
                    assert cards_ui._FAULT_LOG is not None
                    assert Path(cards_ui._FAULT_LOG.name) == home / 'logs/fatal.log'
                    assert (home / 'logs/fatal.log').is_file()
                    assert not list(bundle.iterdir()), list(bundle.rglob('*'))
                    cards_ui._close_faulthandler()
                    print('installed-main-paths-ok')
                    """)
                result = subprocess.run(
                    [sys.executable, "-c", code], env=environment,
                    cwd=str(Path(__file__).resolve().parent), capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=30,
                    creationflags=_ISOLATED_PROCESS_FLAGS)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("installed-main-paths-ok", result.stdout)

    def test_packaged_configuration_reads_and_writes_use_installation(self):
        # Give the installation values which cannot come from source/bundle
        # defaults, then exercise real readers and writers in fresh processes.
        for explicit_environment in (False, True):
            with self.subTest(explicit_environment=explicit_environment), \
                    tempfile.TemporaryDirectory(prefix="smartcut-config-") as home:
                conf = Path(home, "conf")
                conf.mkdir()
                kb = Path(home, "kb")
                kb.mkdir()
                bundle = Path(home, "_internal")
                bundle.mkdir()
                binary = Path(home, "bin", "ffmpeg.exe")
                binary.parent.mkdir()
                binary.touch()
                settings = {
                    "decision.json": {"candidate_rank": {"semantic": 0.123}},
                    "spec_manifest.json": {"installed_marker": "manifest"},
                    "coords_matrix.json": {"installed_marker": "matrix"},
                    "subtitle_style.json": {"size": 73, "font": "Installed Font"},
                    "subtitle_styles.json": {"installed": {"size": 74}},
                    "cut_value.json": {"w": {"const": 0.321}},
                    "bgm.json": {"folder": "installed-bgm", "level_db": "-31"},
                    "settings.json": {"terms_fuzzy": "1"},
                }
                for name, value in settings.items():
                    Path(conf, name).write_text(json.dumps(value), encoding="utf-8")
                Path(kb, "mods_terms.json").write_text(
                    json.dumps({"terms": {"安装词库": "installed.jar"}}),
                    encoding="utf-8")
                Path(kb, "术语_全量.txt").write_text("安装词库\n路径验证\n", encoding="utf-8")
                environment = dict(os.environ)
                environment.pop("SMARTCUT_HOME", None)
                environment.pop("SC_TRAIN_DIR", None)
                environment["SMARTCUT_TEST_INSTALL"] = home
                environment["PYTHONUTF8"] = "1"
                if explicit_environment:
                    environment["SMARTCUT_HOME"] = home
                code = textwrap.dedent("""\
                    import json, os, sys
                    from pathlib import Path
                    from unittest import mock
                    home = Path(os.environ['SMARTCUT_TEST_INSTALL'])
                    sys.frozen = True
                    sys.executable = str(home / 'SmartCut.exe')
                    sys._MEIPASS = str(home / '_internal')
                    import bgm, coords, cutvalue, decision, learn
                    import seam_continuity, subtitles, termbase, terms, terms_fuzzy
                    assert decision.load()['candidate_rank']['semantic'] == 0.123
                    assert coords.manifest()['installed_marker'] == 'manifest'
                    assert coords.matrix()['installed_marker'] == 'matrix'
                    assert subtitles.load_style()['font'] == 'Installed Font'
                    assert subtitles.load_styles()['installed']['size'] == 74
                    subtitles.save_style({'size': 75, 'font': 'Updated Font'})
                    assert json.loads((home / 'conf/subtitle_style.json').read_text(encoding='utf-8'))['size'] == 75
                    assert subtitles.save_named('saved', {'size': 76})
                    assert json.loads((home / 'conf/subtitle_styles.json').read_text(encoding='utf-8'))['saved']['size'] == 76
                    assert cutvalue.load_weights()['const'] == 0.321
                    cutvalue.save_weights({'const': 0.654})
                    assert cutvalue.load_weights()['const'] == 0.654
                    assert bgm.load_conf()['folder'] == 'installed-bgm'
                    bgm.save_conf({'level_db': '-29'})
                    assert json.loads((home / 'conf/bgm.json').read_text(encoding='utf-8'))['level_db'] == '-29'
                    assert termbase.load_all()['安装词库'] == 'installed.jar'
                    assert terms.load(force=True)['terms'] == {'安装词库', '路径验证'}
                    assert terms._fuzzy_enabled()
                    assert terms_fuzzy.train()['terms'] == ['安装词库', '路径验证']
                    assert Path(learn.MODEL_PATH).parent == home / 'conf'
                    assert Path(learn.cache_path('sample.mp4')).is_relative_to(home / 'cache_learn')
                    assert (home / 'cache_learn/cache_learn').is_dir()
                    with mock.patch('index.find_ffmpeg', side_effect=FileNotFoundError):
                        assert Path(seam_continuity._ffmpeg_path()) == home / 'bin/ffmpeg.exe'
                    assert not list((home / '_internal').iterdir())
                    print('installed-config-values-ok')
                    """)
                result = subprocess.run(
                    [sys.executable, "-c", code], env=environment,
                    cwd=str(Path(__file__).resolve().parent), capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=30,
                    creationflags=_ISOLATED_PROCESS_FLAGS)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("installed-config-values-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
