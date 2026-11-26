# -*- coding: utf-8 -*-
"""Focused regression tests for the multimodal model editor stage."""
from __future__ import annotations

import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import model_editor


def _unit(start, end, text):
    return {"a": float(start), "b": float(end), "text": text}


class _Runtime:
    ARTIFACTS = {"editor": "CUT-V3"}

    def __init__(self, tracks=None):
        self.src = "example.mp4"
        self.idx = "example-index"
        self.mode = "gameplay"
        self.level = "medium"
        self.tracks = tracks
        self.should_cancel = lambda: False
        self.messages = []
        self.saved_plan = None

    def log(self, message):
        self.messages.append(str(message))

    def payload(self, _ctx, _stage):
        return {"index_dir": self.idx}

    def selected_tracks(self, data):
        if self.tracks is not None:
            return list(self.tracks)
        return [row.get("ordinal", index)
                for index, row in enumerate(data.get("audio_streams") or [])]

    def source_identity(self):
        return {"path": self.src, "sha256": "source"}

    def write_plan(self, _stage, plan, _artifact_type):
        self.saved_plan = plan
        return "editor-plan.json"

    def done(self, _label, _started):
        return None

    def artifact(self, stage):
        return "artifact-%s" % stage


class ApplicationBaseTests(unittest.TestCase):
    def test_smartcut_home_overrides_source_location(self):
        configured = os.path.join("C:\\", "SmartCut-Test")
        with mock.patch.dict(os.environ, {"SMARTCUT_HOME": configured}):
            self.assertEqual(
                os.path.abspath(configured), model_editor._application_base())

    def test_frozen_build_uses_executable_directory(self):
        executable = os.path.join("D:\\", "SmartCut", "智能剪辑助手.exe")
        with mock.patch.dict(os.environ, {"SMARTCUT_HOME": ""}), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", executable):
            self.assertEqual(os.path.dirname(os.path.abspath(executable)),
                             model_editor._application_base())


