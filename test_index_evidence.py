# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import index


def _word(text, start, end, probability):
    return SimpleNamespace(
        word=text, start=start, end=end, probability=probability)


class IndexEvidenceTests(unittest.TestCase):
    def test_collapsed_low_confidence_short_phrase_is_timing_hallucination(self):
        words = [
            {"text": "请", "start": 10.00, "end": 10.20,
             "probability": 0.0001},
            {"text": "不", "start": 10.20, "end": 10.20,
             "probability": 0.97},
            {"text": "吝", "start": 10.20, "end": 10.24,
             "probability": 0.99},
            {"text": "点", "start": 10.24, "end": 10.24,
             "probability": 0.99},
            {"text": "赞", "start": 10.24, "end": 10.24,
             "probability": 0.99},
        ]
        flags = index._timing_alignment_flags(
            {}, "请不吝点赞", 10.00, 10.24, words)
        self.assertIn("timestamp_alignment_suspected", flags)
        self.assertIn("impossible_speech_rate", flags)
        self.assertIn("collapsed_word_timestamps", flags)
        row = {"start": 10.00, "end": 10.24, "words": words}
        self.assertTrue(index._definite_timing_hallucination(row, "请不吝点赞"))

    def test_low_confidence_template_prefix_requires_prompt_free_relisten(self):
        words = [
            {"text": "请", "start": 1.0, "end": 1.4,
             "probability": 0.0004},
            {"text": "不", "start": 1.4, "end": 2.4,
             "probability": 0.99},
            {"text": "吝", "start": 2.4, "end": 2.8,
             "probability": 0.99},
            {"text": "点", "start": 2.8, "end": 3.8,
             "probability": 0.99},
        ]
        flags = index._timing_alignment_flags(
            {}, "请不吝点", 1.0, 3.8, words)
        self.assertIn("template_hallucination_suspected", flags)
        row = {"start": 1.0, "end": 3.8, "words": words}
        self.assertTrue(index._low_confidence_template_hallucination(
            row, "请不吝点"))
        high_confidence = [dict(word, probability=0.95) for word in words]
        self.assertNotIn("template_hallucination_suspected",
                         index._timing_alignment_flags(
                             {}, "请不吝点", 1.0, 3.8, high_confidence))

    def test_find_ffmpeg_uses_install_home_when_module_is_bundled(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as bundle:
            bin_dir = os.path.join(home, "bin")
            os.makedirs(bin_dir)
            expected = os.path.join(bin_dir, "ffmpeg.exe")
            with open(expected, "wb") as handle:
                handle.write(b"")
            with mock.patch.dict(
                    os.environ, {"SMARTCUT_HOME": home}, clear=False), \
                    mock.patch.object(index, "BASE_DIR", bundle), \
                    mock.patch.object(index.shutil, "which", return_value=None):
                actual = index.find_ffmpeg()
        self.assertEqual(os.path.normcase(actual), os.path.normcase(expected))

    def test_split_preserves_exact_words_confidence_origin_and_track(self):
        seg = SimpleNamespace(
            start=0.123456789,
            end=20.987654321,
            avg_logprob=-0.125,
            no_speech_prob=0.03125,
            words=[
                _word("我", 0.234567891, 0.456789123, 0.97),
                _word("们", 0.47, 0.680000001, 0.91),
                _word("继续", 13.3456789, 14.5678912, 0.88),
            ],
        )
        evidence = []
        pieces = index._split_long_segment(
            seg, "我们继续", max_len=8.0, prefer_gap=0.4,
            evidence_out=evidence, source_audio_track=[0, 2])

        self.assertEqual(len(pieces), 2)
        self.assertEqual([row[2] for row in pieces], ["我们", "继续"])
        self.assertEqual(evidence[0]["words"][0]["start"], 0.234567891)
        self.assertEqual(evidence[0]["words"][1]["end"], 0.680000001)
        self.assertAlmostEqual(evidence[0]["confidence"], 0.94)
        self.assertEqual(evidence[0]["original_asr_segment_start"], 0.123456789)
        self.assertEqual(evidence[0]["original_asr_segment_end"], 20.987654321)
        self.assertEqual(evidence[0]["source_audio_tracks"], [0, 2])
        self.assertIsNone(evidence[0]["source_audio_track"])
        self.assertEqual(evidence[1]["split_reason"], "segment-end")

    def test_split_keeps_parent_gap_diagnostic_and_blocks_stretched_character(self):
        seg = SimpleNamespace(
            start=677.38,
            end=865.22,
            avg_logprob=-0.02,
            no_speech_prob=0.01,
            words=[
                _word("尾", 679.92, 680.10, 0.99),
                _word("翼", 837.96, 865.22, 0.99),
            ],
        )
        evidence = []
        pieces = index._split_long_segment(
            seg, "尾翼", max_len=8.0, prefer_gap=0.35,
            evidence_out=evidence, source_audio_track=1)

        self.assertEqual(["尾", "翼"], [piece[2] for piece in pieces])
        self.assertNotIn("timestamp_alignment_suspected",
                         evidence[0]["asr_flags"])
        self.assertIn("timestamp_alignment_suspected",
                      evidence[1]["asr_flags"])
        self.assertTrue(all(
            "parent_word_gap_outlier" in row["asr_diagnostics"]
            for row in evidence))
        self.assertIn("word_duration_outlier", evidence[1]["asr_flags"])

    def test_short_parent_still_splits_on_clear_word_gap(self):
        seg = SimpleNamespace(
            start=10.0, end=19.4, avg_logprob=-0.1, no_speech_prob=0.0,
            words=[
                _word("好", 10.0, 10.5, 0.99),
                _word("无限", 18.8, 19.4, 0.99),
            ])
        evidence = []
        pieces = index._split_long_segment(
            seg, "好无限", max_len=12.0, prefer_gap=0.45,
            evidence_out=evidence, source_audio_track=0)
        self.assertEqual(["好", "无限"], [piece[2] for piece in pieces])
        self.assertTrue(all("timestamp_alignment_suspected" not in
                            row["asr_flags"] for row in evidence))

    def test_correction_changes_text_without_destroying_word_timing(self):
        raw = [{
            "start": 1.000001,
            "end": 2.000002,
            "text": "过冲",
            "text_original": "过冲",
            "words": [{
                "text": "过冲", "start": 1.123456, "end": 1.765432,
                "probability": 0.83,
            }],
            "source_audio_tracks": [1],
        }]
        rows = index.align_segment_evidence(
            [(1.000001, 2.000002, "过充")], raw, source_audio_track=1)
        self.assertEqual(rows[0]["segment_id"], "s000001")
        self.assertEqual(rows[0]["text"], "过充")
        self.assertEqual(rows[0]["text_original"], "过冲")
        self.assertEqual(rows[0]["words"][0]["start"], 1.123456)
        self.assertEqual(rows[0]["source_audio_track"], 1)

    def test_evidence_json_roundtrip_keeps_unrounded_internal_timing(self):
        rows = index.align_segment_evidence(
            [(0.123456789, 0.987654321, "我来了")], [], source_audio_track=0)
        payload = {
            "schema": {
                "name": index.INDEX_SCHEMA_NAME,
                "version": index.INDEX_SCHEMA_VERSION,
                "segment_evidence_version": index.SEGMENT_EVIDENCE_VERSION,
            },
            "segments": [[0.12, 0.99, "我来了"]],
            "segment_evidence": rows,
        }
        restored = json.loads(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(restored["segments"][0][:2], [0.12, 0.99])
        self.assertEqual(
            restored["segment_evidence"][0]["start"], 0.123456789)
        self.assertEqual(
            restored["segment_evidence"][0]["end"], 0.987654321)

    def test_old_index_gets_conservative_compatible_evidence(self):
        old = {
            "segments": [[1.25, 2.75, "旧索引台词"]],
            "audio_stream_used": 2,
        }
        rows = index.get_segment_evidence(old)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_id"], "s000001")
        self.assertEqual(rows[0]["text"], "旧索引台词")
        self.assertEqual(rows[0]["source_audio_tracks"], [2])
        self.assertEqual(rows[0]["words"], [])
        self.assertEqual(rows[0]["timing_source"], "legacy-segment")

    def test_new_index_reader_preserves_word_evidence(self):
        current = {
            "segments": [[3.1, 4.2, "正确术语"]],
            "audio_stream_used": 0,
            "segment_evidence": [{
                "start": 3.1000001,
                "end": 4.2000002,
                "text": "正确术语",
                "text_original": "识别术语",
                "words": [{
                    "text": "识别术语", "start": 3.234567,
                    "end": 4.012345, "probability": 0.72,
                }],
                "source_audio_tracks": [0],
            }],
        }
        rows = index.get_segment_evidence(current)
        self.assertEqual(rows[0]["words"][0]["start"], 3.234567)
        self.assertEqual(rows[0]["text_original"], "识别术语")
        self.assertEqual(rows[0]["segment_id"], "s000001")

    def test_head_recheck_prepends_matching_evidence_and_clips_overlap(self):
        existing = [{
            "start": 20.0, "end": 21.0, "text": "原第一句",
            "words": [], "source_audio_tracks": [0],
        }]

        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].extend([
                {
                    "start": 1.0, "end": 2.0, "text": "补回开头",
                    "words": [{"text": "补回", "start": 1.1, "end": 1.8,
                               "probability": 0.9}],
                    "source_audio_tracks": [0],
                },
                {
                    "start": 19.4, "end": 20.5, "text": "跨界",
                    "words": [{"text": "跨", "start": 19.5, "end": 19.9,
                               "probability": 0.8},
                              {"text": "界", "start": 20.1, "end": 20.4,
                               "probability": 0.8}],
                    "source_audio_tracks": [0],
                },
            ])
            return [(1.0, 2.0, "补回开头"), (19.4, 20.5, "跨界")]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            merged = index.fill_missing_head(
                [(20.0, 21.0, "原第一句")], np.full(16000 * 30, 0.1),
                min_gap=12.0, evidence_out=existing, source_audio_track=0)

        self.assertEqual(merged, [(1.0, 2.0, "补回开头"),
                                  (19.5, 19.9, "跨"),
                                  (20.0, 21.0, "原第一句")])
        self.assertEqual([row["text"] for row in existing[:2]],
                         ["补回开头", "跨"])
        self.assertEqual([w["text"] for w in existing[1]["words"]], ["跨"])

    def test_alignment_repair_replaces_flagged_parent_with_chunk_decode(self):
        rows = [(0.0, 8.0, "翼")]
        evidence = [{
            "start": 0.0, "end": 8.0, "text": "翼", "text_original": "翼",
            "words": [{"text": "翼", "start": 0.0, "end": 8.0,
                       "probability": 0.99}],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 0.0,
            "original_asr_segment_end": 8.0,
            "asr_flags": ["timestamp_alignment_suspected",
                          "word_duration_outlier"],
        }]

        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 1.0, "end": 2.0, "text": "尾翼",
                "text_original": "尾翼",
                "words": [{"text": "尾翼", "start": 1.0, "end": 2.0,
                           "probability": 0.92}],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 1.0,
                "original_asr_segment_end": 2.0,
                "asr_flags": [],
            })
            return [(1.0, 2.0, "尾翼")]

        audio = np.zeros(16000 * 8, dtype=np.float32)
        audio[16000:16000 * 2] = 0.1
        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed = index.repair_suspected_alignment(
                rows, audio,
                evidence_out=evidence, source_audio_track=0)

        self.assertEqual([(1.0, 2.0, "尾翼")], fixed)
        self.assertEqual("尾翼", evidence[0]["text"])
        self.assertNotIn("timestamp_alignment_suspected",
                         evidence[0]["asr_flags"])

    def test_alignment_repair_failure_keeps_row_and_marks_export_blocker(self):
        rows = [(0.0, 8.0, "翼")]
        evidence = [{
            "start": 0.0, "end": 8.0, "text": "翼", "words": [],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 0.0,
            "original_asr_segment_end": 8.0,
            "asr_flags": ["timestamp_alignment_suspected"],
        }]
        with mock.patch.object(index, "transcribe", return_value=[]):
            fixed = index.repair_suspected_alignment(
                rows, np.full(16000 * 8, 0.1, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)
        self.assertEqual([(0.0, 8.0, "翼")], fixed)
        self.assertIn("alignment_repair_failed", evidence[0]["asr_flags"])

    def test_definite_whisper_template_hallucination_is_removed_when_unconfirmed(self):
        text = "请不吝点赞订阅转发打赏支持明镜与点点栏目"
        rows = [(2.0, 8.0, text)]
        evidence = [{
            "start": 2.0, "end": 8.0, "text": text,
            "text_original": text, "words": [],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 2.0,
            "original_asr_segment_end": 8.0,
            "asr_flags": ["timestamp_alignment_suspected",
                          "word_duration_outlier"],
        }]
        with mock.patch.object(index, "transcribe", return_value=[]), \
                mock.patch.object(index, "silero_speech_windows",
                                  return_value=([], "")):
            fixed = index.repair_suspected_alignment(
                rows, np.zeros(16000 * 10, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)
        self.assertEqual([], fixed)
        self.assertEqual([], evidence)

    def test_definite_hallucination_uses_reliable_local_text_without_old_coverage(self):
        text = "请不吝点赞订阅转发打赏支持明镜与点点栏目"
        rows = [(2.0, 8.0, text)]
        evidence = [{
            "start": 2.0, "end": 8.0, "text": text,
            "text_original": text,
            "words": [{"text": text, "start": 2.0, "end": 4.0,
                       "probability": 0.9}],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 2.0,
            "original_asr_segment_end": 8.0,
            "asr_flags": ["timestamp_alignment_suspected",
                          "word_duration_outlier"],
        }]

        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 5.8, "end": 7.0, "text": "然后继续建造",
                "text_original": "然后继续建造",
                "words": [{"text": "然后继续建造", "start": 5.8,
                           "end": 7.0, "probability": 0.9}],
                "no_speech_prob": 0.1, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(5.8, 7.0, "然后继续建造")]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe), \
                mock.patch.object(index, "silero_speech_windows",
                                  return_value=([], "")):
            fixed = index.repair_suspected_alignment(
                rows, np.zeros(16000 * 10, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)
        self.assertEqual(1, len(fixed))
        self.assertEqual("然后继续建造", fixed[0][2])
        self.assertEqual("然后继续建造", evidence[0]["text"])
        self.assertNotIn("alignment_repair_failed",
                         evidence[0].get("asr_flags") or [])

    def test_generic_like_and_subscribe_sentence_is_not_auto_deleted(self):
        text = "请不吝点赞订阅"
        rows = [(2.0, 8.0, text)]
        evidence = [{
            "start": 2.0, "end": 8.0, "text": text,
            "text_original": text, "words": [],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 2.0,
            "original_asr_segment_end": 8.0,
            "asr_flags": ["timestamp_alignment_suspected",
                          "word_duration_outlier"],
        }]
        with mock.patch.object(index, "transcribe", return_value=[]), \
                mock.patch.object(index, "silero_speech_windows",
                                  return_value=([], "")):
            fixed = index.repair_suspected_alignment(
                rows, np.zeros(16000 * 10, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)
        self.assertEqual(rows, fixed)
        self.assertIn("alignment_repair_failed", evidence[0]["asr_flags"])

    def test_alignment_window_requires_meaningful_overlap(self):
        self.assertFalse(index._alignment_window_covered(
            (0.0, 4.0), [(3.99, 4.01)]))
        self.assertTrue(index._alignment_window_covered(
            (0.0, 4.0), [(1.0, 1.5)]))

    def test_uncovered_speech_window_is_retranscribed_and_paired(self):
        rows = [(0.0, 1.0, "已有台词")]
        evidence = [{
            "start": 0.0, "end": 1.0, "text": "已有台词",
            "text_original": "已有台词", "words": [],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "asr_flags": [],
        }]

        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 1.2, "text": "补回台词",
                "text_original": "补回台词",
                "words": [{"text": "补回台词", "start": 0.6,
                           "end": 1.2, "probability": 0.9}],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 1.2, "补回台词")]

        audio = np.full(16000 * 8, 0.1, dtype=np.float32)
        blockers = []
        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed, blockers = index.repair_uncovered_speech(
                rows, audio, [(0.0, 1.0), (5.0, 6.0)],
                evidence_out=evidence, source_audio_track=0,
                blockers_out=blockers)
        self.assertEqual([], blockers)
        self.assertEqual(["已有台词", "补回台词"],
                         [row[2] for row in fixed])
        self.assertEqual([0, 0],
                         [row["source_audio_track"] for row in evidence])

    def test_uncovered_speech_failure_sets_global_export_blocker(self):
        rows, evidence, blockers = [], [], []
        audio = np.full(16000 * 4, 0.1, dtype=np.float32)
        with mock.patch.object(index, "transcribe", return_value=[]):
            fixed, blockers = index.repair_uncovered_speech(
                rows, audio, [(1.0, 3.0)], evidence_out=evidence,
                source_audio_track=2, blockers_out=blockers)
        self.assertEqual([], fixed)
        self.assertEqual("uncovered_speech_window", blockers[0]["reason"])
        self.assertEqual([2], blockers[0]["source_audio_tracks"])

    def test_unresolved_short_speech_window_is_diagnostic_only(self):
        blockers, diagnostics = [], []
        audio = np.full(16000 * 4, 0.1, dtype=np.float32)
        with mock.patch.object(index, "transcribe", return_value=[]):
            fixed, blockers = index.repair_uncovered_speech(
                [], audio, [(1.0, 1.8)], evidence_out=[],
                source_audio_track=0, blockers_out=blockers,
                diagnostics_out=diagnostics)
        self.assertEqual([], fixed)
        self.assertEqual([], blockers)
        self.assertEqual("unresolved_short_speech_window",
                         diagnostics[0]["reason"])

    def test_short_window_with_partial_reliable_decode_is_still_diagnostic(self):
        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 0.7, "text": "啊",
                "text_original": "啊",
                "words": [{"text": "啊", "start": 0.6,
                           "end": 0.7, "probability": 0.9}],
                "no_speech_prob": 0.1, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 0.7, "啊")]

        diagnostics = []
        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed, blockers = index.repair_uncovered_speech(
                [], np.full(16000 * 4, 0.1, dtype=np.float32),
                [(1.0, 1.8)], evidence_out=[], blockers_out=[],
                diagnostics_out=diagnostics)
        self.assertEqual("啊", fixed[0][2])
        self.assertEqual([], blockers)
        self.assertEqual("unresolved_short_speech_window",
                         diagnostics[0]["reason"])

    def test_uncovered_relisten_merges_prefix_and_disables_prompt(self):
        rows = [(2.0, 4.0, "这是两台太空矿机")]
        evidence = [{
            "start": 2.0, "end": 4.0, "text": "这是两台太空矿机",
            "text_original": "这是两台太空矿机",
            "words": [{"text": "这是两台太空矿机", "start": 2.0,
                       "end": 4.0, "probability": 0.9}],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "asr_flags": [],
        }]

        def fake_transcribe(*_args, **kwargs):
            self.assertEqual("", kwargs["initial_prompt"])
            self.assertEqual("", kwargs["hotwords"])
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 1.8, "text": "这是两台",
                "text_original": "这是两台",
                "words": [{"text": "这是两台", "start": 0.6,
                           "end": 1.8, "probability": 0.9}],
                "no_speech_prob": 0.1, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 1.8, "这是两台")]

        blockers = []
        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed, blockers = index.repair_uncovered_speech(
                rows, np.full(16000 * 6, 0.1, dtype=np.float32),
                [(1.2, 2.1)], evidence_out=evidence,
                source_audio_track=0, blockers_out=blockers)
        self.assertEqual([], blockers)
        self.assertEqual(1, len(fixed))
        self.assertAlmostEqual(1.2, fixed[0][0])
        self.assertEqual("这是两台太空矿机", fixed[0][2])
        self.assertEqual("local-overlap-merge", evidence[0]["timing_source"])

    def test_subsecond_uncovered_window_gets_sentence_context_for_prefix_merge(self):
        rows = [(3.0, 5.0, "还有这里它是有一个种子的")]
        evidence = [{
            "start": 3.0, "end": 5.0, "text": rows[0][2],
            "text_original": rows[0][2],
            "words": [{"text": rows[0][2], "start": 3.0,
                       "end": 5.0, "probability": 0.9}],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "asr_flags": [],
        }]

        def fake_transcribe(samples, *_args, **kwargs):
            # Window 2.2~2.8 must receive the 1.5 s short-window pad rather
            # than the old 0.6 s pad, so the decoder sees a full phrase.
            self.assertGreaterEqual(len(samples), int(3.5 * 16000))
            kwargs["evidence_out"].append({
                "start": 1.5, "end": 3.0, "text": "还有这里它是",
                "text_original": "还有这里它是",
                "words": [{"text": "还有这里它是", "start": 1.5,
                           "end": 3.0, "probability": 0.9}],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [], "no_speech_prob": 0.0,
                "avg_logprob": -0.2,
            })
            return [(1.5, 3.0, "还有这里它是")]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed, blockers = index.repair_uncovered_speech(
                rows, np.full(16000 * 7, 0.1, dtype=np.float32),
                [(2.2, 2.8)], evidence_out=evidence,
                source_audio_track=0, blockers_out=[])
        self.assertEqual([], blockers)
        self.assertEqual(1, len(fixed))
        self.assertAlmostEqual(2.2, fixed[0][0])
        self.assertEqual("还有这里它是有一个种子的", fixed[0][2])

    def test_known_short_window_hallucination_is_not_appended(self):
        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 1.4,
                "text": "请不吝点赞订阅转发打赏支持明镜与点点栏目",
                "text_original": "请不吝点赞订阅转发打赏支持明镜与点点栏目",
                "words": [], "no_speech_prob": 0.1, "avg_logprob": -0.1,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 1.4, "请不吝点赞订阅转发打赏支持明镜与点点栏目")]

        diagnostics = []
        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed, blockers = index.repair_uncovered_speech(
                [], np.full(16000 * 4, 0.1, dtype=np.float32),
                [(1.0, 1.8)], evidence_out=[], blockers_out=[],
                diagnostics_out=diagnostics)
        self.assertEqual([], fixed)
        self.assertEqual([], blockers)
        self.assertEqual("unresolved_short_speech_window",
                         diagnostics[0]["reason"])

    def test_rms_game_activity_without_vad_speech_never_blocks(self):
        rows, evidence, blockers, diagnostics = [], [], [], []
        audio = np.full(16000 * 4, 0.1, dtype=np.float32)
        with mock.patch.object(index, "silero_speech_windows",
                               return_value=([], "")), \
                mock.patch.object(index, "transcribe") as transcribe:
            fixed, blockers = index.repair_uncovered_human_speech(
                rows, audio, rms_windows=[(0.0, 4.0)],
                evidence_out=evidence, source_audio_track=0,
                blockers_out=blockers, diagnostics_out=diagnostics)
        self.assertEqual([], fixed)
        self.assertEqual([], blockers)
        transcribe.assert_not_called()
        self.assertEqual(1, diagnostics[0]["rms_active_window_count"])
        self.assertEqual(0, diagnostics[0]["human_speech_window_count"])

    def test_vad_unavailable_records_warning_without_blocking(self):
        blockers, diagnostics = [], []
        audio = np.full(16000 * 2, 0.1, dtype=np.float32)
        with mock.patch.object(index, "silero_speech_windows",
                               return_value=(None, "missing model")), \
                mock.patch.object(index, "transcribe") as transcribe:
            fixed, blockers = index.repair_uncovered_human_speech(
                [], audio, rms_windows=[(0.0, 2.0)], evidence_out=[],
                blockers_out=blockers, diagnostics_out=diagnostics)
        self.assertEqual([], fixed)
        self.assertEqual([], blockers)
        transcribe.assert_not_called()
        self.assertFalse(diagnostics[0]["vad_available"])
        self.assertIn("missing model", diagnostics[0]["warning"])

    def test_confirmed_vad_speech_failure_still_blocks_export(self):
        blockers, diagnostics = [], []
        audio = np.full(16000 * 3, 0.1, dtype=np.float32)
        with mock.patch.object(index, "silero_speech_windows",
                               return_value=([(1.0, 2.6)], "")), \
                mock.patch.object(index, "transcribe", return_value=[]):
            fixed, blockers = index.repair_uncovered_human_speech(
                [], audio, rms_windows=[(0.0, 3.0)], evidence_out=[],
                source_audio_track=1, blockers_out=blockers,
                diagnostics_out=diagnostics)
        self.assertEqual([], fixed)
        self.assertEqual("uncovered_speech_window", blockers[0]["reason"])
        self.assertEqual(1, diagnostics[0]["human_speech_window_count"])

    def test_initial_asr_can_recover_when_it_emitted_no_segments(self):
        rows, evidence, blockers = [], [], []
        audio = np.full(16000 * 3, 0.1, dtype=np.float32)

        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 1.2, "text": "补回整句",
                "text_original": "补回整句",
                "words": [{"text": "补回整句", "start": 0.6,
                           "end": 1.2, "probability": 0.9}],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 1.2, "补回整句")]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed, blockers = index.repair_uncovered_speech(
                rows, audio, [(1.0, 2.0)], evidence_out=evidence,
                source_audio_track=0, blockers_out=blockers)
        self.assertEqual([], blockers)
        self.assertEqual("补回整句", fixed[0][2])
        self.assertEqual("补回整句", evidence[0]["text"])

    def test_index_flags_three_plus_character_timing_density_outlier(self):
        seg = SimpleNamespace(
            start=0.0, end=12.0, text="四个汉字", words=[],
            avg_logprob=-0.1, no_speech_prob=0.0)
        evidence = []
        rows = index._split_long_segment(
            seg, seg.text, evidence_out=evidence, source_audio_track=0)
        self.assertEqual([(0.0, 12.0, "四个汉字")], rows)
        self.assertIn("short_text_long_duration", evidence[0]["asr_flags"])

    def test_pair_sort_keeps_simultaneous_track_evidence_attached(self):
        segments = [(2.0, 3.0, "A2"), (2.0, 3.0, "A1")]
        evidence = [
            {"start": 2.0, "end": 3.0, "text": "A2",
             "source_audio_track": 1, "source_audio_tracks": [1]},
            {"start": 2.0, "end": 3.0, "text": "A1",
             "source_audio_track": 0, "source_audio_tracks": [0]},
        ]
        sorted_segments, sorted_evidence, issues = \
            index.sort_segment_evidence_pairs(segments, evidence)
        self.assertEqual([], issues)
        self.assertEqual(["A1", "A2"], [row[2] for row in sorted_segments])
        self.assertEqual([0, 1],
                         [row["source_audio_track"] for row in sorted_evidence])

    def test_repair_count_mismatch_records_global_blocker(self):
        blockers = []
        rows = [(0.0, 1.0, "一句")]
        fixed = index.repair_suspected_alignment(
            rows, np.full(16000, 0.1, dtype=np.float32),
            evidence_out=[], blockers_out=blockers)
        self.assertEqual(rows, fixed)
        self.assertEqual("segment_evidence_count_mismatch",
                         blockers[0]["reason"])

    def test_parent_gap_only_does_not_repair_or_mark_sibling_rows(self):
        rows = [(5.0, 6.0, "开场"), (65.0, 66.0, "结尾")]
        evidence = [
            {
                "start": 5.0, "end": 6.0, "text": "开场",
                "text_original": "开场",
                "words": [{"text": "开场", "start": 5.0, "end": 6.0,
                           "probability": 0.9}],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 0.0,
                "original_asr_segment_end": 80.0,
                "asr_flags": ["timestamp_alignment_suspected",
                              "parent_word_gap_outlier"],
            },
            {
                "start": 65.0, "end": 66.0, "text": "结尾",
                "text_original": "结尾",
                "words": [{"text": "结尾", "start": 65.0, "end": 66.0,
                           "probability": 0.9}],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 0.0,
                "original_asr_segment_end": 80.0,
                "asr_flags": [],
            },
        ]
        audio = np.zeros(16000 * 80, dtype=np.float32)
        audio[16000 * 4:16000 * 7] = 0.1
        audio[16000 * 64:16000 * 67] = 0.1
        calls = 0

        def partial_transcribe(*_args, **kwargs):
            nonlocal calls
            calls += 1
            if calls != 1:
                return []
            kwargs["evidence_out"].append({
                "start": 5.0, "end": 6.0, "text": "开场",
                "text_original": "开场", "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 5.0,
                "original_asr_segment_end": 6.0, "asr_flags": [],
            })
            return [(5.0, 6.0, "开场")]

        with mock.patch.object(index, "transcribe", side_effect=partial_transcribe):
            fixed = index.repair_suspected_alignment(
                rows, audio, evidence_out=evidence, source_audio_track=0)

        self.assertEqual(rows, fixed)
        self.assertNotIn("alignment_repair_failed", evidence[0]["asr_flags"])
        self.assertNotIn("alignment_repair_failed", evidence[1]["asr_flags"])

    def test_alignment_repair_allows_empty_chunks_that_are_truly_silent(self):
        rows = [(5.0, 6.0, "开场")]
        evidence = [{
            "start": 5.0, "end": 6.0, "text": "开场",
            "text_original": "开场",
            "words": [{"text": "开场", "start": 5.0, "end": 6.0,
                       "probability": 0.9}],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 0.0,
            "original_asr_segment_end": 80.0,
            "asr_flags": ["timestamp_alignment_suspected",
                          "parent_word_coverage_outlier"],
        }]
        audio = np.zeros(16000 * 80, dtype=np.float32)
        audio[16000 * 4:16000 * 7] = 0.1
        calls = 0

        def silence_aware_transcribe(*_args, **kwargs):
            nonlocal calls
            calls += 1
            if calls != 1:
                return []
            kwargs["evidence_out"].append({
                "start": 5.0, "end": 6.0, "text": "开场",
                "text_original": "开场", "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 5.0,
                "original_asr_segment_end": 6.0, "asr_flags": [],
            })
            return [(5.0, 6.0, "开场")]

        with mock.patch.object(index, "transcribe",
                               side_effect=silence_aware_transcribe):
            fixed = index.repair_suspected_alignment(
                rows, audio, evidence_out=evidence, source_audio_track=0)

        self.assertEqual([(5.0, 6.0, "开场")], fixed)
        self.assertNotIn("alignment_repair_failed", evidence[0]["asr_flags"])

    def test_alignment_repair_tiles_long_continuously_active_parent(self):
        rows = [(5.0, 6.0, "开场")]
        evidence = [{
            "start": 5.0, "end": 6.0, "text": "开场",
            "text_original": "开场",
            "words": [{"text": "开场", "start": 5.0, "end": 6.0,
                       "probability": 0.9}],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 0.0,
            "original_asr_segment_end": 80.0,
            "asr_flags": ["timestamp_alignment_suspected"],
        }]
        audio = np.full(16000 * 80, 0.1, dtype=np.float32)
        calls = 0

        def first_chunk_only(*_args, **kwargs):
            nonlocal calls
            calls += 1
            if calls != 1:
                return []
            kwargs["evidence_out"].append({
                "start": 5.0, "end": 6.0, "text": "开场",
                "text_original": "开场", "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 5.0,
                "original_asr_segment_end": 6.0, "asr_flags": [],
            })
            return [(5.0, 6.0, "开场")]

        with mock.patch.object(index, "silero_speech_windows",
                               return_value=([(0.0, 80.0)], "")), \
                mock.patch.object(index, "transcribe", side_effect=first_chunk_only):
            fixed = index.repair_suspected_alignment(
                rows, audio, evidence_out=evidence, source_audio_track=0)

        self.assertEqual(rows, fixed)
        self.assertIn("alignment_repair_failed", evidence[0]["asr_flags"])

    def test_repetition_parent_is_reheard_and_loop_fragments_are_replaced(self):
        rows = [
            (2.0, 5.0, "量子 量子 量子 量子 量子"),
            (3.4, 5.0, "可靠相邻句"),
            (5.2, 5.8, "量子 量子 量子"),
        ]
        evidence = [
            {"start": 2.0, "end": 5.0,
             "text": rows[0][2], "text_original": rows[0][2],
             "words": [], "source_audio_track": 0,
             "source_audio_tracks": [0],
             "original_asr_segment_start": 2.0,
             "original_asr_segment_end": 8.0,
             "asr_flags": ["repetition_suspected"]},
            {"start": 3.4, "end": 5.0,
             "text": rows[1][2], "text_original": rows[1][2],
             "words": [], "source_audio_track": 0,
             "source_audio_tracks": [0],
             "original_asr_segment_start": 3.4,
             "original_asr_segment_end": 5.0, "asr_flags": []},
            {"start": 5.2, "end": 5.8,
             "text": rows[2][2], "text_original": rows[2][2],
             "words": [], "source_audio_track": 0,
             "source_audio_tracks": [0],
             "original_asr_segment_start": 2.0,
             "original_asr_segment_end": 8.0, "asr_flags": []},
        ]

        def fake_transcribe(*_args, **kwargs):
            values = [(0.8, 2.0, "我们开始建造"),
                      (4.0, 5.0, "继续合成")]
            for start, end, text in values:
                kwargs["evidence_out"].append({
                    "start": start, "end": end, "text": text,
                    "text_original": text,
                    "words": [{"text": text, "start": start, "end": end,
                               "probability": 0.9}],
                    "no_speech_prob": 0.0, "avg_logprob": -0.2,
                    "source_audio_track": 0, "source_audio_tracks": [0],
                    "original_asr_segment_start": start,
                    "original_asr_segment_end": end, "asr_flags": [],
                })
            return values

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed = index.repair_suspected_repetitions(
                rows, np.zeros(16000 * 10, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)

        self.assertEqual(["我们开始建造", "可靠相邻句", "继续合成"],
                         [row[2] for row in fixed])
        self.assertFalse(any(index._loop_suspected_text(row[2]) for row in fixed))
        self.assertEqual(len(fixed), len(evidence))

    def test_prompt_free_relisten_keeps_real_repetition(self):
        rows = [(2.0, 4.0, "再再再再可以了")]
        evidence = [{
            "start": 2.0, "end": 4.0, "text": rows[0][2],
            "text_original": rows[0][2], "words": [],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 2.0,
            "original_asr_segment_end": 4.0,
            "asr_flags": ["repetition_suspected"],
        }]

        def fake_transcribe(*_args, **kwargs):
            text = "再再再再可以了"
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 2.6, "text": text,
                "text_original": text, "words": [],
                "no_speech_prob": 0.0, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": ["repetition_suspected"],
            })
            return [(0.6, 2.6, text)]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed = index.repair_suspected_repetitions(
                rows, np.zeros(16000 * 6, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)
        self.assertEqual(rows, fixed)
        self.assertIn("repetition_suspected", evidence[0]["asr_flags"])

    def test_overlap_cleanup_drops_loop_and_nested_single_character_only(self):
        pairs = [
            ((0.0, 2.0, "正常完整台词"),
             {"source_audio_track": 0, "source_audio_tracks": [0]}),
            ((0.4, 1.4, "量子 量子 量子"),
             {"source_audio_track": 0, "source_audio_tracks": [0]}),
            ((0.8, 0.9, "子"),
             {"source_audio_track": 0, "source_audio_tracks": [0]}),
            ((1.95, 2.4, "嗯"),
             {"source_audio_track": 0, "source_audio_tracks": [0]}),
        ]
        cleaned = index._prune_overlapping_asr_fragments(
            pairs, anchors=[pairs[0]])
        self.assertEqual(["正常完整台词", "嗯"],
                         [segment[2] for segment, _row in cleaned])

    def test_confirmed_real_repetition_survives_overlapping_neighbor(self):
        repeated = "再再再再可以了"
        rows = [
            (0.0, 2.0, repeated),
            (1.0, 3.0, "正常相邻句"),
        ]
        evidence = [
            {
                "start": 0.0, "end": 2.0, "text": repeated,
                "text_original": repeated, "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 0.0,
                "original_asr_segment_end": 2.0,
                "asr_flags": ["repetition_suspected"],
            },
            {
                "start": 1.0, "end": 3.0, "text": "正常相邻句",
                "text_original": "正常相邻句", "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 1.0,
                "original_asr_segment_end": 3.0,
                "asr_flags": [],
            },
        ]

        def fake_transcribe(*_args, **kwargs):
            kwargs["evidence_out"].append({
                "start": 0.0, "end": 2.0, "text": repeated,
                "text_original": repeated, "words": [],
                "no_speech_prob": 0.0, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": ["repetition_suspected"],
            })
            return [(0.0, 2.0, repeated)]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed = index.repair_suspected_repetitions(
                rows, np.zeros(16000 * 4, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)

        self.assertEqual(rows, fixed)
        self.assertEqual(len(fixed), len(evidence))

    def test_repetition_with_credible_word_timing_skips_relisten(self):
        repeated = "那这回再再再再可以了吧"
        rows = [(2.0, 4.4, repeated)]
        words = []
        cursor = 2.0
        for text in ["那", "这", "回", "再", "再", "再", "再", "可以", "了吧"]:
            words.append({"text": text, "start": cursor,
                          "end": cursor + 0.12, "probability": 0.95})
            cursor += 0.12
        evidence = [{
            "start": 2.0, "end": 4.4, "text": repeated,
            "text_original": repeated, "words": words,
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 2.0,
            "original_asr_segment_end": 4.4,
            "asr_flags": ["repetition_suspected"],
        }]

        with mock.patch.object(index, "transcribe") as transcribe_mock:
            fixed = index.repair_suspected_repetitions(
                rows, np.zeros(16000 * 6, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)

        self.assertEqual(rows, fixed)
        transcribe_mock.assert_not_called()

    def test_tiny_relisten_overlap_does_not_replace_whole_parent(self):
        repeated = "量子 量子 量子 量子 量子"
        rows = [(2.0, 8.0, repeated)]
        evidence = [{
            "start": 2.0, "end": 8.0, "text": repeated,
            "text_original": repeated, "words": [],
            "source_audio_track": 0, "source_audio_tracks": [0],
            "original_asr_segment_start": 2.0,
            "original_asr_segment_end": 8.0,
            "asr_flags": ["repetition_suspected"],
        }]

        def fake_transcribe(*_args, **kwargs):
            text = "可靠但覆盖很小"
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 0.8, "text": text,
                "text_original": text, "words": [],
                "no_speech_prob": 0.0, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 0.8, text)]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed = index.repair_suspected_repetitions(
                rows, np.zeros(16000 * 10, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)

        self.assertEqual(rows, fixed)
        self.assertEqual(repeated, evidence[0]["text"])

    def test_uncovered_normal_child_of_repetition_parent_is_not_deleted(self):
        repeated = "量子 量子 量子 量子 量子"
        rows = [
            (2.0, 4.0, repeated),
            (6.0, 8.0, "父段里的正常子句"),
        ]
        evidence = [
            {
                "start": 2.0, "end": 4.0, "text": repeated,
                "text_original": repeated, "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 2.0,
                "original_asr_segment_end": 8.0,
                "asr_flags": ["repetition_suspected"],
            },
            {
                "start": 6.0, "end": 8.0, "text": "父段里的正常子句",
                "text_original": "父段里的正常子句", "words": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
                "original_asr_segment_start": 2.0,
                "original_asr_segment_end": 8.0,
                "asr_flags": [],
            },
        ]

        def fake_transcribe(*_args, **kwargs):
            text = "修复后的量子句"
            kwargs["evidence_out"].append({
                "start": 0.6, "end": 2.0, "text": text,
                "text_original": text, "words": [],
                "no_speech_prob": 0.0, "avg_logprob": -0.2,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [],
            })
            return [(0.6, 2.0, text)]

        with mock.patch.object(index, "transcribe", side_effect=fake_transcribe):
            fixed = index.repair_suspected_repetitions(
                rows, np.zeros(16000 * 10, dtype=np.float32),
                evidence_out=evidence, source_audio_track=0)

        self.assertIn("父段里的正常子句", [row[2] for row in fixed])
        self.assertEqual(len(fixed), len(evidence))

    def test_overlap_cleanup_does_not_assume_unknown_track_matches(self):
        pairs = [
            ((0.0, 2.0, "正常完整台词"),
             {"source_audio_track": 0, "source_audio_tracks": [0]}),
            ((0.4, 1.4, "量子 量子 量子 量子"), {}),
        ]

        cleaned = index._prune_overlapping_asr_fragments(
            pairs, anchors=[pairs[0]])

        self.assertEqual(["正常完整台词", "量子 量子 量子 量子"],
                         [segment[2] for segment, _row in cleaned])


if __name__ == "__main__":
    unittest.main()
