# -*- coding: utf-8 -*-
import unittest

import autocut
import rendered_output_qc as qc
import subtitles


class SequenceTimelineV2Tests(unittest.TestCase):
    def setUp(self):
        self.pieces = [
            ("trim", 0.0, 4.0),
            ("bridge", (4.0, 4.5), (10.0, 10.5), 0.5),
            ("audio_bridge", (10.5, 10.7), (20.0, 20.2), 0.2),
            ("chapter", 1.0),
            ("trim", 20.2, 23.2),
        ]
        self.timeline = autocut.build_sequence_timeline(
            self.pieces, tracks=[0, 1], fixed_tracks=[0, 1])

    def test_lane_map_preserves_both_sources_of_transitions(self):
        rows = self.timeline["pieces"]
        self.assertEqual("smartcut.sequence-timeline", self.timeline["schema"])
        self.assertEqual(2, self.timeline["schema_version"])
        self.assertEqual(8.7, self.timeline["duration"])
        self.assertEqual(64, len(self.timeline["sequence_revision"]))

        crossfade = rows[1]
        self.assertEqual(2, len(crossfade["video"]))
        self.assertEqual({"outgoing", "incoming"},
                         {row["role"] for row in crossfade["video"]})
        self.assertEqual(4, len(crossfade["audio"]))

        audio_bridge = rows[2]
        self.assertEqual(1, len(audio_bridge["video"]))
        self.assertEqual(20.0, audio_bridge["video"][0]["source_start"])
        self.assertEqual(4, len(audio_bridge["audio"]))
        self.assertEqual("hard_cut", audio_bridge["transition"]["video"])
        self.assertEqual("crossfade", audio_bridge["transition"]["audio"])

        chapter = rows[3]
        self.assertEqual([], chapter["video"])
        self.assertTrue(chapter["transition"]["synthetic"])
        self.assertEqual(rows[3]["output_end"], rows[4]["output_start"])

    def test_subtitles_follow_audible_sources_in_audio_bridge(self):
        mapped = subtitles.remap([
            (10.52, 10.68, "左侧尾音"),
            (20.02, 20.18, "右侧开头"),
        ], self.timeline)
        texts = {row[2] for row in mapped}
        self.assertEqual({"左侧尾音", "右侧开头"}, texts)

    def test_qc_coverage_reads_all_sequence_audio_mappings(self):
        words = [
            {"id": "left", "word": "尾", "start": 10.52, "end": 10.68,
             "probability": 0.95, "required": True},
            {"id": "right", "word": "开", "start": 20.02, "end": 20.18,
             "probability": 0.95, "required": True},
        ]
        self.assertEqual([], qc.find_word_boundary_cuts(self.timeline, words))

    def test_revision_changes_when_piece_mapping_changes(self):
        changed = autocut.build_sequence_timeline(
            self.pieces[:-1] + [("trim", 20.2, 23.3)], tracks=[0, 1])
        self.assertNotEqual(self.timeline["sequence_revision"],
                            changed["sequence_revision"])

    def test_burn_subtitle_rows_keep_piece_indices_after_chapter_card(self):
        # Legacy timeline deliberately omits synthetic chapter cards.  The
        # second source mapping must still point at pieces[2], not pieces[1].
        pieces = [
            ("trim", 10.0, 12.0),
            ("chapter", 1.5),
            ("trim", 20.0, 23.0),
        ]
        timeline = [(0.0, 10.0, 2.0), (3.5, 20.0, 3.0)]
        rows = autocut._subtitle_piece_rows(pieces, timeline)
        self.assertEqual([0, 2], [row[0] for row in rows])
        self.assertEqual((2, 3.5, 20.0, 3.0), rows[1])

    def test_burn_subtitle_mapping_fails_closed_on_cardinality_mismatch(self):
        with self.assertRaisesRegex(ValueError, "timeline/piece mismatch"):
            autocut._subtitle_piece_rows(
                [("trim", 0.0, 1.0), ("trim", 2.0, 3.0)],
                [(0.0, 0.0, 1.0)])


if __name__ == "__main__":
    unittest.main()
