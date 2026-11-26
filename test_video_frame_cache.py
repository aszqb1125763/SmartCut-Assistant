# -*- coding: utf-8 -*-
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import index
import director_multimodal
import video_frame_cache as vfc
import visual_critic
import ui_thumbs


def _jpeg_bytes(value=80, width=96, height=54):
    image = np.full((height, width, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise RuntimeError("test jpeg encode failed")
    return encoded.tobytes()


class VideoFrameCacheTests(unittest.TestCase):
    def test_second_role_reuses_proxy_without_decoder(self):
        with tempfile.TemporaryDirectory() as temp:
            source = os.path.join(temp, "source.bin")
            Path(source).write_bytes(b"media" * 20000)
            cache_dir = os.path.join(temp, "cache")
            calls = []

            def decoder(_source, staging):
                calls.append(_source)
                for number, value in enumerate((20, 60, 100, 140), 1):
                    Path(staging, "frame_%06d.jpg" % number).write_bytes(
                        _jpeg_bytes(value))

            first = vfc.VideoFrameCache(source, cache_dir=cache_dir,
                                        decoder=decoder,
                                        ffmpeg="ffmpeg-test")
            first_path, first_meta = first.frame_path_at(0.5)
            self.assertTrue(os.path.isfile(first_path))
            self.assertFalse(first_meta["reused"])
            self.assertEqual(1, len(calls))

            def forbidden_decoder(_source, _staging):
                raise AssertionError("second role decoded source again")

            second = vfc.VideoFrameCache(source, cache_dir=cache_dir,
                                         decoder=forbidden_decoder,
                                         ffmpeg="ffmpeg-test")
            second_path, second_meta = second.frame_path_at(0.5)
            self.assertEqual(first_path, second_path)
            self.assertTrue(second_meta["reused"])
            self.assertEqual(first.cache_key, second_meta["cache_key"])
            self.assertEqual(first.audit["source_fingerprint"],
                             second_meta["source_fingerprint"])
            self.assertEqual(1, len(calls))

    def test_exact_source_frame_is_persistent_and_second_role_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as temp:
            source = os.path.join(temp, "source.bin")
            Path(source).write_bytes(b"video" * 20000)
            cache_dir = os.path.join(temp, "cache")
            calls = []

            def fake_run(command, **_kwargs):
                calls.append(command)
                Path(command[-1]).write_bytes(_jpeg_bytes(110))
                return types.SimpleNamespace(returncode=0, stderr=b"")

            with mock.patch.object(vfc.subprocess, "run", side_effect=fake_run):
                first = vfc.VideoFrameCache(source, cache_dir=cache_dir,
                                            ffmpeg="ffmpeg-test")
                path1, meta1 = first.exact_frame_path_at(1.234567, width=384)
                second = vfc.VideoFrameCache(source, cache_dir=cache_dir,
                                             ffmpeg="ffmpeg-test")
                path2, meta2 = second.exact_frame_path_at(1.234567, width=384)
            self.assertEqual(path1, path2)
            self.assertEqual(1, len(calls))
            self.assertFalse(meta1["reused"])
            self.assertTrue(meta2["reused"])
            self.assertEqual("source_seek_nearest_frame", meta2["precision"])
            self.assertIsNone(meta2["actual_time"])
            self.assertIsNone(meta2["time_error"])

    def test_cancelled_proxy_does_not_publish_partial_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            source = os.path.join(temp, "source.bin")
            Path(source).write_bytes(b"video" * 20000)
            cache = vfc.VideoFrameCache(
                source, cache_dir=os.path.join(temp, "cache"),
                should_cancel=lambda: True,
                decoder=lambda _source, _staging: None,
                ffmpeg="ffmpeg-test")
            with self.assertRaises(vfc.VideoFrameCacheCancelled):
                cache.prepare()
            self.assertFalse(os.path.isfile(cache.manifest_path))

    def test_prune_preserves_entry_with_exact_frame_in_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            key = "a" * 64
            entry = Path(temp, key)
            exact = entry / "exact"
            exact.mkdir(parents=True)
            (exact / "frame.build.lock").write_text("busy", encoding="ascii")
            (entry / "access.json").write_text(
                '{"last_access_ns": 1, "entry_bytes": 10}', encoding="utf-8")
            result = vfc.prune_cache(temp, max_bytes=0, max_entries=0,
                                     max_age_seconds=1)
            self.assertTrue(entry.is_dir())
            self.assertEqual(0, result["entries_removed"])

    def test_visual_nodes_use_exact_seams_and_proxy_context_with_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            image_path = os.path.join(temp, "frame.jpg")
            Path(image_path).write_bytes(_jpeg_bytes(90))

            class FakeCache:
                def exact_frame_path_at(self, seconds, width=384):
                    return image_path, {"cache_key": "exact", "reused": True,
                                        "source_fingerprint": "fp",
                                        "precision": "source_seek_nearest_frame"}

                def frame_path_at(self, seconds):
                    return image_path, {"cache_key": "proxy", "reused": True,
                                        "source_fingerprint": "fp",
                                        "precision": "review_proxy_nearest_0.5s"}

            base = {"id": "node", "time": 1.0, "priority": 1,
                    "reason": "test", "segment_index": None,
                    "sample_times": [0.0, 1.0, 2.0], "gap_seconds": None}
            seam = {**base, "kind": "edit_boundary", "_frame_cache": FakeCache()}
            context = {**base, "kind": "timeline_probe", "_frame_cache": FakeCache()}
            seam_paths = visual_critic.extract_node_frames(
                "unused", seam, os.path.join(temp, "fallback"))
            context_paths = visual_critic.extract_node_frames(
                "unused", context, os.path.join(temp, "fallback"))
            self.assertEqual([image_path] * 3, seam_paths)
            self.assertEqual([image_path] * 3, context_paths)
            self.assertEqual("source_seek_nearest_frame", seam["frame_precision"])
            self.assertEqual("review_proxy_nearest_0.5s",
                             context["frame_precision"])
            self.assertEqual("seam_evidence", seam["visual_authority"])
            self.assertEqual("context_only_no_veto",
                             context["visual_authority"])
            self.assertTrue(all(row["cache_key"] == "exact"
                                for row in seam["frame_cache_evidence"]))

    def test_neutral_motion_uses_supplied_proxy_not_legacy_decoder(self):
        class FakeCache:
            audit = {"cache_key": "shared", "source_fingerprint": "fp",
                     "reused": True}

            def analyze_motion(self, step, small_width):
                return [0.0, 1.0], [0.0, 0.25]

        with mock.patch.object(index, "_sample_motion_ffmpeg",
                               side_effect=AssertionError("legacy decode ran")):
            times, diffs, audit = index.sample_motion(
                "unused", 1.0, 96, frame_cache=FakeCache(),
                return_cache_audit=True)
        self.assertEqual([0.0, 1.0], times)
        self.assertEqual([0.0, 0.25], diffs)
        self.assertEqual("shared", audit["cache_key"])
        self.assertEqual("neutral_motion_ingest", audit["consumer"])

    def test_proxy_context_cannot_create_visual_veto(self):
        report = {"nodes": [{
            "id": "context", "time": 5.0,
            "sample_times": [4.5, 5.0, 5.5],
            "frame_precision": "review_proxy_nearest_0.5s",
            "issues": [{"type": "black_or_blank", "severity": "major",
                        "message": "proxy looks black"}],
        }]}
        findings = director_multimodal._visual_findings(report)
        self.assertEqual(1, len(findings))
        self.assertFalse(findings[0]["veto"])
        self.assertNotEqual("critical", findings[0]["severity"])

    def test_cancelled_ui_proxy_does_not_fallback_to_another_ffmpeg(self):
        class CancelledCache:
            def frame_path_at(self, _seconds):
                raise vfc.VideoFrameCacheCancelled("stop")

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(ui_thumbs.subprocess, "run") as run:
            with self.assertRaises(vfc.VideoFrameCacheCancelled):
                ui_thumbs.extract("unused", 1.0, os.path.join(temp, "x.jpg"),
                                  frame_cache=CancelledCache())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
