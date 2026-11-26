# -*- coding: utf-8 -*-
"""A failed/cancelled PCM read cannot become a passing delivery receipt."""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

import coords_report
from audio_evidence_cache import AudioEvidenceCancelled


class _Calculator:
    def __init__(self, error=None):
        self.error = error
        self.closed = False
        self.audio = np.zeros(3 * 16000, dtype=np.float32)

    @property
    def duration(self):
        if self.error is not None:
            raise self.error
        return 3.0

    def close(self):
        self.closed = True


class ReceiptCacheLifecycleTests(unittest.TestCase):
    def test_audio_failure_is_propagated_and_resources_are_closed(self):
        calc = _Calculator(RuntimeError("PCM read failed"))
        with mock.patch.object(coords_report.brain, "AudioSource",
                               return_value=object()), \
                mock.patch.object(coords_report.brain, "Calculator",
                                  return_value=calc):
            with self.assertRaisesRegex(RuntimeError, "PCM read failed"):
                coords_report.build("fixture.mp4", [1.0])
        self.assertTrue(calc.closed)

    def test_probe_failure_also_closes_resources(self):
        calc = _Calculator()
        with mock.patch.object(coords_report.brain, "AudioSource",
                               return_value=object()), \
                mock.patch.object(coords_report.brain, "Calculator",
                                  return_value=calc), \
                mock.patch.object(coords_report, "seam_row",
                                  side_effect=RuntimeError("probe failed")):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                coords_report.build("fixture.mp4", [1.0])
        self.assertTrue(calc.closed)

    def test_cancelled_empty_receipt_is_not_reported_as_passed(self):
        calc = _Calculator()
        with mock.patch.object(coords_report.brain, "AudioSource",
                               return_value=object()), \
                mock.patch.object(coords_report.brain, "Calculator",
                                  return_value=calc):
            with self.assertRaises(AudioEvidenceCancelled):
                coords_report.build("fixture.mp4", [], should_cancel=lambda: True)
        self.assertTrue(calc.closed)

    def test_success_closes_cache_and_supplies_cancellation_to_reader(self):
        calc = _Calculator()
        cancelled = lambda: False
        with mock.patch.object(coords_report.brain, "Calculator", return_value=calc), \
                mock.patch.object(coords_report.brain, "AudioSource") as source:
            report = coords_report.build("fixture.mp4", [], should_cancel=cancelled)
        self.assertEqual(3.0, report["film_seconds"])
        self.assertEqual(0, report["summary"]["total"])
        self.assertIs(cancelled, source.call_args.kwargs["should_cancel"])
        self.assertTrue(calc.closed)


if __name__ == "__main__":
    unittest.main()
