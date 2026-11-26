# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import pr_subtitles
import pr_workflow
import subtitles
import sync_server


class PremiereEditableSubtitleTests(unittest.TestCase):
    def make_index(self, root, segments):
        index_dir = os.path.join(root, "index")
        os.makedirs(index_dir)
        evidence = [{
            "segment_id": "s%06d" % (i + 1),
            "start": float(row[0]), "end": float(row[1]),
            "text": str(row[2]), "text_original": str(row[2]),
            "words": [], "asr_flags": [],
        } for i, row in enumerate(segments)]
        with open(os.path.join(index_dir, "index.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"segments": segments, "segment_evidence": evidence,
                       "cards": []}, handle,
                      ensure_ascii=False)
        return index_dir

    def test_remaps_source_dialogue_to_compact_premiere_timeline(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [
                [0.2, 0.8, "后台开发"],
                [1.2, 1.8, "这一句被剪掉"],
                [2.2, 2.8, "发型设计"],
            ])
            rows = pr_subtitles.mapped_segments(
                index_dir, [[0.0, 1.0], [2.0, 3.0]])
        self.assertEqual(
            [(0.2, 0.8, "后台开发"), (1.2, 1.8, "发型设计")], rows)

    def test_writes_two_language_srt_files_beside_project(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [
                [0.2, 0.8, "后台开发"],
                [2.2, 2.8, "发型设计"],
            ])
            project = os.path.join(root, "导出", "测试_可编辑工程.prproj")
            result = pr_workflow.write_editable_subtitle_tracks(
                index_dir, [[0.0, 1.0], [2.0, 3.0]], project,
                proofread=False)
            simplified, traditional = result["tracks"]
            with open(simplified["path"], encoding="utf-8") as handle:
                simplified_text = handle.read()
            with open(traditional["path"], encoding="utf-8") as handle:
                traditional_text = handle.read()
        self.assertEqual(2, result["count"])
        self.assertEqual(["简体中文", "繁体中文"],
                         [track["name"] for track in result["tracks"]])
        self.assertIn("00:00:01,200 --> 00:00:01,800", simplified_text)
        self.assertIn("后台开发", simplified_text)
        self.assertIn("後臺開發", traditional_text)
        self.assertIn("髮型設計", traditional_text)

    def test_no_dialogue_returns_no_caption_tracks(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [])
            project = os.path.join(root, "测试.prproj")
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[0.0, 2.0]], project, proofread=False)
        self.assertEqual([], result["tracks"])
        self.assertIn("没有可安全写入", result["warning"])

    def test_opencc_failure_has_non_blocking_fallback(self):
        with mock.patch.object(pr_subtitles, "_converter", return_value=None):
            converted = pr_subtitles.to_traditional("后台剪辑")
        self.assertEqual("後臺剪輯", converted)

    def test_traditional_source_also_gets_a_real_simplified_track(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [
                [0.0, 2.0, "後臺開發與髮型設計"],
            ])
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[0.0, 2.0]], os.path.join(root, "测试.prproj"),
                proofread=False)
            with open(result["tracks"][0]["path"], encoding="utf-8-sig") as handle:
                simplified = handle.read()
            with open(result["tracks"][1]["path"], encoding="utf-8-sig") as handle:
                traditional = handle.read()
            self.assertIn("后台开发与发型设计", simplified)
            self.assertIn("後臺開發與髮型設計", traditional)

    def test_missing_conversion_dictionary_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [
                [0.0, 2.0, "后台剪辑"],
            ])
            with mock.patch.object(pr_subtitles, "_converter", return_value=None):
                result = pr_subtitles.write_editable_tracks(
                    index_dir, [[0.0, 2.0]], os.path.join(root, "测试.prproj"),
                    proofread=False)
            self.assertEqual(2, len(result["tracks"]))
            self.assertIn("基础字表", result["warning"])

    def test_long_caption_is_paginated_without_ellipsis_or_lost_text(self):
        text = "这里是你们的水槽吴战士继续来我然后开始下一段内容"
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [[0.0, 8.0, text]])
            project = os.path.join(root, "测试.prproj")
            style = {"max_chars": 6, "max_lines": 1}
            with mock.patch.object(subtitles, "load_style", return_value=style):
                result = pr_subtitles.write_editable_tracks(
                    index_dir, [[0.0, 8.0]], project, proofread=False)
            contents = []
            for track in result["tracks"]:
                with open(track["path"], encoding="utf-8-sig") as handle:
                    contents.append(handle.read())
        self.assertGreater(result["count"], 1)
        self.assertNotIn("…", contents[0])
        plain = "".join(line for line in contents[0].splitlines()
                        if "-->" not in line and not line.isdigit())
        self.assertEqual(text, plain)
        times = [[line for line in value.splitlines() if "-->" in line]
                 for value in contents]
        self.assertEqual(times[0], times[1], "简繁字幕必须共用相同分页时间")

    def test_semantic_caption_split_uses_subject_changes_and_word_times(self):
        text = "硅岩就这个东西 我们现在已经很多了 我们这一集只做燃料"
        evidence = [{"text_original": text, "words": [
            {"text": "硅岩就这个东西", "start": 0.0, "end": 1.0},
            {"text": "我们现在已经很多了", "start": 1.0, "end": 2.4},
            {"text": "我们这一集只做燃料", "start": 2.4, "end": 4.0},
        ]}]
        rows, sliced = subtitles.semantic_segment_cues(
            [(0.0, 4.0, text)], word_segments=evidence,
            return_evidence=True)
        self.assertEqual([
            "硅岩就这个东西", "我们现在已经很多了", "我们这一集只做燃料",
        ], [row[2] for row in rows])
        self.assertEqual([(0.0, 1.0), (1.0, 2.4), (2.4, 4.0)],
                         [(row[0], row[1]) for row in rows])
        self.assertEqual(
            "".join(text.replace(" ", "") for text in [row[2] for row in rows]),
            "".join(word["text"] for item in sliced for word in item["words"]),
        )

    def test_semantic_caption_split_keeps_short_fragments_with_same_idea(self):
        text = "好了好了 可以了 上面输入 左边输出 物品也是上面输入"
        chunks = subtitles.semantic_caption_chunks(text)
        self.assertEqual([
            "好了好了可以了", "上面输入左边输出", "物品也是上面输入",
        ], chunks)
        self.assertEqual(text.replace(" ", ""), "".join(chunks))

    def test_premiere_tracks_use_same_semantic_cue_boundaries(self):
        text = "后台已经完成了 我们现在开始剪辑 然后导出工程"
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [[0.0, 6.0, text]])
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[0.0, 6.0]], os.path.join(root, "测试.prproj"),
                proofread=False)
            outputs = []
            for track in result["tracks"]:
                with open(track["path"], encoding="utf-8-sig") as handle:
                    outputs.append(handle.read())
        timings = [[line for line in output.splitlines() if "-->" in line]
                   for output in outputs]
        self.assertGreaterEqual(result["quality"]["semantic_caption_splits"], 2)
        self.assertEqual(timings[0], timings[1])
        self.assertIn("后台已经完成了", outputs[0])
        self.assertIn("我们现在开始剪辑", outputs[0])
        self.assertIn("然后导出工程", outputs[0])

    def test_final_srt_ass_and_bridge_outputs_remove_real_ellipses(self):
        self.assertEqual(
            "前半句，后半句\nhello world\n版本1.2 继续..",
            subtitles.clean_caption_text(
                "前半句……后半句...\nhello...world\n版本1.2⋯⋯继续.."),
        )
        segments = [(0.0, 2.0, "前半句……后半句..."),
                    (2.0, 4.0, "hello...world")]
        with tempfile.TemporaryDirectory() as root:
            srt_path = os.path.join(root, "test.srt")
            ass_path = os.path.join(root, "test.ass")
            bridge_path = os.path.join(root, "bridge.srt")
            subtitles.write_srt({}, srt_path, segments=segments, paginated=True)
            subtitles.write_ass({}, ass_path, segments=segments)
            sync_server._write_srt([
                {"a": 0.0, "b": 2.0, "text": "前半句……后半句..."},
                {"a": 2.0, "b": 4.0, "text": "hello...world"},
            ], bridge_path)
            outputs = []
            for path in (srt_path, ass_path, bridge_path):
                with open(path, encoding="utf-8-sig") as handle:
                    outputs.append(handle.read())
        for output in outputs:
            self.assertNotIn("…", output)
            self.assertNotIn("⋯", output)
            self.assertNotIn("...", output)
            self.assertIn("前半句，后半句", output)
            self.assertIn("hello world", output)

    def test_premiere_bilingual_tracks_share_clean_ellipsis_free_timing(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [[0.0, 4.0, "前半句……后半句..."]])
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[0.0, 4.0]], os.path.join(root, "测试.prproj"),
                proofread=False)
            contents = []
            for track in result["tracks"]:
                with open(track["path"], encoding="utf-8-sig") as handle:
                    contents.append(handle.read())
        self.assertEqual(2, len(contents))
        for output in contents:
            self.assertNotIn("…", output)
            self.assertNotIn("...", output)
        self.assertIn("前半句，后半句", contents[0])
        self.assertIn("前半句，後半句", contents[1])
        times = [[line for line in output.splitlines() if "-->" in line]
                 for output in contents]
        self.assertEqual(times[0], times[1])

    def test_word_timestamps_drive_page_boundary_when_text_matches(self):
        rows = subtitles.paginate_segments(
            [(10.0, 14.0, "甲乙丙丁")],
            {"max_chars": 2, "max_lines": 1},
            word_segments=[[{"word": "甲乙", "start": 10.0, "end": 10.8},
                            {"word": "丙丁", "start": 12.0, "end": 14.0}]])
        self.assertEqual(2, len(rows))
        self.assertAlmostEqual(11.4, rows[0][1], places=3)
        self.assertEqual("甲乙丙丁", "".join(row[2] for row in rows))

    def test_same_length_proofread_text_keeps_acoustic_page_boundaries(self):
        rows = subtitles.paginate_segments(
            [(0.0, 10.0, "甲乙丙丁戊己")],
            {"max_chars": 2, "max_lines": 1},
            word_segments=[[{"word": "甲以", "start": 0.0, "end": 1.0},
                            {"word": "丙丁", "start": 1.0, "end": 2.0},
                            {"word": "戊己", "start": 8.0, "end": 10.0}]] )
        self.assertEqual(3, len(rows))
        self.assertAlmostEqual(1.0, rows[0][1], places=3)
        self.assertAlmostEqual(5.0, rows[1][1], places=3)
        self.assertEqual("甲乙丙丁戊己", "".join(row[2] for row in rows))

    def test_premiere_export_pages_on_word_times_before_timeline_remap(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = self.make_index(root, [[0.0, 10.0, "甲乙丙丁戊己"]])
            index_path = os.path.join(index_dir, "index.json")
            with open(index_path, encoding="utf-8") as handle:
                data = json.load(handle)
            data["segment_evidence"][0]["words"] = [
                {"word": "甲乙", "start": 0.0, "end": 1.0},
                {"word": "丙丁", "start": 1.0, "end": 2.0},
                {"word": "戊己", "start": 8.0, "end": 10.0},
            ]
            with open(index_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            style = {"max_chars": 2, "max_lines": 1}
            with mock.patch.object(subtitles, "load_style", return_value=style):
                result = pr_subtitles.write_editable_tracks(
                    index_dir, [[0.0, 10.0]], os.path.join(root, "测试.prproj"),
                    proofread=False)
            with open(result["tracks"][0]["path"], encoding="utf-8-sig") as handle:
                timing = [line for line in handle.read().splitlines()
                          if "-->" in line]
        self.assertEqual([
            "00:00:00,000 --> 00:00:01,000",
            "00:00:01,000 --> 00:00:05,000",
            "00:00:05,000 --> 00:00:10,000",
        ], timing)

    def test_word_evidence_survives_cut_inside_future_caption_page(self):
        evidence = [{"text_original": "甲乙丙丁戊己", "words": [
            {"text": "甲乙", "start": 0.0, "end": 1.0},
            {"text": "丙丁", "start": 1.0, "end": 2.0},
            {"text": "戊己", "start": 8.0, "end": 10.0},
        ]}]
        rows, mapped_evidence = subtitles.remap(
            [(0.0, 10.0, "甲乙丙丁戊己")], [(0.0, 8.0, 2.0)],
            word_segments=evidence, return_evidence=True)
        self.assertEqual([(0.0, 2.0, "戊己")], rows)
        self.assertEqual("戊己", "".join(
            word["text"] for word in mapped_evidence[0]["words"]))
        pages = subtitles.paginate_segments(
            rows, {"max_chars": 1, "max_lines": 1},
            word_segments=mapped_evidence)
        self.assertEqual("戊己", "".join(page[2] for page in pages))
        self.assertEqual([(0.0, 1.0), (1.0, 2.0)],
                         [(page[0], page[1]) for page in pages])

    def test_length_changing_glossary_fix_keeps_original_acoustic_envelope(self):
        evidence = [{"text_original": "甲错误术语丁", "words": [
            {"text": "甲", "start": 0.0, "end": 0.2},
            {"text": "错误术语", "start": 0.2, "end": 1.0},
            {"text": "丁", "start": 4.0, "end": 5.0},
        ]}]
        rows, mapped = subtitles.remap(
            [(0.0, 5.0, "甲正词丁")], [(0.0, 0.0, 5.0)],
            word_segments=evidence, return_evidence=True)
        self.assertEqual("甲正词丁", "".join(
            word["text"] for word in mapped[0]["words"]))
        pages = subtitles.paginate_segments(
            rows, {"max_chars": 3, "max_lines": 1},
            word_segments=mapped)
        self.assertEqual("甲正词丁", "".join(page[2] for page in pages))
        # The page break follows the corrected term's source envelope, not the
        # character-proportional 3.75 second fallback.
        self.assertLess(pages[0][1], 3.0)

    def test_partial_cut_without_word_evidence_does_not_repeat_whole_page(self):
        rows = subtitles.remap(
            [(0.0, 4.0, "甲乙丙丁")], [(0.0, 1.0, 1.0)],
            word_segments=None)
        self.assertEqual([(0.0, 1.0, "乙")], rows)

    def test_short_caption_keeps_acoustic_end_instead_of_forcing_overlap(self):
        rows = subtitles.remap(
            [(0.0, 0.10, "嗯"), (0.12, 0.32, "继续")],
            [(0.0, 0.0, 1.0)])
        self.assertEqual([(0.0, 0.1, "嗯"), (0.12, 0.32, "继续")], rows)

    def test_premiere_caption_overlap_is_clipped_without_losing_text(self):
        rows, repairs = subtitles.normalize_caption_overlaps([
            (0.0, 1.0, "前句"),
            (0.96, 2.0, "后句"),
            (1.99, 2.01, "短"),
            (2.0, 3.0, "结尾"),
        ], return_repairs=True)
        self.assertEqual(3, repairs)
        self.assertEqual(0.96, rows[0][1])
        self.assertEqual(1.99, rows[1][1])
        self.assertEqual(1.99, rows[2][0])
        self.assertIn("短", rows[2][2])
        self.assertIn("结尾", rows[2][2])
        self.assertFalse(any(left[1] > right[0]
                             for left, right in zip(rows, rows[1:])))

    def test_pagination_balances_short_last_page(self):
        rows = subtitles.paginate_segments(
            [(0.0, 2.6, "们上一期是把这个硅岩做完了")],
            {"max_chars": 12, "max_lines": 1})
        self.assertEqual(2, len(rows))
        self.assertGreaterEqual(len(rows[-1][2]), 5)
        self.assertGreater(rows[-1][1] - rows[-1][0], 0.7)
        self.assertEqual("们上一期是把这个硅岩做完了", "".join(row[2] for row in rows))

    def test_partial_kept_span_uses_only_words_inside_cut(self):
        evidence = [{"text_original": "甲乙丙丁", "words": [
            {"word": "甲", "start": 0.0, "end": 1.0},
            {"word": "乙", "start": 1.0, "end": 2.0},
            {"word": "丙", "start": 2.0, "end": 3.0},
            {"word": "丁", "start": 3.0, "end": 4.0},
        ]}]
        rows = subtitles.remap(
            [(0.0, 4.0, "甲乙丙丁")], [(0.0, 1.0, 1.0)],
            word_segments=evidence)
        self.assertEqual([(0.0, 1.0, "乙")], rows)

    def test_old_index_skips_stretched_single_character_without_blocking_project(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = os.path.join(root, "index")
            os.makedirs(index_dir)
            data = {
                "segments": [
                    [0.0, 1.0, "正常字幕"],
                    [837.96, 865.22, "翼"],
                ],
                "segment_evidence": [
                    {"text_original": "正常字幕", "words": []},
                    {
                        "text_original": "翼",
                        "words": [{"text": "翼", "start": 837.96,
                                   "end": 865.22, "probability": 0.99}],
                        "original_asr_segment_start": 677.38,
                        "original_asr_segment_end": 865.22,
                    },
                ],
                "cards": [],
            }
            with open(os.path.join(index_dir, "index.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[0.0, 1.0], [837.96, 865.22]],
                os.path.join(root, "测试.prproj"), proofread=False)
            self.assertEqual(1, result["count"])
            self.assertIn("继续生成 PR 工程", result["warning"])
            with open(result["tracks"][0]["path"], encoding="utf-8-sig") as handle:
                output = handle.read()
            self.assertIn("正常字幕", output)
            self.assertNotIn("翼", output)

    def test_old_index_skips_collapsed_low_confidence_phrase(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = os.path.join(root, "index")
            os.makedirs(index_dir)
            data = {
                "segments": [[10.0, 10.24, "请不吝点赞"]],
                "segment_evidence": [{
                    "segment_id": "s000001", "start": 10.0, "end": 10.24,
                    "text_original": "请不吝点赞", "asr_flags": [],
                    "words": [
                        {"text": "请", "start": 10.0, "end": 10.2,
                         "probability": 0.0001},
                        {"text": "不", "start": 10.2, "end": 10.2,
                         "probability": 0.97},
                        {"text": "吝", "start": 10.2, "end": 10.24,
                         "probability": 0.99},
                        {"text": "点", "start": 10.24, "end": 10.24,
                         "probability": 0.99},
                        {"text": "赞", "start": 10.24, "end": 10.24,
                         "probability": 0.99},
                    ],
                }],
                "cards": [],
            }
            with open(os.path.join(index_dir, "index.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[10.0, 10.24]], os.path.join(root, "测试.prproj"),
                proofread=False)
            self.assertEqual([], result["tracks"])
            self.assertIn("跳过字幕轨", result["warning"])

    def test_parent_gap_diagnostic_does_not_drop_sane_child_rows(self):
        segments = [(0.0, 0.4, "尾"), (20.0, 20.4, "翼")]
        evidence = [
            {
                "words": [{"text": "尾", "start": 0.0, "end": 0.4}],
                "original_asr_segment_start": 0.0,
                "original_asr_segment_end": 20.4,
            },
            {
                "words": [{"text": "翼", "start": 20.0, "end": 20.4}],
                "original_asr_segment_start": 0.0,
                "original_asr_segment_end": 20.4,
            },
        ]
        kept, kept_evidence, blocked = subtitles.sanitize_source_segments(
            segments, evidence)
        self.assertEqual(segments, kept)
        self.assertEqual(evidence, kept_evidence)
        self.assertEqual([], blocked)

    def test_structural_mismatch_skips_subtitles_without_blocking_premiere(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir = os.path.join(root, "index")
            os.makedirs(index_dir)
            with open(os.path.join(index_dir, "index.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({
                    "segments": [[0.0, 1.0, "一句"]],
                    "segment_evidence": [], "cards": [],
                }, handle, ensure_ascii=False)
            result = pr_subtitles.write_editable_tracks(
                index_dir, [[0.0, 1.0]], os.path.join(root, "测试.prproj"),
                proofread=False)
            self.assertEqual([], result["tracks"])
            self.assertEqual("best_effort_nonblocking",
                             result["quality"]["delivery_mode"])

    def test_standalone_srt_export_blocks_global_alignment_failure(self):
        data = {
            "segments": [], "segment_evidence": [],
            "subtitle_alignment_blockers": [{
                "reason": "uncovered_speech_window", "start": 1.0, "end": 2.0,
            }],
        }
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(
                RuntimeError, "已阻止导出"):
            subtitles.write_srt(data, os.path.join(root, "bad.srt"))

    def test_only_real_unresolved_speech_is_reported_for_final_edit(self):
        data = {"subtitle_alignment_blockers": [
            {"reason": "uncovered_speech_window",
             "start": 5.0, "end": 5.5},
            {"reason": "uncovered_speech_window",
             "start": 50.0, "end": 50.5},
        ]}
        self.assertEqual([], pr_subtitles.kept_unresolved_speech(
            data, [[0.0, 4.0], [6.0, 10.0]]))
        issues = pr_subtitles.kept_unresolved_speech(
            data, [[0.0, 5.25], [6.0, 10.0]])
        self.assertEqual(1, len(issues))
        self.assertAlmostEqual(0.25, issues[0]["kept_overlap"])

    def test_workflow_subtitle_exception_never_blocks_project_delivery(self):
        with mock.patch.object(
                pr_subtitles, "write_editable_tracks",
                side_effect=RuntimeError("模拟字幕故障")):
            result = pr_workflow.write_editable_subtitle_tracks(
                "missing", [], "test.prproj")
        self.assertEqual([], result["tracks"])
        self.assertIn("继续生成可编辑 PR 工程", result["warning"])

    def test_semantic_caption_hard_limit_is_about_fifteen_characters(self):
        rows = subtitles.semantic_caption_chunks(
            "这一段完全没有标点但是仍然必须按可读长度拆成字幕")
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(len(row.replace(" ", "")) <= 15 for row in rows))
        self.assertEqual(
            "这一段完全没有标点但是仍然必须按可读长度拆成字幕",
            "".join(row.replace(" ", "") for row in rows))

    def test_standalone_filler_is_attached_but_never_deleted(self):
        rows = subtitles.semantic_segment_cues([
            (0.0, 1.0, "我们已经做好了"),
            (1.0, 1.25, "呃"),
            (2.0, 3.0, "继续下一步"),
        ])
        self.assertEqual(2, len(rows))
        self.assertEqual("我们已经做好了 呃", rows[0][2])
        self.assertEqual("继续下一步", rows[1][2])


if __name__ == "__main__":
    unittest.main()
