# -*- coding: utf-8 -*-
"""小脑隔离测试（把子枫的三条规矩变成闸门）。

规矩：
    1. 小脑之间不能互相交流 → 不许 import 别的小脑 / 不许拿到兄弟实例
    2. 原始数据**分类打包** → 每个脑只拿到自己声明的那几类，多一类就报错
    3. 小脑不受本地模型干扰 → 不许 import 模型/业务层

任何一条破了，这个文件先红。
"""
from __future__ import annotations

import os
import re
import unittest

import numpy as np

import brains
from brains import dsp
from brains.base import (AudioSource, BrainPlugin, FrameSource, RAW_KINDS,
                         WordSource)

BASE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BASE, "brains")
PLUGIN_FILES = ("audio_brain.py", "speech_brain.py", "picture_brain.py")

FORBIDDEN = ("proofread", "llm_worker", "llm_config", "longchain",
             "director", "autopilot", "cards_ui", "seam_picker", "brain")


class NoInterBrainTalkTest(unittest.TestCase):
    def test_no_plugin_imports_another_plugin(self):
        offenders = []
        for name in PLUGIN_FILES:
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                text = handle.read()
            for other in PLUGIN_FILES:
                stem = other[:-3]
                if stem == name[:-3]:
                    continue
                if re.search(r"(from\s+\.+\s*%s|import\s+%s)" % (stem, stem),
                             text):
                    offenders.append("%s → %s" % (name, stem))
        self.assertEqual([], offenders, "小脑之间不许互相 import：%s" % offenders)

    def test_no_plugin_imports_model_or_business(self):
        offenders = []
        for name in PLUGIN_FILES:
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                text = handle.read()
            for bad in FORBIDDEN:
                if re.search(r"^\s*(import|from)\s+%s\b" % re.escape(bad),
                             text, re.M):
                    offenders.append("%s → %s" % (name, bad))
        self.assertEqual([], offenders, "小脑不许碰模型/业务层：%s" % offenders)

    def test_plugin_only_holds_own_sources(self):
        plugin = brains.load(
            "audio", audio=AudioSource("synthetic", track=0,
                                       ffmpeg="ffmpeg-test"))
        self.assertEqual(set(plugin.src), set(plugin.accepts))
        for attr in vars(plugin).values():
            self.assertNotIsInstance(attr, BrainPlugin,
                                     "小脑实例里出现了另一个小脑")


class ClassifiedSourceTest(unittest.TestCase):
    def test_extra_source_rejected(self):
        with self.assertRaises(TypeError):
            brains.load("audio", audio=AudioSource(
                "synthetic", ffmpeg="ffmpeg-test"),
                        words=WordSource(""))

    def test_wrong_type_rejected(self):
        with self.assertRaises(TypeError):
            brains.load("audio", audio="not-a-source")

    def test_missing_source_rejected(self):
        with self.assertRaises(TypeError):
            brains.load("audio")

    def test_accepts_only_raw_kinds(self):
        for name, cls in brains.available().items():
            self.assertTrue(cls.accepts, "%s 没声明要什么原始数据" % name)
            for kind in cls.accepts:
                self.assertIn(kind, RAW_KINDS,
                              "%s 接受了非原始数据：%s" % (name, kind))

    def test_results_carry_scope(self):
        source = AudioSource("synthetic", track=3, ffmpeg="ffmpeg-test")
        source._audio = np.zeros(1600, dtype=np.float32)
        plugin = brains.load("audio", audio=source)
        result = plugin.probe_band(0.0, 0.1)
        for field in ("brain", "schema_version", "win_ms", "track"):
            self.assertIn(field, result, "探针结果缺口径字段 %s" % field)
        self.assertEqual(3, result["track"])

    def test_picture_only_gets_frames(self):
        with self.assertRaises(TypeError):
            brains.load("picture", frames=FrameSource("synthetic"),
                        audio=AudioSource("synthetic",
                                          ffmpeg="ffmpeg-test"))


class NoDriftTest(unittest.TestCase):
    """小脑用的 dsp 与选点用的 seam_picker 必须是同一个口径（防两把尺子）。"""

    def test_seam_axes_match(self):
        import numpy as np
        import seam_picker as spc
        sr = 16000
        rng = np.random.default_rng(7)
        audio = (0.2 * rng.standard_normal(sr * 3)).astype(np.float32)
        for left, right in ((1.0, 1.5), (2.0, 2.4), (0.5, 2.5)):
            mine = dsp.seam_axes(audio, left, right, sr)
            theirs = spc.seam_axes(audio, left, right, sr)
            self.assertAlmostEqual(mine["energy_db"], theirs["energy_db"], 2)
            self.assertAlmostEqual(mine["tone_hz"], theirs["tone_hz"], 0)
            self.assertAlmostEqual(mine["shape_db"], theirs["shape_db"], 2)
            self.assertEqual(mine["grade"], theirs["grade"])


if __name__ == "__main__":
    unittest.main()
