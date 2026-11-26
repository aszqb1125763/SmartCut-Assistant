# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

import autoedit


class AutoEditRoutingTests(unittest.TestCase):
    def test_default_cli_routes_to_formal_role_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            source = os.path.join(temp, "source.mp4")
            with open(source, "wb") as handle:
                handle.write(b"video")
            with mock.patch.object(autoedit, "run_role_workflow",
                                   return_value=7) as run:
                code = autoedit.main([source, "--track", "2"])
            self.assertEqual(7, code)
            self.assertEqual(source, run.call_args.args[0])
            self.assertEqual(2, run.call_args.kwargs["track"])

    def test_adapter_enables_model_editor_and_full_director_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            source = os.path.join(temp, "source.mp4")
            with open(source, "wb") as handle:
                handle.write(b"video")
            result = {
                "out": os.path.join(temp, "out.mp4"), "minutes": 1.0,
                "task_id": "TASK", "director_passed": True,
                "rendered_review_passed": True,
            }
            with mock.patch.object(autoedit, "BASE", temp), \
                    mock.patch.object(autoedit, "duration_of", return_value=100.0), \
                    mock.patch("autopilot.run_one", return_value=result) as run:
                code = autoedit.run_role_workflow(
                    source, track=2, target=30, burn_subs=True,
                    use_bgm=True, loud=True, out_dir=temp)
            self.assertEqual(0, code)
            self.assertTrue(run.call_args.kwargs["model_editor"])
            self.assertEqual("gameplay", run.call_args.kwargs["mode"])
            self.assertEqual([2], run.call_args.kwargs["tracks"])
            self.assertEqual("heavy", run.call_args.kwargs["level"])
            with open(os.path.join(temp, "data", "autoedit_last.json"),
                      encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual("full_auto_roles", saved["workflow"])
            self.assertTrue(saved["director_passed"])


if __name__ == "__main__":
    unittest.main()
