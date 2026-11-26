# -*- coding: utf-8 -*-
"""Security regression tests for the localhost Premiere bridge."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import sync_server


class BridgeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.token = "test-session-token-0123456789abcdef"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.Handler)
        self.server.bridge_token = self.token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", token=None, origin=None, body=None):
        headers = {}
        if token is not None:
            headers[sync_server.BRIDGE_HEADER] = token
        if origin is not None:
            headers["Origin"] = origin
        data = None if body is None else body.encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                raw = response.read().decode("utf-8")
                return response.status, response.headers, json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
                return exc.code, exc.headers, json.loads(raw or "{}")
            finally:
                exc.close()

    def test_all_routes_require_session_token(self):
        status, headers, data = self.request("/health")
        self.assertEqual(401, status)
        self.assertFalse(data["ok"])
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

        status, _headers, data = self.request(
            "/health", token="wrong-session-token-0123456789")
        self.assertEqual(401, status)
        self.assertFalse(data["ok"])

    def test_authenticated_native_client_can_read_health(self):
        status, headers, data = self.request("/health", token=self.token)
        self.assertEqual(200, status)
        self.assertTrue(data["ok"])
        self.assertEqual("no-store", headers.get("Cache-Control"))

    def test_web_origin_is_rejected_even_with_valid_token(self):
        status, headers, data = self.request(
            "/health", token=self.token, origin="https://example.invalid")
        self.assertEqual(403, status)
        self.assertFalse(data["ok"])
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_cep_null_origin_is_echoed_after_authentication(self):
        status, headers, data = self.request(
            "/health", token=self.token, origin="null")
        self.assertEqual(200, status)
        self.assertTrue(data["ok"])
        self.assertEqual("null", headers.get("Access-Control-Allow-Origin"))

    def test_preflight_allows_only_cep_origin_and_named_headers(self):
        status, headers, _data = self.request(
            "/health", method="OPTIONS", origin="null")
        self.assertEqual(204, status)
        self.assertEqual("null", headers.get("Access-Control-Allow-Origin"))
        self.assertIn(sync_server.BRIDGE_HEADER,
                      headers.get("Access-Control-Allow-Headers"))
        self.assertNotIn("*", headers.get("Access-Control-Allow-Headers"))

        status, headers, data = self.request(
            "/health", method="OPTIONS", origin="https://example.invalid")
        self.assertEqual(403, status)
        self.assertFalse(data["ok"])
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_state_changing_get_is_rejected(self):
        status, _headers, data = self.request("/transcribe", token=self.token)
        self.assertEqual(405, status)
        self.assertFalse(data["ok"])


class BridgeSessionFileTests(unittest.TestCase):
    def test_session_file_is_random_atomic_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bridge-session.json")
            first = sync_server.write_bridge_session(port=17890, path=path)
            loaded = sync_server.load_bridge_session(path)
            self.assertEqual(first, loaded["token"])
            self.assertEqual(17890, loaded["port"])
            self.assertGreaterEqual(len(first), 32)

            second = sync_server.write_bridge_session(port=17891, path=path)
            self.assertNotEqual(first, second)
            self.assertFalse(sync_server.clear_bridge_session(first, path))
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(sync_server.clear_bridge_session(second, path))
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