class AudioEvidenceTests(unittest.TestCase):
    def test_explicit_tracks_are_zero_based_deduplicated_and_filtered(self):
        runtime = SimpleNamespace(tracks="2, 0, 2, bad, -1")
        data = {"audio_streams": [{}, {}, {}], "audio_stream_used": 1}
        self.assertEqual([2, 0], model_editor._selected_tracks(runtime, data))

    def test_old_runtime_falls_back_to_index_selection(self):
        runtime = SimpleNamespace(tracks=None)
        data = {"audio_streams": [{}, {}, {}],
                "audio_stream_used": [2, 1, 2]}
        self.assertEqual([2, 1], model_editor._selected_tracks(runtime, data))

    def test_runtime_selection_api_has_priority(self):
        runtime = SimpleNamespace(
            tracks=[0], selected_tracks=lambda _data: [1, 0])
        data = {"audio_streams": [{}, {}], "audio_stream_used": 0}
        self.assertEqual([1, 0], model_editor._selected_tracks(runtime, data))

    def test_each_track_is_decoded_separately_then_labels_are_combined(self):
        runtime = _Runtime(tracks=[0, 2])
        data = {"audio_streams": [{}, {}, {}], "audio_stream_used": 1}
        units = [_unit(0, 1, "甲"), _unit(1, 2, "乙")]

        def fake_audio(_src, ordinal):
            return np.asarray([ordinal + 1], dtype=np.float32)

        def fake_tags(_units, audio):
            marker = int(audio[0])
            return ["强%d·正常·渐变" % marker,
                    "弱%d·慢·平稳" % marker]

        with mock.patch.object(model_editor, "_track_audio",
                               side_effect=fake_audio) as decode, \
                mock.patch.object(model_editor, "_voice_tags",
                                  side_effect=fake_tags):
            tags, tracks, reports = model_editor._multi_track_tags(
                runtime, data, units)

        self.assertEqual([0, 2], tracks)
        self.assertEqual([0, 2], [call.args[1] for call in decode.call_args_list])
        self.assertEqual(
            "轨1:强1·正常·渐变；轨3:强3·正常·渐变", tags[0])
        self.assertEqual([2, 2],
                         [row["tagged_sentences"] for row in reports])
        self.assertTrue(all(row["decoded"] for row in reports))

    def test_packed_portraits_map_back_by_time_instead_of_list_position(self):
        units = [_unit(0.0, 0.20, "甲"),
                 _unit(0.30, 0.50, "乙"),
                 _unit(1.20, 1.50, "丙")]
        packed = [
            {"t0": 0.0, "t1": 0.5, "level": "合并",
             "speed": "正常", "shape": "平"},
            {"t0": 1.2, "t1": 1.5, "level": "第三句",
             "speed": "快", "shape": "升"},
        ]
        with mock.patch("utterance.pack", return_value=packed):
            tags = model_editor._voice_tags(
                units, np.zeros(16000 * 2, dtype=np.float32))

        self.assertEqual(3, len(tags))
        self.assertEqual("合并·正常·平", tags[0])
        self.assertEqual("合并·正常·平", tags[1])
        self.assertEqual("第三句·快·升", tags[2])

    def test_runtime_audio_cache_is_shared_and_audited_per_track(self):
        runtime = _Runtime(tracks=[0, 1])
        units = [_unit(0.0, 0.5, "甲"), _unit(1.0, 1.5, "乙")]

        class FakeCache:
            def __init__(self):
                self.entry_calls = []

            def entry(self, track):
                self.entry_calls.append(track)
                return {"key": "key-%d" % track, "sample_count": 100,
                        "reused": track == 0}

        cache = FakeCache()
        runtime.ensure_audio_cache = mock.Mock(return_value=cache)

        def fake_cached_tags(_units, _cache, track, cancel_check=None):
            del cancel_check
            return ["轨%d甲" % track, "轨%d乙" % track]

        with mock.patch.object(model_editor, "_voice_tags_from_cache",
                               side_effect=fake_cached_tags), \
                mock.patch.object(model_editor, "_track_audio") as legacy:
            tags, tracks, reports = model_editor._multi_track_tags(
                runtime, {"audio_streams": [{}, {}]}, units)

        runtime.ensure_audio_cache.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual([0, 1], cache.entry_calls)
        self.assertEqual([0, 1], tracks)
        self.assertIn("轨1:轨0甲", tags[0])
        self.assertIn("轨2:轨1甲", tags[0])
        self.assertEqual(["key-0", "key-1"],
                         [row["cache_key"] for row in reports])
        self.assertEqual([True, False], [row["reused"] for row in reports])
        self.assertTrue(all(row["cache_source"] == "runtime"
                            for row in reports))

    def test_owned_global_cache_prepares_once_and_always_closes(self):
        runtime = _Runtime(tracks=[0, 1])
        units = [_unit(0.0, 0.5, "甲")]

        class FakeCache:
            def __init__(self):
                self.prepare_calls = 0
                self.close_calls = 0

            def prepare(self):
                self.prepare_calls += 1
                return {}

            def entry(self, track):
                return {"key": "owned-%d" % track, "sample_count": 50,
                        "reused": False}

            def close(self):
                self.close_calls += 1

        cache = FakeCache()
        with mock.patch("audio_evidence_cache.AudioEvidenceCache",
                        return_value=cache) as factory, \
                mock.patch.object(model_editor, "_voice_tags_from_cache",
                                  return_value=["中·正常·平"]):
            _tags, _tracks, reports = model_editor._multi_track_tags(
                runtime, {"audio_streams": [{}, {}]}, units)

        factory.assert_called_once()
        self.assertNotIn("cache_dir", factory.call_args.kwargs)
        self.assertEqual(1, cache.prepare_calls)
        self.assertEqual(1, cache.close_calls)
        self.assertTrue(all(row["cache_source"] == "global"
                            for row in reports))

    def test_cache_cancellation_becomes_workflow_cancellation(self):
        import audio_evidence_cache
        import workflow_engine

        runtime = _Runtime(tracks=[0])
        runtime.ensure_audio_cache = mock.Mock(
            side_effect=audio_evidence_cache.AudioEvidenceCancelled("stop"))
        with self.assertRaises(workflow_engine.WorkflowCancelled):
            model_editor._multi_track_tags(
                runtime, {"audio_streams": [{}]}, [_unit(0, 1, "甲")])


