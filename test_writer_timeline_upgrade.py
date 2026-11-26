# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import autopilot
import script_writer
import source_timeline


def _run():
    run = object.__new__(autopilot._WorkflowRun)
    run.idx = "index-dir"
    run.mode = "gameplay"
    run.level = "medium"
    run.should_cancel = lambda: False
    run.task_id = "TEST"
    run.stages = []
    run._sink = lambda _message: None
    return run


def _ctx():
    return SimpleNamespace(outputs={
        "analyze": SimpleNamespace(payload={"index_dir": "index-dir"})
    })


class WriterTimelineUpgradeTests(unittest.TestCase):
    def test_matching_old_prose_script_is_rebuilt(self):
        data = {
            "duration": 1.0,
            "analysis": {"ready": True},
            "segments": [[0.0, 1.0, "测试"]],
            "script": {
                "mode": "gameplay", "cut_level": "medium",
                "sections": [{"start": 0.0, "end": 1.0}],
            },
        }
        with mock.patch.object(autopilot.autocut, "load_index",
                               return_value=data), \
                mock.patch.object(autopilot.edit_modes, "profile",
                                  return_value={"story_structure": True}), \
                mock.patch("script_writer.build", return_value=1) as build:
            result = _run().writer(_ctx())
        build.assert_called_once()
        self.assertTrue(result.metadata["built"])

    def test_matching_event_timeline_script_is_reused(self):
        data = {
            "duration": 1.0,
            "analysis": {"ready": True},
            "segments": [[0.0, 1.0, "测试"]],
            "script": {
                "mode": "gameplay", "cut_level": "medium",
                "sections": [{"start": 0.0, "end": 1.0}],
                "timeline_schema": source_timeline.SCHEMA,
                "decision_space": "source_timeline_event_ids",
            },
        }
        data["script"]["timeline_revision"] = source_timeline.revision(
            script_writer._source_events(data))
        with mock.patch.object(autopilot.autocut, "load_index",
                               return_value=data), \
                mock.patch.object(autopilot.edit_modes, "profile",
                                  return_value={"story_structure": True}), \
                mock.patch("script_writer.build", return_value=1) as build:
            result = _run().writer(_ctx())
        build.assert_not_called()
        self.assertFalse(result.metadata["built"])

    def test_changed_text_invalidates_matching_schema_script(self):
        old_events = script_writer._source_events({
            "segments": [[0.0, 1.0, "旧字幕"]], "duration": 1.0})
        data = {
            "duration": 1.0,
            "analysis": {"ready": True},
            "segments": [[0.0, 1.0, "修正后的字幕"]],
            "script": {
                "mode": "gameplay", "cut_level": "medium",
                "sections": [{"start": 0.0, "end": 1.0}],
                "timeline_schema": source_timeline.SCHEMA,
                "decision_space": "source_timeline_event_ids",
                "timeline_revision": source_timeline.revision(old_events),
            },
        }
        with mock.patch.object(autopilot.autocut, "load_index",
                               return_value=data), \
                mock.patch.object(autopilot.edit_modes, "profile",
                                  return_value={"story_structure": True}), \
                mock.patch("script_writer.build", return_value=1) as build:
            result = _run().writer(_ctx())
        build.assert_called_once()
        self.assertTrue(result.metadata["built"])

    def test_changed_model_visible_audio_invalidates_script_before_editor(self):
        old_data = {
            "duration": 1.0,
            "segments": [[0.0, 1.0, "同一句"]],
            "seg_feat": [[1.0, -28.0]],
        }
        data = {
            "duration": 1.0,
            "analysis": {"ready": True},
            "segments": [[0.0, 1.0, "同一句"]],
            "seg_feat": [[8.0, -9.0]],
            "script": {
                "mode": "gameplay", "cut_level": "medium",
                "sections": [{"start": 0.0, "end": 1.0}],
                "timeline_schema": source_timeline.SCHEMA,
                "decision_space": "source_timeline_event_ids",
                "timeline_revision": source_timeline.revision(
                    script_writer._source_events(old_data)),
            },
        }
        with mock.patch.object(autopilot.autocut, "load_index",
                               return_value=data), \
                mock.patch.object(autopilot.edit_modes, "profile",
                                  return_value={"story_structure": True}), \
                mock.patch("script_writer.build", return_value=1) as build:
            result = _run().writer(_ctx())
        build.assert_called_once()
        self.assertTrue(result.metadata["built"])


if __name__ == "__main__":
    unittest.main()

