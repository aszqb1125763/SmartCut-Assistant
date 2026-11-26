# -*- coding: utf-8 -*-
import io
import json
import os
import tempfile
import unittest
from unittest import mock
import urllib.error

import llm_config
import llm_gateway


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class _Remote:
    enabled = True
    provider = "openai"
    role = "editor"
    model = "remote-model"

    def chat(self, *_args, **_kwargs):
        return '{"ok":true}'


class LLMGatewayTests(unittest.TestCase):
    def _client(self, protocol="openai", **overrides):
        settings = {
            "enabled": True, "provider": protocol, "protocol": protocol,
            "base_url": "https://example.test/v1", "api_key": "secret-key",
            "model": "model-x", "timeout": 37, "headers": {"X-Team": "cut"},
        }
        settings.update(overrides)
        return llm_gateway.OpenAICompatibleClient(settings, role="editor")

    def test_flat_ui_settings_are_the_backend_source_of_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "llm_provider": "deepseek",
                    "llm_api_base_url": "https://custom.example/v1",
                    "llm_api_key": "k",
                    "llm_api_model": "deepseek-chat",
                    "llm_api_timeout": 88,
                }, handle)
            with mock.patch.object(llm_config, "APP_SETTINGS", path), \
                    mock.patch.dict(os.environ, {}, clear=False):
                cfg = llm_config.api_settings({"api": {"headers": {"X-A": "1"}}})
            self.assertEqual("deepseek", cfg["provider"])
            self.assertEqual("openai", cfg["protocol"])
            self.assertEqual("https://custom.example/v1", cfg["base_url"])
            self.assertEqual("deepseek-chat", cfg["model"])
            self.assertEqual(88, cfg["timeout"])
            self.assertEqual({"X-A": "1"}, cfg["headers"])
            self.assertFalse(cfg["fallback_local"])

    def test_openai_compatible_request_and_role_contract(self):
        client = self._client()
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response({"choices": [{"message": {"content": "done"}}]})

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            result = client.chat("只输出文本", "素材", max_tokens=321)
        self.assertEqual("done", result)
        self.assertEqual("https://example.test/v1/chat/completions",
                         captured["request"].full_url)
        self.assertEqual("Bearer secret-key",
                         captured["request"].get_header("Authorization"))
        self.assertEqual("cut", captured["request"].get_header("X-team"))
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("model-x", body["model"])
        self.assertIn("执行剪辑师", body["messages"][0]["content"])
        self.assertNotIn("只输出文本", body["messages"][0]["content"])
        self.assertEqual(37, captured["timeout"])

    def test_anthropic_native_messages(self):
        client = self._client("anthropic")
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            return _Response({"content": [{"type": "text", "text": "ok"}]})

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.assertEqual("ok", client.chat("合同", "素材"))
        self.assertEqual("https://example.test/v1/messages",
                         captured["request"].full_url)
        self.assertEqual("secret-key", captured["request"].get_header("X-api-key"))
        self.assertEqual("2023-06-01",
                         captured["request"].get_header("Anthropic-version"))
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("model-x", body["model"])
        self.assertIn("执行剪辑师", body["system"])

    def test_gemini_native_generate_content(self):
        client = self._client(
            "gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai")
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            return _Response({"candidates": [{"content": {
                "parts": [{"text": "gemini-ok"}]}}]})

        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            self.assertEqual("gemini-ok", client.chat("合同", "素材"))
        url = captured["request"].full_url
        self.assertIn("/v1beta/models/model-x:generateContent", url)
        self.assertIn("key=secret-key", url)
        self.assertIsNone(captured["request"].get_header("Authorization"))
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertIn("systemInstruction", body)
        self.assertIn("contents", body)

    def test_invalid_structured_output_is_retried_once(self):
        client = self._client()
        replies = [
            _Response({"choices": [{"message": {"content": "not-json"}}]}),
            _Response({"choices": [{"message": {"content": '{"delete":[]}'}}]}),
        ]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as opened:
            text = client.chat("只输出 JSON，输出格式包含 delete", "剪辑单")
        self.assertEqual('{"delete":[]}', text)
        self.assertEqual(2, opened.call_count)

    def test_failure_log_and_exception_redact_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "api.log")
            client = self._client()
            client.log_path = path
            error = urllib.error.HTTPError(
                "https://example.test/v1/chat/completions", 401, "bad",
                {}, io.BytesIO(b'{"error":"secret-key rejected"}'))
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(llm_gateway.APIRequestError) as caught:
                    client.chat("contract", "data")
            self.assertNotIn("secret-key", str(caught.exception))
            with open(path, encoding="utf-8") as handle:
                saved = handle.read()
            self.assertNotIn("secret-key", saved)
            self.assertNotIn("?", saved)

    def test_api_mode_never_starts_local_llama_server(self):
        from ui.llm_bridge import LLM
        with mock.patch(
                "llm_gateway.OpenAICompatibleClient.from_config",
                return_value=_Remote()):
            llm = LLM(base_dir=os.getcwd(), task="editor")
            with mock.patch.object(llm, "_start_local",
                                   side_effect=AssertionError("local started")):
                self.assertIs(llm, llm.start())
                self.assertEqual('{"ok":true}', llm.chat("contract", "data"))
            self.assertIsNone(llm.proc)

    def test_proofread_server_api_mode_skips_local_start(self):
        import proofread
        with mock.patch(
                "llm_gateway.OpenAICompatibleClient.from_config",
                return_value=_Remote()), \
                mock.patch.object(proofread, "_port_busy",
                                  side_effect=AssertionError("local checked")):
            with proofread.Server(task="paragraph", api_role="writer") as server:
                self.assertEqual('{"ok":true}',
                                 server.chat("contract", "data"))
                self.assertIsNone(server.proc)


if __name__ == "__main__":
    unittest.main()