class VisualEvidenceTests(unittest.TestCase):
    def test_every_chapter_has_start_middle_end_audit_rows(self):
        runtime = _Runtime()
        chapters = [(0.0, 10.0), (10.0, 20.0)]

        def fake_frame(_engine, _src, seconds, _log):
            return "画面%.1f" % seconds

        with mock.patch.object(model_editor, "_frame_text",
                               side_effect=fake_frame):
            frames, notes, evidence = model_editor._collect_visual_evidence(
                runtime, chapters, engine=object())

        self.assertEqual(6, len(evidence))
        self.assertEqual(["开头", "中间", "结尾"],
                         [row["label"] for row in evidence[:3]])
        self.assertIn("开头@", frames[1])
        self.assertIn("关键帧 3/3 可用", notes[1])
        self.assertTrue(all(row["available"] for row in evidence))


class ChapterCompressionTests(unittest.TestCase):
    def test_more_than_twelve_chapters_partition_without_overlap_or_duplicates(self):
        duration = 48.0
        shots = [float(value) for value in range(2, 48, 2)]
        chapters = model_editor._chapters(
            [], shots, duration, min_len=1.0)

        self.assertEqual(model_editor.MAX_CHAPTERS, len(chapters))
        self.assertEqual((0.0, 4.0), chapters[0])
        self.assertEqual((44.0, 48.0), chapters[-1])
        for previous, current in zip(chapters, chapters[1:]):
            self.assertEqual(previous[1], current[0])

        units = [_unit(value + 0.25, value + 0.75, "U%d" % value)
                 for value in range(0, 48, 2)]
        rows = model_editor._chapter_lines(units, chapters)
        speech_ids = [event["event_id"]
                      for _chapter, _start, _end, inside in rows
                      for _source_pos, event in inside
                      if event["kind"] == "speech"]
        self.assertEqual(24, len(speech_ids))
        self.assertEqual(24, len(set(speech_ids)))
        self.assertEqual(["U%04d" % value for value in range(1, 25)],
                         speech_ids)


