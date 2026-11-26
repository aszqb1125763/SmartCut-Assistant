# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import script_writer


class _Server:
    replies = []
    prompts = []

    def __init__(self, *args, **kwargs):
        self.task = kwargs.get("task")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def chat(self, system, user, max_tokens=None):
        self.prompts.append((system, user, max_tokens))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class ScriptWriterTimelineTests(unittest.TestCase):
    def setUp(self):
        _Server.replies = []
        _Server.prompts = []
        self.proofread = types.ModuleType("proofread")
        self.proofread.Server = _Server

    def test_prompt_records_are_time_ordered_and_text_is_an_attribute(self):
        data = {
            "duration": 8.0,
            "segments": [[5.0, 6.0, "后说"], [1.0, 2.0, "先说"]],
            "seg_feat": [[8.0, -12.0], [1.0, -30.0]],
            "analysis": {"audio_1s": [-80, -30, -70, -65, -20, -12, -50, -90],
                         "times": [0.0, 3.0, 7.0],
                         "diffs": [0.1, 4.5, 0.2]},
        }
        lines = script_writer._fmt_signals(data)
        speech = [line for line in lines if " | U | " in line]
        self.assertIn("U0001 | U | t=1.00-2.00", speech[0])
        self.assertTrue(speech[0].endswith("文本=先说"))
        self.assertIn("gap-=", speech[0])
        gaps = [line for line in lines if " | G | " in line]
        self.assertTrue(gaps)
        self.assertTrue(any("声音峰=" in line and "画面变化峰=" in line
                            for line in gaps))

    def test_read_pass_maps_event_id_back_to_original_source_row(self):
        data = {
            "duration": 8.0,
            "segments": [[5.0, 6.0, "后说"], [1.0, 2.0, "先说"]],
            "seg_feat": [[8.0, -12.0], [1.0, -30.0]],
        }
        _Server.replies = ["摘要：按时间发生\n重要度：4\n关键：U0001"]
        with mock.patch.dict(sys.modules, {"proofread": self.proofread}):
            chunks = script_writer.read_pass(data, batch=50, on_log=lambda _m: None)
        self.assertEqual([1], chunks[0]["keys"])
        self.assertEqual(["U0001"], chunks[0]["key_event_ids"])
        self.assertEqual("B001", chunks[0]["chunk_id"])
        prompt = _Server.prompts[0][1]
        self.assertLess(prompt.index("t=1.00-2.00"),
                        prompt.index("t=5.00-6.00"))

    def test_event_batches_obey_character_and_token_budgets(self):
        data = {
            "duration": 24.0,
            "segments": [
                [index * 2.0, index * 2.0 + 1.0,
                 "长中文台词" * 45]
                for index in range(12)
            ],
            "seg_feat": [[1.0, -20.0] for _ in range(12)],
        }
        events = script_writer._source_events(data)
        batches = script_writer._event_batches(
            events, max_speech=50, data=data,
            max_chars=1000, max_tokens=900)

        flattened = [event["event_id"]
                     for part in batches for event in part]
        self.assertEqual([event["event_id"] for event in events], flattened)
        self.assertGreater(len(batches), 1)
        for part in batches:
            body = "\n".join(script_writer._fmt_event(data, event)
                             for event in part)
            if len(part) > 1:
                self.assertLessEqual(len(body), 1000)
                self.assertLessEqual(script_writer._estimate_tokens(body), 900)

    def test_read_pass_splits_oversized_http_400_and_logs_body(self):
        data = {
            "duration": 8.0,
            "segments": [
                [0.0, 1.0, "第一句"],
                [2.0, 3.0, "第二句"],
                [4.0, 5.0, "第三句"],
                [6.0, 7.0, "第四句"],
            ],
            "seg_feat": [[1.0, -20.0] for _ in range(4)],
        }
        error = urllib.error.HTTPError(
            url="http://127.0.0.1", code=400, msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error":{"message":"prompt exceeds context window"}}'))
        _Server.replies = [
            error,
            "摘要：前半段\n重要度：3\n关键：U0001",
            "摘要：后半段\n重要度：4\n关键：U0003",
        ]
        logs = []
        with mock.patch.dict(sys.modules, {"proofread": self.proofread}):
            chunks = script_writer.read_pass(
                data, batch=50, on_log=logs.append)

        self.assertEqual(3, len(_Server.prompts))
        self.assertEqual(2, len(chunks))
        self.assertEqual(["前半段", "后半段"],
                         [chunk["summary"] for chunk in chunks])
        self.assertIn("prompt exceeds context window", "\n".join(logs))
        self.assertTrue(any("自动拆为 2 + 2 句重试" in line
                            for line in logs))

    def test_writer_revision_changes_when_model_visible_audio_changes(self):
        first = {"duration": 3.0, "segments": [[1.0, 2.0, "同一句"]],
                 "seg_feat": [[1.0, -30.0]],
                 "analysis": {"audio_1s": [-80, -30, -80]}}
        second = {**first, "seg_feat": [[8.0, -10.0]]}
        first_revision = script_writer.source_timeline.revision(
            script_writer._source_events(first))
        second_revision = script_writer.source_timeline.revision(
            script_writer._source_events(second))
        self.assertNotEqual(first_revision, second_revision)

    def test_plan_uses_chunk_ids_to_resolve_real_time_boundaries(self):
        chunks = [
            {"idx": 1, "chunk_id": "B001", "start": 2.0, "end": 9.0,
             "summary": "开场", "importance": 4},
            {"idx": 2, "chunk_id": "B002", "start": 9.5, "end": 18.0,
             "summary": "过程", "importance": 3},
            {"idx": 3, "chunk_id": "B003", "start": 20.0, "end": 30.0,
             "summary": "结果", "importance": 5},
        ]
        _Server.replies = [
            "1|B001|B002|目标和过程|全部保留\n"
            "2|B003|B003|结果|全部保留\n"
            "主线：完成测试"
        ]
        with mock.patch.dict(sys.modules, {"proofread": self.proofread}):
            sections, mainline = script_writer.plan_pass(
                chunks, on_log=lambda _m: None, level="medium")
        self.assertEqual("完成测试", mainline)
        self.assertEqual((2.0, 18.0),
                         (sections[0]["start"], sections[0]["end"]))
        self.assertEqual(["B001", "B002"],
                         sections[0]["source_chunks"])
        self.assertIn("章节编号|开始段Bxxx|结束段Bxxx", _Server.prompts[0][1])

    def test_changed_text_does_not_reuse_old_read_pass_chunks(self):
        old_data = {"duration": 3.0,
                    "segments": [[1.0, 2.0, "旧文字"]],
                    "seg_feat": [[1.0, -20.0]]}
        old_revision = script_writer.source_timeline.revision(
            script_writer._source_events(old_data))
        old_chunk = {
            "idx": 1, "chunk_id": "B001", "start": 0.0, "end": 3.0,
            "summary": "旧摘要", "importance": 2,
        }
        script = {
            "mainline": "旧主线",
            "sections": [{"title": "旧章", "start": 0.0, "end": 3.0,
                          "advice": "全部保留"}],
            "chunks": [old_chunk], "sentences": 1,
            "cut_level": "medium", "mode": "gameplay",
            "timeline_schema": script_writer.source_timeline.SCHEMA,
            "timeline_revision": old_revision,
            "decision_space": "source_timeline_event_ids",
        }
        new_data = dict(old_data)
        new_data["segments"] = [[1.0, 2.0, "纠正后的文字"]]
        new_data["script"] = script
        new_data["script_variants"] = {"gameplay:medium": script}
        fresh_chunks = [{
            "idx": 1, "chunk_id": "B001", "start": 0.0, "end": 3.0,
            "summary": "新摘要", "importance": 4,
            "keys": [0], "key_event_ids": ["U0001"],
            "event_ids": ["G0001", "U0001", "G0002"],
        }]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "index.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(new_data, handle, ensure_ascii=False)
            with mock.patch.object(script_writer, "read_pass",
                                   return_value=fresh_chunks) as read, \
                    mock.patch.object(script_writer, "plan_pass",
                                      return_value=([{
                                          "title": "新章", "start": 0.0,
                                          "end": 3.0, "advice": "全部保留",
                                          "source_chunks": ["B001"],
                                      }], "新主线")):
                count = script_writer.build(
                    path, on_log=lambda _m: None, level="medium",
                    mode="gameplay", reuse_chunks=True)
            self.assertEqual(1, count)
            read.assert_called_once()
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual("新摘要", saved["script"]["chunks"][0]["summary"])
            self.assertNotEqual(old_revision,
                                saved["script"]["timeline_revision"])


if __name__ == "__main__":
    unittest.main()

