# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import asr
import asr_hints
import autoedit
import index
import proofread


class AsrHintTests(unittest.TestCase):
    def test_terms_are_deduplicated_and_never_cut_mid_word(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            os.makedirs(os.path.join(root, "models", "asr"))
            with open(os.path.join(root, "conf", "asr_terms.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("子枫|子风\n附魔书|父母书\n")
            with open(os.path.join(root, "models", "asr", "term.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("子枫\n重铸\n")
            context, hotwords, selected = asr_hints.build(
                root, extra_terms=["精准采集"], max_hotword_chars=12)

        self.assertEqual(asr_hints.INITIAL_CONTEXT, context)
        self.assertEqual(len(selected), len(set(selected)))
        self.assertLessEqual(len(hotwords), 12)
        self.assertEqual(hotwords, " ".join(selected))
        self.assertIn("精准采集", selected)
        self.assertNotIn("精准", selected, "预算不足时不应切断术语")

    def test_standalone_asr_uses_deterministic_chinese_hotword_mode(self):
        calls = []

        class Model:
            def transcribe(self, _audio, **kwargs):
                calls.append(kwargs)
                return iter(()), SimpleNamespace(duration=0.0)

        with mock.patch.object(asr, "_load", return_value=Model()), \
                mock.patch.object(asr, "_decode", return_value=np.ones(16000)), \
                mock.patch.object(asr, "_hotwords", return_value="项目专用术语"):
            self.assertEqual([], asr.transcribe("sample.wav"))

        options = calls[0]
        self.assertEqual("zh", options["language"])
        self.assertEqual("transcribe", options["task"])
        self.assertEqual(0.0, options["temperature"])
        self.assertFalse(options["condition_on_previous_text"])
        self.assertEqual(asr_hints.INITIAL_CONTEXT, options["initial_prompt"])
        self.assertEqual("项目专用术语", options["hotwords"])

    def test_index_forwards_context_hotwords_without_touching_timestamps(self):
        calls = []

        class Model:
            def transcribe(self, _audio, **kwargs):
                calls.append(kwargs)
                return iter(()), SimpleNamespace(duration=0.0)

        with mock.patch.object(index, "_load_model", return_value=Model()), \
                mock.patch.object(index, "_vad_ready", return_value=False):
            rows = index.transcribe(np.zeros(16000, dtype=np.float32),
                                    initial_prompt="领域", hotwords="附魔 重铸")

        self.assertEqual([], rows)
        self.assertEqual("zh", calls[0]["language"])
        self.assertEqual("领域", calls[0]["initial_prompt"])
        self.assertEqual("附魔 重铸", calls[0]["hotwords"])
        self.assertTrue(calls[0]["word_timestamps"])
        self.assertEqual(0.0, calls[0]["temperature"])


class ContextProofreadTests(unittest.TestCase):
    def test_prompt_uses_readonly_neighbor_context(self):
        prompt = proofread.build_batch(
            [("1", "父母书")], "附魔书 ← 父母书",
            before=[("0", "刚才说到附魔台")],
            after=[("2", "下一步去附魔")])
        self.assertIn("只读上文（不要输出）", prompt)
        self.assertIn("刚才说到附魔台", prompt)
        self.assertIn("待校对（只输出这些编号）", prompt)
        self.assertIn("1. 父母书", prompt)
        self.assertIn("只读下文（不要输出）", prompt)

    def test_only_high_confidence_text_changes_are_applied(self):
        class Server:
            def __init__(self):
                self.calls = []

            def complete(self, prompt, n_predict=0):
                self.calls.append(prompt)
                return "1. 附魔书" if len(self.calls) == 1 else "1. 现在开始吧"

        server = Server()
        changes, low = proofread.correct_texts(
            [("a", "父母书"), ("b", "我们开始")], "附魔书 ← 父母书", server,
            batch=1, wrong_set={"父母"})
        self.assertEqual({"a": "附魔书"}, changes)
        self.assertEqual({"b": "现在开始吧"}, low)
        self.assertIn("只读下文", server.calls[0])
        self.assertIn("只读上文", server.calls[1])

    def test_autoedit_polish_keeps_timestamps_and_rejects_rewrite(self):
        segs = [(1.25, 2.5, "这是浮酸"), (3.0, 4.0, "我们开始")]
        answer = "1|这是氟酸\n2|现在开始吧"
        with mock.patch.object(autoedit, "ask", return_value=answer):
            out = autoedit._polish_subs(object(), segs, batch=2)
        self.assertEqual((1.25, 2.5), out[0][:2])
        self.assertEqual("这是氟酸", out[0][2])
        self.assertEqual((3.0, 4.0, "我们开始"), out[1])


if __name__ == "__main__":
    unittest.main()
