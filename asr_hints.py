# -*- coding: utf-8 -*-
"""为 Whisper 构造短上下文和热词；只提供词形，不参与剧情判断。"""
from __future__ import annotations

import json
import os
import re


INITIAL_CONTEXT = "以下是中文游戏实况解说，语言为普通话。"
MAX_HOTWORD_CHARS = 180
DEFAULT_MODEL = "large-v3"
DEFAULT_BEAM_SIZE = 5
VAD_PARAMETERS = {
    "threshold": 0.5,
    # Keep one-word acknowledgements; segmentation is based on silence, not a
    # minimum utterance duration. Padding protects soft consonants at edges.
    "min_speech_duration_ms": 0,
    "min_silence_duration_ms": 350,
    "speech_pad_ms": 400,
}
COMPRESSION_RATIO_THRESHOLD = 1.8
NO_SPEECH_THRESHOLD = 0.6


def settings(base):
    """Read the shared ASR preferences without retaining API credentials."""
    try:
        with open(os.path.join(base, "conf", "settings.json"), encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            return {key: value[key] for key in ("model", "asr_batch") if key in value}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def configured_model(base):
    """Keep explicit environment/user choices; new installs use the accurate model."""
    value = os.environ.get("ASR_MODEL") or settings(base).get("model") or DEFAULT_MODEL
    return str(value).strip() or DEFAULT_MODEL


def beam_size(value=None):
    value = os.environ.get("ASR_BEAM", DEFAULT_BEAM_SIZE) if value is None else value
    try:
        beam = int(value)
        return beam if beam > 0 else DEFAULT_BEAM_SIZE
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_BEAM_SIZE


def batch_size(base):
    try:
        return max(0, int(float(settings(base).get("asr_batch", 0))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _clean(value):
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not (2 <= len(value) <= 16):
        return ""
    if any(mark in value for mark in ("#", "{", "}", "\n", "\r")):
        return ""
    return value


def _terms_from_file(path):
    out = []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # 所有项目术语表都以左侧为正确写法；右侧只是常见误写。
                correct = line.split("|", 1)[0].split("=", 1)[-1].strip()
                term = _clean(correct)
                if term:
                    out.append(term)
    except OSError:
        pass
    return out


def load_terms(base, extra_terms=()):
    """按可信优先级读取少量正确词形，并稳定去重。"""
    asr_terms = os.path.join(base, "conf", "asr_terms.txt")
    subtitle_terms = os.path.join(base, "conf", "terms.txt")
    model_terms = os.path.join(base, "models", "asr", "term.txt")
    game_terms = os.path.join(base, "kb", "04_游戏机制.txt")
    # Both user-maintained glossaries are more specific than the built-in game
    # vocabulary.  Keep them ahead of generic caller hints so a small Whisper
    # hotword budget still contains the creator name, series name and current
    # pack's chemistry/item terms.
    values = _terms_from_file(asr_terms)
    values.extend(_terms_from_file(subtitle_terms))
    for value in extra_terms or ():
        term = _clean(value)
        if term:
            values.append(term)
    for path in (model_terms, game_terms):
        values.extend(_terms_from_file(path))
    seen, out = set(), []
    for term in values:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def bounded_join(terms, max_chars=MAX_HOTWORD_CHARS):
    """按完整词截断，绝不在字符预算边缘切坏一个术语。"""
    out, used = [], 0
    for term in terms:
        extra = len(term) + (1 if out else 0)
        if used + extra > int(max_chars):
            continue
        out.append(term)
        used += extra
    return " ".join(out)


def build(base, extra_terms=(), max_hotword_chars=MAX_HOTWORD_CHARS):
    """返回 ``(稳定领域说明, 热词串, 热词列表)``。"""
    terms = load_terms(base, extra_terms=extra_terms)
    hotwords = bounded_join(terms, max_chars=max_hotword_chars)
    selected = hotwords.split() if hotwords else []
    return INITIAL_CONTEXT, hotwords, selected

