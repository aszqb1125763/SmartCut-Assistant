# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import index
import subtitle_quality


class DeliveryAlignmentRepairTests(unittest.TestCase):
    def make_index(self, root, *, structural_mismatch=False):
        source = os.path.join(root, "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"test source placeholder")
        index_dir = os.path.join(root, "index")
        os.makedirs(index_dir)
        data = {
            "schema": 1,
            "source": source,
            "audio_stream_used": 0,
            "asr_profile": {
                "version": 9,
                "model": "large-v3",
                "beam_size": 5,
                "initial_prompt": "水槽叙事",
                "hotwords": "水槽战士",
            },
            "segments": [[10.0, 13.0, "镜"]],
            "segment_evidence": [] if structural_mismatch else [{
                "segment_id": "s000001",
                "start": 10.0,
                "end": 13.0,
                "text": "镜",
                "text_original": "镜",
                "source_audio_track": 0,
                "asr_flags": [
                    "alignment_repair_failed",
                    "timestamp_alignment_suspected",
                ],
                "words": [{
                    "text": "镜",
                    "start": 10.0,
                    "end": 13.0,
                    "probability": 0.99,
                }],
            }],
            "cards": [],
        }
        path = os.path.join(index_dir, "index.json")
        self.write_json(path, data)
        return index_dir, path, data

    @staticmethod
    def write_json(path, data):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    @staticmethod
    def repaired_single_row(text_segs, evidence_out):
        text_segs[:] = [(10.10, 10.52, "镜子")]
        evidence_out[:] = [{
            "segment_id": "s000001",
            "start": 10.10,
            "end": 10.52,
            "text": "镜子",
            "text_original": "镜子",
            "source_audio_track": 0,
            "asr_flags": [],
            "words": [{
                "text": "镜子",
                "start": 10.10,
                "end": 10.52,
                "probability": 0.98,
            }],
        }]

    def test_stretched_single_character_is_reheard_and_saved_locally(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path, _data = self.make_index(root)
            cache = os.path.join(index_dir, subtitle_quality.CACHE_NAME)
            report = os.path.join(index_dir, subtitle_quality.REPORT_NAME)
            self.write_json(cache, {"stale": True})
            self.write_json(report, {"stale": True})

            def repair(text_segs, _audio, *, evidence_out=None, **_kwargs):
                self.repaired_single_row(text_segs, evidence_out)
                return text_segs

            with mock.patch.object(
                    index, "load_audio_mono16k",
                    return_value=np.ones(16_000 * 14, dtype=np.float32)) as load_audio, \
                    mock.patch.object(
                        index, "repair_suspected_alignment",
                        side_effect=repair) as local_repair, \
                    mock.patch.object(
                        index, "index_video",
                        side_effect=AssertionError("不得触发全片重新分析")) as full_index:
                result = index.repair_index_before_delivery(index_dir)

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertTrue(result["changed"])
            self.assertEqual(1, result["attempts"])
            self.assertEqual([], result["remaining_issues"])
            self.assertEqual([[10.1, 10.52, "镜子"]], saved["segments"])
            self.assertEqual([], subtitle_quality.alignment_issues(saved))
            self.assertFalse(os.path.exists(cache))
            self.assertFalse(os.path.exists(report))
            load_audio.assert_called_once()
            local_repair.assert_called_once()
            full_index.assert_not_called()

    def test_first_local_failure_then_second_attempt_succeeds(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path, _data = self.make_index(root)
            calls = []

            def repair(text_segs, _audio, *, evidence_out=None, **_kwargs):
                calls.append(len(calls) + 1)
                if len(calls) == 2:
                    self.repaired_single_row(text_segs, evidence_out)
                return text_segs

            with mock.patch.object(
                    index, "load_audio_mono16k",
                    return_value=np.ones(16_000 * 14, dtype=np.float32)), \
                    mock.patch.object(
                        index, "repair_suspected_alignment",
                        side_effect=repair), \
                    mock.patch.object(
                        index, "index_video",
                        side_effect=AssertionError("不得触发全片重新分析")) as full_index:
                result = index.repair_index_before_delivery(
                    index_dir, max_attempts=2)

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual([1, 2], calls)
            self.assertEqual(2, result["attempts"])
            self.assertEqual([], subtitle_quality.alignment_issues(saved))
            full_index.assert_not_called()

    def test_two_failed_attempts_keep_original_index_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path, _data = self.make_index(root)
            with open(path, "rb") as handle:
                before = handle.read()

            with mock.patch.object(
                    index, "load_audio_mono16k",
                    return_value=np.ones(16_000 * 14, dtype=np.float32)), \
                    mock.patch.object(
                        index, "repair_suspected_alignment",
                        side_effect=lambda text_segs, _audio, **_kwargs: text_segs
                    ) as local_repair, \
                    mock.patch.object(
                        index, "index_video",
                        side_effect=AssertionError("不得触发全片重新分析")) as full_index:
                with self.assertRaisesRegex(RuntimeError, "自动修复.*2"):
                    index.repair_index_before_delivery(index_dir, max_attempts=2)

            with open(path, "rb") as handle:
                after = handle.read()
            self.assertEqual(before, after)
            self.assertEqual(2, local_repair.call_count)
            full_index.assert_not_called()

    def test_structural_mismatch_fails_closed_without_decoding_audio(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path, _data = self.make_index(
                root, structural_mismatch=True)
            with open(path, "rb") as handle:
                before = handle.read()
            with mock.patch.object(index, "load_audio_mono16k") as load_audio, \
                    mock.patch.object(
                        index, "repair_suspected_alignment") as local_repair, \
                    mock.patch.object(index, "index_video") as full_index:
                with self.assertRaisesRegex(
                        RuntimeError, "结构|数量|segment_evidence"):
                    index.repair_index_before_delivery(index_dir)
            with open(path, "rb") as handle:
                self.assertEqual(before, handle.read())
            load_audio.assert_not_called()
            local_repair.assert_not_called()
            full_index.assert_not_called()

    def test_repair_output_with_adjacent_overlap_is_not_committed(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path, _data = self.make_index(root)
            with open(path, "rb") as handle:
                before = handle.read()

            def overlapping_repair(text_segs, _audio, *, evidence_out=None,
                                   **_kwargs):
                text_segs[:] = [
                    (10.0, 11.2, "前句"),
                    (11.0, 12.0, "后句"),
                ]
                evidence_out[:] = [
                    {
                        "segment_id": "s000001", "start": 10.0, "end": 11.2,
                        "text": "前句", "text_original": "前句",
                        "source_audio_track": 0, "asr_flags": [], "words": [],
                    },
                    {
                        "segment_id": "s000002", "start": 11.0, "end": 12.0,
                        "text": "后句", "text_original": "后句",
                        "source_audio_track": 0, "asr_flags": [], "words": [],
                    },
                ]
                return text_segs

            with mock.patch.object(
                    index, "load_audio_mono16k",
                    return_value=np.ones(16_000 * 14, dtype=np.float32)), \
                    mock.patch.object(
                        index, "repair_suspected_alignment",
                        side_effect=overlapping_repair), \
                    mock.patch.object(index, "index_video") as full_index:
                with self.assertRaisesRegex(RuntimeError, "自动修复|重叠|时间"):
                    index.repair_index_before_delivery(index_dir, max_attempts=1)

            with open(path, "rb") as handle:
                self.assertEqual(before, handle.read())
            full_index.assert_not_called()

    def test_stretched_single_overlap_is_marked_for_prompt_free_drop(self):
        data = {
            "audio_stream_used": 1,
            "segment_evidence": [
                {
                    "segment_id": "s000001",
                    "start": 10.0,
                    "end": 13.0,
                    "text": "镜",
                    "text_original": "镜",
                    "source_audio_track": 1,
                    "source_audio_tracks": [1],
                    "original_asr_segment_start": 8.0,
                    "original_asr_segment_end": 15.0,
                    "asr_flags": [
                        "timestamp_alignment_suspected",
                        "word_duration_outlier",
                        "alignment_repair_failed",
                    ],
                    "words": [{
                        "text": "镜", "start": 10.0, "end": 13.0,
                        "probability": 0.99,
                    }],
                },
                {
                    "segment_id": "s000002",
                    "start": 12.4,
                    "end": 13.4,
                    "text": "有心灰",
                    "text_original": "有心灰",
                    "source_audio_track": 1,
                    "source_audio_tracks": [1],
                    "asr_flags": [],
                    "words": [],
                },
            ],
        }

        targets = index._delivery_repair_targets(data)

        self.assertEqual(1, len(targets))
        self.assertTrue(targets[0]["allow_drop_stretched_single"])
        self.assertEqual(8.0 - 1.4, targets[0]["window_start"])
        self.assertEqual(15.0 + 1.4, targets[0]["window_end"])

    def test_prompt_free_relisten_replaces_same_acoustic_event(self):
        old_segments = [
            (10.0, 13.0, "镜"),
            (12.4, 13.4, "有心灰"),
            (14.0, 15.0, "下一句"),
        ]
        old_evidence = [
            {
                "segment_id": "s000001", "start": 10.0, "end": 13.0,
                "text": "镜", "source_audio_track": 1,
                "source_audio_tracks": [1],
            },
            {
                "segment_id": "s000002", "start": 12.4, "end": 13.4,
                "text": "有心灰", "source_audio_track": 1,
                "source_audio_tracks": [1],
            },
            {
                "segment_id": "s000003", "start": 14.0, "end": 15.0,
                "text": "下一句", "source_audio_track": 1,
                "source_audio_tracks": [1],
            },
        ]
        # Local prompt-free ASR supplies corrected text for the same acoustic
        # event.  It intentionally has no stable ID until the repaired index is
        # realigned and committed.
        old_segments.append((12.38, 13.32, "有星辉"))
        old_evidence.append({
            "start": 12.38, "end": 13.32, "text": "有星辉",
            "source_audio_track": 1, "source_audio_tracks": [1],
        })

        segments, evidence = index._dedupe_delivery_relisten_pairs(
            old_segments, old_evidence)

        self.assertEqual(
            [(10.0, 13.0, "镜"), (12.38, 13.32, "有星辉"),
             (14.0, 15.0, "下一句")],
            segments)
        self.assertFalse(evidence[1].get("segment_id"))

    def test_prompt_free_relisten_never_merges_different_audio_tracks(self):
        segments = [(12.4, 13.4, "有心灰"), (12.38, 13.32, "有星辉")]
        evidence = [
            {
                "segment_id": "s000001", "start": 12.4, "end": 13.4,
                "text": "有心灰", "source_audio_track": 1,
                "source_audio_tracks": [1],
            },
            {
                "start": 12.38, "end": 13.32, "text": "有星辉",
                "source_audio_track": 0, "source_audio_tracks": [0],
            },
        ]

        kept_segments, _kept_evidence = index._dedupe_delivery_relisten_pairs(
            segments, evidence)

        self.assertEqual(2, len(kept_segments))

    def make_low_confidence_index(self, root):
        source = os.path.join(root, "source.mp4")
        with open(source, "wb") as handle:
            handle.write(b"test source placeholder")
        index_dir = os.path.join(root, "index-low-confidence")
        os.makedirs(index_dir)
        path = os.path.join(index_dir, "index.json")
        self.write_json(path, {
            "source": source,
            "audio_stream_used": 0,
            "asr_profile": {
                "version": 9, "model": "small", "beam_size": 5,
                "initial_prompt": "水槽叙事", "hotwords": "富硅岩",
            },
            "segments": [[10.0, 11.0, "我"]],
            "segment_evidence": [{
                "segment_id": "s000001", "start": 10.0, "end": 11.0,
                "text": "我", "text_original": "我",
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [], "avg_logprob": -0.3,
                "no_speech_prob": 0.01,
                "words": [{
                    "text": "我", "start": 10.1, "end": 10.4,
                    "probability": 0.07,
                }],
            }],
            "cards": [],
        })
        return index_dir, path

    @staticmethod
    def local_decode(text):
        def decode(_samples, *, evidence_out=None, **_kwargs):
            evidence_out.append({
                "start": 1.05, "end": 1.45,
                "text": text, "text_original": text,
                "source_audio_track": 0, "source_audio_tracks": [0],
                "asr_flags": [], "avg_logprob": -0.08,
                "no_speech_prob": 0.01,
                "words": [{
                    "text": text, "start": 1.05, "end": 1.45,
                    "probability": 0.96,
                }],
            })
            return [(1.05, 1.45, text)]
        return decode

    def test_low_confidence_word_uses_saved_model_and_two_decode_consensus(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path = self.make_low_confidence_index(root)
            decode = self.local_decode("我们")
            with mock.patch.object(
                    index, "load_audio_window_mono16k",
                    return_value=np.ones(16_000 * 3, dtype=np.float32)), \
                    mock.patch.object(index, "transcribe", side_effect=decode) as asr:
                result = index.repair_low_confidence_before_delivery(index_dir)

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertTrue(result["changed"])
            self.assertEqual(1, result["corrected_count"])
            self.assertEqual([[10.05, 10.45, "我们"]], saved["segments"])
            self.assertEqual(2, asr.call_count)
            self.assertTrue(all(call.kwargs["model_size"] == "small"
                                for call in asr.call_args_list))
            self.assertEqual("", asr.call_args_list[0].kwargs["initial_prompt"])
            self.assertEqual("水槽叙事",
                             asr.call_args_list[1].kwargs["initial_prompt"])

    def test_low_confidence_decode_disagreement_keeps_original_text(self):
        with tempfile.TemporaryDirectory() as root:
            index_dir, path = self.make_low_confidence_index(root)
            cache_path = os.path.join(index_dir, "subtitle_quality.json")
            report_path = os.path.join(index_dir, "字幕校对报告.json")
            self.write_json(cache_path, {"cached": True})
            self.write_json(report_path, {"cached": True})
            decodes = [self.local_decode("我们"), self.local_decode("我门")]

            def disagree(samples, **kwargs):
                return decodes.pop(0)(samples, **kwargs)

            with mock.patch.object(
                    index, "load_audio_window_mono16k",
                    return_value=np.ones(16_000 * 3, dtype=np.float32)), \
                    mock.patch.object(index, "transcribe", side_effect=disagree):
                result = index.repair_low_confidence_before_delivery(index_dir)

            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertFalse(result["changed"])
            self.assertEqual(1, result["unresolved_count"])
            self.assertEqual([[10.0, 11.0, "我"]], saved["segments"])
            self.assertTrue(os.path.isfile(cache_path))
            self.assertTrue(os.path.isfile(report_path))


if __name__ == "__main__":
    unittest.main()
