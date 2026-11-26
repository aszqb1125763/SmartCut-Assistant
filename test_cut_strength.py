# -*- coding: utf-8 -*-
import unittest

import numpy as np

import autopilot
import cut_strength
import silence_trim
import workflow_engine


class CutStrengthTests(unittest.TestCase):
    def test_percentage_is_the_authority_and_level_is_derived(self):
        self.assertEqual("light", cut_strength.level_for_ratio(0.90))
        self.assertEqual("medium", cut_strength.level_for_ratio(0.62))
        self.assertEqual("heavy", cut_strength.level_for_ratio(45))
        self.assertAlmostEqual(0.68, cut_strength.normalize_ratio(68))

    def test_run_signature_changes_for_same_strategy_different_percentage(self):
        args = ("missing.mp4", "index", "gameplay", "medium", None,
                False, False, None, workflow_engine.PREMIERE_EDIT)
        first = autopilot._run_signature(*args, keep_ratio=0.62)
        second = autopilot._run_signature(*args, keep_ratio=0.74)
        self.assertNotEqual(first, second)

    def test_no_dialogue_trim_ignores_continuous_background_audio(self):
        spans = [(0.0, 20.0)]
        speech = [(0.5, 2.0, "开场"), (15.0, 17.0, "继续")]
        result, gaps = silence_trim.trim_no_dialogue(
            spans, speech, min_gap=2.5)
        self.assertTrue(any(start <= 2.0 and end >= 15.0
                            for start, end in gaps))
        self.assertLess(sum(end - start for start, end in result), 8.0)

    def test_boundary_word_does_not_exempt_entire_energy_silence(self):
        audio = np.zeros(16000 * 10, dtype=np.float32)
        silences = silence_trim.find_silence(
            audio,
            words=[{"start": 0.8, "end": 1.2}],
            min_silence=1.2)
        self.assertTrue(any(start >= 1.3 and end >= 9.9
                            for start, end in silences))


if __name__ == "__main__":
    unittest.main()
