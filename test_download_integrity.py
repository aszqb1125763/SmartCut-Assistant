# -*- coding: utf-8 -*-
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import installer
import llm_models
import setup_llm


class _Response:
    def __init__(self, payload, status=200):
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._stream.read(size)


class DownloadIntegrityTests(unittest.TestCase):
    def test_ffmpeg_source_is_immutable_and_hash_pinned(self):
        self.assertEqual(1, len(installer.FFMPEG_SOURCES))
        url, _label, size, digest = installer.FFMPEG_SOURCES[0]
        self.assertIn("autobuild-2026-11-25-12-41", url)
        self.assertNotIn("/latest/", url)
        self.assertEqual(205524259, size)
        self.assertEqual(64, len(digest))

    def test_ffmpeg_verifier_rejects_wrong_size_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ffmpeg.zip"
            path.write_bytes(b"verified payload")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertTrue(installer._verified_download(
                str(path), path.stat().st_size, digest))
            self.assertFalse(installer._verified_download(
                str(path), path.stat().st_size + 1, digest))
            self.assertFalse(installer._verified_download(
                str(path), path.stat().st_size, "0" * 64))

    def test_llama_archives_are_size_and_hash_pinned(self):
        self.assertEqual(3, len(setup_llm.ZIPS))
        for name, _label, size, digest in setup_llm.ZIPS:
            with self.subTest(name=name):
                self.assertTrue(name.endswith(".zip"))
                self.assertGreater(size, 10 * 1024 * 1024)
                self.assertEqual(64, len(digest))

    def test_llama_verifier_checks_hash_not_only_file_size(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "llama.zip"
            path.write_bytes(b"archive")
            digest = hashlib.sha256(b"archive").hexdigest()
            self.assertTrue(setup_llm.verified(
                str(path), len(b"archive"), digest))
            self.assertFalse(setup_llm.verified(
                str(path), len(b"archive"), "f" * 64))

    def test_llama_fetch_handles_server_ignoring_range(self):
        payload = b"complete fixed archive"
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "llama.zip"
            Path(str(target) + ".part").write_bytes(b"old")
            with mock.patch.object(
                    setup_llm.urllib.request, "urlopen",
                    return_value=_Response(payload, status=200)):
                self.assertTrue(setup_llm.fetch(
                    "https://example.invalid/llama.zip",
                    str(target), len(payload),
                    hashlib.sha256(payload).hexdigest(), retries=1))
            self.assertEqual(payload, target.read_bytes())

    def test_model_fetch_handles_server_ignoring_range(self):
        payload = b"complete fixed model"
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "model.gguf"
            part = Path(str(target) + ".part")
            part.write_bytes(b"old")
            catalog = {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            with mock.patch.object(
                    llm_models.urllib.request, "urlopen",
                    return_value=_Response(payload, status=200)):
                result = llm_models._fetch(
                    "https://example.invalid/model.gguf", str(part),
                    part.stat().st_size, catalog, None, None, str(target))
            self.assertEqual(str(target), result)
            self.assertEqual(payload, target.read_bytes())

    def test_safe_extract_rejects_parent_traversal(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("../escaped.txt", "unsafe")
        payload.seek(0)
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "llama"
            destination.mkdir()
            escaped = Path(temp) / "escaped.txt"
            with zipfile.ZipFile(payload) as archive:
                with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                    setup_llm.safe_extract(archive, str(destination))
            self.assertFalse(escaped.exists())

    def test_safe_extract_rejects_symlink_member(self):
        payload = io.BytesIO()
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(info, "target")
        payload.seek(0)
        with tempfile.TemporaryDirectory() as temp:
            with zipfile.ZipFile(payload) as archive:
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    setup_llm.safe_extract(archive, temp)

    def test_all_local_models_have_exact_integrity_metadata(self):
        expected = {
            "fast": (
                2497280448,
                "8cdb57cbb880d313736a9bc4e3d3d2485f145b5e19cf33783746e753e82641fc",
            ),
            "smart": (
                5027783488,
                "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
            ),
            "large": (
                9001752960,
                "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
            ),
        }
        for tier, (size, digest) in expected.items():
            with self.subTest(tier=tier):
                item = llm_models.info(tier)
                self.assertEqual(size, item["bytes"])
                self.assertEqual(digest, item["sha256"])
        fast = llm_models.info("fast")
        self.assertEqual(
            "lmstudio-community/Qwen3-4B-Instruct-2507-GGUF",
            fast["repo"],
        )
        self.assertEqual("", fast["ms"])


if __name__ == "__main__":
    unittest.main()
