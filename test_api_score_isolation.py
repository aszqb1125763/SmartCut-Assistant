# -*- coding: utf-8 -*-
"""API content decisions must not inherit local training or cached preferences."""
import contextlib
import io
import unittest
from unittest import mock

import autocut
import autopilot
import cutvalue
import director_critic
import director_multimodal
import index as indexer
import llm_gateway
import model_editor
import visual_critic


def sample_data(scores=None):
    return {
        "duration": 30.0,
        "segments": [
            [1.0, 4.0, "首先把材料放进工作台"],
            [10.0, 13.0, "然后接上电源开始测试"],
            [21.0, 24.0, "机器已经完成可以使用"],
        ],
        "cards": [
            {"start": 1.0, "end": 4.0, "text": "短句"},
            {"start": 10.0, "end": 13.0, "text": "然后接上电源开始测试"},
            {"start": 21.0, "end": 24.0, "text": "机器已经完成可以使用"},
        ],
        "scores": list(scores if scores is not None else (0.0, 1.0, 0.01)),
        "script": {"sections": [
            {"start": 0.0, "end": 30.0, "advice": "只留关键"},
        ]},
    }


class APIScoreIsolationTests(unittest.TestCase):
    def api_mode(self, enabled=True):
        return mock.patch("llm_config.api_settings", return_value={"enabled": enabled})

    def test_api_enabled_tracks_current_backend(self):
        with self.api_mode(True):
            self.assertTrue(llm_gateway.api_enabled())
        with self.api_mode(False):
            self.assertFalse(llm_gateway.api_enabled())

    def test_api_legacy_cli_stops_before_loading_media(self):
        stderr = io.StringIO()
        with self.api_mode(), contextlib.redirect_stderr(stderr), \
                mock.patch.object(autocut, "load_index") as load:
            with self.assertRaises(SystemExit) as stopped:
                autocut.main(["unused-index.json", "--dry-run"])
        self.assertEqual(2, stopped.exception.code)
        self.assertIn("智能剪辑", stderr.getvalue())
        load.assert_not_called()

    def test_analysis_api_scores_are_neutral_without_local_inference(self):
        data = sample_data()
        with self.api_mode(), \
                mock.patch.object(autocut, "load_model") as trained, \
                mock.patch.object(autocut, "fallback_scores") as fallback:
            result = indexer.score_sentences(data["segments"])
        self.assertEqual([0.5, 0.5, 0.5], result)
        trained.assert_not_called()
        fallback.assert_not_called()

    def test_api_skips_cached_model_without_erasing_local_cache(self):
        model = {"mean": [0.0], "std": [1.0], "weights": [99.0], "bias": 0.0}
        with mock.patch.dict(autocut._MODEL_CACHE, {"m": model}, clear=True):
            with self.api_mode():
                self.assertIsNone(autocut.load_model())
                self.assertIs(model, autocut._MODEL_CACHE["m"])
            with self.api_mode(False):
                self.assertIs(model, autocut.load_model())

    def test_api_units_ignore_extreme_cached_scores(self):
        for scores in ((0.0, 1.0, 0.01), (1.0, 0.0, 0.99)):
            with self.subTest(scores=scores), self.api_mode():
                units = autocut.load_units(sample_data(scores))
                self.assertEqual([0.5, 0.5, 0.5], [unit["score"] for unit in units])

    def test_api_model_card_scores_ignore_cached_and_supplied_models(self):
        for scores in ((0.0, 1.0, 0.01), (1.0, 0.0, 0.99)):
            with self.subTest(scores=scores), self.api_mode(), \
                    mock.patch.object(autocut, "_seg_feature_row") as features:
                result = autocut.model_card_scores(
                    sample_data(scores), {"invalid_local_model": True})
                self.assertEqual([0.5, 0.5, 0.5], result)
                features.assert_not_called()

    def test_api_rule_card_scores_do_not_use_local_preferences(self):
        conf = {"select": {"weight_cps": 9999.0, "weight_prog": -9999.0}}
        with self.api_mode():
            result = autocut.score_cards(sample_data()["cards"], conf)
        self.assertEqual([0.5, 0.5, 0.5], result)

    def test_api_scores_of_is_neutral_even_when_cached_scores_are_missing(self):
        for scores in ((0.0, 1.0, 0.01), (1.0, 0.0, 0.99), ()):
            with self.subTest(scores=scores), self.api_mode(), \
                    mock.patch.object(autocut, "fallback_scores") as fallback:
                result, source = autocut.scores_of(sample_data(scores))
                self.assertEqual([0.5, 0.5, 0.5], result)
                self.assertEqual("api_neutral", source)
                fallback.assert_not_called()

    def test_api_span_value_never_calls_cutvalue_training(self):
        for scores in ((0.0, 1.0, 0.01), (1.0, 0.0, 0.99)):
            with self.subTest(scores=scores), self.api_mode(), \
                    mock.patch.object(cutvalue, "features") as features, \
                    mock.patch.object(cutvalue, "predict") as predict, \
                    mock.patch.object(cutvalue, "load_weights") as weights:
                result = autocut.span_value(sample_data(scores), 0.0, 30.0)
                self.assertEqual(0.5, result)
                features.assert_not_called()
                predict.assert_not_called()
                weights.assert_not_called()

    def test_api_director_ignores_explicit_and_cached_sentence_scores(self):
        for cached, explicit in (
                ((0.0, 1.0, 0.01), (1.0, 0.0, 0.99)),
                ((1.0, 0.0, 0.99), (0.0, 1.0, 0.01))):
            with self.subTest(cached=cached, explicit=explicit), self.api_mode():
                data = sample_data(cached)
                self.assertEqual([0.5, 0.5, 0.5], director_critic._scores_of(data))
                self.assertEqual(
                    [0.5, 0.5, 0.5], director_critic._scores_of(data, explicit))

    def test_api_script_selection_does_not_apply_local_thresholds(self):
        with self.api_mode():
            self.assertIsNone(autocut.select_by_script(sample_data(), {}, level="heavy"))

    def test_api_budget_does_not_override_api_content_decisions(self):
        spans = [(1.0, 4.0), (10.0, 13.0), (21.0, 24.0)]
        with self.api_mode(), \
                mock.patch.object(autocut, "span_value", return_value=0.5) as value:
            result = autocut.enforce_budget(sample_data(), spans, target_sec=1.0)
        self.assertEqual(spans, result)
        value.assert_not_called()

    def test_api_legacy_editor_entry_routes_to_model_before_loading_payload(self):
        runtime = autopilot._WorkflowRun.__new__(autopilot._WorkflowRun)
        runtime.model_editor = False
        runtime.log = mock.Mock()
        runtime.payload = mock.Mock(side_effect=AssertionError("legacy payload was read"))
        context = object()
        expected = object()
        with self.api_mode(), \
                mock.patch.object(model_editor, "editor_by_model",
                                  return_value=expected) as editor:
            result = runtime.editor(context)
        self.assertIs(expected, result)
        self.assertTrue(runtime.model_editor)
        editor.assert_called_once_with(runtime, context)
        runtime.payload.assert_not_called()

    def test_api_refuses_all_legacy_local_selection_entries(self):
        data = sample_data()
        entries = (
            (autocut.apply_cut_level, (data, [(0.0, 30.0)], {}), {"level": "heavy"}),
            (autocut.select_by_sentence, (data, {}), {"target_seconds": 1.0}),
            (autocut.select_cards, (data, {}), {"target_seconds": 1.0}),
            (autocut.select_whole_chapters, (data, {}), {"level": "heavy"}),
        )
        for select, args, kwargs in entries:
            with self.subTest(entry=select.__name__), self.api_mode():
                with self.assertRaisesRegex(llm_gateway.APIRequestError, "智能剪辑"):
                    select(*args, **kwargs)

    def test_api_fallback_scores_do_not_reintroduce_rule_preferences(self):
        with self.api_mode():
            self.assertEqual([0.5, 0.5, 0.5], autocut.fallback_scores(sample_data()))

    def test_api_neutral_scores_do_not_create_local_offtopic_deletions(self):
        data = {
            "duration": 600.0,
            "segments": [
                [10.0, 13.0, "这期欢迎大家"],
                [301.0, 304.0, "找一下东西我不知道放哪"],
                [350.0, 353.0, "嗯啊呃额那个这个就是然后"],
                [590.0, 593.0, "感谢观看到这里"],
            ],
            "scores": [0.5] * 4,
        }
        labels = ["hook", "progress", "progress", "closing"]
        spans = [(0.0, 600.0)]
        with mock.patch.object(director_critic, "classify_stages", return_value=labels):
            with self.api_mode():
                remote = director_critic.review(data, spans)
            with self.api_mode(False):
                local = director_critic.review(data, spans)
        self.assertNotIn("offtopic", {issue["type"] for issue in remote["issues"]})
        self.assertIn("offtopic", {issue["type"] for issue in local["issues"]})

    def test_api_audio_review_windows_ignore_opposite_cached_scores(self):
        spans = [(1.0, 4.0), (10.0, 13.0), (21.0, 24.0)]
        with self.api_mode():
            first = director_multimodal.select_review_windows(
                sample_data((0.0, 1.0, 0.01)), spans, max_windows=20)
            second = director_multimodal.select_review_windows(
                sample_data((1.0, 0.0, 0.99)), spans, max_windows=20)
        self.assertEqual(first, second)
        self.assertNotIn("score_peak", {window["kind"] for window in first})
        self.assertEqual(3, sum(window["kind"] == "cut_start" for window in first))
        self.assertEqual(3, sum(window["kind"] == "cut_end" for window in first))

    def test_api_visual_review_nodes_ignore_cached_and_explicit_score_peaks(self):
        spans = [(1.0, 4.0), (10.0, 13.0), (21.0, 24.0)]
        with self.api_mode():
            first = visual_critic.select_nodes(
                sample_data((0.0, 1.0, 0.01)), spans,
                sentence_scores=[1.0, 0.0, 0.99], max_nodes=30)
            second = visual_critic.select_nodes(
                sample_data((1.0, 0.0, 0.99)), spans,
                sentence_scores=[0.0, 1.0, 0.01], max_nodes=30)
        self.assertEqual(first, second)
        self.assertNotIn("score_peak", {node["kind"] for node in first})
        self.assertEqual(2, sum(node["kind"] == "edit_boundary" for node in first))

    def test_local_cached_scores_keep_their_original_behavior(self):
        data = sample_data((0.1, 0.8, 0.3))
        with self.api_mode(False):
            scores, source = autocut.scores_of(data)
            units = autocut.load_units(data)
            cards = autocut.model_card_scores(data, None)
            explicit = director_critic._scores_of(data, (0.9, 0.2, 0.4))
        self.assertEqual([0.1, 0.8, 0.3], scores)
        self.assertEqual("model", source)
        self.assertEqual([0.1, 0.8, 0.3], [unit["score"] for unit in units])
        self.assertEqual([0.1, 0.8, 0.3], cards)
        self.assertEqual([0.9, 0.2, 0.4], explicit)

    def test_local_analysis_still_uses_original_fallback(self):
        data = sample_data()
        with self.api_mode(False), \
                mock.patch.object(autocut, "load_model", return_value=None) as trained, \
                mock.patch.object(autocut, "fallback_scores",
                                  return_value=[0.1, 0.8, 0.3]) as fallback:
            result = indexer.score_sentences(data["segments"])
        self.assertEqual([0.1, 0.8, 0.3], result)
        trained.assert_called_once_with()
        fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