class TimelineEventTests(unittest.TestCase):
    def test_ids_absolute_times_and_track_state_survive_chapters_and_batches(self):
        chapters = [(0.0, 5.0), (5.0, 10.0)]
        # Deliberately provide source rows out of order.  Event ids must follow
        # absolute time while tags must still follow their source-row index.
        units = [_unit(6.25, 7.0, "后半段"),
                 _unit(1.0, 2.5, "前半段")]
        tags = ["轨1:弱·慢·平稳；轨2:强·快·渐变",
                "轨1:中·正常·平稳；轨2:弱·慢·收束"]

        events = model_editor._timeline_events(units, chapters, tags=tags)
        speech = [row for row in events if row["kind"] == "speech"]
        self.assertEqual(["U0001", "U0002"],
                         [row["event_id"] for row in speech])
        self.assertEqual([1.0, 6.25], [row["a"] for row in speech])
        self.assertEqual([1, 2], [row["chapter"] for row in speech])
        self.assertIn("轨2:弱", speech[0]["audio"])
        self.assertIn("轨2:强", speech[1]["audio"])

        rows = model_editor._chapter_lines(units, chapters, tags=tags)
        batches = model_editor._batch_rows(
            rows, {1: "画面1", 2: "画面2"},
            {1: "关键帧", 2: "关键帧"}, tags=tags, max_chars=360)
        combined = "\n".join(batch["body"] for batch in batches)
        self.assertGreater(len(batches), 1)
        self.assertEqual(1, combined.count("U0001 C1 t1.00-2.50"))
        self.assertEqual(1, combined.count("U0002 C2 t6.25-7.00"))
        self.assertIn("听[轨1:中·正常·平稳；轨2:弱·慢·收束]", combined)

    def test_meaningful_no_speech_span_is_an_explicit_gap_event(self):
        units = [_unit(1.0, 2.0, "甲"), _unit(5.0, 6.0, "乙")]
        events = model_editor._timeline_events(
            units, [(0.0, 8.0)], min_gap=0.35)
        middle = [row for row in events
                  if row["kind"] == "gap"
                  and row["a"] == 2.0 and row["b"] == 5.0]
        self.assertEqual(1, len(middle))
        self.assertEqual(3.0, middle[0]["duration"])

        body = model_editor._body(
            model_editor._chapter_lines(units, [(0.0, 8.0)]),
            {}, {})
        self.assertRegex(body, r"G\d{4} C1 t2\.00-5\.00 d3\.00 G")
        self.assertIn("无台词候选区3.00", body)

    def test_overlap_and_next_safe_margin_use_real_timeline(self):
        units = [_unit(1.0, 4.0, "主句"),
                 _unit(2.5, 3.5, "同时声音"),
                 _unit(5.0, 6.0, "下一句")]
        events = model_editor._timeline_events(units, [(0.0, 8.0)])
        speech = [row for row in events if row["kind"] == "speech"]

        self.assertEqual(1.5, speech[0]["next_overlap"])
        self.assertEqual(1.0, speech[0]["safe_after"])
        self.assertEqual(1.5, speech[1]["prev_overlap"])
        # The short nested event ends while the main event is still sounding.
        self.assertEqual(0.0, speech[1]["safe_after"])
        self.assertEqual(1.0, speech[2]["prev_gap"])

        rows = model_editor._chapter_lines(units, [(0.0, 8.0)])
        atoms = model_editor._split_row_for_budget(
            rows[0], {}, {}, [], [], max_chars=160)
        event_groups = [[event["event_id"] for _pos, event in atom["row"][3]]
                        for atom in atoms]
        containing_main = [group for group in event_groups if "U0001" in group]
        self.assertEqual(1, len(containing_main))
        self.assertIn("U0002", containing_main[0])

    def test_compact_event_metadata_stays_inside_batch_budget(self):
        units = [_unit(index * 1.1, index * 1.1 + 0.9,
                       "句%d" % index)
                 for index in range(20)]
        chapters = [(0.0, 22.0)]
        rows = model_editor._chapter_lines(units, chapters,
                                           tags=["轨1:中·正常·平稳"] * 20)
        batches = model_editor._batch_rows(
            rows, {}, {}, tags=["轨1:中·正常·平稳"] * 20,
            max_chars=500)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(batch["body"]) <= 500 for batch in batches))
        combined = "\n".join(batch["body"] for batch in batches)
        for index in range(1, 21):
            self.assertEqual(1, combined.count("U%04d " % index))
        expected_ids = [event["event_id"]
                        for event in model_editor._timeline_events(
                            units, chapters,
                            tags=["轨1:中·正常·平稳"] * 20)]
        core_ids = [event_id for batch in batches
                    for event_id in batch["core_event_ids"]]
        self.assertEqual(expected_ids, core_ids)
        self.assertEqual(len(core_ids), len(set(core_ids)))

    def test_batch_invariant_rejects_omitted_or_duplicate_core_events(self):
        units = [_unit(0.5, 1.0, "甲"), _unit(2.0, 2.5, "乙")]
        rows = model_editor._chapter_lines(units, [(0.0, 3.0)])
        batches = model_editor._batch_rows(rows, {}, {}, max_chars=180)
        expected = [event_id for batch in batches
                    for event_id in batch["core_event_ids"]]
        with self.assertRaisesRegex(RuntimeError, "重复或遗漏"):
            model_editor._attach_neighbor_context(
                batches, expected + ["U9999"], [], [], context_events=1)

        duplicate = [dict(batch) for batch in batches]
        duplicate[0]["core_event_ids"] = (
            list(duplicate[0]["core_event_ids"])
            + [duplicate[0]["core_event_ids"][0]])
        with self.assertRaisesRegex(RuntimeError, "重复 event_id"):
            model_editor._attach_neighbor_context(
                duplicate, [], [], [], context_events=1)


