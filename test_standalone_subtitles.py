# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import audiofeat_bounds
import index
import pr_workflow
import sync_server


class StandaloneSubtitleTests(unittest.TestCase):
    def sequence(self, source, clip_count=1):
        audio = []
        for number in range(clip_count):
            start = float(number)
            audio.append({
                "kind": "A", "track": 1,
                "start": start, "end": start + 0.25,
                "in": start, "out": start + 0.25,
                "name": "素材", "path": source,
            })
        return {
            "name": "独立字幕测试", "project_path": "",
            "head": ["SEQ", "独立字幕测试", "1", "2",
                     str(max(1.0, float(clip_count)))],
            "clips": list(audio), "audio": audio, "video": [],
            "tracks": [
                {"kind": "A", "track": 1, "muted": False},
                {"kind": "A", "track": 2, "muted": True},
            ],
        }

    def test_track_numbers_are_one_based_and_auto_skips_muted_tracks(self):
        seq = self.sequence("unused")
        seq["audio"].append(dict(seq["audio"][0], track=2))
        self.assertEqual(
            [1], sync_server._selected_transcription_tracks(
                seq, {"track_numbers": []}))
        self.assertEqual(
            [2], sync_server._selected_transcription_tracks(
                seq, {"track_numbers": [2]}))
        with self.assertRaisesRegex(ValueError, "A3"):
            sync_server._selected_transcription_tracks(
                seq, {"track_numbers": [3]})

    def test_audio_assembly_has_no_six_clip_limit_and_preserves_gaps(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source.mp4")
            with open(source, "wb") as handle:
                handle.write(b"test")
            seq = self.sequence(source, clip_count=7)

            def decode(_path, stream, start, end):
                self.assertEqual(0, stream)
                return np.ones(int(round((end - start) * 16000)),
                               dtype=np.float32)

            with mock.patch.object(index, "load_audio_window_mono16k",
                                   side_effect=decode):
                audio, duration, count = sync_server._assemble_sequence_audio(
                    seq, [1], log=lambda _message: None)
        self.assertEqual(7, count)
        self.assertEqual(7.0, duration)
        self.assertGreater(float(audio[int(0.10 * 16000)]), 0.9)
        self.assertEqual(0.0, float(audio[int(0.50 * 16000)]))
        self.assertGreater(float(audio[int(6.10 * 16000)]), 0.9)

    def test_destination_a_track_falls_back_when_source_stream_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "mono.mp4")
            with open(source, "wb") as handle:
                handle.write(b"test")
            seq = self.sequence(source)
            seq["audio"][0]["track"] = 2
            warnings = []

            def decode(_path, stream, start, end):
                if stream == 1:
                    raise RuntimeError("Stream map '0:a:1' matches no streams")
                return np.ones(int((end - start) * 16000), dtype=np.float32)

            with mock.patch.object(index, "load_audio_window_mono16k",
                                   side_effect=decode):
                _audio, _duration, count = sync_server._assemble_sequence_audio(
                    seq, [2], log=lambda _message: None,
                    warnings_out=warnings)
        self.assertEqual(1, count)
        self.assertTrue(any("回退素材源音轨 1" in value for value in warnings))

    def test_generates_simplified_and_traditional_semantic_srt(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source.mp4")
            project = os.path.join(root, "project.prproj")
            for path in (source, project):
                with open(path, "wb") as handle:
                    handle.write(b"test")
            seq = self.sequence(source)
            seq["project_path"] = project
            cache = os.path.join(root, "cache")
            audio = np.ones(8 * 16000, dtype=np.float32) * 0.05
            original = "这是一个需要按照上下文语义进行断句的字幕测试内容"
            evidence = [{
                "start": 0.5, "end": 6.5, "text": original,
                "text_original": original, "words": [], "asr_flags": [],
                "source_audio_track": 0, "source_audio_tracks": [0],
            }]

            def transcribe(_audio, **kwargs):
                kwargs["evidence_out"].extend(evidence)
                return [(0.5, 6.5, original)]

            identity = lambda segments, *_args, **_kwargs: segments
            with mock.patch.object(sync_server, "SUB_DIR", cache), \
                    mock.patch.object(sync_server, "load_sequence", return_value=seq), \
                    mock.patch.object(sync_server, "load_pr_audio_settings",
                                      side_effect=AssertionError(
                                          "独立转写不应读取剪辑设置")), \
                    mock.patch.object(sync_server, "_assemble_sequence_audio",
                                      return_value=(audio, 8.0, 1)), \
                    mock.patch.object(index, "asr_profile", return_value={
                        "model": "large-v3-turbo", "beam_size": 5,
                        "batch_size": 0, "initial_prompt": "", "hotwords": "",
                        "signature": "profile-v1",
                    }), \
                    mock.patch.object(index, "transcribe", side_effect=transcribe), \
                    mock.patch.object(index, "fill_missing_head", side_effect=identity), \
                    mock.patch.object(index, "repair_suspected_alignment",
                                      side_effect=identity), \
                    mock.patch.object(index, "repair_suspected_repetitions",
                                      side_effect=identity), \
                    mock.patch.object(index, "repair_uncovered_human_speech",
                                      side_effect=lambda segments, *_a, **_k:
                                      (segments, [])), \
                    mock.patch.object(audiofeat_bounds, "refine_caption_segments",
                                      side_effect=lambda _audio, segments, *_a, **_k:
                                      [(row[0], row[1]) for row in segments]), \
                    mock.patch.object(sync_server, "calibrate_subtitles",
                                      side_effect=lambda rows, *_a, **_k: rows):
                result = sync_server.transcribe_sequence(
                    log=lambda _message: None, requested_tracks="1")

            self.assertTrue(result["ok"], result)
            self.assertEqual("large-v3-turbo", result["model"])
            self.assertEqual([1], result["track_numbers"])
            self.assertEqual(["简体中文", "繁体中文"],
                             [row["name"] for row in result["tracks"]])
            for row in result["tracks"]:
                self.assertTrue(os.path.isfile(row["path"]), row)
            with open(result["simplified_srt"], encoding="utf-8") as handle:
                text = handle.read()
            caption_lines = [line for line in text.splitlines()
                             if line and "-->" not in line and not line.isdigit()]
            self.assertTrue(caption_lines)
            self.assertLessEqual(max(len(line) for line in caption_lines), 15)
            with open(result["traditional_srt"], encoding="utf-8") as handle:
                traditional = handle.read()
            self.assertNotEqual(text, traditional)
            self.assertTrue(os.path.isfile(
                os.path.join(cache, "序列字幕.json")))

    def test_unsaved_project_uses_internal_subtitle_directory(self):
        with tempfile.TemporaryDirectory() as root:
            seq = {"project_path": ""}
            with mock.patch.object(sync_server, "SUB_DIR", root):
                folder = sync_server._standalone_subtitle_directory(seq)
        self.assertEqual(os.path.abspath(root), os.path.abspath(folder))

    def test_build_view_rejects_subtitle_cache_from_old_timeline(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source.mp4")
            with open(source, "wb") as handle:
                handle.write(b"test")
            seq = self.sequence(source)
            seq["video"] = [{
                "kind": "V", "track": 1, "start": 0.0, "end": 1.0,
                "in": 0.0, "out": 1.0, "name": "素材", "path": source,
            }]
            seq["clips"] += seq["video"]
            cache = {
                "schema": "smartcut.sequence-subtitle-cache.v1",
                "sequence_fingerprint": "old-timeline",
                "track_numbers": [1],
                "asr_profile_signature": "profile-v1",
                "segments": [{"a": 0.1, "b": 0.8, "text": "不应复用"}],
            }
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, "序列字幕.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(cache, handle, ensure_ascii=False)
            with mock.patch.object(sync_server, "SUB_DIR", root), \
                    mock.patch.object(sync_server, "load_sequence", return_value=seq), \
                    mock.patch.object(sync_server, "load_pr_audio_settings",
                                      return_value={"track_numbers": [1]}), \
                    mock.patch.object(sync_server, "find_index", return_value=None), \
                    mock.patch.object(index, "asr_profile",
                                      return_value={"signature": "profile-v1"}):
                stale = sync_server.build_view()
                cache["sequence_fingerprint"] = pr_workflow.sequence_fingerprint(seq)
                cache["asr_profile_signature"] = "old-profile"
                with open(os.path.join(root, "序列字幕.json"), "w",
                          encoding="utf-8") as handle:
                    json.dump(cache, handle, ensure_ascii=False)
                stale_profile = sync_server.build_view()
                cache["asr_profile_signature"] = "profile-v1"
                with open(os.path.join(root, "序列字幕.json"), "w",
                          encoding="utf-8") as handle:
                    json.dump(cache, handle, ensure_ascii=False)
                current = sync_server.build_view()
                with mock.patch.object(sync_server, "load_pr_audio_settings",
                                       return_value={"track_numbers": [2]}):
                    stale_track = sync_server.build_view()
        self.assertEqual(0, stale["clips"][0]["segments_in_clip"])
        self.assertEqual(0, stale_profile["clips"][0]["segments_in_clip"])
        self.assertEqual(1, current["clips"][0]["segments_in_clip"])
        self.assertEqual(0, stale_track["clips"][0]["segments_in_clip"])

    def test_panel_refreshes_sequence_before_standalone_post(self):
        path = os.path.join(os.path.dirname(__file__), "pr_bridge", "js", "main.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("function transcribeSequence()")
        end = source.index("/* ---- 绑定 ---- */", start)
        body = source[start:end]
        self.assertLess(body.index("captureSequence("),
                        body.index('xhrPost("/transcribe"'))
        self.assertNotIn("saveAudioSettings(", body)
        self.assertIn("JSON.stringify({tracks: subtitleTracks})", body)
        self.assertIn("data.tracks", body)
        self.assertIn("没有修改时间轴", body)
        self.assertGreaterEqual(body.count("captureSequence("), 2)
        self.assertIn("latestSnapshot.raw !== startSnapshot.raw", body)


if __name__ == "__main__":
    unittest.main()
