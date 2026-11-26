# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import subtitle_quality


class SubtitleQualityTests(unittest.TestCase):
    def test_detects_legacy_long_single_word_and_parent_gap(self):
        data = {
            "segments": [
                [677.38, 680.10, "加火箭 尾"],
                [837.96, 865.22, "翼"],
            ],
            "segment_evidence": [
                {
                    "start": 677.38, "end": 680.10,
                    "text_original": "加火箭 尾",
                    "original_asr_segment_start": 677.38,
                    "original_asr_segment_end": 865.22,
                    "source_audio_track": 1,
                    "words": [
                        {"text": "加火箭", "start": 677.38, "end": 679.86},
                        {"text": "尾", "start": 679.92, "end": 680.10},
                    ],
                },
                {
                    "start": 837.96, "end": 865.22,
                    "text_original": "翼",
                    "original_asr_segment_start": 677.38,
                    "original_asr_segment_end": 865.22,
                    "source_audio_track": 1,
                    "words": [
                        {"text": "翼", "start": 837.96, "end": 865.22},
                    ],
                },
            ],
        }
        issues = subtitle_quality.alignment_issues(data)
        reasons = {reason for issue in issues for reason in issue["reasons"]}
        self.assertIn("word_duration_outlier", reasons)
        self.assertIn("parent_word_gap_outlier", reasons)
        self.assertIn("short_text_long_duration", reasons)

    def test_alignment_issues_are_persisted_in_quality_report(self):
        data = {
            "segments": [[10.0, 20.0, "翼"]],
            "segment_evidence": [{
                "start": 10.0, "end": 20.0, "text_original": "翼",
                "words": [{"text": "翼", "start": 10.0, "end": 20.0}],
            }],
        }
        with tempfile.TemporaryDirectory() as root:
            _rows, report = subtitle_quality.correct_source_segments(
                data, root, use_model=False)
        self.assertGreater(report["timestamp_alignment_issue_count"], 0)
        self.assertTrue(report["timestamp_alignment_issues"])

    def test_coverage_checked_profile_does_not_block_sparse_parent_only(self):
        data = {
            "asr_profile": {"version": 6},
            "segments": [[0.0, 0.4, "尾"], [20.0, 20.4, "翼"]],
            "segment_evidence": [
                {
                    "segment_id": "s000001", "start": 0.0, "end": 0.4,
                    "text_original": "尾", "words": [
                        {"text": "尾", "start": 0.0, "end": 0.4}],
                    "original_asr_segment_start": 0.0,
                    "original_asr_segment_end": 20.4,
                },
                {
                    "segment_id": "s000002", "start": 20.0, "end": 20.4,
                    "text_original": "翼", "words": [
                        {"text": "翼", "start": 20.0, "end": 20.4}],
                    "original_asr_segment_start": 0.0,
                    "original_asr_segment_end": 20.4,
                },
            ],
        }
        self.assertEqual([], subtitle_quality.alignment_issues(data))

    def test_arbitrary_short_text_density_outlier_is_detected(self):
        data = {
            "segments": [[0.0, 12.0, "四个汉字"]],
            "segment_evidence": [{
                "segment_id": "s000001", "start": 0.0, "end": 12.0,
                "text_original": "四个汉字", "words": [],
            }],
        }
        reasons = {reason for issue in subtitle_quality.alignment_issues(data)
                   for reason in issue["reasons"]}
        self.assertIn("short_text_long_duration", reasons)

    def test_segment_evidence_structure_mismatch_is_blocked(self):
        data = {
            "segments": [[0.0, 1.0, "甲"], [2.0, 3.0, "乙"]],
            "segment_evidence": [{
                "segment_id": "wrong", "start": 0.2, "end": 1.0,
                "text_original": "甲", "words": [],
            }],
        }
        reasons = {reason for issue in subtitle_quality.alignment_issues(data)
                   for reason in issue["reasons"]}
        self.assertIn("segment_evidence_count_mismatch", reasons)
        self.assertIn("segment_id_mismatch", reasons)
        self.assertIn("segment_time_mismatch", reasons)

    def test_global_uncovered_speech_blocker_is_reported(self):
        data = {
            "segments": [], "segment_evidence": [],
            "subtitle_alignment_blockers": [{
                "reason": "uncovered_speech_window",
                "start": 4.0, "end": 5.0,
            }],
        }
        issues = subtitle_quality.alignment_issues(data)
        self.assertEqual(["uncovered_speech_window"], issues[0]["reasons"])

    def test_historic_deletion_is_restored_from_raw_asr_evidence(self):
        data = {
            "segments": [[20.0, 24.0, "这里是你们的基地 继续来 我"]],
            "segment_evidence": [{
                "text_original": "这里是你们的秘密基地 继续来 我",
                "words": [],
            }],
        }
        with tempfile.TemporaryDirectory() as root:
            rows, report = subtitle_quality.correct_source_segments(
                data, root, use_model=False)
            rows2, report2 = subtitle_quality.correct_source_segments(
                data, root, use_model=False)
        self.assertEqual("这里是你们的秘密基地 继续来 我", rows[0][2])
        self.assertEqual(1, report["historic_loss_restored"])
        self.assertEqual(rows, rows2)
        self.assertTrue(report2["cache_hit"])

    def test_model_changes_are_applied_only_through_shared_proofread_gate(self):
        data = {"segments": [[0.0, 1.0, "父母书"]], "segment_evidence": []}

        class Server:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def complete(self, _prompt, n_predict=256):
                return "1. 附魔书"

        with tempfile.TemporaryDirectory() as root, \
                mock.patch("proofread.Server", Server), \
                mock.patch("proofread.load_resources", return_value=(
                    "附魔书 ← 父母书", {"父母书"},
                    [("附魔书", ["父母书"], "测试")])):
            rows, report = subtitle_quality.correct_source_segments(data, root)
            self.assertTrue(os.path.isfile(
                os.path.join(root, subtitle_quality.REPORT_NAME)))
        self.assertEqual("附魔书", rows[0][2])
        self.assertEqual(1, report["accepted_corrections"])

    def test_explicit_glossary_alias_prevents_correct_term_being_restored(self):
        data = {
            "segments": [[40.13, 41.41, "液态六氟化铀"]],
            "segment_evidence": [{
                "text_original": "液态六伏化油",
                "words": [],
                "confidence": 0.9,
            }],
        }
        resources = (
            "六氟化铀 ← 六伏化油", {"液态六伏化油"},
            [("液态六氟化铀", ["液态六伏化油"], "化学")],
        )
        with tempfile.TemporaryDirectory() as root, \
                mock.patch("proofread.load_resources", return_value=resources):
            rows, report = subtitle_quality.correct_source_segments(
                data, root, use_model=False)
        self.assertEqual("液态六氟化铀", rows[0][2])
        self.assertEqual(0, report["historic_loss_restored"])

    def test_explicit_glossary_applies_even_when_model_is_disabled(self):
        data = {
            "segments": [[0.0, 1.0, "不能用量子定的配方"]],
            "segment_evidence": [{
                "text_original": "不能用量子定的配方",
                "words": [], "confidence": 0.99,
            }],
        }
        resources = ("量子锭 ← 量子定", {"量子定"}, [])
        with tempfile.TemporaryDirectory() as root, \
                mock.patch("proofread.load_resources", return_value=resources):
            rows, report = subtitle_quality.correct_source_segments(
                data, root, use_model=False)
        self.assertEqual("不能用量子锭的配方", rows[0][2])
        self.assertEqual(1, report["accepted_corrections"])

    def test_project_glossary_canonicalises_confirmed_sink_terms(self):
        data = {
            "segments": [[0.0, 1.0, "这里是水槽无战士，子凤在说话"]],
            "segment_evidence": [{
                "text_original": "这里是水槽无战士，子凤在说话",
                "words": [],
                "confidence": 0.9,
            }],
        }
        table = "水槽无战事 ← 水槽无战士\n子枫 ← 子凤"
        resources = (table, {"水槽无战士", "子凤"}, [])

        class Server:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def complete(self, prompt, n_predict=256):
                body = prompt.split("【待校对（只输出这些编号）】", 1)[1]
                line = next(line for line in body.splitlines()
                            if line.startswith("1. "))
                return line

        with tempfile.TemporaryDirectory() as root, \
                mock.patch("proofread.load_resources", return_value=resources), \
                mock.patch("proofread.Server", Server), \
                mock.patch.object(subtitle_quality, "_backend_identity",
                                  return_value="test"):
            rows, report = subtitle_quality.correct_source_segments(data, root)
        self.assertEqual("这里是水槽无战事，子枫在说话", rows[0][2])
        self.assertEqual(1, report["accepted_corrections"])

    def test_project_glossary_canonicalises_verified_gameplay_terms(self):
        data = {
            "segments": [[0.0, 4.0,
                          "液态含浮燃料 超限算术 氢金石 星辉钉 "
                          "限量子计算机多线程处理器 缺时音 "
                          "进度条蒙蒙涨 液态反复制"]],
            "segment_evidence": [{
                "text_original": (
                    "液态含浮燃料 超限算术 氢金石 星辉钉 "
                    "限量子计算机多线程处理器 缺时音 "
                    "进度条蒙蒙涨 液态反复制"),
                "words": [], "confidence": 0.99,
            }],
        }
        resources = (
            "\n".join((
                "液态富硅岩燃料 ← 液态含浮燃料",
                "超限算枢 ← 超限算术",
                "青金石 ← 氢金石",
                "星辉锭 ← 星辉钉",
                "无限量子计算机多线程处理器 ← 限量子计算机多线程处理器",
                "石英 ← 时音",
                "猛猛涨 ← 蒙蒙涨",
                "液态反物质 ← 液态反复制",
            )),
            {"液态含浮燃料", "超限算术", "氢金石", "星辉钉",
             "限量子计算机多线程处理器", "时音", "蒙蒙涨",
             "液态反复制"},
            [],
        )
        with tempfile.TemporaryDirectory() as root, \
                mock.patch("proofread.load_resources", return_value=resources):
            rows, report = subtitle_quality.correct_source_segments(
                data, root, use_model=False)
        self.assertEqual(
            "液态富硅岩燃料 超限算枢 青金石 星辉锭 "
            "无限量子计算机多线程处理器 缺石英 "
            "进度条猛猛涨 液态反物质",
            rows[0][2])
        self.assertEqual(1, report["accepted_corrections"])

    def test_high_confidence_rows_are_context_reviewed_in_batches(self):
        total = 33
        data = {
            "segments": [[float(i), float(i + 1),
                          "我们在入口汇合" if i == 17 else "第%d句保持原样" % i]
                         for i in range(total)],
            "segment_evidence": [{"text_original": (
                "我们在入口汇合" if i == 17 else "第%d句保持原样" % i),
                "confidence": 0.99, "words": []} for i in range(total)],
        }

        class Server:
            calls = 0

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def complete(self, prompt, n_predict=256):
                type(self).calls += 1
                body = prompt.split("【待校对（只输出这些编号）】", 1)[1]
                lines = []
                for line in body.splitlines():
                    if line.startswith("【"):
                        break
                    if re.match(r"^\d+\. ", line):
                        lines.append(line.replace("入口汇合", "入口会合"))
                return "\n".join(lines)

        import re
        Server.calls = 0
        with tempfile.TemporaryDirectory() as root, \
                mock.patch("proofread.load_resources", return_value=("", set(), [])), \
                mock.patch("proofread.Server", Server), \
                mock.patch.object(subtitle_quality, "_backend_identity",
                                  return_value="test"):
            rows, report = subtitle_quality.correct_source_segments(data, root)
        self.assertEqual("我们在入口会合", rows[17][2])
        self.assertEqual(total, report["review_candidates"])
        self.assertEqual(1, report["accepted_corrections"])
        self.assertEqual(2, Server.calls)


if __name__ == "__main__":
    unittest.main()
