# -*- coding: utf-8 -*-
"""Regression tests for the final editable cut timeline.

These tests pin the contracts that keep the model edit, boundary closure and
director review on one timeline revision.  They intentionally mock expensive
media/LLM work so the suite stays fast.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import autopilot
import edit_plan
import workflow_engine


class _FakePcmCache:
    sample_rate = 16000

    def entry(self, _track):
        return {"duration": 20.0}

    def review_samples(self, _track, start, end):
        size = max(1, int((float(end) - float(start)) * self.sample_rate))
        return np.zeros(size, dtype=np.float32)


class TimelineClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source.mp4")
        with open(self.source, "wb") as handle:
            handle.write(b"source")
        self.idx = os.path.join(self.tmp.name, "index")
        os.makedirs(self.idx)
        self.data = {
            "source": self.source,
            "duration": 20.0,
            "duration_exact": 20.0,
            "fps": 30.0,
            "audio_streams": [{"ordinal": 0}],
            "audio_stream_used": 0,
            "speech_tracks": [0],
            "segments": [[0.0, 3.0, "第一句"], [7.0, 10.0, "第二句"],
                         [12.0, 14.0, "第三句"]],
            "segment_evidence": [],
            "analysis": {"times": [0.0, 3.0, 7.0, 10.0],
                         "diffs": [0.1, 0.4, 0.3, 0.2], "runs": []},
            "script": {"sections": [{"start": 0.0, "end": 14.0,
                                       "title": "测试"}]},
            "scores": [0.8, 0.8, 0.8],
            "semantic": [{}, {}, {}],
        }
        self._write_data()
        self.runtime = autopilot._WorkflowRun(
            self.source, "medium", False, self.idx, lambda _message: None,
            False, [0], self.tmp.name, False, None, None,
            "gameplay", "TASK-CLOSURE", model_editor=True)
        self.runtime.director_conf.update({
            "auto_learn": False,
            "rendered_review_enabled": False,
            "visual_review_enabled": True,
        })

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def _write_data(self):
        with open(os.path.join(self.idx, "index.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False)

    def _context(self, stage, outputs):
        task = workflow_engine.TaskSpec("TASK-CLOSURE", self.source)
        return workflow_engine.StageContext(
            task, workflow_engine.STAGES[stage], (), outputs, lambda: False)

    def _plan(self, spans):
        return edit_plan.build_plan(
            source={"path": self.source, "size": 6, "mtime_ns": 1,
                    "sha256": "a" * 64, "algorithm": "sha256"},
            mode="gameplay", level="medium", config={},
            semantic_spans=spans, versions=self.runtime.ARTIFACTS)

    def test_timeline_revision_is_stable_and_boundary_sensitive(self):
        first = self.runtime.timeline_revision([(1, 2), (3.0, 4.0)])
        same = self.runtime.timeline_revision([[1.0, 2.0], [3, 4]])
        changed = self.runtime.timeline_revision([(1.0, 2.01), (3.0, 4.0)])
        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)
        self.assertEqual(64, len(first))

    def test_boundary_closure_reports_the_exact_delivered_revision(self):
        words = [{"text": "句", "start": 1.0, "end": 1.8}]
        final_spans = [(0.9, 2.1), (4.0, 5.0)]
        self.runtime.word_safe_spans = mock.Mock(
            side_effect=[([(1.0, 2.0), (4.0, 5.0)], []),
                         (final_spans, [(1.0, 2.0, 0.9, 2.1)])])
        self.runtime.sentence_safe_spans = mock.Mock(return_value=(
            [(1.0, 2.0), (4.0, 5.0)],
            {"expanded": 0, "dropped": 0, "before": 2.0, "after": 2.0}))
        self.runtime.waveform_edge_spans = mock.Mock(return_value=(
            final_spans, [(1.0, 2.0, 0.9, 2.1)], [0]))
        self.runtime.frame_safe_spans = mock.Mock(return_value=final_spans)

        closed, info = self.runtime.close_timeline_boundaries(
            self.data, [(1.0, 2.0), (4.0, 5.0)], words, [], [])

        self.assertEqual(final_spans, closed)
        self.assertEqual(self.runtime.timeline_revision(closed),
                         info["timeline_revision"])
        self.assertTrue(info["changed"])

    def test_silence_created_seams_are_closed_even_when_picker_moves_nothing(self):
        initial = [(0.0, 10.0), (12.0, 14.0)]
        trimmed = [(0.0, 3.0), (7.0, 10.0), (12.0, 14.0)]
        closed = [(0.0, 3.1), (6.9, 10.0), (12.0, 14.0)]
        revision = "b" * 64
        self.runtime.director_conf.update({
            "seam_pick_enabled": True, "silence_trim_enabled": True})
        self.runtime.ensure_audio_cache = mock.Mock(return_value=_FakePcmCache())
        self.runtime.close_timeline_boundaries = mock.Mock(return_value=(
            closed, {"timeline_revision": revision, "changed": True}))

        with mock.patch("seam_picker.load_audio",
                        return_value=np.zeros(320000, dtype=np.float32)), \
                mock.patch("seam_picker.pick_cuts", return_value=[{}]), \
                mock.patch("llm_config.model_path", return_value="model.gguf"), \
                mock.patch("proofread.Server",
                           return_value=contextlib.nullcontext(object())), \
                mock.patch("silence_trim.trim", return_value=(trimmed, [(3.0, 7.0)])), \
                mock.patch("silence_trim.find_silence",
                           return_value=[(3.0, 7.0)]), \
                mock.patch("silence_trim.apply", return_value=trimmed):
            result, info = self.runtime.finalize_cut_points(
                self.data, list(initial), [], self.source)

        self.runtime.close_timeline_boundaries.assert_called_once()
        self.assertEqual(closed, result)
        self.assertEqual(revision, info["timeline_revision"])
        self.assertGreater(info["trimmed_seconds"], 0.0)

    def test_axis_overage_never_reverts_semantic_boundary_closure(self):
        initial = [(0.0, 2.0), (4.0, 6.0)]
        semantic_safe = [(0.0, 2.1), (3.9, 6.0)]
        self.runtime.director_conf.update({
            "seam_pick_enabled": True, "silence_trim_enabled": False})
        self.runtime.ensure_audio_cache = mock.Mock(return_value=_FakePcmCache())
        self.runtime.close_timeline_boundaries = mock.Mock(return_value=(
            semantic_safe,
            {"timeline_revision": "c" * 64, "changed": True}))
        best = {"left_t": 1.8, "right_t": 4.2, "energy_gap": 1.0,
                "tone_gap_hz": 10.0, "shape_rms": 1.0}

        with mock.patch("seam_picker.load_audio",
                        return_value=np.zeros(320000, dtype=np.float32)), \
                mock.patch("seam_picker.pick_cuts",
                           return_value=[{"best": best}]), \
                mock.patch("seam_picker.seam_axes",
                           return_value={"energy_db": 99.0, "tone_hz": 9999.0,
                                         "shape_db": 99.0}), \
                mock.patch("tolerance.combine", return_value="over"), \
                mock.patch("llm_config.model_path", return_value="model.gguf"), \
                mock.patch("proofread.Server",
                           return_value=contextlib.nullcontext(object())):
            result, info = self.runtime.finalize_cut_points(
                self.data, list(initial), [], self.source)

        self.assertEqual(semantic_safe, result)
        self.assertEqual(0, info.get("reverts", 0))
        self.assertGreaterEqual(info.get("protected_over", 0), 0)

    def test_model_editor_director_does_not_rebuild_the_model_timeline(self):
        spans = [(0.0, 3.0), (7.0, 10.0)]
        plan = self._plan(spans)
        previous = {
            "index_dir": self.idx, "spans": [list(row) for row in spans],
            "conf": {}, "logs": [], "plan": plan,
            "task_seed": plan["task_seed"],
        }
        text_report = {"score": 96.0, "passed": True, "issues": []}
        audio = [{"actual_audio_inspected": True, "score": 98.0,
                  "findings": []}]
        visual = {"nodes": [{"id": "visual_001", "issues": [],
                              "sample_times": [1.0, 8.0]}],
                  "node_count": 1, "score": 100.0, "passed": True}
        revision = self.runtime.timeline_revision(spans)
        self.runtime.refine_boundaries = mock.Mock(
            side_effect=lambda _data, rows, _seed: (list(rows), [], {"words": 0}))
        self.runtime.audio_review = mock.Mock(return_value=([], audio))
        self.runtime.finalize_cut_points = mock.Mock(return_value=(
            spans, {"finalized": True, "moved": 0, "trimmed_seconds": 0.0,
                    "timeline_revision": revision}))
        self.runtime.close_timeline_boundaries = mock.Mock(return_value=(
            spans, {"timeline_revision": revision, "changed": False}))

        with mock.patch("director_critic.review_and_rebuild") as rebuild, \
                mock.patch("director_critic.review", return_value=text_report) as review, \
                mock.patch("visual_critic.review_video", return_value=visual):
            result = self.runtime.director(self._context(
                "director", {"boundary": workflow_engine.StageResult(
                    "TASK-CLOSURE.BOUNDARY-V1", previous)}))

        rebuild.assert_not_called()
        self.assertTrue(review.called)
        self.assertEqual([list(row) for row in spans], result.payload["spans"])
        self.assertEqual(revision,
                         result.payload["cut_final"]["timeline_revision"])


if __name__ == "__main__":
    unittest.main()
