# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

import autopilot
import index
import media_identity
import workflow_engine


class MediaIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source.mp4")
        with open(self.source, "wb") as handle:
            handle.write(b"A" * 8192)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_index(self, payload):
        path = os.path.join(self.tmp.name, "index.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_identity_changes_when_size_changes(self):
        before = media_identity.source_identity(self.source)
        with open(self.source, "ab") as handle:
            handle.write(b"more")
        after = media_identity.source_identity(self.source)
        self.assertFalse(media_identity.matches(before, after))
        self.assertNotEqual(before["identity_key"], after["identity_key"])

    def test_same_size_and_mtime_content_replacement_is_detected(self):
        before = media_identity.source_identity(self.source)
        stat = os.stat(self.source)
        with open(self.source, "r+b") as handle:
            handle.seek(0)
            handle.write(b"B" * 8192)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = media_identity.source_identity(self.source)
        self.assertEqual(before["source_size"], after["source_size"])
        self.assertEqual(before["source_mtime_ns"], after["source_mtime_ns"])
        self.assertNotEqual(before["source_fingerprint"],
                            after["source_fingerprint"])
        self.assertFalse(media_identity.matches(before, after))

    def test_source_change_during_sampling_retries_to_stable_version(self):
        original = media_identity.sampled_sha256
        calls = []

        def changing(path, sample_bytes=media_identity.DEFAULT_SAMPLE_BYTES,
                     **kwargs):
            result = original(path, sample_bytes, **kwargs)
            calls.append(result)
            if len(calls) == 1:
                with open(path, "ab") as handle:
                    handle.write(b"changed-during-hash")
            return result

        with mock.patch("media_identity.sampled_sha256", side_effect=changing):
            identity = media_identity.source_identity(self.source)
        self.assertEqual(2, len(calls))
        self.assertEqual(os.path.getsize(self.source), identity["source_size"])
        self.assertEqual(media_identity.sampled_sha256(self.source),
                         identity["source_fingerprint"])

    def test_new_index_matches_only_the_same_source_identity(self):
        saved = media_identity.source_identity(self.source)
        path = self._write_index({
            "source": os.path.abspath(self.source),
            "source_identity": saved,
            "index_dir": self.tmp.name,
        })
        fresh = index.index_freshness(path, self.source)
        self.assertTrue(fresh["reusable"])
        self.assertFalse(fresh["legacy"])

        stat = os.stat(self.source)
        with open(self.source, "r+b") as handle:
            handle.write(b"C" * 8192)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        stale = index.index_freshness(path, self.source)
        self.assertFalse(stale["reusable"])
        self.assertEqual("source_identity_mismatch", stale["reason"])

    def test_mtime_only_change_invalidates_new_index_conservatively(self):
        saved = media_identity.source_identity(self.source)
        path = self._write_index({
            "source": os.path.abspath(self.source),
            "source_identity": saved,
            "index_dir": self.tmp.name,
        })
        stat = os.stat(self.source)
        later = stat.st_mtime_ns + 1_000_000_000
        os.utime(self.source, ns=(stat.st_atime_ns, later))
        stale = index.index_freshness(path, self.source)
        self.assertFalse(stale["reusable"])
        self.assertEqual("source_identity_mismatch", stale["reason"])

    def test_legacy_index_requires_matching_path_and_not_newer_source(self):
        path = self._write_index({
            "source": os.path.abspath(self.source),
            "index_dir": self.tmp.name,
        })
        now = os.stat(path).st_mtime_ns
        os.utime(self.source, ns=(now - 2_000_000_000,
                                  now - 2_000_000_000))
        fresh = index.index_freshness(path, self.source)
        self.assertTrue(fresh["reusable"])
        self.assertTrue(fresh["legacy"])

        future = os.stat(path).st_mtime_ns + 2_000_000_000
        os.utime(self.source, ns=(future, future))
        stale = index.index_freshness(path, self.source)
        self.assertFalse(stale["reusable"])
        self.assertEqual("legacy_source_newer_than_index", stale["reason"])

    def test_run_signature_changes_when_source_bytes_change(self):
        def signature():
            return autopilot._run_signature(
                self.source, self.tmp.name, "gameplay", "medium", None,
                True, False, None, workflow_engine.FULL_AUTO)

        before = signature()
        stat = os.stat(self.source)
        with open(self.source, "r+b") as handle:
            handle.write(b"D" * 8192)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = signature()
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
