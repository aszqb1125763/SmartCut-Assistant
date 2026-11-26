# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

import llm_config


class SingleLocalModelTests(unittest.TestCase):
    def _paths(self, root):
        conf = os.path.join(root, "conf", "llm.json")
        models = os.path.join(root, "models", "llm")
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        os.makedirs(models, exist_ok=True)
        return conf, models

    def _patch_paths(self, conf, models):
        return mock.patch.multiple(
            llm_config,
            CONF=conf,
            LLM_DIR=models,
            BASE=os.path.dirname(os.path.dirname(conf)),
        )

    def test_old_mixed_config_migrates_to_selected_8b_for_every_task(self):
        with tempfile.TemporaryDirectory() as root:
            conf, models = self._paths(root)
            name = "Qwen3-8B-Q4_K_M.gguf"
            with open(os.path.join(models, name), "wb") as handle:
                handle.write(b"selected-model")
            with open(conf, "w", encoding="utf-8") as handle:
                json.dump({
                    "models": {
                        "fast": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
                        "smart": name,
                    },
                    "tasks": {
                        "proofread": "fast",
                        "segment": "fast",
                        "paragraph": "smart",
                        "complete": "smart",
                    },
                }, handle)

            with self._patch_paths(conf, models):
                cfg = llm_config.load()
                self.assertEqual("smart", cfg["active_tier"])
                for task in llm_config.LOCAL_TASKS:
                    self.assertEqual("smart", cfg["tasks"][task])
                    self.assertEqual(
                        os.path.join(models, name), llm_config.pick(task))
                # Legacy callers that still request a tier must also follow
                # the user's one active model.
                self.assertEqual(
                    os.path.join(models, name), llm_config.model_path("fast"))
                self.assertFalse(os.path.exists(os.path.join(
                    models, "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")))

    def test_selecting_any_tier_applies_it_to_the_complete_workflow(self):
        cases = (
            ("fast", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
            ("smart", "Qwen3-8B-Q4_K_M.gguf"),
            ("large", "Qwen3-14B-Q4_K_M.gguf"),
        )
        for tier, name in cases:
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as root:
                conf, models = self._paths(root)
                with open(os.path.join(models, name), "wb") as handle:
                    handle.write(b"selected-model")
                with self._patch_paths(conf, models):
                    cfg = llm_config.select_model(tier, name)
                    self.assertEqual(tier, cfg["active_tier"])
                    self.assertFalse(cfg["escalate"])
                    for task in llm_config.LOCAL_TASKS:
                        self.assertEqual(tier, cfg["tasks"][task])
                        self.assertEqual(
                            os.path.join(models, name), llm_config.pick(task))

                    with open(conf, encoding="utf-8") as handle:
                        saved = json.load(handle)
                    self.assertEqual(tier, saved["active_tier"])
                    self.assertTrue(all(
                        saved["tasks"][task] == tier
                        for task in llm_config.LOCAL_TASKS))

    def test_missing_selected_model_does_not_fall_back_to_another_tier(self):
        with tempfile.TemporaryDirectory() as root:
            conf, models = self._paths(root)
            fast_name = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
            smart_name = "Qwen3-8B-Q4_K_M.gguf"
            with open(os.path.join(models, fast_name), "wb") as handle:
                handle.write(b"wrong-model")
            with open(conf, "w", encoding="utf-8") as handle:
                json.dump({
                    "active_tier": "smart",
                    "models": {"fast": fast_name, "smart": smart_name},
                    "tasks": {"paragraph": "smart"},
                }, handle)

            with self._patch_paths(conf, models):
                selected = llm_config.pick("proofread")
                self.assertEqual(os.path.join(models, smart_name), selected)
                self.assertFalse(os.path.exists(selected))
                self.assertNotEqual(os.path.join(models, fast_name), selected)


if __name__ == "__main__":
    unittest.main()
