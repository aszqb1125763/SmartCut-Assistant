# -*- coding: utf-8 -*-
"""Single source of truth for user-facing editing strength.

Users choose an exact content-retention percentage.  The old light/medium/heavy
value remains an internal compatibility strategy derived from that percentage.
"""
from __future__ import annotations


MIN_KEEP_PERCENT = 15
MAX_KEEP_PERCENT = 100
DEFAULT_KEEP_PERCENT = 75

LEGACY_KEEP_RATIO = {
    "light": 0.90,
    "medium": 0.75,
    "heavy": 0.45,
}


def normalize_ratio(value=None, fallback=None):
    """Return a clamped 0.15..1.00 ratio, accepting either 0.45 or 45."""
    if fallback is None:
        fallback = DEFAULT_KEEP_PERCENT / 100.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(fallback)
    if number > 1.0:
        number /= 100.0
    return max(MIN_KEEP_PERCENT / 100.0,
               min(MAX_KEEP_PERCENT / 100.0, number))


def ratio_from_settings(settings, fallback=None):
    settings = settings if isinstance(settings, dict) else {}
    if settings.get("keep_ratio") is not None:
        return normalize_ratio(settings.get("keep_ratio"), fallback=fallback)
    level = str(settings.get("cut_level") or "").lower()
    return normalize_ratio(LEGACY_KEEP_RATIO.get(level), fallback=fallback)


def percent(value=None, fallback=None):
    return int(round(normalize_ratio(value, fallback=fallback) * 100.0))


def level_for_ratio(value=None):
    """Derive the legacy semantic strategy without exposing fixed presets."""
    keep_percent = percent(value)
    if keep_percent >= 85:
        return "light"
    if keep_percent >= 60:
        return "medium"
    return "heavy"


def no_dialogue_gap_seconds(value=None):
    """Minimum speech-free gap removed at a given retention percentage.

    Lower percentages produce a tighter cut.  This threshold is independent of
    audio energy, so continuous game audio cannot disguise a long no-dialogue
    section as spoken content.
    """
    keep_percent = percent(value)
    if keep_percent <= 40:
        return 1.8
    if keep_percent <= 59:
        return 2.5
    if keep_percent <= 84:
        return 4.0
    return 6.0


def describe(value=None):
    keep_percent = percent(value)
    return "保留 %d%% 内容" % keep_percent
