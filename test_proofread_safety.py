# -*- coding: utf-8 -*-
"""Non-destructive subtitle proofread gates and persisted corrected timeline."""
import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

import ai_api_contracts as contracts
import proofread


class CorrectionSafetyTests(unittest.TestCase):
    def test_accepts_explicit_glossary_but_homophones_need_trusted_evidence(self):
        pairs = [("水槽 战士", "水槽无战事"), ("父母书", "附魔书")]
        self.assertTrue(proofread.accept_correction("这里是水槽 战士", "这里是水槽无战事", pairs))
        self.assertFalse(proofread.accept_correction("这是浮酸", "这是氟酸"))
        self.assertTrue(proofread.accept_correction(
            "这是浮酸", "这是氟酸", [("浮酸", "氟酸")]))
        self.assertFalse(proofread.accept_correction("拿起吉他", "拿起吉它"))
        self.assertTrue(proofread.accept_correction(
            "拿起吉他", "拿起吉它", allow_homophone=True))

    def test_relevant_pairs_bound_a_large_knowledge_base(self):
        entries = [("无关术语%d" % index, [], "kb") for index in range(200)]
        entries.append(("水槽无战事", ["水槽 战士"], "series"))
        pairs = proofread.relevant_known_pairs(["这里是水槽 战士"], entries)
        self.assertIn(("水槽 战士", "水槽无战事"), pairs)
        self.assertLess(len(pairs), 10)

    def test_phonetic_lookup_surfaces_canonical_terms_without_aliases(self):
        entries = [
            ("量子锭", [], "模组词库"),
            ("闪电装备室", [], "模组词库"),
            ("完全无关", [], "模组词库"),
        ]
        chosen, _used = proofread.select_entries(
            ["不能用量子定的配方，还需要闪电装备是"], entries)
        terms = {row[0] for row in chosen}
        self.assertIn("量子锭", terms)
        self.assertIn("闪电装备室", terms)
        self.assertNotIn("完全无关", terms)

    def test_only_explicit_glossary_aliases_authorise_rule_writes(self):
        entries = [
            ("量子灌注液", ["量子关注液"], "kb"),
            ("普通词甲", ["东西"], "kb"),
            ("冲突术语甲", ["相同错误名"], "kb"),
            ("冲突术语乙", ["相同错误名"], "kb"),
            ("未出现术语", ["没有出现在本片"], "kb"),
        ]
        pairs = proofread.trusted_known_pairs(
            ["这里需要很多量子关注液，也提到了相同错误名和东西"],
            entries, "量子灌注液 ← 用户明确错写")
        self.assertIn(("用户明确错写", "量子灌注液"), pairs)
        self.assertNotIn(("量子关注液", "量子灌注液"), pairs)
        self.assertNotIn(("东西", "普通词甲"), pairs)
        self.assertFalse(any(wrong == "相同错误名" for wrong, _right in pairs))
        self.assertFalse(any(wrong == "没有出现在本片" for wrong, _right in pairs))

    def test_correct_texts_keeps_kb_alias_prompt_only(self):
        class Server:
            def complete(self, _prompt, n_predict=0):
                return "1. 还需要量子关注液"

        changes, low = proofread.correct_texts(
            [("a", "还需要量子关注液")], "", Server(),
            kb_all=[("量子灌注液", ["量子关注液"], "测试")],
            allow_context_homophones=False)
        self.assertEqual({}, changes)
        self.assertEqual({}, low)

    def test_rejects_destructive_or_non_homophone_rewrites(self):
        cases = (
            ("这里是水槽 吴战士 继续来 我", "这里是水槽 战士 继续来 我"),
            ("二阶火箭", "二二阶火箭"),
            ("材料已经准备好了", "材料已经准备好…"),
            ("材料……准备好了", "材料准备好了"),
            ("我现在开始测试", "你现在开始测试"),
            ("这里是你们的基地", "这里是他们的基地"),
            ("准备了三组材料", "准备了四组材料"),
            ("开始测试。", "开始测试！"),
            ("材料准备好了", "总结：材料已完成"),
            ("我们开始", "现在开始吧"),
        )
        for old, new in cases:
            with self.subTest(old=old, new=new):
                self.assertTrue(proofread.correction_violation(old, new))
                self.assertFalse(proofread.accept_correction(old, new))

    def test_duplicate_or_extra_numbered_rows_fail_closed(self):
        self.assertIsNone(proofread.parse_batch("1. 甲\n1. 乙\n2. 丙", 2))
        self.assertIsNone(proofread.parse_batch("1. 甲\n解释文字\n2. 丙", 2))

    def test_correct_texts_keeps_unsafe_suggestion_as_low_confidence(self):
        class Server:
            def complete(self, _prompt, n_predict=0):
                return "1. 你现在开始测试\n2. 这是氟酸"

        changes, low = proofread.correct_texts(
            [("a", "我现在开始测试"), ("b", "这是浮酸")],
            "氟酸 ← 浮酸", Server())
        self.assertEqual({"b": "这是氟酸"}, changes)
        self.assertEqual({"a": "你现在开始测试"}, low)

    def test_context_model_accepts_only_same_length_homophone_replacement(self):
        class Server:
            def complete(self, _prompt, n_predict=0):
                return "1. 我们在入口会合\n2. 拿好工具出发"

        changes, low = proofread.correct_texts(
            [("a", "我们在入口汇合"), ("b", "拿好工具出发")], "", Server())
        self.assertEqual({"a": "我们在入口会合"}, changes)
        self.assertEqual({}, low)

    def test_context_model_rejects_unknown_non_homophone_rewrite(self):
        class Server:
            def complete(self, _prompt, n_predict=0):
                return "1. 我们在入口集合"

        changes, low = proofread.correct_texts(
            [("a", "我们在入口汇合")], "", Server())
        self.assertEqual({}, changes)
        self.assertEqual({"a": "我们在入口集合"}, low)

    def test_malformed_large_batch_never_falls_back_to_one_call_per_sentence(self):
        class Server:
            def __init__(self):
                self.calls = 0

            def complete(self, _prompt, n_predict=0):
                self.calls += 1
                return "格式不合格"

        server = Server()
        items = [(str(index), "第%d句保持原样" % index) for index in range(32)]
        changes, low = proofread.correct_texts(items, "", server, batch=16)
        self.assertEqual({}, changes)
        self.assertEqual({}, low)
        self.assertLessEqual(server.calls, 6)

    def test_context_defaults_to_four_neighbor_sentences(self):
        prompts = []

        class Server:
            def complete(self, prompt, n_predict=0):
                prompts.append(prompt)
                return "1. " + next(line.split(". ", 1)[1] for line in prompt.splitlines()
                                    if line.startswith("1. "))

        items = [(str(index), "第%d句" % index) for index in range(9)]
        proofread.correct_texts(items, "", Server(), batch=1)
        middle = prompts[4]
        for index in range(4):
            self.assertIn("第%d句" % index, middle)
        for index in range(5, 9):
            self.assertIn("第%d句" % index, middle)


