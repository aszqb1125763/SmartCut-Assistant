# -*- coding: utf-8 -*-
"""Persistent low-resolution source proxy shared by visual review stages.

The first consumer creates one 2-fps proxy for a source file.  Later consumers
read the same frames by absolute source time instead of launching an FFmpeg
seek/decode for every requested picture.  This is deliberately a *review proxy*;
it must not be used for frame-accurate edit boundaries or final rendering.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

import numpy as np
import media_identity

SCHEMA = "smartcut.video-frame-proxy"
SCHEMA_VERSION = 1
ALGORITHM_VERSION = "ffmpeg-fps2-scale384-jpeg5-v1"
PROXY_FPS = 2.0
PROXY_WIDTH = 384
JPEG_QUALITY = 5
DEFAULT_MAX_CACHE_BYTES = 12 * 1024 ** 3
DEFAULT_MAX_CACHE_ENTRIES = 32
DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600
ACCESS_TOUCH_INTERVAL_NS = 3600 * 1_000_000_000
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()
_PRUNE_GUARD = threading.Lock()
_LAST_PRUNE: dict[str, float] = {}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class VideoFrameCacheError(RuntimeError):
    """Proxy creation or validation failed."""


class VideoFrameCacheCancelled(VideoFrameCacheError):
    """The owning workflow requested cancellation during ingest."""


def _cache_fs_path(path: str) -> str:
    """Keep cache locks, staging frames and manifests usable past MAX_PATH.

    Only internal cache directories receive this Win32 namespace prefix;
    source paths and the identity hashed into cache keys remain unchanged.
    """
    path = os.path.abspath(os.fspath(path))
    if os.name != "nt" or path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _folder_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def prune_cache(cache_dir: str, max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
                max_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
                max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
                protected_keys=()) -> dict[str, int]:
    """Remove least-recently-used source proxies without touching active builds."""
    cache_dir = _cache_fs_path(cache_dir)
    protected = {str(key) for key in protected_keys or ()}
    result = {"entries_removed": 0, "bytes_removed": 0}
    now_ns = time.time_ns()
    rows = []
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return result
    # A hard process crash can leave a staging directory before the atomic
    # publish.  It is never a valid cache entry and may be reclaimed after the
    # same stale-build horizon used by the lock files.
    for name in list(names):
        if not name.startswith(".proxy-"):
            continue
        path = os.path.join(cache_dir, name)
        try:
            if (os.path.isdir(path)
                    and time.time() - os.path.getmtime(path) > 12 * 3600):
                removed = _folder_size(path)
                shutil.rmtree(path)
                result["bytes_removed"] += removed
        except OSError:
            pass
    for name in names:
        folder = os.path.join(cache_dir, name)
        if not os.path.isdir(folder) or len(name) != 64:
            continue
        access = 0
        for manifest_name in ("access.json", os.path.join("proxy", "manifest.json")):
            try:
                with open(os.path.join(folder, manifest_name), encoding="utf-8") as handle:
                    value = json.load(handle)
                access = max(access, int(value.get("last_access_ns") or
                                         value.get("created_ns") or 0))
            except (OSError, ValueError, TypeError):
                pass
        if not access:
            try:
                access = os.stat(folder).st_mtime_ns
            except OSError:
                access = 0
        cached_bytes = None
        try:
            with open(os.path.join(folder, "access.json"), encoding="utf-8") as handle:
                cached_bytes = int(json.load(handle).get("entry_bytes"))
        except (OSError, ValueError, TypeError):
            pass
        if cached_bytes is None:
            try:
                with open(os.path.join(folder, "proxy", "manifest.json"),
                          encoding="utf-8") as handle:
                    cached_bytes = int(json.load(handle).get("entry_bytes"))
            except (OSError, ValueError, TypeError):
                # Only legacy/incomplete entries pay one fallback walk; all new
                # manifests keep an exact byte counter.
                cached_bytes = _folder_size(folder)
        rows.append({"key": name, "path": folder,
                     "bytes": max(0, int(cached_bytes)), "access_ns": access})
    rows.sort(key=lambda row: (row["access_ns"], row["key"]))
    total, count = sum(row["bytes"] for row in rows), len(rows)
    for row in rows:
        too_old = (max_age_seconds > 0 and
                   now_ns - row["access_ns"] > max_age_seconds * 1_000_000_000)
        over_limit = total > max_bytes or count > max_entries
        if (not too_old and not over_limit) or row["key"] in protected:
            continue
        if os.path.exists(os.path.join(cache_dir, row["key"] + ".build.lock")):
            continue
        exact = os.path.join(row["path"], "exact")
        try:
            if any(name.endswith(".build.lock") for name in os.listdir(exact)):
                continue
        except OSError:
            pass
        try:
            shutil.rmtree(row["path"])
        except OSError:
            continue
        total -= row["bytes"]
        count -= 1
        result["entries_removed"] += 1
        result["bytes_removed"] += row["bytes"]
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _default_cache_dir() -> str:
    try:
        import index as indexer
        root = indexer.work_root()
    except Exception:  # noqa: BLE001
        root = tempfile.gettempdir()
    return os.path.join(root, "_cache", "video_frame_proxy")


def _default_ffmpeg() -> str:
    import index as indexer
    return indexer.find_ffmpeg()


def _atomic_json(path: str, payload: dict[str, Any]) -> None:
    folder = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".video-proxy-", suffix=".tmp",
                                     dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                      indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


class VideoFrameCache:
    """A disk-backed source-frame proxy, keyed by sampled source fingerprint.

    The proxy has a fixed 2-fps, 384-pixel-wide profile to maximize reuse among
    the editor, chapter review, card thumbnails and director.  Requested times
    resolve to the nearest cached frame (maximum nominal error 0.25 s). Callers
    that need a precise cut point must use the source timeline/audio evidence,
    not infer timing from this review proxy.
    """

    def __init__(self, source: str, cache_dir: str | None = None,
                 ffmpeg: str | None = None,
                 should_cancel: Callable[[], bool] | None = None,
                 decoder: Callable[[str, str], Any] | None = None) -> None:
        self.source = os.path.normcase(os.path.realpath(os.path.abspath(
            os.fspath(source))))
        if not os.path.isfile(self.source):
            raise FileNotFoundError(self.source)
        self.cache_dir = _cache_fs_path(cache_dir or _default_cache_dir())
        self.ffmpeg = ffmpeg or _default_ffmpeg()
        self.should_cancel = should_cancel or (lambda: False)
        self.decoder = decoder
        self._lock = threading.RLock()
        self._manifest: dict[str, Any] | None = None
        self._manifest_reused = False
        self._requested: set[int] = set()
        self._fingerprint: str | None = None
        self._identity: dict[str, Any] | None = None
        os.makedirs(self.cache_dir, exist_ok=True)
        public_identity = media_identity.source_identity(
            self.source, include_path=False)
        self._identity = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "source_size": int(public_identity["source_size"]),
            "source_mtime_ns": int(public_identity["source_mtime_ns"]),
            "fps": PROXY_FPS,
            "width": PROXY_WIDTH,
            "jpeg_quality": JPEG_QUALITY,
            "source_identity": public_identity,
        }
        self._fingerprint = str(public_identity["source_fingerprint"])
        self._identity["source_fingerprint"] = self._fingerprint
        self.cache_key = hashlib.sha256(_canonical(self._identity)).hexdigest()
        self.entry_dir = os.path.join(self.cache_dir, self.cache_key)
        self.proxy_dir = os.path.join(self.entry_dir, "proxy")
        self.exact_dir = os.path.join(self.entry_dir, "exact")
        self.manifest_path = os.path.join(self.proxy_dir, "manifest.json")
        self.access_path = os.path.join(self.entry_dir, "access.json")

    @property
    def audit(self) -> dict[str, Any]:
        """Non-sensitive cache provenance suitable for workflow reports."""
        return {
            "schema": SCHEMA,
            "cache_key": self.cache_key,
            "source_fingerprint": self._fingerprint,
            "reused": bool(self._manifest_reused),
            "algorithm_version": ALGORITHM_VERSION,
            "fps": PROXY_FPS,
            "width": PROXY_WIDTH,
            "frame_count": int((self._manifest or {}).get("frame_count") or 0),
        }

    def _cancel_check(self) -> None:
        if self.should_cancel():
            raise VideoFrameCacheCancelled("video frame proxy creation cancelled")

    def _verify_source_identity(self) -> None:
        current = media_identity.source_identity(self.source, include_path=False)
        if not media_identity.matches(self._identity.get("source_identity"),
                                      current):
            raise VideoFrameCacheError(
                "source changed while visual evidence was being created")

    def _load_valid(self) -> dict[str, Any] | None:
        try:
            with open(self.manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            if (manifest.get("schema") != SCHEMA
                    or manifest.get("schema_version") != SCHEMA_VERSION
                    or manifest.get("identity") != self._identity
                    or manifest.get("completed") is not True):
                return None
            count = int(manifest.get("frame_count") or 0)
            if count <= 0:
                return None
            # Spot-check both ends and the middle; incomplete/corrupt builds are
            # never surfaced as a completed reusable proxy.
            for index in sorted(set((0, count // 2, count - 1))):
                frame = self._frame_path(index)
                if not os.path.isfile(frame) or os.path.getsize(frame) < 128:
                    return None
            return manifest
        except (OSError, ValueError, TypeError):
            return None

    def _frame_path(self, index: int) -> str:
        # ffmpeg image sequences start at 1.
        return os.path.join(self.proxy_dir, "frame_%06d.jpg" % (index + 1))

    def _touch_access(self, proxy_bytes: int | None = None,
                      exact_added_bytes: int = 0) -> None:
        now = time.time_ns()
        previous = 0
        value = {}
        try:
            with open(self.access_path, encoding="utf-8") as handle:
                value = json.load(handle)
                previous = int(value.get("last_access_ns") or 0)
        except (OSError, ValueError, TypeError):
            pass
        if (now - previous < ACCESS_TOUCH_INTERVAL_NS
                and proxy_bytes is None and not exact_added_bytes):
            return
        os.makedirs(self.entry_dir, exist_ok=True)
        if proxy_bytes is not None:
            value["proxy_bytes"] = max(0, int(proxy_bytes))
        if exact_added_bytes:
            value["exact_bytes"] = max(0, int(value.get("exact_bytes") or 0)
                                       + int(exact_added_bytes))
        value.update({
            "schema": SCHEMA, "cache_key": self.cache_key,
            "source_fingerprint": self._fingerprint,
            "last_access_ns": now,
        })
        value["entry_bytes"] = (max(0, int(value.get("proxy_bytes") or 0))
                                + max(0, int(value.get("exact_bytes") or 0)))
        try:
            _atomic_json(self.access_path, value)
        except OSError:
            pass

    def _build(self) -> dict[str, Any]:
        self._cancel_check()
        staging = tempfile.mkdtemp(prefix=".proxy-%s-" % self.cache_key[:10],
                                   dir=self.cache_dir)
        try:
            if self.decoder is not None:
                result = self.decoder(self.source, staging)
                if result is False:
                    raise VideoFrameCacheError("injected frame decoder failed")
            else:
                output = os.path.join(staging, "frame_%06d.jpg")
                command = [
                    self.ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-nostdin", "-y", "-i", self.source,
                    "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vf", "fps=%g,scale=%d:-2:flags=area" %
                    (PROXY_FPS, PROXY_WIDTH),
                    "-q:v", str(JPEG_QUALITY), output,
                ]
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                started = time.monotonic()
                while process.poll() is None:
                    if self.should_cancel():
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise VideoFrameCacheCancelled(
                            "video frame proxy creation cancelled")
                    if time.monotonic() - started > 4 * 3600:
                        process.kill()
                        raise VideoFrameCacheError(
                            "video frame proxy exceeded 4-hour timeout")
                    time.sleep(0.1)
                _stdout, stderr = process.communicate()
                if process.returncode != 0:
                    message = (stderr or b"").decode("utf-8", "replace")[-1200:]
                    raise VideoFrameCacheError("ffmpeg proxy decode failed: " + message)
            self._cancel_check()
            names = sorted(name for name in os.listdir(staging)
                           if name.startswith("frame_") and name.endswith(".jpg"))
            if not names:
                raise VideoFrameCacheError("ffmpeg produced no proxy frames")
            for name in (names[0], names[len(names) // 2], names[-1]):
                if os.path.getsize(os.path.join(staging, name)) < 128:
                    raise VideoFrameCacheError("proxy contains an invalid frame")
            entry_bytes = sum(os.path.getsize(os.path.join(staging, name))
                              for name in names)
            manifest = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "identity": self._identity,
                "frame_count": len(names),
                "entry_bytes": int(entry_bytes),
                "created_ns": time.time_ns(),
                "completed": True,
            }
            self._verify_source_identity()
            manifest["last_access_ns"] = time.time_ns()
            _atomic_json(os.path.join(staging, "manifest.json"), manifest)
            os.makedirs(self.entry_dir, exist_ok=True)
            if os.path.isdir(self.proxy_dir):
                shutil.rmtree(self.proxy_dir, ignore_errors=True)
            os.replace(staging, self.proxy_dir)
            self._touch_access(proxy_bytes=entry_bytes)
            return manifest
        finally:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)

    def prepare(self) -> dict[str, Any]:
        if self._manifest is not None:
            return self.audit
        with self._lock:
            if self._manifest is not None:
                return self.audit
            with _BUILD_LOCKS_GUARD:
                lock = _BUILD_LOCKS.setdefault(self.cache_key, threading.Lock())
            with lock:
                with _PRUNE_GUARD:
                    now = time.monotonic()
                    if now - _LAST_PRUNE.get(self.cache_dir, 0.0) >= 3600.0:
                        _LAST_PRUNE[self.cache_dir] = now
                        prune_cache(self.cache_dir,
                                    protected_keys=(self.cache_key,))
                self._manifest = self._load_valid()
                self._manifest_reused = self._manifest is not None
                if self._manifest is None:
                    # Cross-process lock: an exclusive lock file prevents two
                    # workflow processes from decoding the same source at once.
                    lock_path = os.path.join(self.cache_dir,
                                             self.cache_key + ".build.lock")
                    owner = False
                    while not owner:
                        self._cancel_check()
                        try:
                            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            with os.fdopen(fd, "w", encoding="ascii") as handle:
                                handle.write("%d %d\n" % (os.getpid(), time.time_ns()))
                            owner = True
                        except FileExistsError:
                            # Dead builds older than 12 hours can be reclaimed.
                            try:
                                age = time.time() - os.path.getmtime(lock_path)
                                if age > 12 * 3600:
                                    os.remove(lock_path)
                                    continue
                            except OSError:
                                continue
                            time.sleep(0.1)
                    try:
                        self._manifest = self._load_valid()
                        if self._manifest is None:
                            self._manifest = self._build()
                            self._manifest_reused = False
                        else:
                            self._manifest_reused = True
                    finally:
                        try:
                            os.remove(lock_path)
                        except OSError:
                            pass
            self._touch_access(proxy_bytes=int(
                (self._manifest or {}).get("entry_bytes") or 0))
        return self.audit

    def frame_path_at(self, seconds: float) -> tuple[str | None, dict[str, Any]]:
        """Return nearest cached JPEG plus auditable request/cache metadata."""
        self.prepare()
        seconds = max(0.0, float(seconds))
        index = int(round(seconds * PROXY_FPS))
        index = min(index, int(self._manifest["frame_count"]) - 1)
        path = self._frame_path(index)
        valid = os.path.isfile(path) and os.path.getsize(path) > 128
        reused = bool(self._manifest_reused or self._requested)
        self._requested.add(index)
        info = {
            "cache_key": self.cache_key,
            "source_fingerprint": self._fingerprint,
            "reused": reused,
            "requested_time": round(seconds, 6),
            "actual_time": round(index / PROXY_FPS, 6),
            "time_error": round(index / PROXY_FPS - seconds, 6),
            "frame_index": index,
            "precision": "review_proxy_nearest_0.5s",
        }
        return (path if valid else None), info

    def frame_at(self, seconds: float):
        """Return a BGR uint8 image and metadata; OpenCV stays an optional import."""
        path, info = self.frame_path_at(seconds)
        if not path:
            return None, info
        try:
            import cv2
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:  # noqa: BLE001
            image = None
        return image, info

    def exact_frame_path_at(self, seconds: float, width: int = PROXY_WIDTH,
                            quality: int = 4) -> tuple[str | None, dict[str, Any]]:
        """Cache one source-accurate seek for seam/start/end inspection.

        Unlike :meth:`frame_path_at`, this does not quantize to the sparse proxy
        grid.  Each exact source timestamp is decoded once and then shared by all
        later roles/runs.  It is intentionally used only for precision-sensitive
        nodes so normal context review still costs one sequential ingest scan.
        """
        seconds = max(0.0, float(seconds))
        width, quality = max(32, int(width)), max(2, min(31, int(quality)))
        token_payload = {"source_cache_key": self.cache_key,
                         "time_us": int(round(seconds * 1_000_000)),
                         "width": width, "quality": quality,
                         "algorithm": "exact-seek-jpeg-v1"}
        token = hashlib.sha256(_canonical(token_payload)).hexdigest()
        os.makedirs(self.exact_dir, exist_ok=True)
        output = os.path.join(self.exact_dir, token + ".jpg")
        existed = os.path.isfile(output) and os.path.getsize(output) > 128
        if not existed:
            with _BUILD_LOCKS_GUARD:
                lock = _BUILD_LOCKS.setdefault("exact:" + token,
                                               threading.Lock())
            with lock:
                existed = os.path.isfile(output) and os.path.getsize(output) > 128
                if not existed:
                    lock_path = output + ".build.lock"
                    owner = False
                    while not owner:
                        self._cancel_check()
                        try:
                            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            os.close(fd)
                            owner = True
                        except FileExistsError:
                            try:
                                if time.time() - os.path.getmtime(lock_path) > 3600:
                                    os.remove(lock_path)
                                    prefix = token + "."
                                    for name in os.listdir(self.exact_dir):
                                        if name.startswith(prefix) and name.endswith(
                                                ".part.jpg"):
                                            try:
                                                os.remove(os.path.join(self.exact_dir,
                                                                       name))
                                            except OSError:
                                                pass
                                    continue
                            except OSError:
                                continue
                            time.sleep(0.05)
                    part = output + ".%d.part.jpg" % os.getpid()
                    try:
                        # A process may have completed this frame while we waited.
                        if os.path.isfile(output) and os.path.getsize(output) > 128:
                            existed = True
                        else:
                            command = [
                                self.ffmpeg, "-hide_banner", "-loglevel", "error",
                                "-nostdin", "-y", "-ss", "%.6f" % seconds,
                                "-i", self.source, "-map", "0:v:0", "-an", "-sn",
                                "-dn", "-frames:v", "1", "-vf",
                                "scale=%d:-2:flags=area" % width,
                                "-q:v", str(quality), part,
                            ]
                            result = subprocess.run(
                                command, capture_output=True, timeout=180,
                                creationflags=CREATE_NO_WINDOW)
                            if (result.returncode != 0 or not os.path.isfile(part)
                                    or os.path.getsize(part) <= 128):
                                message = (result.stderr or b"").decode(
                                    "utf-8", "replace")[-800:]
                                raise VideoFrameCacheError(
                                    "exact source frame decode failed: " + message)
                            self._verify_source_identity()
                            os.replace(part, output)
                            self._touch_access(
                                exact_added_bytes=os.path.getsize(output))
                    finally:
                        for path in (part, lock_path):
                            try:
                                if os.path.exists(path):
                                    os.remove(path)
                            except OSError:
                                pass
        self._verify_source_identity()
        self._touch_access()
        valid = os.path.isfile(output) and os.path.getsize(output) > 128
        info = {
            "cache_key": token,
            "source_cache_key": self.cache_key,
            "source_fingerprint": self._fingerprint,
            "reused": bool(existed),
            "requested_time": round(seconds, 6),
            "actual_time": None,
            "time_error": None,
            "precision": "source_seek_nearest_frame",
            "width": width,
        }
        return (output if valid else None), info

    def exact_frame_at(self, seconds: float, width: int = PROXY_WIDTH,
                       quality: int = 4):
        path, info = self.exact_frame_path_at(seconds, width=width,
                                              quality=quality)
        if not path:
            return None, info
        try:
            import cv2
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8),
                                 cv2.IMREAD_COLOR)
        except Exception:  # noqa: BLE001
            image = None
        return image, info

    def frames(self, start: float, end: float, fps: float = PROXY_FPS,
               width: int = PROXY_WIDTH, limit: int = 240):
        """Return proxy frames over an interval; output timestamps are actual proxy times."""
        self.prepare()
        start, end = max(0.0, float(start)), max(0.0, float(end))
        if end <= start:
            return [], []
        stride = max(1, int(round(PROXY_FPS / max(0.1, float(fps)))))
        maximum = int(self._manifest["frame_count"]) - 1
        first = min(maximum, int(round(start * PROXY_FPS)))
        last = min(maximum, int(round(end * PROXY_FPS)))
        indices = list(range(first, last + 1, stride))
        if len(indices) > int(limit):
            positions = np.linspace(0, len(indices) - 1, int(limit)).round().astype(int)
            indices = [indices[int(position)] for position in positions]
        import cv2
        times, images = [], []
        for index in indices:
            index = min(index, int(self._manifest["frame_count"]) - 1)
            path = self._frame_path(index)
            buf = np.fromfile(path, dtype=np.uint8)
            image = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
            if image is None:
                continue
            if int(width) != PROXY_WIDTH:
                height = max(2, round(image.shape[0] * int(width) / image.shape[1]))
                image = cv2.resize(image, (int(width), height),
                                   interpolation=cv2.INTER_AREA)
            times.append(index / PROXY_FPS)
            images.append(image)
            self._requested.add(index)
        return times, images

    def iter_frames(self, start: float = 0.0, end: float | None = None,
                    fps: float = PROXY_FPS, width: int = PROXY_WIDTH):
        """Yield proxy frames sequentially without retaining the whole film."""
        self.prepare()
        maximum = int(self._manifest["frame_count"]) - 1
        start = max(0.0, float(start))
        end = maximum / PROXY_FPS if end is None else max(start, float(end))
        first = min(maximum, int(round(start * PROXY_FPS)))
        last = min(maximum, int(round(end * PROXY_FPS)))
        stride = max(1, int(round(PROXY_FPS / max(0.1, float(fps)))))
        import cv2
        for index in range(first, last + 1, stride):
            path = self._frame_path(index)
            try:
                buf = np.fromfile(path, dtype=np.uint8)
                image = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
            except OSError:
                image = None
            if image is None:
                continue
            if int(width) != PROXY_WIDTH:
                height = max(2, round(image.shape[0] * int(width) /
                                      image.shape[1]))
                image = cv2.resize(image, (int(width), height),
                                   interpolation=cv2.INTER_AREA)
            self._requested.add(index)
            yield index / PROXY_FPS, image

    def analyze_motion(self, step: float = 1.0, small_width: int = 96):
        """Compute deterministic grayscale diffs from the shared proxy frames."""
        self.prepare()
        duration = max(0.0, (int(self._manifest["frame_count"]) - 1) / PROXY_FPS)
        step = max(0.1, float(step))
        count = max(1, int(duration / step) + 1)
        import cv2
        times, grays = [], []
        for index in range(count):
            requested = min(duration, index * step)
            path, _meta = self.frame_path_at(requested)
            if not path:
                continue
            buf = np.fromfile(path, dtype=np.uint8)
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
            if frame is None:
                continue
            height = max(2, int(frame.shape[0] * int(small_width) / frame.shape[1]))
            gray = cv2.cvtColor(cv2.resize(frame, (int(small_width), height)),
                                cv2.COLOR_BGR2GRAY)
            times.append(requested)
            grays.append(gray)
        diffs = []
        previous = None
        for gray in grays:
            if previous is not None and previous.shape == gray.shape:
                diffs.append(float(np.abs(gray.astype(np.int16) -
                                          previous.astype(np.int16)).mean()) / 64.0)
            else:
                diffs.append(0.0)
            previous = gray
        return times, diffs


__all__ = ["VideoFrameCache", "VideoFrameCacheError", "VideoFrameCacheCancelled",
           "PROXY_FPS",
           "PROXY_WIDTH", "ALGORITHM_VERSION", "SCHEMA", "SCHEMA_VERSION"]
