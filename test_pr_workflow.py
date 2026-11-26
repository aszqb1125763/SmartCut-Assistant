# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest

import pr_workflow


class PremiereWorkflowMappingTests(unittest.TestCase):
    def test_canonical_keep_spans_drops_one_frame_residual(self):
        self.assertEqual(
            [[0.0, 4.0], [6.0, 10.0]],
            pr_workflow.canonical_keep_spans(
                [[0.0, 4.0], [5.0, 5.033], [6.0, 10.0]]))

    def test_canonical_keep_spans_merges_neighbouring_normal_ranges(self):
        self.assertEqual(
            [[0.0, 8.0]],
            pr_workflow.canonical_keep_spans(
                [[0.0, 4.0], [4.035, 8.0]]))

    def test_delete_projection_uses_same_canonical_keep_spans(self):
        clips = [{"start": 0.0, "end": 10.0, "in": 0.0, "out": 10.0}]
        rows = pr_workflow.delete_ranges_from_keep(
            clips, [[0.0, 4.0], [5.0, 5.033], [6.0, 10.0]])
        self.assertEqual(
            [(4.0, 6.0)],
            [(row["start"], row["end"]) for row in rows])

    def test_delete_ranges_are_per_clip_and_back_to_front(self):
        clips = [
            {"start": 0.0, "end": 10.0, "in": 0.0, "out": 10.0},
            {"start": 10.0, "end": 20.0, "in": 15.0, "out": 25.0},
        ]
        rows = pr_workflow.delete_ranges_from_keep(
            clips, [[0.0, 4.0], [6.0, 18.0], [20.0, 25.0]])
        self.assertEqual(
            [(13.0, 15.0, 1), (4.0, 6.0, 0)],
            [(row["start"], row["end"], row["clip"]) for row in rows])
        self.assertAlmostEqual(4.0, sum(row["expected"] for row in rows))

    def test_operation_does_not_recut_an_existing_clip_boundary(self):
        clips = [{"start": 10.0, "end": 20.0, "in": 15.0, "out": 25.0}]
        rows = [{"start": 10.0, "end": 12.0, "expected": 2.0, "clip": 0}]
        self.assertEqual(
            [{"op": "seek", "t": 12.0},
             {"op": "key", "k": "Q", "expected": 2.0}],
            pr_workflow.operations_for_ranges(rows, clips))

    def test_fingerprint_changes_when_timeline_shortens(self):
        base = {
            "name": "seq", "tracks": [],
            "clips": [{"kind": "V", "track": 1, "start": 0, "end": 10,
                       "in": 0, "out": 10, "path": "C:/source.mp4"}],
        }
        changed = {
            "name": "seq", "tracks": [],
            "clips": [{"kind": "V", "track": 1, "start": 0, "end": 8,
                       "in": 0, "out": 8, "path": "C:/source.mp4"}],
        }
        self.assertNotEqual(pr_workflow.sequence_fingerprint(base),
                            pr_workflow.sequence_fingerprint(changed))

    def test_saved_source_audio_mapping_wins_over_pr_destination_number(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            with open(os.path.join(root, "conf", "settings.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"autocut_tracks": "2,3"}, handle)
            source = os.path.normcase(os.path.abspath("C:/source.mp4"))
            sequence = {
                "tracks": [{"kind": "A", "track": 1, "muted": False},
                           {"kind": "A", "track": 2, "muted": False}],
                "audio": [{"track": 1, "path": source},
                          {"track": 2, "path": source}],
            }
            self.assertEqual(
                [1, 2], pr_workflow.selected_audio_tracks(
                    sequence, source, base_dir=root))

    def test_explicit_audio_mapping_is_not_truncated_by_muted_tracks(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            with open(os.path.join(root, "conf", "settings.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"autocut_tracks": "2,3"}, handle)
            source = os.path.normcase(os.path.abspath("C:/source.mp4"))
            sequence = {
                "tracks": [{"kind": "A", "track": 1, "muted": False},
                           {"kind": "A", "track": 2, "muted": True},
                           {"kind": "A", "track": 3, "muted": True}],
                "audio": [{"track": 1, "path": source},
                          {"track": 2, "path": source},
                          {"track": 3, "path": source}],
            }
            self.assertEqual(
                [1, 2], pr_workflow.selected_audio_tracks(
                    sequence, source, base_dir=root))

    def test_selected_output_rejects_missing_one_to_one_destination(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            with open(os.path.join(root, "conf", "settings.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"autocut_tracks": "2,3",
                           "pr_output_audio_mode": "selected"}, handle)
            source = os.path.normcase(os.path.abspath("C:/source.mp4"))
            sequence = {
                "tracks": [{"kind": "A", "track": 1, "muted": False},
                           {"kind": "A", "track": 2, "muted": False}],
                "audio": [{"track": 1, "path": source},
                          {"track": 2, "path": source}],
            }
            with self.assertRaisesRegex(ValueError, "缺少 A3"):
                pr_workflow.selected_audio_tracks(
                    sequence, source, base_dir=root)

    def test_pr_audio_output_mode_is_read_from_shared_settings(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf"))
            with open(os.path.join(root, "conf", "settings.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"pr_output_audio_mode": "selected"}, handle)
            self.assertEqual("selected", pr_workflow._audio_output_mode(root))


if __name__ == "__main__":
    unittest.main()