class RemoteContractSafetyTests(unittest.TestCase):
    def test_numbered_contract_rejects_person_and_ellipsis_changes(self):
        user = proofread.build_batch(
            [("a", "我现在开始测试"), ("b", "材料准备好了")], "")
        for output in ("1. 你现在开始测试\n2. 材料准备好了",
                       "1. 我现在开始测试\n2. 材料准备好…"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                contracts.validate("proofread", output, user)

    def test_pipe_contract_rejects_deletion_and_accepts_explicit_term(self):
        destructive = "待校对（只输出这些编号）：\n41|这里是水槽 吴战士"
        with self.assertRaises(ValueError):
            contracts.validate("proofread_pipe", "41|这里是水槽 战士", destructive)
        glossary = ("水槽无战事 ← 水槽 战士\n"
                    "待校对（只输出这些编号）：\n41|这里是水槽 战士")
        self.assertEqual("41|这里是水槽无战事",
                         contracts.validate("proofread_pipe", "41|这里是水槽无战事", glossary))

    def test_json_subtitle_contract_reads_text_after_time_columns(self):
        user = "1 | 0~1 | 我现在开始\n2 | 1~2 | 材料好了"
        valid = '{"fixed":["我现在开始","材料好了"]}'
        self.assertEqual(valid, contracts.validate("subtitle_fixed", valid, user))
        with self.assertRaises(ValueError):
            contracts.validate("subtitle_fixed",
                               '{"fixed":["你现在开始","材料好…"]}', user)


class ProofreadPersistenceTests(unittest.TestCase):
    def test_main_writes_parallel_segment_timeline_without_overwriting_raw(self):
        source = {
            "name": "sample",
            "segments": [[1.25, 2.5, "这是浮酸"], [3.0, 4.0, "我们开始"]],
            "cards": [{"id": "c0001", "start": 1.25, "end": 4.0,
                       "text": "卡片摘要保持不动"}],
        }

        class Server:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def complete(self, _prompt, n_predict=0):
                return "1. 这是氟酸\n2. 我们开始"

        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "index.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(source, handle, ensure_ascii=False)
            with mock.patch.object(proofread, "Server", return_value=Server()), \
                    mock.patch.object(proofread, "load_resources", return_value=(
                        "氟酸 ← 浮酸", {"浮酸"}, [])):
                self.assertEqual(0, proofread.main([path]))
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)

        self.assertEqual(source["segments"], saved["segments"])
        self.assertEqual([[1.25, 2.5, "这是氟酸"], [3.0, 4.0, "我们开始"]],
                         saved["segments_proofread"])
        self.assertEqual("卡片摘要保持不动", saved["cards"][0]["text"])


if __name__ == "__main__":
    unittest.main()
