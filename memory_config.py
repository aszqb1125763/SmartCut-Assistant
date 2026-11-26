# -*- coding: utf-8 -*-
"""Director/task-memory configuration with installation-local defaults."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path


DEFAULT_CONFIG = {
    "enabled": True,
    "root": "",
    "session_retention_days": 30,
    "completed_task_limit": 10,
    "cross_mode_sharing": "facts_only",
    "auto_learn": True,
    "candidate_promotion": "validated_only",
    "checkpoint_seconds": 120,
    "journal_flush_ms": 1000,
    "audio_review_enabled": True,
    "director_max_review_windows": 28,
    "director_text_rework_rounds": 10,
    "director_audio_rework_rounds": 12,
    "visual_review_enabled": True,
    "visual_max_nodes": 18,
    "visual_use_vlm": False,
    "visual_vlm_limit": 2,
    "visual_vlm_tier": "fast",
    "rendered_review_enabled": True,
    "rendered_review_max_nodes": 12,
    "schema_version": 2,
}

_SHARING = {"none", "facts_only", "validated", "all"}
_PROMOTION = {"manual", "validated_only", "automatic"}


def app_home(home: str | os.PathLike | None = None) -> str:
    """Return the writable installation root, never the PyInstaller bundle dir."""
    value = str(home or os.environ.get("SMARTCUT_HOME") or "").strip()
    if not value:
        value = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__))
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))


def config_path(home: str | os.PathLike | None = None) -> str:
    return os.path.join(app_home(home), "conf", "director_memory.json")


def _coerce(raw: dict | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
        # Version 1 shipped the audio-rework limit as 8.  That value was a
        # product default rather than a deliberate user choice, and it can stop
        # before a complete word tail is recovered.  Only migrate that exact
        # legacy default; values saved under schema 2 remain user-controlled.
        try:
            old_schema = int(raw.get("schema_version", 1))
            old_rounds = int(raw.get("director_audio_rework_rounds", 12))
        except (TypeError, ValueError):
            old_schema, old_rounds = 1, 12
        if old_schema < 2 and old_rounds == 8:
            cfg["director_audio_rework_rounds"] = 12
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["auto_learn"] = bool(cfg.get("auto_learn", True))
    cfg["audio_review_enabled"] = bool(cfg.get("audio_review_enabled", True))
    cfg["visual_review_enabled"] = bool(cfg.get("visual_review_enabled", True))
    cfg["visual_use_vlm"] = bool(cfg.get("visual_use_vlm", False))
    cfg["rendered_review_enabled"] = bool(cfg.get("rendered_review_enabled", True))
    cfg["root"] = str(cfg.get("root") or "").strip()
    cfg["session_retention_days"] = max(1, int(cfg.get("session_retention_days", 30)))
    cfg["completed_task_limit"] = max(1, int(cfg.get("completed_task_limit", 10)))
    cfg["checkpoint_seconds"] = max(15, int(cfg.get("checkpoint_seconds", 120)))
    cfg["journal_flush_ms"] = max(100, int(cfg.get("journal_flush_ms", 1000)))
    cfg["director_max_review_windows"] = max(
        4, min(120, int(cfg.get("director_max_review_windows", 28))))
    cfg["director_audio_rework_rounds"] = max(
        1, min(12, int(cfg.get("director_audio_rework_rounds", 12))))
    cfg["director_text_rework_rounds"] = max(
        1, min(12, int(cfg.get("director_text_rework_rounds", 10))))
    cfg["visual_max_nodes"] = max(3, min(60, int(cfg.get("visual_max_nodes", 18))))
    cfg["rendered_review_max_nodes"] = max(
        4, min(40, int(cfg.get("rendered_review_max_nodes", 12))))
    cfg["visual_vlm_limit"] = max(0, min(12, int(cfg.get("visual_vlm_limit", 2))))
    tier = str(cfg.get("visual_vlm_tier") or "fast").strip().lower()
    cfg["visual_vlm_tier"] = tier if tier in ("fast", "accurate") else "fast"
    sharing = str(cfg.get("cross_mode_sharing") or "facts_only")
    cfg["cross_mode_sharing"] = sharing if sharing in _SHARING else "facts_only"
    promotion = str(cfg.get("candidate_promotion") or "validated_only")
    cfg["candidate_promotion"] = promotion if promotion in _PROMOTION else "validated_only"
    cfg["schema_version"] = max(2, int(cfg.get("schema_version", 2)))
    return cfg


def load_config(path: str | os.PathLike | None = None,
                home: str | os.PathLike | None = None) -> dict:
    target = os.fspath(path) if path is not None else config_path(home)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    return _coerce(raw if isinstance(raw, dict) else {})


def _atomic_json(path: str, payload: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".director_memory.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def save_config(updates: dict, path: str | os.PathLike | None = None,
                home: str | os.PathLike | None = None) -> dict:
    """Merge and atomically save settings while preserving unknown future keys."""
    if not isinstance(updates, dict):
        raise TypeError("memory config updates must be a dict")
    target = os.fspath(path) if path is not None else config_path(home)
    existing = {}
    try:
        with open(target, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                existing = loaded
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    existing.update(updates)
    payload = _coerce(existing)
    _atomic_json(target, payload)
    return payload


def resolve_root(config: dict | None = None,
                 home: str | os.PathLike | None = None) -> str:
    cfg = _coerce(config) if config is not None else load_config(home=home)
    base = app_home(home)
    value = str(cfg.get("root") or "").strip()
    if value:
        value = os.path.expandvars(os.path.expanduser(value))
        if not os.path.isabs(value):
            value = os.path.join(base, value)
        root = os.path.abspath(value)
    else:
        work = str(os.environ.get("SC_WORK") or "").strip()
        if work:
            work = os.path.abspath(os.path.expandvars(os.path.expanduser(work)))
        else:
            work = os.path.join(base, "工作目录")
        root = os.path.join(work, "导演记忆")

    lowered = {part.lower() for part in Path(root).parts}
    if "_internal" in lowered or "build" in lowered or "dist" in lowered:
        raise ValueError("导演记忆目录不能放在 _internal、build 或 dist 中")
    return root


def ensure_root(config: dict | None = None,
                home: str | os.PathLike | None = None) -> str:
    root = resolve_root(config=config, home=home)
    os.makedirs(root, exist_ok=True)
    probe = os.path.join(root, ".write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise OSError("导演记忆目录不可写：%s" % root) from exc
    return root
