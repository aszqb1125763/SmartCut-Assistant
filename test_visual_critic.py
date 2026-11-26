# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

import visual_critic


class VisualCriticTests(unittest.TestCase):
    def test_selects_edit_boundary_and_timeline_fallback(self):
        data = {
            "duration": 30.0,
            "segments": [[1.0, 3.0, "普通过程"], [12.0, 14.0, "继续过程"]],
        }
        nodes = visual_critic.select_nodes(data, spans=[(1, 4), (12, 15)], max_nodes=6)
        self.assertTrue(any(row["kind"] == "edit_boundary" for row in nodes))
        self.assertGreaterEqual(len(nodes), 3)
        boundary = next(row for row in nodes if row["kind"] == "edit_boundary")
        self.assertAlmostEqual(boundary["gap_seconds"], 8.0)

    def test_long_timeline_reserves_nodes_beyond_edit_boundaries(self):
        segments = [[float(i * 2), float(i * 2 + 1), "句子%d" % i]
                    for i in range(40)]
        data = {"duration": 82.0, "segments": segments,
                "scores": [float(i) for i in range(40)],
                "script": {"sections": [{"start": 0.0, "title": "开头"},
                                          {"start": 70.0, "title": "后段"}]}}
        spans = [(float(i * 4), float(i * 4 + 2)) for i in range(20)]
        nodes = visual_critic.select_nodes(data, spans=spans, max_nodes=10)
        self.assertLessEqual(sum(row["kind"] == "edit_boundary" for row in nodes), 5)
        self.assertTrue(any(row["kind"] == "score_peak" for row in nodes))
        self.assertTrue(any(row["time"] >= 70.0 for row in nodes))

    def test_no_dialogue_video_still_gets_timeline_visual_nodes(self):
        data = {"duration": 30.0, "segments": [], "scores": []}
        nodes = visual_critic.select_nodes(data, spans=[(0.0, 30.0)], max_nodes=6)
        self.assertGreaterEqual(len(nodes), 3)
        self.assertTrue(all(row["kind"] == "timeline_probe" for row in nodes))

    def test_black_frame_is_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for index in range(3):
                path = Path(temp) / ("black%d.jpg" % index)
                cv2.imwrite(str(path), np.zeros((90, 160, 3), dtype=np.uint8))
                paths.append(str(path))
            node = {"id": "x", "kind": "timeline_probe", "time": 1.0,
                    "priority": 1, "reason": "test", "segment_index": None,
                    "sample_times": [0, 1, 2], "gap_seconds": None}
            row = visual_critic.analyze_node(node, paths)
            self.assertTrue(any(issue["type"] == "black_or_blank" for issue in row["issues"]))
            self.assertLess(row["score"], 80)

    def test_hard_edit_jump_is_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            images = [np.full((90, 160, 3), 30, np.uint8),
                      np.full((90, 160, 3), 30, np.uint8),
                      np.full((90, 160, 3), 240, np.uint8)]
            paths = []
            for index, image in enumerate(images):
                path = Path(temp) / ("frame%d.jpg" % index)
                cv2.imwrite(str(path), image)
                paths.append(str(path))
            node = {"id": "x", "kind": "edit_boundary", "time": 10.0,
                    "priority": 110, "reason": "test", "segment_index": None,
                    "sample_times": [3, 4, 10], "gap_seconds": 6.0}
            row = visual_critic.analyze_node(node, paths)
            self.assertTrue(any(issue["type"] == "hard_visual_jump" for issue in row["issues"]))

    def test_static_score_peak_is_hint_but_not_result_veto(self):
        with tempfile.TemporaryDirectory() as temp:
            image = np.zeros((90, 160, 3), dtype=np.uint8)
            image[:, ::8] = 255
            paths = []
            for index in range(3):
                path = Path(temp) / ("static%d.jpg" % index)
                cv2.imwrite(str(path), image)
                paths.append(str(path))
            base = {"id": "x", "time": 10.0, "priority": 80,
                    "reason": "test", "segment_index": 0,
                    "sample_times": [9, 10, 11], "gap_seconds": None}
            score_peak = visual_critic.analyze_node(
                {**base, "kind": "score_peak"}, paths)
            story_result = visual_critic.analyze_node(
                {**base, "kind": "story_result"}, paths)
            self.assertFalse(any(issue["type"] == "claimed_progress_without_visual_change"
                                 for issue in score_peak["issues"]))
            self.assertTrue(any(issue["type"] == "claimed_progress_without_visual_change"
                                for issue in story_result["issues"]))

    def test_dark_textured_gameplay_is_not_called_black_screen(self):
        with tempfile.TemporaryDirectory() as temp:
            image = np.zeros((90, 160, 3), dtype=np.uint8)
            image[:, ::20] = 255
            paths = []
            for index in range(3):
                path = Path(temp) / ("dark%d.jpg" % index)
                cv2.imwrite(str(path), image)
                paths.append(str(path))
            node = {"id": "dark", "kind": "timeline_probe", "time": 1.0,
                    "priority": 1, "reason": "test", "segment_index": None,
                    "sample_times": [0, 1, 2], "gap_seconds": None}
            row = visual_critic.analyze_node(node, paths)
            self.assertFalse(any(issue["type"] == "black_or_blank"
                                 for issue in row["issues"]))


if __name__ == "__main__":
    unittest.main()