class ChapterBatchTests(unittest.TestCase):
    def _fixture(self):
        chapters = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
        units = []
        expected = []
        for chapter in range(3):
            for offset in range(5):
                marker = "C%dU%d" % (chapter + 1, offset + 1)
                expected.append(marker)
                start = chapter * 10 + offset * 1.5 + 0.2
                units.append(_unit(start, start + 1.0,
                                   marker + " 完整句子内容" * 2))
        rows = model_editor._chapter_lines(units, chapters)
        frames = {index: "第%d章视觉" % index for index in range(1, 4)}
        notes = {index: "关键帧 3/3 可用" for index in range(1, 4)}
        return rows, frames, notes, expected

    def test_long_script_reaches_later_chapters_without_char_truncation(self):
        rows, frames, notes, expected = self._fixture()
        batches = model_editor._batch_rows(
            rows, frames, notes, max_chars=280)
        self.assertGreater(len(batches), 1)
        combined = "\n".join(batch["body"] for batch in batches)
        for marker in expected:
            self.assertIn(marker, combined)
        self.assertIn("【第 3 章】", combined)
        self.assertNotIn("后面还有内容，已按章压缩", combined)

        class FakeLLM:
            def __init__(self):
                self.prompts = []

            def chat(self, _system, prompt, max_tokens=None):
                self.prompts.append(prompt)
                return json.dumps({"delete": [[-100, 999]],
                                   "reason": "本批测试"}, ensure_ascii=False)

        llm = FakeLLM()
        result = model_editor._ask_batches(
            llm, batches, "medium", 30.0, lambda _message: None)
        self.assertEqual(len(batches) * 2, len(llm.prompts))
        self.assertTrue(any("上次使用了自由秒数" in prompt
                            for prompt in llm.prompts))
        self.assertTrue(any("C3" in prompt for prompt in llm.prompts))
        for row in result["batches"]:
            self.assertEqual("legacy_seconds_projected", row["decision_mode"])
            self.assertTrue(row["projected_from_seconds"])
            self.assertTrue(row["accepted_event_ids"])
            for left, right in row["delete"]:
                self.assertGreaterEqual(left, row["start"])
                self.assertLessEqual(right, row["end"])

    def test_event_decisions_cannot_delete_readonly_neighbor_context(self):
        units = [_unit(index * 2.0 + 0.25, index * 2.0 + 1.25,
                       "事件%d" % index + "内容" * 8)
                 for index in range(6)]
        rows = model_editor._chapter_lines(units, [(0.0, 13.0)])
        batches = model_editor._batch_rows(
            rows, {}, {}, max_chars=230, context_events=2)
        self.assertGreaterEqual(len(batches), 3)
        target = batches[1]
        self.assertTrue(target["readonly_before_ids"])
        self.assertTrue(target["readonly_after_ids"])
        before_id = target["readonly_before_ids"][0]
        after_id = target["readonly_after_ids"][0]
        first_core = target["core_event_ids"][0]
        last_core = target["core_event_ids"][-1]

        decision = model_editor._resolve_batch_decisions({
            "delete_events": [before_id, first_core, after_id, "U9999"],
            "delete_ranges": [[first_core, last_core],
                              [before_id, first_core]],
        }, target, duration=13.0)

        self.assertEqual("event_ids", decision["decision_mode"])
        self.assertIn(first_core, decision["accepted_event_ids"])
        self.assertIn(before_id, decision["rejected_context_ids"])
        self.assertIn(after_id, decision["rejected_context_ids"])
        self.assertIn("U9999", decision["rejected_unknown_ids"])
        core_start = min(float(event["a"]) for event in target["events"])
        core_end = max(float(event["b"]) for event in target["events"])
        for left, right in decision["delete"]:
            self.assertGreaterEqual(left, core_start)
            self.assertLessEqual(right, core_end)

    def test_legacy_seconds_are_projected_to_complete_events_not_executed(self):
        batch = {
            "start": 0.0, "end": 5.0,
            "events": [
                {"event_id": "U0001", "kind": "speech", "a": 1.0, "b": 2.0},
                {"event_id": "G0001", "kind": "gap", "a": 2.0, "b": 2.4},
                {"event_id": "U0002", "kind": "speech", "a": 2.4, "b": 3.5},
            ],
            "readonly_events": [
                {"event_id": "U0003", "kind": "speech", "a": 5.0, "b": 6.0},
            ],
            "readonly_event_ids": ["U0003"],
        }
        decision = model_editor._resolve_batch_decisions({
            "delete": [[1.3, 2.1], [2.2, 3.0]],
        }, batch, duration=10.0)

        self.assertEqual("legacy_seconds_projected", decision["decision_mode"])
        self.assertEqual([(1.0, 3.5)], decision["delete"])
        self.assertEqual(["U0001", "G0001", "U0002"],
                         decision["accepted_event_ids"])
        self.assertEqual([], decision["rejected_context_ids"])
        self.assertFalse(decision["projection_failed"])
        self.assertEqual([1.3, 2.1],
                         decision["projected_from_seconds"][0]["seconds"])
        # A proposed cut inside a sentence expands to its entire source event;
        # the original arbitrary seconds never reach renderer output.
        self.assertEqual([1.0, 2.4],
                         decision["projected_from_seconds"][0]["projected"])

    def test_legacy_seconds_targeting_readonly_context_fail_closed(self):
        batch = {
            "start": 0.0, "end": 5.0,
            "events": [{"event_id": "U0001", "kind": "speech",
                        "a": 1.0, "b": 2.0}],
            "readonly_events": [{"event_id": "U0002", "kind": "speech",
                                 "a": 5.0, "b": 6.0}],
            "readonly_event_ids": ["U0002"],
        }
        decision = model_editor._resolve_batch_decisions(
            {"delete": [[5.1, 5.8]]}, batch, duration=10.0)
        self.assertEqual([], decision["delete"])
        self.assertTrue(decision["projection_failed"])
        self.assertEqual(["U0002"], decision["rejected_context_ids"])

    def test_legacy_seconds_without_a_core_event_fail_closed(self):
        batch = {"start": 0.0, "end": 5.0, "events": [],
                 "readonly_events": [], "readonly_event_ids": []}
        decision = model_editor._resolve_batch_decisions(
            {"delete": [[1.0, 2.0]]}, batch, duration=10.0)
        self.assertEqual([], decision["delete"])
        self.assertTrue(decision["projection_failed"])
        self.assertEqual([[1.0, 2.0]], decision["unmapped_seconds"])

    def test_second_legacy_answer_aborts_batch_instead_of_cutting_seconds(self):
        batch = {
            "body": "核心事件",
            "start": 0.0,
            "end": 5.0,
            "chapters": [1],
            "event_ids": ["U0001"],
            "core_event_ids": ["U0001"],
            "events": [{"event_id": "U0001", "kind": "speech",
                        "a": 1.0, "b": 2.0}],
            "readonly_event_ids": [],
            "readonly_events": [],
        }

        class LegacyTwiceLLM:
            def __init__(self):
                self.prompts = []

            def chat(self, _system, prompt, max_tokens=None):
                self.prompts.append(prompt)
                return '{"delete": [[3.2, 3.5]], "reason": "旧模型"}'

        llm = LegacyTwiceLLM()
        with self.assertRaisesRegex(RuntimeError, "无法安全映射"):
            model_editor._ask_batches(
                llm, [batch], "medium", 5.0, lambda _message: None)
        self.assertEqual(2, len(llm.prompts))
        self.assertIn("上次使用了自由秒数", llm.prompts[1])

    def test_prompts_include_script_strength_and_readonly_neighbor_events(self):
        units = [_unit(index * 2.0 + 0.25, index * 2.0 + 1.25,
                       "剧情%d" % index + "内容" * 8)
                 for index in range(5)]
        rows = model_editor._chapter_lines(units, [(0.0, 11.0)])
        batches = model_editor._batch_rows(
            rows, {}, {}, max_chars=230, context_events=1)
        self.assertGreater(len(batches), 1)
        sections = [{
            "start": batch["start"], "end": batch["end"],
            "title": "章节%d" % number,
            "advice": "第%d批专属取舍" % number,
        } for number, batch in enumerate(batches, 1)]
        script = {
            "mainline": "找到出口并完成最终撤离",
            "cut_level": "heavy",
            "strategy": "最短完整故事链，保留目标、过程、结果和过渡",
            "sections": sections,
        }

        class FakeLLM:
            def __init__(self):
                self.prompts = []

            def chat(self, _system, prompt, max_tokens=None):
                self.prompts.append(prompt)
                return '{"delete_events": [], "delete_ranges": [], "reason": "保留"}'

        llm = FakeLLM()
        result = model_editor._ask_batches(
            llm, batches, "heavy", 11.0, lambda _message: None,
            script=script)

        self.assertEqual(len(batches), len(llm.prompts))
        for number, (batch, prompt, audit) in enumerate(
                zip(batches, llm.prompts, result["batches"]), 1):
            self.assertIn("主线：找到出口并完成最终撤离", prompt)
            self.assertIn("当前设置力度：heavy；剧本产出力度：heavy", prompt)
            self.assertIn("最短完整故事链", prompt)
            self.assertIn("不是只留孤立高光", prompt)
            self.assertIn("章节%d" % number, prompt)
            self.assertIn("第%d批专属取舍" % number, prompt)
            self.assertIn("只允许这些 ID", prompt)
            self.assertEqual("event_ids", audit["decision_mode"])
            if batch["readonly_before_ids"]:
                self.assertIn(batch["readonly_before_ids"][0], prompt)
                self.assertIn("前一批只读上下文", prompt)
            if batch["readonly_after_ids"]:
                self.assertIn(batch["readonly_after_ids"][0], prompt)
                self.assertIn("后一批只读上下文", prompt)

        light_text, _sections = model_editor._script_context({
            "mainline": "同一主线", "cut_level": "light",
            "strategy": "保留完整过程",
            "sections": [{"start": 0, "end": 11,
                          "title": "轻度章", "advice": "全部保留"}],
        }, 0.0, 11.0, "light")
        self.assertIn("剧本产出力度：light", light_text)
        self.assertIn("全部保留", light_text)
        self.assertNotEqual(light_text,
                            model_editor._script_context(
                                script, 0.0, 11.0, "heavy")[0])

    def test_single_long_utterance_is_not_character_truncated(self):
        text = "这一句必须完整保留" * 40
        row = (1, 0.0, 5.0, [(0, _unit(0.5, 4.5, text))])
        batches = model_editor._batch_rows(
            [row], {1: "视觉"}, {1: "关键帧可用"}, max_chars=120)
        self.assertEqual(1, len(batches))
        self.assertIn(text, batches[0]["body"])
        self.assertNotIn("…", batches[0]["body"])

    def test_exact_keep_ratio_is_included_in_model_prompt(self):
        batch = {
            "body": "U0001 C1 t1.00-2.00 d1.00 U 文[测试]",
            "start": 0.0, "end": 3.0, "chapters": [1],
            "event_ids": ["U0001"], "core_event_ids": ["U0001"],
            "events": [{"event_id": "U0001", "kind": "speech",
                        "a": 1.0, "b": 2.0}],
            "readonly_event_ids": [], "readonly_events": [],
        }

        class FakeLLM:
            def __init__(self):
                self.prompts = []

            def chat(self, _system, prompt, max_tokens=None):
                self.prompts.append(prompt)
                return ('{"delete_events": [], "delete_ranges": [], '
                        '"reason": "保留"}')

        llm = FakeLLM()
        model_editor._ask_batches(
            llm, [batch], "heavy", 3.0, lambda _message: None,
            target_keep_ratio=0.62)
        self.assertEqual(1, len(llm.prompts))
        self.assertIn("目标约保留 62%", llm.prompts[0])
        self.assertIn("±5 个百分点", llm.prompts[0])

    def test_invalid_batch_aborts_instead_of_skipping_material(self):
        batches = [
            {"body": "第一章", "start": 0.0, "end": 5.0, "chapters": [1]},
            {"body": "第二章", "start": 5.0, "end": 10.0, "chapters": [2]},
        ]

        class FakeLLM:
            calls = 0

            def chat(self, _system, _prompt, max_tokens=None):
                self.calls += 1
                if self.calls == 1:
                    return ('{"delete_events": [], "delete_ranges": [], '
                            '"reason": "好"}')
                return "not-json"

        with self.assertRaisesRegex(RuntimeError, "第 2/2 批"):
            model_editor._ask_batches(
                FakeLLM(), batches, "light", 10.0,
                lambda _message: None)


