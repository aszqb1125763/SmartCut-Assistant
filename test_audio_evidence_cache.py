# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import time
import unittest

import numpy as np

from audio_evidence_cache import (
    AudioEvidenceCache, AudioEvidenceCancelled, prune_cache,
)


class FakeDecoder:
    def __init__(self, values_by_track):
        self.values_by_track = values_by_track
        self.calls = []

    def __call__(self, source, track, sample_rate, output_path):
        self.calls.append((source, track, sample_rate))
        np.asarray(self.values_by_track[track], dtype="<i2").tofile(output_path)
        return True


class AudioEvidenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source.mp4")
        with open(self.source, "wb") as handle:
            handle.write(b"fake media identity")
        self.cache_dir = os.path.join(self.tmp.name, "cache")

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_reads_only_requested_pcm_as_float(self):
        decoder = FakeDecoder({0: [-32768, -16384, 0, 16384, 32767, 0]})
        cache = AudioEvidenceCache(
            self.source, [0], sample_rate=2, cache_dir=self.cache_dir,
            decoder=decoder)
        with cache:
            values = cache.review_samples(0, 0.5, 2.0)
        np.testing.assert_allclose(values, [-0.5, 0.0, 0.5], atol=1e-5)
        self.assertEqual(1, len(decoder.calls))

    def test_completed_entry_is_reused_without_decoding(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        first = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder)
        first.prepare()
        first.close()
        second = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder)
        entry = second.prepare_track(0)
        second.close()
        self.assertTrue(entry["reused"])
        self.assertEqual(1, len(decoder.calls))

    def test_activity_intervals_are_shifted_to_absolute_time(self):
        # 2 seconds silence, 1 second tone, 2 seconds silence at 1 kHz.
        raw = np.zeros(5000, dtype=np.int16)
        raw[2000:3000] = 12000
        decoder = FakeDecoder({0: raw})
        with AudioEvidenceCache(
                self.source, [0], sample_rate=1000, cache_dir=self.cache_dir,
                decoder=decoder) as cache:
            rows = cache.activity_intervals(0, 1.0, 4.0)
        self.assertTrue(rows)
        self.assertGreaterEqual(rows[0]["start"], 1.9)
        self.assertLessEqual(rows[0]["start"], 2.1)
        self.assertGreaterEqual(rows[-1]["end"], 2.9)
        self.assertTrue(rows[0]["cache_key"])

    def test_multitrack_entries_are_independent_and_deterministic(self):
        decoder = FakeDecoder({0: [1, 2], 2: [3, 4, 5, 6]})
        cache = AudioEvidenceCache(
            self.source, [2, 0, 2], sample_rate=2,
            cache_dir=self.cache_dir, decoder=decoder)
        entries = cache.prepare()
        self.assertEqual((0, 2), cache.tracks)
        self.assertNotEqual(entries[0]["key"], entries[2]["key"])
        self.assertEqual([0, 2], [call[1] for call in decoder.calls])
        cache.close()

    def test_purge_removes_owned_raw_and_manifest(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        cache = AudioEvidenceCache(
            self.source, [0], sample_rate=2, cache_dir=self.cache_dir,
            decoder=decoder)
        entry = cache.prepare_track(0)
        self.assertTrue(os.path.isfile(entry["raw_path"]))
        cache.purge()
        self.assertFalse(os.path.exists(entry["raw_path"]))
        self.assertFalse(os.path.exists(entry["manifest_path"]))

    def test_cancel_before_decode_leaves_no_valid_entry(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        cache = AudioEvidenceCache(
            self.source, [0], sample_rate=2, cache_dir=self.cache_dir,
            decoder=decoder, should_cancel=lambda: True)
        with self.assertRaises(AudioEvidenceCancelled):
            cache.prepare_track(0)
        self.assertEqual([], os.listdir(self.cache_dir))
        self.assertEqual([], decoder.calls)

    def test_mix_reuses_each_track_and_keeps_timeline_alignment(self):
        decoder = FakeDecoder({0: [16384, 16384, 0, 0],
                               1: [16384, -16384]})
        with AudioEvidenceCache(
                self.source, [0, 1], sample_rate=2,
                cache_dir=self.cache_dir, decoder=decoder) as cache:
            mixed = cache.review_mix([0, 1], 0.0, 2.0)
        np.testing.assert_allclose(mixed, [32767 / 32768.0, 0.0, 0.0, 0.0],
                                   atol=1e-5)
        self.assertEqual([0, 1], [call[1] for call in decoder.calls])

    def test_fingerprint_invalidates_same_size_same_mtime_replacement(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        before = os.stat(self.source)
        first = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder)
        first_entry = first.prepare_track(0)
        first.close()
        with open(self.source, "wb") as handle:
            handle.write(b"x" * before.st_size)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder)
        second_entry = second.prepare_track(0)
        second.close()
        self.assertNotEqual(first_entry["key"], second_entry["key"])
        self.assertEqual(2, len(decoder.calls))

    def test_algorithm_version_invalidates_decoder_contract(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        first = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder, algorithm_version="decoder-v1")
        first_entry = first.prepare_track(0)
        first.close()
        second = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder, algorithm_version="decoder-v2")
        second_entry = second.prepare_track(0)
        second.close()
        self.assertNotEqual(first_entry["key"], second_entry["key"])
        self.assertEqual(2, len(decoder.calls))

    def test_concurrent_instances_have_only_one_builder(self):
        calls = []
        calls_lock = threading.Lock()

        def slow_decoder(source, track, sample_rate, output_path):
            with calls_lock:
                calls.append((source, track, sample_rate))
            time.sleep(0.08)
            np.asarray([1, 2, 3, 4], dtype="<i2").tofile(output_path)
            return True

        caches = [AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=slow_decoder) for _ in range(2)]
        barrier = threading.Barrier(2)
        entries, errors = [], []

        def worker(cache):
            try:
                barrier.wait()
                entries.append(cache.prepare_track(0))
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(cache,))
                   for cache in caches]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        for cache in caches:
            cache.close()
        self.assertEqual([], errors)
        self.assertEqual(2, len(entries))
        self.assertEqual(1, len(calls))
        self.assertEqual(1, sum(bool(entry["reused"]) for entry in entries))

    def test_lru_prune_removes_old_entries_and_crash_orphans(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        for version in ("one", "two"):
            cache = AudioEvidenceCache(
                self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
                decoder=decoder, algorithm_version=version,
                prune_on_prepare=False)
            cache.prepare_track(0)
            cache.close()
            time.sleep(0.002)
        orphan = os.path.join(self.cache_dir, "orphan.s16le")
        with open(orphan, "wb") as handle:
            handle.write(b"\0\0")
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        result = prune_cache(self.cache_dir, max_entries=1,
                             max_bytes=10_000, max_age_seconds=0)
        self.assertEqual(1, result["entries_removed"])
        self.assertGreaterEqual(result["orphans_removed"], 1)
        self.assertEqual(1, len([name for name in os.listdir(self.cache_dir)
                                 if name.endswith(".json")]))
        self.assertFalse(os.path.exists(orphan))

    def test_dead_builder_lock_is_recovered_without_waiting_for_timeout(self):
        decoder = FakeDecoder({0: [1, 2, 3, 4]})
        cache = AudioEvidenceCache(
            self.source, [0], sample_rate=4, cache_dir=self.cache_dir,
            decoder=decoder)
        _raw, _manifest, key, _identity = cache._paths(0)
        lock_path = os.path.join(self.cache_dir, key + ".build.lock")
        with open(lock_path, "w", encoding="ascii") as handle:
            handle.write("pid=99999999\ncreated_ns=1\n")
        old = time.time() - 2.0
        os.utime(lock_path, (old, old))
        entry = cache.prepare_track(0)
        cache.close()
        self.assertEqual(key, entry["key"])
        self.assertEqual(1, len(decoder.calls))
        self.assertFalse(os.path.exists(lock_path))


if __name__ == "__main__":
    unittest.main()
