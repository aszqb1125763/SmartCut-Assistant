# -*- coding: utf-8 -*-
"""Disk-backed, multi-track PCM evidence cache.

Long gameplay recordings can occupy gigabytes when decoded to float32.  The
boundary refiner and director only need small windows around edit decisions,
so each selected audio stream is decoded once to mono PCM16 on disk and then
read through ``numpy.memmap``.  A cache entry becomes visible only after both
the raw file and an atomically-written completion manifest exist.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping

import numpy as np


SCHEMA = "smartcut.audio-evidence-cache"
SCHEMA_VERSION = 2
CACHE_ALGORITHM_VERSION = "pcm16le-mono-v2"
DEFAULT_MAX_CACHE_BYTES = 24 * 1024 ** 3
DEFAULT_MAX_CACHE_ENTRIES = 128
DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600
DEFAULT_STALE_BUILD_SECONDS = 12 * 3600
ACCESS_TOUCH_INTERVAL_NS = 3600 * 1_000_000_000
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_ENTRY_LOCKS: dict[str, threading.Lock] = {}
_ENTRY_LOCKS_GUARD = threading.Lock()
_PRUNE_GUARD = threading.Lock()
_LAST_PRUNE: dict[str, float] = {}


class AudioEvidenceError(RuntimeError):
    pass


class AudioEvidenceCancelled(AudioEvidenceError):
    pass


def _cache_fs_path(path: str) -> str:
    """Use Win32 extended paths only for internal cache filesystem access.

    Long source names already contribute to the surrounding work directory;
    hash keys, lock suffixes and atomic-write names can then exceed MAX_PATH.
    This prefix does not require the machine-wide long-path policy and never
    changes the source path used for media identity or FFmpeg input.
    """
    path = os.path.abspath(os.fspath(path))
    if os.name != "nt" or path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _entry_lock(key: str) -> threading.Lock:
    with _ENTRY_LOCKS_GUARD:
        return _ENTRY_LOCKS.setdefault(key, threading.Lock())


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _atomic_json(path: str, value: Mapping[str, Any]) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".audio-evidence-", suffix=".tmp",
                                     dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, sort_keys=True,
                      indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _default_cache_dir() -> str:
    try:
        import index as indexer
        root = indexer.work_root()
    except Exception:  # noqa: BLE001
        root = tempfile.gettempdir()
    return os.path.join(root, "_cache", "audio_evidence")


def _default_ffmpeg() -> str:
    import index as indexer
    return indexer.find_ffmpeg()


def _normalise_tracks(tracks: Iterable[int] | None) -> tuple[int, ...]:
    result = []
    for value in tracks or (0,):
        track = int(value)
        if track < 0:
            raise ValueError("audio track ordinal cannot be negative")
        if track not in result:
            result.append(track)
    if not result:
        result.append(0)
    return tuple(sorted(result))


def _fast_file_fingerprint(path: str, chunk_bytes: int = 65536) -> str:
    """Hash small samples from the beginning, middle and end of a media file.

    Size + mtime are usually enough for invalidation, but capture software and
    file-copy tools can preserve both.  Sampling keeps identity verification
    bounded (normally 192 KiB) while catching that practical failure mode.
    """
    chunk_bytes = max(4096, int(chunk_bytes))
    size = os.path.getsize(path)
    offsets = sorted(set((0, max(0, size // 2 - chunk_bytes // 2),
                          max(0, size - chunk_bytes))))
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with open(path, "rb", buffering=0) as handle:
        for offset in offsets:
            handle.seek(offset)
            data = handle.read(min(chunk_bytes, max(0, size - offset)))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    return digest.hexdigest()


def _process_alive(pid: int) -> bool:
    """Check a lock owner without sending a signal on Windows.

    ``os.kill(pid, 0)`` is the conventional POSIX existence check, but Python's
    Windows implementation treats most signal values as ``TerminateProcess``.
    Query the process handle directly there so stale-lock recovery can never
    stop an unrelated application.
    """
    pid = int(pid)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid)
            if not handle:
                # Access denied means a protected process exists; all other
                # common failures mean the PID is no longer present.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                        handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            # When in doubt, preserve the lock and let its age timeout recover.
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return getattr(exc, "errno", None) != errno.ESRCH


class _BuildFileLock:
    """Cross-process single-builder lock based on an atomic lock-file create."""

    def __init__(self, path: str, should_cancel: Callable[[], bool],
                 stale_seconds: float = DEFAULT_STALE_BUILD_SECONDS) -> None:
        self.path = path
        self.should_cancel = should_cancel
        self.stale_seconds = max(300.0, float(stale_seconds))
        self._owned = False

    def __enter__(self) -> "_BuildFileLock":
        while True:
            if self.should_cancel():
                raise AudioEvidenceCancelled("audio evidence caching cancelled")
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    owner_pid = None
                    try:
                        with open(self.path, encoding="ascii", errors="ignore") as handle:
                            for line in handle:
                                if line.startswith("pid="):
                                    owner_pid = int(line.split("=", 1)[1].strip())
                                    break
                    except (OSError, TypeError, ValueError):
                        owner_pid = None
                    owner_dead = (owner_pid is not None
                                  and not _process_alive(owner_pid))
                    if ((owner_dead and age > 1.0)
                            or (owner_pid is None and age > 300.0)
                            or age > self.stale_seconds):
                        os.remove(self.path)
                        continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
                time.sleep(0.08)
                continue
            try:
                payload = ("pid=%d\ncreated_ns=%d\n" %
                           (os.getpid(), time.time_ns())).encode("ascii")
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._owned = True
            return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._owned:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._owned = False
        return False


def prune_cache(cache_dir: str, *, max_bytes: int | None = None,
                max_entries: int | None = None,
                max_age_seconds: float | None = None,
                protected_keys: Iterable[str] = ()) -> dict[str, int]:
    """Remove stale/orphaned evidence and evict least-recently-used entries.

    The routine is deliberately best-effort: Windows may refuse to remove a
    memmapped entry owned by another process, in which case that entry is left
    untouched.  Only this cache's known suffixes are ever considered.
    """
    cache_dir = _cache_fs_path(cache_dir)
    max_bytes = (DEFAULT_MAX_CACHE_BYTES if max_bytes is None
                 else max(0, int(max_bytes)))
    max_entries = (DEFAULT_MAX_CACHE_ENTRIES if max_entries is None
                   else max(0, int(max_entries)))
    max_age_seconds = (DEFAULT_MAX_AGE_SECONDS if max_age_seconds is None
                       else max(0.0, float(max_age_seconds)))
    protected = {str(key) for key in protected_keys}
    result = {"entries_removed": 0, "bytes_removed": 0,
              "orphans_removed": 0}
    if not os.path.isdir(cache_dir):
        return result
    now_ns = time.time_ns()
    rows = []
    names = list(os.listdir(cache_dir))
    for name in names:
        path = os.path.join(cache_dir, name)
        if name.startswith((".pcm-", ".audio-evidence-")) and \
                name.endswith((".part", ".tmp")):
            try:
                if time.time() - os.path.getmtime(path) > 24 * 3600:
                    os.remove(path)
                    result["orphans_removed"] += 1
            except OSError:
                pass
            continue
        if name.endswith(".build.lock"):
            try:
                if time.time() - os.path.getmtime(path) > DEFAULT_STALE_BUILD_SECONDS:
                    os.remove(path)
                    result["orphans_removed"] += 1
            except OSError:
                pass
            continue
        if not name.endswith(".json"):
            continue
        key = name[:-5]
        raw_path = os.path.join(cache_dir, key + ".s16le")
        manifest_path = path
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            size = os.path.getsize(raw_path)
            valid = (isinstance(manifest, dict)
                     and manifest.get("schema") == SCHEMA
                     and manifest.get("completed") is True
                     and size > 0 and size % 2 == 0
                     and int(manifest.get("bytes") or -1) == size)
        except (OSError, ValueError, TypeError):
            valid = False
            size = 0
            manifest = {}
        if not valid:
            for candidate in (manifest_path, raw_path):
                try:
                    if os.path.isfile(candidate):
                        removed_size = os.path.getsize(candidate)
                        os.remove(candidate)
                        result["bytes_removed"] += removed_size
                except OSError:
                    pass
            result["orphans_removed"] += 1
            continue
        access_ns = int(manifest.get("last_access_ns")
                        or manifest.get("created_ns") or 0)
        if not access_ns:
            try:
                access_ns = int(os.path.getmtime(manifest_path) * 1_000_000_000)
            except OSError:
                access_ns = now_ns
        rows.append({"key": key, "raw": raw_path, "manifest": manifest_path,
                     "bytes": int(size), "access_ns": access_ns})

    # A raw file left behind before the atomic completion manifest is never
    # reusable.  Remove it once it is old enough that no active builder could
    # reasonably still be about to publish its manifest.
    manifest_keys = {row["key"] for row in rows}
    for name in names:
        if not name.endswith(".s16le"):
            continue
        key = name[:-6]
        if key in manifest_keys or key in protected:
            continue
        raw_path = os.path.join(cache_dir, name)
        try:
            if time.time() - os.path.getmtime(raw_path) > 3600:
                size = os.path.getsize(raw_path)
                os.remove(raw_path)
                result["bytes_removed"] += size
                result["orphans_removed"] += 1
        except OSError:
            pass

    rows.sort(key=lambda row: (row["access_ns"], row["key"]))
    total = sum(row["bytes"] for row in rows)
    count = len(rows)
    for row in rows:
        too_old = (max_age_seconds > 0 and
                   now_ns - row["access_ns"] > max_age_seconds * 1_000_000_000)
        over_limit = total > max_bytes or count > max_entries
        if not too_old and not over_limit:
            continue
        if row["key"] in protected:
            continue
        removed = 0
        ok = True
        for candidate in (row["manifest"], row["raw"]):
            try:
                if os.path.isfile(candidate):
                    removed += os.path.getsize(candidate)
                    os.remove(candidate)
            except OSError:
                ok = False
                break
        if ok:
            total -= row["bytes"]
            count -= 1
            result["entries_removed"] += 1
            result["bytes_removed"] += removed
    return result


class AudioEvidenceCache:
    """Persistent PCM cache with bounded-memory evidence reads.

    ``decoder`` is an optional test/embedding hook with signature
    ``decoder(source, track, sample_rate, output_path)``.  It must write raw
    little-endian signed PCM16 and return anything except ``False`` on success.
    """

    def __init__(self, source: str, tracks: Iterable[int] | None = None,
                 sample_rate: int = 16000, cache_dir: str | None = None,
                 ffmpeg: str | None = None,
                 should_cancel: Callable[[], bool] | None = None,
                 decoder: Callable[[str, int, int, str], Any] | None = None,
                 cleanup_on_close: bool = False,
                 algorithm_version: str = CACHE_ALGORITHM_VERSION,
                 verify_fingerprint: bool = True,
                 fingerprint_bytes: int = 65536,
                 max_cache_bytes: int | None = None,
                 max_cache_entries: int | None = None,
                 max_cache_age_seconds: float | None = None,
                 prune_on_prepare: bool = True) -> None:
        self.source = os.path.normcase(os.path.realpath(os.path.abspath(
            os.fspath(source))))
        if not os.path.isfile(self.source):
            raise FileNotFoundError(self.source)
        self.tracks = _normalise_tracks(tracks)
        self.sample_rate = int(sample_rate)
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.cache_dir = _cache_fs_path(cache_dir or _default_cache_dir())
        self.ffmpeg = ffmpeg
        self.should_cancel = should_cancel or (lambda: False)
        self.decoder = decoder
        self.cleanup_on_close = bool(cleanup_on_close)
        self.algorithm_version = str(algorithm_version or CACHE_ALGORITHM_VERSION)
        self.verify_fingerprint = bool(verify_fingerprint)
        self.fingerprint_bytes = max(4096, int(fingerprint_bytes))
        self.max_cache_bytes = max_cache_bytes
        self.max_cache_entries = max_cache_entries
        self.max_cache_age_seconds = max_cache_age_seconds
        self.prune_on_prepare = bool(prune_on_prepare)
        self._maps: dict[int, np.memmap] = {}
        self._entries: dict[int, dict[str, Any]] = {}
        self._fingerprint_state: tuple[int, int, str] | None = None
        self._instance_lock = threading.RLock()
        self._pruned = False
        self._closed = False
        os.makedirs(self.cache_dir, exist_ok=True)

    def __enter__(self) -> "AudioEvidenceCache":
        self.prepare()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    def _cancel_check(self) -> None:
        if self.should_cancel():
            raise AudioEvidenceCancelled("audio evidence caching cancelled")

    def _identity(self, track: int) -> dict[str, Any]:
        stat = os.stat(self.source)
        size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
        fingerprint = None
        if self.verify_fingerprint:
            cached = self._fingerprint_state
            if cached is not None and cached[:2] == (size, mtime_ns):
                fingerprint = cached[2]
            else:
                fingerprint = _fast_file_fingerprint(
                    self.source, self.fingerprint_bytes)
                self._fingerprint_state = (size, mtime_ns, fingerprint)
        identity = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": self.algorithm_version,
            "source": self.source,
            "source_size": size,
            "source_mtime_ns": mtime_ns,
            "track": int(track),
            "sample_rate": self.sample_rate,
            "channels": 1,
            "sample_format": "s16le",
        }
        if fingerprint:
            identity["source_fingerprint"] = fingerprint
            identity["fingerprint_mode"] = "sampled-sha256-v1"
            identity["fingerprint_bytes"] = self.fingerprint_bytes
        return identity

    def _paths(self, track: int) -> tuple[str, str, str, dict[str, Any]]:
        identity = self._identity(track)
        key = hashlib.sha256(_canonical(identity)).hexdigest()
        stem = os.path.join(self.cache_dir, key)
        return stem + ".s16le", stem + ".json", key, identity

    @staticmethod
    def _load_manifest(path: str) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _valid_entry(self, raw_path: str, manifest_path: str,
                     identity: Mapping[str, Any]) -> dict[str, Any] | None:
        manifest = self._load_manifest(manifest_path)
        if not manifest or manifest.get("completed") is not True:
            return None
        if manifest.get("identity") != dict(identity):
            return None
        try:
            size = os.path.getsize(raw_path)
        except OSError:
            return None
        if size <= 0 or size % 2 or int(manifest.get("bytes") or -1) != size:
            return None
        if int(manifest.get("sample_count") or -1) != size // 2:
            return None
        return manifest

    def _touch_manifest(self, path: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(manifest)
        now_ns = time.time_ns()
        previous = int(value.get("last_access_ns") or value.get("created_ns") or 0)
        if now_ns - previous < ACCESS_TOUCH_INTERVAL_NS:
            return value
        value["last_access_ns"] = now_ns
        try:
            _atomic_json(path, value)
        except OSError:
            # Reuse must not fail merely because a read-only/near-full cache
            # directory could not persist its approximate LRU timestamp.
            return dict(manifest)
        return value

    def _maybe_prune(self) -> None:
        if self._pruned or not self.prune_on_prepare:
            return
        self._pruned = True
        now = time.monotonic()
        with _PRUNE_GUARD:
            previous = _LAST_PRUNE.get(self.cache_dir, 0.0)
            if now - previous < 60.0:
                return
            _LAST_PRUNE[self.cache_dir] = now
        try:
            protected = [self._paths(track)[2] for track in self.tracks]
            prune_cache(
                self.cache_dir, max_bytes=self.max_cache_bytes,
                max_entries=self.max_cache_entries,
                max_age_seconds=self.max_cache_age_seconds,
                protected_keys=protected)
        except OSError:
            pass

    def _decode_ffmpeg(self, track: int, output_path: str) -> None:
        command = [
            self.ffmpeg or _default_ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-nostdin", "-y", "-i", self.source,
            "-map", "0:a:%d" % track, "-vn", "-ac", "1",
            "-ar", str(self.sample_rate), "-c:a", "pcm_s16le",
            "-f", "s16le", output_path,
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        try:
            while process.poll() is None:
                if self.should_cancel():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise AudioEvidenceCancelled(
                        "audio evidence caching cancelled on track %d" % track)
                time.sleep(0.05)
            _stdout, stderr = process.communicate()
        except Exception:
            if process.poll() is None:
                process.kill()
                process.communicate()
            raise
        if process.returncode != 0:
            message = (stderr or b"").decode("utf-8", "replace")[-1200:]
            raise AudioEvidenceError(
                "audio track %d decode failed: %s" % (track, message))

    def _build(self, track: int, raw_path: str, manifest_path: str,
               identity: Mapping[str, Any]) -> dict[str, Any]:
        self._cancel_check()
        fd, part = tempfile.mkstemp(prefix=".pcm-", suffix=".part",
                                    dir=self.cache_dir)
        os.close(fd)
        try:
            if self.decoder is not None:
                result = self.decoder(self.source, track, self.sample_rate, part)
                if result is False:
                    raise AudioEvidenceError(
                        "audio decoder rejected track %d" % track)
            else:
                self._decode_ffmpeg(track, part)
            self._cancel_check()
            size = os.path.getsize(part)
            if size <= 0 or size % 2:
                raise AudioEvidenceError(
                    "decoded PCM for track %d is empty or truncated" % track)
            os.replace(part, raw_path)
            manifest = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "identity": dict(identity),
                "bytes": size,
                "sample_count": size // 2,
                "duration": round((size // 2) / float(self.sample_rate), 9),
                "created_ns": time.time_ns(),
                "last_access_ns": time.time_ns(),
                "completed": True,
            }
            _atomic_json(manifest_path, manifest)
            return manifest
        except Exception:
            # A raw file without its completion manifest is intentionally not
            # reusable, but remove it now to avoid wasting disk after failure.
            for path in (part, raw_path, manifest_path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            raise
        finally:
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass

    def prepare_track(self, track: int) -> dict[str, Any]:
        with self._instance_lock:
            if self._closed:
                raise AudioEvidenceError("audio evidence cache is closed")
            track = int(track)
            if track not in self.tracks:
                raise KeyError("track %d was not selected" % track)
            if track in self._entries:
                return dict(self._entries[track])
            raw_path, manifest_path, key, identity = self._paths(track)
            with _entry_lock(key):
                manifest = self._valid_entry(raw_path, manifest_path, identity)
                reused = manifest is not None
                if manifest is None:
                    lock_path = os.path.join(self.cache_dir,
                                             key + ".build.lock")
                    with _BuildFileLock(lock_path, self.should_cancel):
                        # Another process may have completed the entry while
                        # this process waited for the single-builder lock.
                        manifest = self._valid_entry(
                            raw_path, manifest_path, identity)
                        reused = manifest is not None
                        if manifest is None:
                            manifest = self._build(
                                track, raw_path, manifest_path, identity)
                self._cancel_check()
                manifest = self._touch_manifest(manifest_path, manifest)
                memmap = np.memmap(raw_path, dtype="<i2", mode="r",
                                   shape=(int(manifest["sample_count"]),))
                entry = {
                    "track": track, "key": key, "raw_path": raw_path,
                    "manifest_path": manifest_path,
                    "sample_count": int(manifest["sample_count"]),
                    "duration": float(manifest["duration"]),
                    "sample_rate": self.sample_rate, "reused": reused,
                    "algorithm_version": self.algorithm_version,
                    "source_fingerprint": identity.get("source_fingerprint"),
                }
                self._maps[track] = memmap
                self._entries[track] = entry
            self._maybe_prune()
            return dict(entry)

    def prepare(self) -> dict[int, dict[str, Any]]:
        for track in self.tracks:
            self._cancel_check()
            self.prepare_track(track)
        return {track: dict(self._entries[track]) for track in self.tracks}

    def entry(self, track: int) -> dict[str, Any]:
        return self.prepare_track(track)

    def review_samples(self, track: int, start: float, end: float) -> np.ndarray:
        """Read one absolute-time window as a bounded float32 copy."""
        entry = self.prepare_track(track)
        start = max(0.0, float(start))
        end = min(float(entry["duration"]), float(end))
        if end <= start:
            return np.zeros(0, dtype=np.float32)
        first = max(0, int(np.floor(start * self.sample_rate)))
        last = min(int(entry["sample_count"]),
                   int(np.ceil(end * self.sample_rate)))
        raw = self._maps[int(track)][first:last]
        return raw.astype(np.float32, copy=True) / 32768.0

    def review_mix(self, tracks: Iterable[int], start: float,
                   end: float) -> np.ndarray:
        """Read and sum aligned track windows, matching ffmpeg amix/no-normalize.

        Shorter streams are zero-padded to the longest selected stream.  The
        final result is clipped once to PCM's legal range, which is equivalent
        to the old decoded mixed-WAV path while still decoding every source
        stream only once.
        """
        selected = _normalise_tracks(tracks)
        missing = [track for track in selected if track not in self.tracks]
        if missing:
            raise KeyError("tracks were not selected: %s" % missing)
        arrays = [self.review_samples(track, start, end) for track in selected]
        length = max((array.size for array in arrays), default=0)
        if not length:
            return np.zeros(0, dtype=np.float32)
        mixed = np.zeros(length, dtype=np.float32)
        for array in arrays:
            mixed[:array.size] += array
        return np.clip(mixed, -1.0, 32767.0 / 32768.0)

    def activity_intervals(self, track: int, start: float, end: float,
                           config=None, kind: str = "speech") -> list[dict[str, Any]]:
        """Detect local activity and translate it back to absolute time."""
        entry = self.prepare_track(track)
        clipped_start = max(0.0, float(start))
        clipped_end = min(float(entry["duration"]), float(end))
        samples = self.review_samples(track, clipped_start, clipped_end)
        if samples.size == 0:
            return []
        from boundary_refiner import detect_activity_intervals
        rows = detect_activity_intervals(
            samples, self.sample_rate, config=config,
            track=str(track), kind=str(kind))
        for row in rows:
            row["start"] = round(clipped_start + float(row["start"]), 9)
            row["end"] = round(clipped_start + float(row["end"]), 9)
            row["cache_key"] = str(entry["key"])
        return rows

    def close(self) -> None:
        with self._instance_lock:
            if self._closed:
                return
            for track in list(self._maps):
                mmap = self._maps.pop(track)
                try:
                    mmap._mmap.close()  # NumPy exposes no public close method.
                except (AttributeError, OSError):
                    pass
            self._closed = True
            if self.cleanup_on_close:
                self.purge()

    def purge(self) -> None:
        """Release mappings and remove only entries owned by this instance."""
        if not self._closed:
            for track in list(self._maps):
                mmap = self._maps.pop(track)
                try:
                    mmap._mmap.close()
                except (AttributeError, OSError):
                    pass
        owned = [(entry.get("raw_path"), entry.get("manifest_path"))
                 for entry in self._entries.values()]
        if not owned:
            owned = [(self._paths(track)[0], self._paths(track)[1])
                     for track in self.tracks]
        for raw_path, manifest_path in owned:
            for path in (manifest_path, raw_path):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
        self._entries.clear()
        self._closed = True


__all__ = [
    "AudioEvidenceCache", "AudioEvidenceCancelled", "AudioEvidenceError",
    "CACHE_ALGORITHM_VERSION", "CREATE_NO_WINDOW", "SCHEMA", "SCHEMA_VERSION",
    "prune_cache",
]
