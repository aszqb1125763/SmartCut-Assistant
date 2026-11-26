# -*- coding: utf-8 -*-
"""Immutable, reproducible edit-plan artifacts.

The workflow may run for hours and can be resumed after a crash.  Mutable
``index.json`` state is therefore not sufficient evidence for what was
actually rendered.  This module writes one content-addressed JSON artifact per
planning stage.  Volatile fields (task id and creation time) are deliberately
excluded from the plan hash so identical inputs and settings produce the same
seed and decisions on a later run.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Mapping, Sequence


SCHEMA = "smartcut.edit-plan"
SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256"


def _normalise(value: Any) -> Any:
    """Return a stable JSON-safe representation without runtime-only types."""
    if isinstance(value, Mapping):
        return {str(key): _normalise(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, float):
        # Sub-frame noise must not change a plan identity.  Nine decimals still
        # preserves much more precision than any supported media timebase.
        return round(value, 9)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalise(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(path: str, cache_dir: str | None = None) -> dict:
    """Return a full-file SHA-256, cached against size and nanosecond mtime."""
    full = os.path.abspath(os.fspath(path))
    stat = os.stat(full)
    marker = {
        "path": full,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "source.sha256.json")
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if all(cached.get(key) == value for key, value in marker.items()):
                digest = str(cached.get("sha256") or "")
                if len(digest) == 64:
                    return dict(marker, sha256=digest, algorithm=HASH_ALGORITHM)
        except (OSError, ValueError, TypeError):
            pass

    result = dict(marker, sha256=_file_sha256(full), algorithm=HASH_ALGORITHM)
    if cache_path:
        _atomic_json(cache_path, result, replace=True)
    return result


def config_fingerprint(config: Mapping[str, Any] | None) -> str:
    return sha256_value(config or {})


def _span_rows(rows: Sequence[Sequence[float]] | None) -> list[list[float]]:
    result = []
    for row in rows or ():
        if len(row) < 2:
            continue
        result.append([round(float(row[0]), 9), round(float(row[1]), 9)])
    return result


def build_plan(
        *, source: Mapping[str, Any], mode: str, level: str,
        config: Mapping[str, Any] | None = None,
        semantic_spans: Sequence[Sequence[float]] | None = None,
        refined_spans: Sequence[Sequence[float]] | None = None,
        director_modifications: Sequence[Mapping[str, Any]] | None = None,
        final_render_spans: Sequence[Sequence[float]] | None = None,
        evidence: Mapping[str, Any] | None = None,
        versions: Mapping[str, Any] | None = None) -> dict:
    """Build a deterministic plan and derive its task seed and SHA-256."""
    core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": _normalise(source),
        "mode": str(mode),
        "level": str(level),
        "config_fingerprint": config_fingerprint(config),
        "versions": _normalise(versions or {}),
        "semantic_spans": _span_rows(semantic_spans),
        "refined_spans": _span_rows(refined_spans),
        "director_modifications": _normalise(director_modifications or []),
        "final_render_spans": _span_rows(final_render_spans),
        "evidence": _normalise(evidence or {}),
    }
    # Seed from the complete deterministic core.  It is a string on purpose:
    # random.Random accepts it consistently and configs can persist it intact.
    task_seed = sha256_value(core)
    hashed = dict(core, task_seed=task_seed)
    return dict(hashed, plan_sha256=sha256_value(hashed))


def extend_plan(plan: Mapping[str, Any], **changes: Any) -> dict:
    """Create the next immutable plan version from a previous plan."""
    allowed = {
        "semantic_spans", "refined_spans", "director_modifications",
        "final_render_spans", "evidence", "versions",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("unsupported plan fields: %s" % ", ".join(sorted(unknown)))
    values = {key: plan.get(key) for key in allowed}
    values.update(changes)
    # The prior config fingerprint is already deterministic input.  Preserve it
    # directly instead of trying to reconstruct a lost config object.
    core = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": _normalise(plan.get("source") or {}),
        "mode": str(plan.get("mode") or ""),
        "level": str(plan.get("level") or ""),
        "config_fingerprint": str(plan.get("config_fingerprint") or ""),
        "versions": _normalise(values.get("versions") or {}),
        "semantic_spans": _span_rows(values.get("semantic_spans")),
        "refined_spans": _span_rows(values.get("refined_spans")),
        "director_modifications": _normalise(values.get("director_modifications") or []),
        "final_render_spans": _span_rows(values.get("final_render_spans")),
        "evidence": _normalise(values.get("evidence") or {}),
    }
    task_seed = sha256_value(core)
    hashed = dict(core, task_seed=task_seed)
    return dict(hashed, plan_sha256=sha256_value(hashed))


def artifact_path(index_dir: str, stage: str, plan: Mapping[str, Any]) -> str:
    digest = str(plan.get("plan_sha256") or "")
    if len(digest) != 64:
        raise ValueError("plan_sha256 is missing or invalid")
    safe_stage = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                         for ch in str(stage or "plan"))
    return os.path.join(os.path.abspath(index_dir), "plans",
                        "%s-%s.json" % (safe_stage, digest[:16]))


def _atomic_json(path: str, value: Any, replace: bool) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(value, fh, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            # Content-addressed paths must never be silently overwritten.
            try:
                os.link(tmp, path)
                os.remove(tmp)
            except FileExistsError:
                os.remove(tmp)
            except OSError:
                if os.path.exists(path):
                    os.remove(tmp)
                else:
                    os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_artifact(index_dir: str, stage: str, plan: Mapping[str, Any],
                   task_id: str | None = None) -> str:
    """Write a content-addressed plan without changing its deterministic hash."""
    path = artifact_path(index_dir, stage, plan)
    document = {
        "artifact": {
            "task_id": str(task_id or ""),
            "stage": str(stage),
            "created_at_unix": round(time.time(), 6),
        },
        "plan": _normalise(dict(plan)),
    }
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
        if (existing.get("plan") or {}) != document["plan"]:
            raise RuntimeError("content-addressed plan collision: %s" % path)
        return path
    _atomic_json(path, document, replace=False)
    return path


__all__ = [
    "HASH_ALGORITHM", "SCHEMA", "SCHEMA_VERSION", "artifact_path",
    "build_plan", "canonical_bytes", "config_fingerprint", "extend_plan",
    "sha256_value", "source_fingerprint", "write_artifact",
]
