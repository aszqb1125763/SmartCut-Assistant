# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import termbase
import terms


class TermWriteSafetyTests(unittest.TestCase):
    def test_full_kb_never_rewrites_transcript_by_default(self):
        text = "硅岩锭已经做好，硅岩燃还在继续"
        with tempfile.TemporaryDirectory() as root:
            glossary = Path(root) / "terms.txt"
            glossary.write_text("子枫|子缝\n", encoding="utf-8")
            with mock.patch.object(termbase, "TERMS_FILE", str(glossary)), \
                    mock.patch.object(termbase, "load_all",
                                      side_effect=AssertionError("KB must stay read-only")), \
                    mock.patch.object(terms, "USER_TERMS_FILE", str(glossary)), \
                    mock.patch.object(terms, "load",
                                      side_effect=AssertionError("KB must stay read-only")):
                rows, changed = termbase.apply_to_segments([[0.0, 1.0, text]])
                fixed, replacements = terms.correct(text)
                segment_rows, segment_changed, segment_replacements = \
                    terms.correct_segments([(0.0, 1.0, text)])
        self.assertEqual(text, rows[0][2])
        self.assertEqual(0, changed)
        self.assertEqual(text, fixed)
        self.assertEqual([], replacements)
        self.assertEqual(text, segment_rows[0][2])
        self.assertEqual(0, segment_changed)
        self.assertEqual([], segment_replacements)

    def test_only_explicit_user_aliases_are_applied(self):
        with tempfile.TemporaryDirectory() as root:
            glossary = Path(root) / "terms.txt"
            glossary.write_text(
                "子枫|子缝\n六氟化铀|六氟化油\n富硅岩燃料|含富硅岩燃料\n",
                encoding="utf-8",
            )
            source = "子缝说六氟化油和含富硅岩燃料"
            expected = "子枫说六氟化铀和富硅岩燃料"
            with mock.patch.object(termbase, "TERMS_FILE", str(glossary)), \
                    mock.patch.object(terms, "USER_TERMS_FILE", str(glossary)):
                rows, changed = termbase.apply_to_segments([[0.0, 1.0, source]])
                fixed, replacements = terms.correct(source)
                segment_rows, segment_changed, _ = terms.correct_segments(
                    [(0.0, 1.0, source)])
        self.assertEqual(expected, rows[0][2])
        self.assertEqual(3, changed)
        self.assertEqual(expected, fixed)
        self.assertEqual(3, len(replacements))
        self.assertEqual(expected, segment_rows[0][2])
        self.assertEqual(3, segment_changed)


if __name__ == "__main__":
    unittest.main()