class EditorContractTests(unittest.TestCase):
    def test_editor_contract_and_multimodal_evidence_remain_intact(self):
        runtime = _Runtime(tracks=[0, 1])
        data = {
            "duration": 12.0,
            "schema": {"name": "index-test"},
            "audio_streams": [{"ordinal": 0}, {"ordinal": 1}],
            "audio_stream_used": 0,
            "neutral": {"segments": [{"text": "第一句"}]},
            "script": {
                "mainline": "完成测试任务",
                "cut_level": "medium",
                "strategy": "保留完整因果",
                "sections": [{"start": 0.0, "end": 12.0,
                              "title": "测试章节", "advice": "全部保留"}],
            },
        }
        units = [_unit(1.0, 2.5, "第一句"),
                 _unit(3.0, 5.0, "第二句")]

        prompts = []

        class FakeLLM:
            def __init__(self, base_dir=None):
                self.base_dir = base_dir

            def start(self):
                return None

            def stop(self):
                return None

            def chat(self, _system, _prompt, max_tokens=None):
                prompts.append(_prompt)
                return ('{"delete_events": ["G0001"], '
                        '"delete_ranges": [], "reason": "删片头空场"}')

        rapid = types.ModuleType("rapidocr_onnxruntime")
        rapid.RapidOCR = lambda: object()
        with mock.patch.object(model_editor.autocut, "load_index",
                               return_value=data), \
                mock.patch.object(model_editor.autocut, "load_conf",
                                  return_value={"render": {}}), \
                mock.patch.object(model_editor.autocut, "load_units",
                                  return_value=units), \
                mock.patch.object(model_editor.autocut, "shot_cuts_of",
                                  return_value=[]), \
                mock.patch.object(model_editor.edit_modes, "apply_to_conf",
                                  side_effect=lambda conf, _mode: conf), \
                mock.patch.object(model_editor, "_frame_text",
                                  side_effect=lambda _e, _s, at, _l:
                                  "视觉%.1f" % at), \
                mock.patch.object(model_editor, "_track_audio",
                                  side_effect=lambda _s, track:
                                  np.asarray([track + 1], dtype=np.float32)), \
                mock.patch.object(model_editor, "_voice_tags",
                                  return_value=["中·正常·平稳", "强·快·渐变"]), \
                mock.patch.object(
                    model_editor, "_first_voiced_near",
                    side_effect=lambda _src, start, **_kwargs: float(start)), \
                mock.patch.dict(sys.modules,
                                {"rapidocr_onnxruntime": rapid}), \
                mock.patch("ui.llm_bridge.LLM", FakeLLM):
            result = model_editor.editor_by_model(runtime, ctx=object())

        self.assertEqual("artifact-editor", result.artifact_id)
        self.assertEqual({
            "index_dir", "duration", "conf", "spans", "logs", "plan",
            "plan_path", "plan_sha256", "task_seed"}, set(result.payload))
        self.assertEqual({
            "artifact_type", "segments", "plan_sha256", "by"},
            set(result.metadata))
        self.assertEqual("model_editor", result.metadata["by"])
        evidence = result.payload["plan"]["evidence"]["model_editor"]
        self.assertEqual([0, 1], evidence["audio_tracks"])
        self.assertEqual(2, len(evidence["audio_evidence"]))
        self.assertEqual(3, len(evidence["visual_evidence"]))
        self.assertEqual(1, evidence["batch_count"])
        self.assertEqual("local-llm", evidence["reviewed_by"])
        self.assertIn("主线：完成测试任务", prompts[0])
        self.assertEqual("完成测试任务",
                         evidence["batches"][0]["script_mainline"])
        self.assertEqual("event_ids",
                         evidence["batches"][0]["decision_mode"])


if __name__ == "__main__":
    unittest.main()
