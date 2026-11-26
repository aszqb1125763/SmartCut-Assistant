# -*- coding: utf-8 -*-
"""Stable, inexpensive identities for source media files.

The editor keeps several long-lived artifacts (neutral index, conformed PCM,
review frames and role state).  A pathname alone is not a safe cache key: an
OBS recording can be replaced in-place while an old transcript still exists.
This module gives every consumer the same source identity contract.

Only small samples from the beginning, middle and end are hashed, so checking
a multi-gigabyte recording does not decode or scan the whole file.  Size and
nanosecond mtime are deliberately part of the identity as conservative cache
invalidation signals.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from typing import Any

SCHEMA = "smartcut.media-identity"
SCHEMA_VERSION = 1
FINGERPRINT_MODE = "sampled-sha256-v1"
DEFAULT_SAMPLE_BYTES = 65536

_CACHE: dict[tuple[str, int, int, int], str] = {}
_CACHE_LOCK = threading.Lock()


def normalized_path(path: str) -> str:
    """Return the canonical local pathname used for source binding."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sampled_sha256(path: str, sample_bytes: int = DEFAULT_SAMPLE_BYTES,
                   *, use_cache: bool = False) -> str:
    """Hash three bounded samples plus their offsets and the file size."""
    source = normalized_path(path)
    stat = os.stat(source)
    size = int(stat.st_size)
    mtime_ns = int(stat.st_mtime_ns)
    sample_bytes = max(4096, int(sample_bytes))
    cache_key = (source, size, mtime_ns, sample_bytes)
    # Cache reuse is opt-in.  Size and mtime alone are not a proof of content:
    # copy tools (and tests that model them) can preserve both while replacing
    # the bytes.  Freshness gates therefore always resample the bounded chunks.
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

    offsets = sorted(set((0, max(0, size // 2 - sample_bytes // 2),
                          max(0, size - sample_bytes))))
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with open(source, "rb", buffering=0) as handle:
        for offset in offsets:
            handle.seek(offset)
            chunk = handle.read(min(sample_bytes, max(0, size - offset)))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(chunk)
    result = digest.hexdigest()
    # Do not let a long-running batch accumulate one entry for every historic
    # revision of the same pathname.
    if use_cache:
        with _CACHE_LOCK:
            stale = [key for key in _CACHE if key[0] == source and key != cache_key]
            for key in stale:
                _CACHE.pop(key, None)
            _CACHE[cache_key] = result
    return result


def identity_key(identity: Mapping[str, Any], purpose: str | None = None) -> str:
    """Return a deterministic key without recursively hashing ``identity_key``."""
    payload = dict(identity)
    payload.pop("identity_key", None)
    if purpose:
        payload["purpose"] = str(purpose)
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def source_identity(path: str, *, include_path: bool = True,
                    sample_bytes: int = DEFAULT_SAMPLE_BYTES) -> dict[str, Any]:
    """Build the public source identity stored alongside derived artifacts."""
    source = normalized_path(path)
    sample_bytes = max(4096, int(sample_bytes))
    stable = None
    for _attempt in range(2):
        before = os.stat(source)
        fingerprint = sampled_sha256(source, sample_bytes, use_cache=False)
        after = os.stat(source)
        if (int(before.st_size), int(before.st_mtime_ns)) == (
                int(after.st_size), int(after.st_mtime_ns)):
            stable = (after, fingerprint)
            break
    if stable is None:
        raise OSError("source changed while its identity was being sampled: %s"
                      % source)
    stat, fingerprint = stable
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source_size": int(stat.st_size),
        "source_mtime_ns": int(stat.st_mtime_ns),
        "source_fingerprint": fingerprint,
        "fingerprint_mode": FINGERPRINT_MODE,
        "fingerprint_bytes": sample_bytes,
    }
    if include_path:
        result["source_path"] = source
    result["identity_key"] = identity_key(result)
    return result


def matches(saved: Mapping[str, Any] | None,
            current: Mapping[str, Any] | None) -> bool:
    """Strictly compare two versioned identities.

    A missing field never counts as a match.  Legacy indexes are handled by
    the index freshness adapter, not silently weakened here.
    """
    if not isinstance(saved, Mapping) or not isinstance(current, Mapping):
        return False
    required = (
        "schema", "schema_version", "source_size", "source_mtime_ns",
        "source_fingerprint", "fingerprint_mode", "fingerprint_bytes",
    )
    if any(key not in saved or key not in current for key in required):
        return False
    if any(saved.get(key) != current.get(key) for key in required):
        return False
    saved_path = saved.get("source_path")
    current_path = current.get("source_path")
    if (saved_path is None) != (current_path is None):
        return False
    if saved_path is not None and current_path is not None:
        if normalized_path(str(saved_path)) != normalized_path(str(current_path)):
            return False
    return True


__all__ = [
    "SCHEMA", "SCHEMA_VERSION", "FINGERPRINT_MODE",
    "DEFAULT_SAMPLE_BYTES", "normalized_path", "canonical_bytes",
    "sampled_sha256", "source_identity", "identity_key", "matches",
]
