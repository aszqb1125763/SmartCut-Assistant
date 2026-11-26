# -*- coding: utf-8 -*-
"""ASR preferences, cache provenance and text preservation; no model download."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

import asr
import asr_hints
import autopilot
import autocut
import fetch_model
import index
import media_identity


def write_settings(home, **settings):
    conf = Path(home, "conf")
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


class AsrPreferenceTests(unittest.TestCase):
    def test_model_integrity_check_defaults_to_application_directory(self):
        payload = b"fixed-asr-model"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as home:
            model_dir = Path(home, "models", "test-full")
            model_dir.mkdir(parents=True)
            (model_dir / "model.bin").write_bytes(payload)
            manifest = {"test": {"model.bin": (len(payload), digest)}}
            with mock.patch.object(fetch_model, "BASE", home), \
                    mock.patch.object(fetch_model, "MODEL_MANIFEST", manifest):
                self.assertTrue(fetch_model.verify_model_dir("test"))

    def test_saved_model_environment_and_explicit_cli_choice_reach_actual_index_call(self):
        with tempfile.TemporaryDirectory() as home:
            source = str(Path(home, "source.mp4"))
            Path(source).write_bytes(b"media")
            write_settings(home, model="large-v3")
            result = {"cards": [], "duration": 1.0, "audio_stream_used": 0}
            with mock.patch.object(index, "BASE_DIR", home), \
                    mock.patch.dict(os.environ, {"ASR_MODEL": ""}), \
                    mock.patch.object(index, "index_video", return_value=result) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, index.main([source]))
                self.assertEqual("large-v3", run.call_args.kwargs["model_size"])
                write_settings(home, model="large-v3-turbo")
                self.assertEqual(0, index.main([source]))
                self.assertEqual("large-v3-turbo", run.call_args.kwargs["model_size"])
                self.assertEqual(0, index.main([source, "--model", "small"]))
                self.assertEqual("small", run.call_args.kwargs["model_size"])
                self.assertEqual(0, index.main([source, "--no-asr"]))
                self.assertIsNone(run.call_args.kwargs["model_size"])
            with mock.patch.dict(os.environ, {"ASR_MODEL": "medium"}):
                self.assertEqual("medium", asr_hints.configured_model(home))

    def test_new_install_uses_accurate_model_and_beam_search_without_breaking_fast_choice(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"ASR_MODEL": "", "ASR_BEAM": ""}):
            self.assertEqual("large-v3", asr_hints.configured_model(home))
            self.assertEqual(5, asr_hints.beam_size())
            self.assertEqual(1, asr_hints.beam_size(1))
            self.assertEqual(5, asr_hints.beam_size("bad"))
            self.assertEqual(5, asr_hints.beam_size(-2))

    def test_standalone_model_choice_never_silently_substitutes_turbo(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "large-v3-turbo-full").mkdir()
            Path(home, "large-v3-turbo-full/model.bin").write_bytes(b"turbo")
            with mock.patch.object(asr, "ASR_DIR", home):
                self.assertFalse(asr.available("large-v3"))
                self.assertTrue(asr.available("large-v3-turbo"))

    def test_standalone_decoder_uses_beam_five_and_keeps_low_confidence_word(self):
        word = SimpleNamespace(start=0.1, end=0.3, word="嗯", probability=0.01)
        segment = SimpleNamespace(start=0.1, end=0.3, text="嗯", words=[word])
        calls = []

        class Model:
            def transcribe(self, _audio, **kwargs):
                calls.append(kwargs)
                return iter([segment]), SimpleNamespace(duration=0.5)

        with mock.patch.object(asr, "_load", return_value=Model()), \
                mock.patch.object(asr, "_decode", return_value=np.ones(8000)):
            rows = asr.transcribe("short.wav", with_probability=True)
        self.assertEqual("嗯", rows[0]["word"])
        self.assertEqual(0.01, rows[0]["prob"])
        self.assertEqual(5, calls[0]["beam_size"])
        self.assertEqual(0, calls[0]["vad_parameters"]["min_speech_duration_ms"])
        self.assertEqual(400, calls[0]["vad_parameters"]["speech_pad_ms"])

    def test_user_hotwords_have_priority_over_generic_terms_in_bounded_budget(self):
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home, "conf")
            conf.mkdir()
            (conf / "asr_terms.txt").write_text("用户术语|错误写法\n", encoding="utf-8")
            _context, hints, selected = asr_hints.build(
                home, extra_terms=["通用术语", "其他术语"], max_hotword_chars=8)
        self.assertEqual(["用户术语"], selected)
        self.assertNotIn("错误写法", hints)

    def test_subtitle_glossary_precedes_generic_hotwords(self):
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home, "conf")
            conf.mkdir()
            (conf / "asr_terms.txt").write_text("", encoding="utf-8")
            (conf / "terms.txt").write_text(
                "六氟化铀|六伏化油\n富硅岩燃料|韩服硅岩燃料\n",
                encoding="utf-8")
            _context, _hints, selected = asr_hints.build(
                home, extra_terms=["通用术语", "其他术语"], max_hotword_chars=13)
        self.assertEqual(["六氟化铀", "富硅岩燃料"], selected)


class AsrProfileCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.source = str(Path(self.home, "source.mp4"))
        Path(self.source).write_bytes(b"source-media")
        write_settings(self.home, model="large-v3")
        self.enterContext(mock.patch.object(index, "BASE_DIR", self.home))
        self.enterContext(mock.patch.dict(os.environ, {"ASR_MODEL": "", "ASR_BEAM": "5"}))
        self.enterContext(mock.patch.object(index.llm_gateway, "backend_signature", return_value="local"))

    def payload(self):
        return {
            "source": self.source,
            "source_identity": media_identity.source_identity(self.source),
            "ai_backend_signature": "local", "asr_requested": True,
            "segments": [[0.0, 1.0, "原台词"]],
            "asr_profile": index.asr_profile("large-v3"),
        }

    def test_matching_model_beam_and_hotwords_reuse_source_index(self):
        profile = self.payload()["asr_profile"]
        self.assertEqual(index.ASR_PROFILE_VERSION, profile["version"])
        self.assertEqual("large-v3", profile["model"])
        self.assertEqual(5, profile["beam_size"])
        self.assertEqual(0, profile["vad_parameters"]["min_speech_duration_ms"])
        self.assertEqual(64, len(profile["signature"]))
        self.assertTrue(index.index_freshness(self.payload(), self.source)["reusable"])

    def test_changed_saved_model_and_beam_invalidate_transcription(self):
        payload = self.payload()
        write_settings(self.home, model="large-v3-turbo")
        self.assertEqual("asr_profile_changed", index.index_freshness(payload, self.source)["reason"])
        write_settings(self.home, model="large-v3")
        with mock.patch.dict(os.environ, {"ASR_BEAM": "1"}):
            self.assertEqual("asr_profile_changed", index.index_freshness(payload, self.source)["reason"])

    def test_only_selected_hotword_content_changes_signature(self):
        payload = self.payload()
        terms = Path(self.home, "conf/asr_terms.txt")
        terms.write_text("新增术语|旧写法\n", encoding="utf-8")
        self.assertEqual("asr_profile_changed", index.index_freshness(payload, self.source)["reason"])
        updated = self.payload()
        terms.write_text("# a comment is not an ASR input\n新增术语|另一错字\n", encoding="utf-8")
        self.assertTrue(index.index_freshness(updated, self.source)["reusable"])

    def test_unknown_or_older_asr_profile_requires_reanalysis(self):
        payload = self.payload()
        payload.pop("asr_profile")
        self.assertEqual("asr_profile_missing", index.index_freshness(payload, self.source)["reason"])
        payload["asr_profile"] = {"version": -1, "signature": "old"}
        self.assertEqual("asr_profile_version_changed", index.index_freshness(payload, self.source)["reason"])

    def test_visual_only_index_keeps_its_existing_identity_reuse(self):
        payload = self.payload()
        payload.update(asr_requested=False, segments=[])
        payload.pop("asr_profile")
        self.assertTrue(index.index_freshness(payload, self.source)["reusable"])

    def test_autopilot_rebuilds_an_old_asr_index_instead_of_reusing_text(self):
        index_dir = Path(self.home, "old-index")
        index_dir.mkdir()
        old = self.payload()
        old.pop("asr_profile")
        (index_dir / "index.json").write_text(json.dumps(old), encoding="utf-8")
        logs = []
        with mock.patch("llm_gateway.api_enabled", return_value=False):
            runtime = autopilot._WorkflowRun(
                self.source, "medium", False, str(index_dir), logs.append,
                False, None, self.home, False, False, None, "gameplay", "TASK")
        completed = SimpleNamespace(returncode=0, stdout="rebuilt", stderr="")
        data = {"duration": 1.0, "segments": [], "cards": []}
        context = SimpleNamespace(cancel_check=lambda: None)
        with mock.patch("winproc.run_hidden", return_value=completed) as run, \
                mock.patch.object(autocut, "load_index", return_value=data):
            runtime.analyze(context)
        run.assert_called_once()
        self.assertTrue(any("asr_profile_missing" in message for message in logs))


class AsrTextPreservationTests(unittest.TestCase):
    def test_repeated_real_speech_and_low_confidence_words_survive_both_decoders(self):
        text = "对对对对对对，我们继续"
        segment = SimpleNamespace(start=1.0, end=3.0, text=text,
                                  words=[SimpleNamespace(start=1.0, end=3.0,
                                                         word=text, probability=0.01)])
        model = SimpleNamespace(transcribe=lambda *_a, **_kw: (
            iter([segment]), SimpleNamespace(duration=4.0)))
        for batch in (0, 8):
            with self.subTest(batch=batch), \
                    mock.patch.object(index, "_load_model", return_value=model), \
                    mock.patch.object(index, "_vad_ready", return_value=False), \
                    mock.patch("faster_whisper.BatchedInferencePipeline", return_value=model):
                evidence = []
                rows = index.transcribe(np.ones(16000), batch_size=batch,
                                        evidence_out=evidence)
            self.assertEqual([(1.0, 3.0, text)], rows)
            self.assertEqual(0.01, evidence[0]["words"][0]["probability"])
            self.assertIn("repetition_suspected", evidence[0]["asr_flags"])

    def test_word_alignment_gap_does_not_erase_segment_text_when_splitting(self):
        segment = SimpleNamespace(start=0.0, end=20.0, words=[
            SimpleNamespace(start=0.2, end=1.0, word="我们", probability=0.8),
            SimpleNamespace(start=18.0, end=19.0, word="继续", probability=0.7),
        ])
        evidence = []
        rows = index._split_long_segment(segment, "我们继续吧", evidence_out=evidence)
        self.assertEqual([(0.0, 20.0, "我们继续吧")], rows)
        self.assertEqual("unaligned-text-preserved", evidence[0]["split_reason"])

    def test_head_recheck_is_bounded_to_missing_prefix_and_skips_actual_silence(self):
        existing = [(500.0, 501.0, "原第一句")]
        with mock.patch.object(index, "transcribe", return_value=[]) as run:
            index.fill_missing_head(existing, np.ones(16000 * 510), window=120.0)
            self.assertEqual(16000 * 120, run.call_args.args[0].size)
        with mock.patch.object(index, "transcribe") as run:
            self.assertEqual(existing, index.fill_missing_head(existing, np.zeros(16000 * 510)))
            run.assert_not_called()

    def test_head_recheck_does_not_crop_text_without_word_timing(self):
        existing = [(20.0, 21.0, "原第一句")]
        with mock.patch.object(index, "transcribe", return_value=[(19.0, 22.0, "跨越原第一句")]):
            rows = index.fill_missing_head(existing, np.ones(16000 * 25))
        self.assertEqual(existing, rows)


if __name__ == "__main__":
    unittest.main()
