# -*- coding: utf-8 -*-
"""Real Windows/FFmpeg regression for cache files beyond MAX_PATH."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

import audio_evidence_cache as audio_cache
import video_frame_cache as frame_cache


@unittest.skipUnless(os.name == "nt", "Windows extended-path regression")
class MediaCacheLongPathTests(unittest.TestCase):
    def test_drive_unc_and_existing_prefix_only_normalize_cache_paths(self):
        for module in (audio_cache, frame_cache):
            with self.subTest(module=module.__name__):
                self.assertEqual(r"\\?\F:\cache\leaf",
                                 module._cache_fs_path(r"F:\cache\other\..\leaf"))
                self.assertEqual(r"\\?\UNC\server\share\cache",
                                 module._cache_fs_path(r"\\server\share\cache"))
                self.assertEqual(r"\\?\UNC\server\share\cache",
                                 module._cache_fs_path(r"\\?\UNC\server\share\cache"))
                self.assertEqual(r"\\?\F:\cache",
                                 module._cache_fs_path(r"\\?\F:\cache"))

    def test_real_pcm_proxy_exact_cold_and_warm_over_max_path(self):
        ffmpeg = os.path.abspath(os.path.join("bin", "ffmpeg.exe"))
        if not os.path.isfile(ffmpeg):
            self.skipTest("bundled FFmpeg is unavailable")
        # tempfile's normal-path cleanup cannot descend into long Windows
        # names when LongPathsEnabled=0. Clean this generated root explicitly
        # through the same extended namespace instead.
        temp_root = tempfile.mkdtemp(prefix="smartcut-longpath-test-")
        self.addCleanup(shutil.rmtree, audio_cache._cache_fs_path(temp_root))
        source = os.path.join(temp_root, "source.mp4")
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", source],
            capture_output=True, timeout=30,
            creationflags=audio_cache.CREATE_NO_WINDOW)
        self.assertEqual(0, result.returncode,
                         result.stderr.decode("utf-8", "replace"))
        self.assertEqual(b"", result.stderr)

        cache_dir = temp_root
        while len(cache_dir) < 290:
            cache_dir = os.path.join(cache_dir, "long_cache_segment_1234567890")
        audio_dir = os.path.join(cache_dir, "audio")
        video_dir = os.path.join(cache_dir, "video")
        canonical_source = os.path.normcase(os.path.realpath(os.path.abspath(source)))
        processes = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            process._cache_test_stderr = None
            communicate = process.communicate

            def recording_communicate(*args, **kwargs):
                output = communicate(*args, **kwargs)
                process._cache_test_stderr = output[1]
                return output

            process.communicate = recording_communicate
            processes.append(process)
            return process

        with mock.patch.object(subprocess, "Popen", side_effect=recording_popen):
            with audio_cache.AudioEvidenceCache(
                    source, [0], cache_dir=audio_dir, ffmpeg=ffmpeg) as pcm:
                cold_audio = pcm.entry(0)
                samples = pcm.review_samples(0, 0.0, 0.5)
                self.assertFalse(cold_audio["reused"])
                self.assertEqual(canonical_source, pcm.source)
                self.assertFalse(pcm.source.startswith("\\\\?\\"))
                self.assertGreater(float(np.max(np.abs(samples))), 0.01)
                with open(cold_audio["manifest_path"], encoding="utf-8") as handle:
                    audio_manifest = json.load(handle)
                self.assertEqual(canonical_source, audio_manifest["identity"]["source"])

            video = frame_cache.VideoFrameCache(source, cache_dir=video_dir,
                                                 ffmpeg=ffmpeg)
            proxy_path, cold_proxy = video.frame_path_at(0.5)
            exact_path, cold_exact = video.exact_frame_path_at(0.35)
            self.assertFalse(cold_proxy["reused"])
            self.assertFalse(cold_exact["reused"])
            self.assertEqual(canonical_source, video.source)
            self.assertEqual(3, len(processes), "one PCM, one proxy, one exact decode")

        for process in processes:
            self.assertEqual(0, process.returncode)
            self.assertEqual(b"", process._cache_test_stderr)
            self.assertTrue(process.stderr is None or process.stderr.closed)

        for path in (cold_audio["raw_path"], cold_audio["manifest_path"],
                     proxy_path, video.manifest_path, video.access_path, exact_path):
            self.assertTrue(path.startswith("\\\\?\\"), path)
            self.assertGreater(len(path[4:]), 260, path)
            self.assertTrue(os.path.isfile(path), path)

        # Fresh role objects must reuse all files without spawning FFmpeg.
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError(
                "warm cache launched an external decoder")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError(
                    "warm exact-frame cache launched an external decoder")):
            with audio_cache.AudioEvidenceCache(
                    source, [0], cache_dir=audio_cache._cache_fs_path(audio_dir),
                    ffmpeg=ffmpeg) as pcm:
                warm_audio = pcm.entry(0)
                self.assertTrue(warm_audio["reused"])
                self.assertEqual(cold_audio["key"], warm_audio["key"])
                np.testing.assert_array_equal(samples, pcm.review_samples(0, 0.0, 0.5))
            warm = frame_cache.VideoFrameCache(
                source, cache_dir=frame_cache._cache_fs_path(video_dir),
                ffmpeg=ffmpeg)
            warm_proxy_path, warm_proxy = warm.frame_path_at(0.5)
            warm_exact_path, warm_exact = warm.exact_frame_path_at(0.35)
            self.assertTrue(warm_proxy["reused"])
            self.assertTrue(warm_exact["reused"])
            self.assertEqual(proxy_path, warm_proxy_path)
            self.assertEqual(exact_path, warm_exact_path)
            self.assertEqual(video.cache_key, warm.cache_key)
            image, _info = warm.exact_frame_at(0.35)
            self.assertIsNotNone(image)
            self.assertGreater(image.size, 0)

        # Locks and temporary files must have been removed through long paths.
        for directory, _dirs, names in os.walk(audio_cache._cache_fs_path(cache_dir)):
            for name in names:
                self.assertFalse(name.endswith((".build.lock", ".part", ".part.jpg", ".tmp")),
                                 os.path.join(directory, name))


if __name__ == "__main__":
    unittest.main()
