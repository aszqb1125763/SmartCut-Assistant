# -*- coding: utf-8 -*-
import unittest

import numpy as np

import director_multimodal as dm


SR = 16000


def tone(start, end, duration=2.0, amplitude=0.35, frequency=220.0):
    samples = np.zeros(int(duration * SR), dtype=np.float32)
    lo, hi = int(start * SR), int(end * SR)
    times = np.arange(max(0, hi - lo), dtype=np.float32) / SR
    samples[lo:hi] = amplitude * np.sin(2.0 * np.pi * frequency * times)
    return samples


class DirectorMultimodalTests(unittest.TestCase):
    def test_disk_window_keeps_absolute_timestamps_and_one_sided_check(self):
        # 只加载原片 10.0~11.0 秒的缓存窗口，报告仍必须使用绝对时间。
        audio = tone(0.0, 0.55, duration=1.0)
        words = [{"id": "absolute", "word": "我", "start": 10.02,
                  "end": 10.22, "probability": 0.91}]
        report = dm.analyze_audio_span(
            audio, SR, (10.02, 10.30), words, time_offset=10.0,
            check_start=True, check_end=False, check_content=False)
        self.assertEqual(10.0, report["time_offset"])
        self.assertTrue(report["checks"]["start"])
        self.assertFalse(report["checks"]["end"])
        findings = [row for row in report["findings"]
                    if row["type"] == "speech_cut_start"]
        self.assertTrue(findings)
        self.assertGreaterEqual(findings[0]["suggested_interval"][0], 10.0)

    def test_review_windows_prioritize_cuts_low_confidence_and_no_dialogue(self):
        data = {
            "duration": 10.0,
            "segments": [[0.5, 1.5, "开头"]],
            "semantic_findings": [{"time": 4.4, "message": "上下文冲突"}],
        }
        words = [{"id": "weak", "word": "可能", "start": 0.7, "end": 0.9,
                  "confidence": 0.4}]
        rows = dm.select_review_windows(data, [(0.4, 1.6), (4.0, 5.0)], words)
        kinds = {row["kind"] for row in rows}
        self.assertIn("cut_start", kinds)
        self.assertIn("cut_end", kinds)
        self.assertIn("low_confidence_word", kinds)
        self.assertIn("no_dialogue_event", kinds)
        self.assertIn("semantic_conflict", kinds)

    def test_review_window_budget_never_drops_required_cut_pairs(self):
        spans = [(float(index * 3), float(index * 3 + 2)) for index in range(20)]
        data = {"duration": 60.0, "segments": [[0.0, 1.0, "测试"]]}
        rows = dm.select_review_windows(data, spans, max_windows=8)
        by_span = {}
        for row in rows:
            if row["kind"] not in {"cut_start", "cut_end"}:
                continue
            for ref in row.get("evidence_refs") or []:
                if ref.startswith("span:"):
                    by_span.setdefault(int(ref.split(":", 1)[1]), set()).add(row["kind"])
        self.assertIn(0, by_span)
        self.assertIn(19, by_span)
        self.assertEqual(set(range(20)), set(by_span))
        self.assertEqual(40, sum(row["kind"] in {"cut_start", "cut_end"}
                                 for row in rows))
        self.assertTrue(all(kinds == {"cut_start", "cut_end"}
                            for kinds in by_span.values()))

    def test_deleted_uncertain_words_do_not_spend_delivered_film_review_budget(self):
        # Regression from the real failed run: both silence reports referred
        # to uncertain source words that the final timeline had removed.
        data = {
            "duration": 200.0,
            "segments": [[101.8, 102.16, "删去"], [150.7, 150.98, "删去"],
                         [170.4, 170.9, "留下"]],
            "scores": [0.99, 0.98, 0.8],
            "semantic_findings": [{"time": 101.98}, {"time": 150.84}],
            "script": {"sections": [{"start": 101.98}, {"start": 150.84}]},
        }
        spans = [(5.0, 8.0), (100.0, 101.0), (151.2, 153.0), (170.0, 175.0)]
        words = [{"id": "removed_a", "start": 101.8, "end": 102.16,
                  "confidence": 0.3},
                 {"id": "removed_b", "start": 150.7, "end": 150.98,
                  "confidence": 0.4},
                 {"id": "kept", "start": 170.4, "end": 170.9,
                  "confidence": 0.4}]
        rows = dm.select_review_windows(data, spans, words, max_windows=1)
        boundaries = [row for row in rows if row["kind"] in {"cut_start", "cut_end"}]
        extras = [row for row in rows if row["kind"] not in {"cut_start", "cut_end"}]
        self.assertEqual(2 * len(spans), len(boundaries))
        self.assertEqual(1, len(extras))
        self.assertEqual(["kept"], extras[0]["evidence_refs"])
        self.assertEqual(3, extras[0]["span_index"])
        self.assertFalse(any(abs(row["time"] - deleted) < 1e-6
                             for row in extras for deleted in (101.98, 150.84)))

    def test_partially_retained_low_confidence_word_uses_retained_intersection(self):
        spans = [(10.1, 10.9)]
        words = [{"id": "partial", "start": 9.8, "end": 10.2,
                  "confidence": 0.4}]
        rows = dm.select_review_windows({"duration": 20.0}, spans, words)
        row = next(row for row in rows if row["kind"] == "low_confidence_word")
        self.assertEqual(10.15, row["time"])
        self.assertEqual(0, row["span_index"])
        self.assertEqual([10.1, 10.45], row["target_interval"])
        self.assertEqual(2, sum(row["kind"] in {"cut_start", "cut_end"}
                                for row in rows))

    def test_optional_content_targets_stay_inside_their_retained_span(self):
        spans = [(1.0, 1.15), (3.0, 3.4)]
        data = {"duration": 5.0,
                "semantic_findings": [{"time": 3.05}],
                "script": {"sections": [{"start": 3.38}]}}
        rows = dm.select_review_windows(data, spans)
        extras = [row for row in rows if row["kind"] not in {"cut_start", "cut_end"}]
        self.assertTrue(extras)
        self.assertIn("semantic_conflict", {row["kind"] for row in extras})
        self.assertIn("chapter_transition", {row["kind"] for row in extras})
        for row in extras:
            left, right = spans[row["span_index"]]
            self.assertLessEqual(left, row["time"])
            self.assertLessEqual(row["time"], right)
            self.assertLessEqual(left, row["target_interval"][0])
            self.assertLess(row["target_interval"][0], row["target_interval"][1])
            self.assertLessEqual(row["target_interval"][1], right)

    def test_zero_optional_budget_still_reviews_every_actual_cut(self):
        rows = dm.select_review_windows(
            {"duration": 10.0}, [(0.0, 1.0), (9.0, 10.0)], max_windows=0)
        self.assertEqual(4, len(rows))
        self.assertEqual({"cut_start", "cut_end"}, {row["kind"] for row in rows})

    def test_review_windows_clip_media_limits_and_infer_missing_duration(self):
        clipped = dm.select_review_windows(
            {"duration": 10.0}, [(-1.0, 1.0), (9.0, 11.0), (4.0, 4.0)],
            max_windows=0)
        self.assertEqual([0.0, 1.0, 9.0, 10.0], [row["time"] for row in clipped])
        inferred = dm.select_review_windows(
            {"duration": 0.0}, [(1.0, 2.0)], max_windows=0)
        self.assertEqual([1.0, 2.0], [row["time"] for row in inferred])
        self.assertEqual([], dm.select_review_windows({"duration": 0.0}, []))

    def test_audio_coverage_must_include_every_edge_and_selected_track(self):
        windows = [{
            "id": "review_001", "kind": "cut_start",
            "expected_audio_tracks": ["0", "1"],
        }, {
            "id": "review_002", "kind": "cut_end",
            "expected_audio_tracks": ["0", "1"],
        }]
        reports = [{
            "review_window_id": "review_001", "track_id": "0",
            "actual_audio_inspected": True, "score": 100.0,
            "findings": [],
        }]
        review = dm.combine_review(
            {"score": 100.0, "issues": []}, reports,
            require_visual=False, expected_audio_windows=windows)
        self.assertFalse(review["passed"])
        self.assertEqual(4, review["audio_coverage"]["expected_edge_tracks"])
        self.assertEqual(1, review["audio_coverage"]["reviewed_edge_tracks"])
        self.assertIn("audio_boundary_coverage_incomplete",
                      {row["type"] for row in review["findings"]})

    def test_mixed_timeline_revisions_are_rejected(self):
        revision = "a" * 64
        windows = [{
            "id": "review_001", "kind": "cut_start",
            "expected_audio_tracks": ["0"],
        }]
        text = {"score": 100.0, "issues": [],
                "timeline_revision": revision}
        audio = [{
            "review_window_id": "review_001", "track_id": "0",
            "actual_audio_inspected": True, "score": 100.0,
            "findings": [], "timeline_revision": "b" * 64,
        }]
        visual = {"nodes": [{"id": "v1", "issues": []}],
                  "timeline_revision": revision}
        review = dm.combine_review(
            text, audio, visual, expected_audio_windows=windows,
            expected_timeline_revision=revision)
        self.assertFalse(review["passed"])
        self.assertTrue(review["vetoed"])
        self.assertIn("timeline_revision_mismatch",
                      {row["type"] for row in review["findings"]})

    def test_text_event_coordinates_survive_multimodal_merge(self):
        source_revision = "c" * 64
        text = {
            "score": 80.0,
            "source_timeline_revision": source_revision,
            "issues": [{
                "type": "abrupt_start", "severity": "major",
                "message": "从半句话开始", "indices": [4, 5],
                "event_ids": ["U0005", "U0006"],
                "coordinate_space": "source",
                "interval": [12.25, 16.75],
                "action": "add_previous",
            }],
        }
        review = dm.combine_review(
            text, [], require_audio=False, require_visual=False)
        finding = next(row for row in review["findings"]
                       if row["type"] == "abrupt_start")
        self.assertEqual(["U0005", "U0006"], finding["event_ids"])
        self.assertEqual("source", finding["coordinate_space"])
        self.assertEqual([12.25, 16.75], finding["suggested_interval"])
        self.assertEqual(source_revision,
                         finding["source_timeline_revision"])
        self.assertIn("event:U0005", finding["evidence_refs"])
        self.assertIn([12.25, 16.75], review["rework_intervals"])

    def test_short_first_word_uses_waveform_and_word_guard(self):
        # “我”的波形在 ASR 词时间前已经开始，入点正好落在词时间时必须向前扩。
        audio = tone(0.43, 1.15)
        words = [{"id": "w1", "word": "我", "start": 0.50, "end": 0.68,
                  "probability": 0.94},
                 {"id": "w2", "word": "来了", "start": 0.69, "end": 1.08,
                  "probability": 0.97}]
        report = dm.analyze_audio_span(audio, SR, (0.50, 1.25), words)
        issue = next(row for row in report["findings"]
                     if row["type"] == "speech_cut_start")
        self.assertTrue(issue["veto"])
        self.assertLess(issue["suggested_interval"][0], 0.50)
        self.assertIn("w1", issue["evidence_refs"])

    def test_clean_silence_around_words_has_no_boundary_veto(self):
        audio = tone(0.62, 1.18)
        words = [{"id": "w", "word": "完整句子", "start": 0.64, "end": 1.14,
                  "confidence": 0.96}]
        report = dm.analyze_audio_span(audio, SR, (0.48, 1.32), words)
        self.assertFalse([row for row in report["findings"] if row["veto"]])
        self.assertGreaterEqual(report["score"], 95)
        self.assertIn("dynamic_range_db", report["metrics"])

    def test_speech_word_evidence_is_isolated_by_source_track(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "s1.w1", "word": "我", "start": 0.50,
                  "end": 0.70, "confidence": 0.98, "source_track": 2}]
        own = dm.analyze_audio_span(
            audio, SR, (0.58, 0.64), words, track_id="2")
        other = dm.analyze_audio_span(
            audio, SR, (0.58, 0.64), words, track_id="1")
        own_cuts = [row for row in own["findings"]
                    if row["type"].startswith("speech_cut_")]
        other_cuts = [row for row in other["findings"]
                      if row["type"].startswith("speech_cut_")]
        self.assertEqual(2, len(own_cuts))
        self.assertTrue(all(row["confidence"] == 0.98 for row in own_cuts))
        self.assertTrue(all(row["boundary_word_refs"] == ["s1.w1"]
                            for row in own_cuts))
        self.assertEqual(2, len(other_cuts))
        self.assertTrue(all(row["confidence"] == 0.72 for row in other_cuts))
        self.assertTrue(all(row["boundary_word_refs"] == []
                            for row in other_cuts))
        self.assertEqual([], other["word_refs"])
        self.assertEqual([], other["context_word_refs"])
        self.assertTrue(all("s1.w1" not in row["evidence_refs"]
                            for row in other_cuts))

    def test_unassigned_legacy_words_still_protect_short_first_word(self):
        audio = tone(0.43, 0.90)
        words = [{"id": "legacy", "word": "我", "start": 0.50,
                  "end": 0.59, "confidence": 0.95}]
        report = dm.analyze_audio_span(
            audio, SR, (0.53, 0.77), words, track_id="2", check_end=False)
        issue = next(row for row in report["findings"]
                     if row["type"] == "speech_cut_start")
        self.assertEqual(["legacy"], issue["boundary_word_refs"])
        self.assertEqual("word_guard", issue["evidence_kind"])
        self.assertEqual(0.50, issue["boundary_anchor"]["start"])
        self.assertEqual(0.95, issue["confidence"])

    def test_continuous_background_does_not_reopen_completed_word_guards(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "whole", "word": "完整", "start": 0.65,
                  "end": 1.05, "confidence": 0.97, "source_track": "2"}]
        report = dm.analyze_audio_span(
            audio, SR, (0.50, 1.20), words, track_id="2")
        self.assertEqual([], [row for row in report["findings"]
                              if row["type"].startswith("speech_cut_")])
        self.assertEqual([], report["word_refs"])
        self.assertEqual(["whole"], report["context_word_refs"])

    def test_arbitrary_word_inside_short_target_is_not_boundary_proof(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "inside", "word": "在", "start": 0.66,
                  "end": 0.83, "confidence": 0.99}]
        start_report = dm.analyze_audio_span(
            audio, SR, (0.50, 0.74), words,
            check_end=False, check_content=False)
        end_report = dm.analyze_audio_span(
            audio, SR, (0.76, 1.00), words,
            check_start=False, check_content=False)
        for report in (start_report, end_report):
            self.assertEqual([], [row for row in report["findings"]
                                  if row["type"].startswith("speech_cut_")])
            self.assertEqual([], report["word_refs"])
            self.assertEqual(["inside"], report["context_word_refs"])

    def test_word_outside_short_target_can_confirm_tail_guard_is_complete(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "finished", "word": "结束", "start": 0.50,
                  "end": 0.65, "confidence": 0.99}]
        report = dm.analyze_audio_span(
            audio, SR, (0.76, 1.00), words,
            check_start=False, check_content=False)
        self.assertEqual([], [row for row in report["findings"]
                              if row["type"] == "speech_cut_end"])
        self.assertEqual([], report["word_refs"])

    def test_real_missing_tail_guard_is_word_backed_and_settles_after_repair(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "s2.w9", "word": "好", "start": 0.91,
                  "end": 1.12, "confidence": 0.96, "source_track": "2"}]
        report = dm.analyze_audio_span(
            audio, SR, (0.86, 1.10), words, track_id="2",
            check_start=False, check_content=False)
        issue = next(row for row in report["findings"]
                     if row["type"] == "speech_cut_end")
        self.assertEqual(["s2.w9"], issue["boundary_word_refs"])
        self.assertEqual(1.10, issue["boundary_time"])
        self.assertEqual("end", issue["boundary_side"])
        self.assertTrue(issue["veto"])
        repaired_end = issue["suggested_interval"][1]
        self.assertGreaterEqual(repaired_end - 1.12, 0.08)
        fixed = dm.analyze_audio_span(
            audio, SR, (repaired_end - 0.24, repaired_end), words,
            track_id="2", check_start=False, check_content=False)
        self.assertEqual([], [row for row in fixed["findings"]
                              if row["type"] == "speech_cut_end"])

    def test_real_first_word_guard_settles_despite_continuous_background(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "s1.w0", "word": "我", "start": 0.50,
                  "end": 0.59, "confidence": 0.96}]
        report = dm.analyze_audio_span(
            audio, SR, (0.50, 0.74), words,
            check_end=False, check_content=False)
        issue = next(row for row in report["findings"]
                     if row["type"] == "speech_cut_start")
        repaired_start = issue["suggested_interval"][0]
        fixed = dm.analyze_audio_span(
            audio, SR, (repaired_start, repaired_start + 0.24), words,
            check_end=False, check_content=False)
        self.assertEqual([], [row for row in fixed["findings"]
                              if row["type"] == "speech_cut_start"])

    def test_waveform_only_protection_keeps_low_confidence_and_no_word_anchor(self):
        report = dm.analyze_audio_span(tone(0.0, 2.0), SR, (0.50, 1.10), [])
        cuts = [row for row in report["findings"]
                if row["type"].startswith("speech_cut_")]
        self.assertEqual(2, len(cuts))
        for issue in cuts:
            self.assertEqual(0.72, issue["confidence"])
            self.assertEqual("waveform_only", issue["evidence_kind"])
            self.assertEqual([], issue["boundary_word_refs"])
            self.assertIsNone(issue["boundary_anchor"])

    def test_exactly_complete_guard_does_not_reopen_while_short_guard_is_protected(self):
        audio = tone(0.0, 2.0)
        words = [{"id": "word", "word": "好了", "start": 0.60,
                  "end": 1.00, "confidence": 0.96}]
        for side, broken, complete in (
                ("start", (0.56, 0.80), (0.52, 0.76)),
                ("end", (0.80, 1.04), (0.84, 1.08))):
            with self.subTest(side=side):
                flags = {"check_start": side == "start",
                         "check_end": side == "end", "check_content": False}
                needs_guard = dm.analyze_audio_span(audio, SR, broken, words, **flags)
                guarded = dm.analyze_audio_span(audio, SR, complete, words, **flags)
                self.assertTrue([row for row in needs_guard["findings"]
                                 if row["type"] == "speech_cut_" + side])
                self.assertFalse([row for row in guarded["findings"]
                                  if row["type"] == "speech_cut_" + side])

    def test_shared_legacy_words_do_not_turn_game_track_into_speech_evidence(self):
        audio = tone(0.75, 1.38, frequency=90.0)
        words = [{"id": "shared", "word": "我", "start": 1.05,
                  "end": 1.25, "confidence": 0.99}]
        report = dm.analyze_audio_span(
            audio, SR, (0.60, 1.10), words, track_id="game", role="game")
        tail = next(row for row in report["findings"]
                    if row["type"] == "important_audio_tail")
        self.assertEqual(0.84, tail["confidence"])
        self.assertNotIn("shared", tail["evidence_refs"])
        self.assertFalse([row for row in report["findings"]
                          if row["type"].startswith("speech_cut_")])
        self.assertEqual([], report["word_refs"])

    def test_cached_window_never_assigns_samples_before_its_absolute_offset(self):
        audio = tone(0.10, 0.22, duration=0.7)
        words = [{"id": "absolute_short", "word": "我", "start": 10.10,
                  "end": 10.20, "confidence": 0.96}]
        report = dm.analyze_audio_span(
            audio, SR, (10.10, 10.34), words, time_offset=10.0,
            check_end=False, check_content=False)
        issue = next(row for row in report["findings"]
                     if row["type"] == "speech_cut_start")
        self.assertEqual(10.0, report["window"][0])
        self.assertEqual(10.10, issue["boundary_time"])
        self.assertEqual(["absolute_short"], issue["boundary_word_refs"])
        self.assertGreaterEqual(issue["suggested_interval"][0], 10.0)

    def test_secondary_game_track_preserves_event_tail(self):
        audio = tone(0.75, 1.38, frequency=90.0)
        report = dm.analyze_audio_span(
            audio, SR, (0.60, 1.10), [], track_id="game", role="game")
        issue = next(row for row in report["findings"]
                     if row["type"] == "important_audio_tail")
        self.assertEqual(issue["severity"], "major")
        self.assertGreater(issue["suggested_interval"][1], 1.10)

    def test_continuous_background_track_is_not_a_major_event_tail(self):
        audio = tone(0.0, 2.0)
        report = dm.analyze_audio_span(
            audio, SR, (0.60, 1.10), [], track_id="game", role="game")
        self.assertFalse([row for row in report["findings"]
                          if row["type"] == "important_audio_tail"])

    def test_media_end_does_not_report_an_impossible_tail_expansion(self):
        audio = tone(0.4, 2.0)
        words = [{"id": "last", "word": "结束", "start": 1.72,
                  "end": 1.98, "probability": 0.97}]
        report = dm.analyze_audio_span(
            audio, SR, (1.60, 2.0), words, check_start=False)
        self.assertFalse([row for row in report["findings"]
                          if row["type"] == "speech_cut_end"])

    def test_same_game_event_on_many_tracks_counts_once_per_review_window(self):
        reports = []
        for track in range(5):
            reports.append({
                "actual_audio_inspected": True, "score": 88.0,
                "review_window_id": "review_007",
                "findings": [{
                    "id": "tail_%d" % track, "type": "important_audio_tail",
                    "modality": "audio", "severity": "major", "veto": False,
                    "message": "同一个游戏事件尾音", "evidence_refs": ["audio:%d" % track],
                    "suggested_interval": [1.0, 1.3 + track * 0.01],
                    "confidence": 0.84, "action": "expand_end", "learnable": True,
                }],
            })
        result = dm.combine_review({"score": 100.0, "issues": []}, reports)
        tails = [row for row in result["findings"]
                 if row["type"] == "important_audio_tail"]
        self.assertEqual(1, len(tails))
        self.assertEqual(1, result["major_issues"])

    def test_multi_track_order_and_output_are_deterministic(self):
        tracks = {
            "speech": {"samples": tone(0.4, 0.8), "role": "speech",
                       "sample_rate": SR},
            "game": {"samples": tone(0.5, 0.9), "role": "game",
                     "sample_rate": SR},
        }
        kwargs = (tracks, SR, [(0.2, 1.1)])
        left = dm.analyze_audio_tracks(*kwargs)
        right = dm.analyze_audio_tracks(*kwargs)
        self.assertEqual(left, right)
        self.assertEqual([row["track_id"] for row in left], ["game", "speech"])

    def test_black_visual_node_has_veto_power(self):
        text = {"score": 95.0, "issues": []}
        audio = [dm.analyze_audio_span(tone(0.5, 1.0), SR, (0.3, 1.2), [])]
        visual = {"nodes": [{
            "id": "visual_1", "time": 1.0, "sample_times": [0.0, 1.0, 2.0],
            "issues": [{"type": "black_or_blank", "severity": "major",
                        "message": "黑屏"}],
        }]}
        result = dm.combine_review(text, audio, visual)
        finding = next(row for row in result["findings"]
                       if row["type"] == "black_or_blank")
        self.assertEqual(finding["severity"], "critical")
        self.assertTrue(finding["veto"])
        self.assertFalse(result["passed"])

    def test_text_and_audio_are_equal_primary_evidence(self):
        text = {"score": 80.0, "issues": []}
        audio = [{"actual_audio_inspected": True, "score": 100.0, "findings": []}]
        result = dm.combine_review(text, audio)
        self.assertEqual(result["primary_weights"], {"text": 0.5, "audio": 0.5})
        self.assertEqual(result["scores"]["primary"], 90.0)
        self.assertTrue(result["passed"])

    def test_missing_audio_is_explicit_failure_not_silent_fallback(self):
        result = dm.combine_review({"score": 100.0, "issues": []}, [])
        self.assertEqual(result["evidence_status"]["audio"], "missing")
        self.assertTrue(result["vetoed"])
        self.assertFalse(result["passed"])

    def test_confirmed_no_dialogue_uses_audio_as_primary_evidence(self):
        audio = [{"actual_audio_inspected": True, "score": 96.0, "findings": []}]
        visual = {"nodes": [{"id": "v1", "issues": [],
                              "sample_times": [1.0, 2.0, 3.0]}]}
        result = dm.combine_review(None, audio, visual, require_visual=True,
                                   text_applicable=False)
        self.assertEqual("not_applicable", result["evidence_status"]["text"])
        self.assertEqual({"text": 0.0, "audio": 1.0}, result["primary_weights"])
        self.assertEqual(96.0, result["scores"]["primary"])
        self.assertTrue(result["passed"])

    def test_low_confidence_evidence_cannot_auto_learn(self):
        text = {"score": 90.0, "issues": [{
            "type": "uncertain_semantics", "severity": "minor",
            "message": "语义不确定", "confidence": 0.55,
        }]}
        audio = [{"actual_audio_inspected": True, "score": 100.0, "findings": []}]
        result = dm.combine_review(text, audio)
        self.assertFalse(result["auto_learning_eligible"])
        self.assertTrue(result["low_confidence_findings"])


if __name__ == "__main__":
    unittest.main()
