# -*- coding: utf-8 -*-
"""Deterministic, conservative audio/visual edit-boundary refinement.

This module deliberately keeps frame-accurate decisions outside the LLM.  A
semantic span may nominate content, while word timestamps, audio activity and
visual boundaries decide how much surrounding material must be retained.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class BoundaryConfig:
    search_lead: float = 0.75
    search_trail: float = 0.90
    word_guard_before: float = 0.09
    word_guard_after: float = 0.12
    normal_preroll: float = 0.08
    normal_postroll: float = 0.12
    low_confidence_preroll: float = 0.22
    low_confidence_postroll: float = 0.30
    low_confidence_threshold: float = 0.68
    primary_join_gap: float = 0.18
    secondary_join_gap: float = 0.28
    secondary_max_seconds: float = 1.20
    secondary_edge_limit: float = 0.45
    secondary_window_coverage: float = 0.70
    visual_snap_limit: float = 0.65
    visual_min_confidence: float = 0.45
    merge_gap: float = 0.0
    activity_frame_ms: float = 20.0
    activity_hop_ms: float = 10.0
    activity_margin_db: float = 8.0
    activity_min_seconds: float = 0.04
    activity_join_seconds: float = 0.12


@dataclass(frozen=True)
class WaveformEdgeConfig:
    """Conservative limits for the final local waveform-edge pass."""

    search_seconds: float = 0.80
    preroll_seconds: float = 0.040
    postroll_seconds: float = 0.070
    word_guard_seconds: float = 0.005
    minimum_span_seconds: float = 0.20


def _finite(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _bounds(value: Any) -> Tuple[float, float]:
    if isinstance(value, Mapping):
        start = _finite(value.get("start", value.get("a")))
        end = _finite(value.get("end", value.get("b")))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        start, end = _finite(value[0]), _finite(value[1])
    else:
        start = end = None
    if start is None or end is None or end <= start:
        raise ValueError("boundary interval must have finite start < end")
    return max(0.0, start), max(0.0, end)


def _confidence(value: Mapping[str, Any], default: float = 1.0) -> float:
    raw = value.get("confidence", value.get("probability", value.get("prob", default)))
    number = _finite(raw, default)
    return min(1.0, max(0.0, float(number)))


def _frame_floor(value: float, fps: float) -> float:
    return math.floor(value * fps + 1e-9) / fps


def _frame_ceil(value: float, fps: float) -> float:
    return math.ceil(value * fps - 1e-9) / fps


def _round_time(value: float) -> float:
    return round(float(value), 9)


def detect_activity_intervals(
        samples: Sequence[float], sample_rate: int,
        config: Optional[BoundaryConfig] = None,
        track: str = "primary", kind: str = "speech") -> List[Dict[str, Any]]:
    """Return deterministic RMS activity intervals for a mono waveform.

    This is intentionally a conservative evidence producer, not a replacement
    for a dedicated VAD.  Production callers can pass their VAD intervals
    directly to :func:`refine_span` instead.
    """
    cfg = config or BoundaryConfig()
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    audio = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return []
    win = max(1, int(round(sample_rate * cfg.activity_frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * cfg.activity_hop_ms / 1000.0)))
    if audio.size < win:
        audio = np.pad(audio, (0, win - audio.size))
    count = 1 + (audio.size - win) // hop
    starts = hop * np.arange(count)
    frames = np.lib.stride_tricks.as_strided(
        audio, shape=(count, win),
        strides=(hop * audio.itemsize, audio.itemsize), writeable=False)
    mean_square = np.einsum("ij,ij->i", frames, frames, optimize=True) / float(win)
    rms = np.sqrt(mean_square + 1e-12)
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    noise = float(np.percentile(db, 20.0))
    peak = float(np.percentile(db, 95.0))
    threshold = min(peak - 3.0, noise + cfg.activity_margin_db)
    active = db >= threshold
    if not np.any(active) or peak - noise < 3.0:
        return []

    raw: List[Tuple[float, float, float]] = []
    begin = None
    for index, is_active in enumerate(active.tolist() + [False]):
        if is_active and begin is None:
            begin = index
        elif not is_active and begin is not None:
            end_index = index - 1
            start_t = starts[begin] / float(sample_rate)
            end_t = min(audio.size / float(sample_rate),
                        (starts[end_index] + win) / float(sample_rate))
            segment_peak = float(np.max(db[begin:index]))
            conf = min(1.0, max(0.0, (segment_peak - threshold) / 20.0 + 0.5))
            raw.append((start_t, end_t, conf))
            begin = None

    joined: List[List[float]] = []
    for start, end, conf in raw:
        if joined and start - joined[-1][1] <= cfg.activity_join_seconds:
            joined[-1][1] = end
            joined[-1][2] = max(joined[-1][2], conf)
        else:
            joined.append([start, end, conf])
    return [
        {"start": _round_time(start), "end": _round_time(end),
         "confidence": round(conf, 6), "track": track, "kind": kind,
         "source": "energy"}
        for start, end, conf in joined
        if end - start >= cfg.activity_min_seconds
    ]


def _normalise_activity(values: Optional[Iterable[Any]], default_track: str,
                        default_kind: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for value in values or []:
        start, end = _bounds(value)
        if isinstance(value, Mapping):
            row = {
                "start": start, "end": end,
                "confidence": _confidence(value),
                "track": str(value.get("track") or default_track),
                "kind": str(value.get("kind") or default_kind),
                "source": str(value.get("source") or "provided"),
            }
        else:
            row = {"start": start, "end": end, "confidence": 1.0,
                   "track": default_track, "kind": default_kind,
                   "source": "provided"}
        result.append(row)
    return sorted(result, key=lambda row: (
        row["start"], row["end"], row["track"], row["kind"], row["source"]))


def _normalise_secondary(values: Any) -> List[Dict[str, Any]]:
    if isinstance(values, Mapping):
        rows: List[Dict[str, Any]] = []
        for track in sorted(values, key=str):
            rows.extend(_normalise_activity(values[track], str(track), "audio_event"))
        return rows
    return _normalise_activity(values, "secondary", "audio_event")


_EXPRESSIVE_SECONDARY_KINDS = {
    "breath", "laughter", "laugh", "emotion", "prosody",
}


def _eligible_secondary_activity(
        intervals: Sequence[Mapping[str, Any]], start: float, end: float,
        window_start: float, window_end: float,
        config: BoundaryConfig) -> Tuple[List[Mapping[str, Any]],
                                         List[Mapping[str, Any]]]:
    """Keep nearby short events, but ignore continuous background audio."""
    eligible: List[Mapping[str, Any]] = []
    rejected: List[Mapping[str, Any]] = []
    window_seconds = max(1e-9, window_end - window_start)
    for row in intervals:
        a, b = float(row["start"]), float(row["end"])
        overlap = max(0.0, min(b, window_end) - max(a, window_start))
        coverage = overlap / window_seconds
        kind = str(row.get("kind") or "audio_event").lower()
        duration_limit = config.secondary_max_seconds
        if kind in _EXPRESSIVE_SECONDARY_KINDS:
            duration_limit *= 1.5
        too_long = b - a > duration_limit
        covers_window = coverage >= config.secondary_window_coverage
        reaches_start = a < start and b >= start - config.secondary_edge_limit
        reaches_end = b > end and a <= end + config.secondary_edge_limit
        if (overlap > 0.0 and not too_long and not covers_window and
                (reaches_start or reaches_end)):
            eligible.append(row)
        else:
            rejected.append(row)
    return eligible, rejected


def _connected_union(start: float, end: float, intervals: Sequence[Mapping[str, Any]],
                     gap: float, window_start: float, window_end: float) -> Tuple[float, float, List[Mapping[str, Any]]]:
    used: List[Mapping[str, Any]] = []
    changed = True
    while changed:
        changed = False
        for row in intervals:
            if row in used:
                continue
            a, b = float(row["start"]), float(row["end"])
            if b < window_start or a > window_end:
                continue
            if a <= end + gap and b >= start - gap:
                next_start = max(window_start, min(start, a))
                next_end = min(window_end, max(end, b))
                used.append(row)
                if next_start != start or next_end != end:
                    start, end = next_start, next_end
                    changed = True
    return start, end, used


def _visual_rows(values: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    rows = []
    for value in values or []:
        if isinstance(value, Mapping):
            time = _finite(value.get("time", value.get("at")))
            confidence = _confidence(value)
            kind = str(value.get("kind") or "visual_boundary")
        else:
            time = _finite(value)
            confidence = 1.0
            kind = "visual_boundary"
        if time is not None and time >= 0.0:
            rows.append({"time": time, "confidence": confidence, "kind": kind})
    return sorted(rows, key=lambda row: (row["time"], row["kind"]))


def snap_spans_to_activity_edges(
        spans: Iterable[Any], activity: Optional[Iterable[Any]],
        words: Optional[Iterable[Mapping[str, Any]]] = None,
        duration: Optional[float] = None,
        config: Optional[WaveformEdgeConfig] = None,
        ) -> Tuple[List[Tuple[float, float]], List[Dict[str, Any]]]:
    """Move nearby cut points to the selected speech track's RMS edges.

    Semantic/model selection is already complete when this function runs.  It
    only searches a bounded local window and uses word timestamps as hard
    guards, so the DSP pass can remove leading/trailing silence without
    clipping retained speech or changing which scene the model selected.
    """
    cfg = config or WaveformEdgeConfig()
    timeline_end = _finite(duration)
    if timeline_end is not None and timeline_end <= 0.0:
        raise ValueError("duration must be positive")

    rows: List[List[float]] = []
    for value in spans or []:
        try:
            start, end = _bounds(value)
        except ValueError:
            continue
        if timeline_end is not None:
            end = min(end, timeline_end)
        if end > start:
            rows.append([start, end])
    rows.sort(key=lambda row: (row[0], row[1]))
    activities = _normalise_activity(activity, "primary", "speech")
    if not rows or not activities:
        return [(start, end) for start, end in rows], []

    word_rows: List[Dict[str, float]] = []
    for value in words or []:
        try:
            start, end = _bounds(value)
        except ValueError:
            continue
        word_rows.append({"start": start, "end": end})
    word_rows.sort(key=lambda row: (row["start"], row["end"]))

    search = max(0.0, float(cfg.search_seconds))
    preroll = max(0.0, float(cfg.preroll_seconds))
    postroll = max(0.0, float(cfg.postroll_seconds))
    word_guard = max(0.0, float(cfg.word_guard_seconds))
    minimum = max(0.0, float(cfg.minimum_span_seconds))
    changes: List[Dict[str, Any]] = []

    def pick(boundary: float, side: str,
             retained_words: Sequence[Mapping[str, float]]):
        edge_key = "start" if side == "start" else "end"
        # An interval whose edge is exactly the search-window boundary usually
        # means the signal stayed active for the whole probe.  There is no
        # observed onset/offset to snap to, so keep the original cut.
        observed_limit = max(0.0, search - 0.02)
        candidates = [row for row in activities
                      if abs(float(row[edge_key]) - boundary) <= observed_limit]
        if not candidates:
            return None
        if retained_words:
            anchor = retained_words[0] if side == "start" else retained_words[-1]
            anchored = [row for row in candidates
                        if float(row["end"]) >= float(anchor["start"]) - 0.08
                        and float(row["start"]) <= float(anchor["end"]) + 0.08]
            if anchored:
                # Multiple selected microphone tracks may contain the same
                # utterance.  Keep the outer edge so none of them is clipped.
                return (min(anchored, key=lambda row: float(row["start"]))
                        if side == "start"
                        else max(anchored, key=lambda row: float(row["end"])))
        return min(candidates, key=lambda row: (
            abs(float(row[edge_key]) - boundary),
            -float(row.get("confidence", 0.0)),
            float(row["start"]), float(row["end"])))

    original_rows = [tuple(row) for row in rows]
    for index, row in enumerate(rows):
        old_start, old_end = row
        retained_words = [word for word in word_rows
                          if word["end"] > old_start and word["start"] < old_end]
        start_activity = pick(old_start, "start", retained_words)
        end_activity = pick(old_end, "end", retained_words)

        new_start = old_start
        if start_activity is not None:
            new_start = max(0.0, float(start_activity["start"]) - preroll)
            if retained_words:
                new_start = min(
                    new_start, float(retained_words[0]["start"]) - word_guard)
            new_start = max(0.0, new_start)

        new_end = old_end
        if end_activity is not None:
            new_end = float(end_activity["end"]) + postroll
            if retained_words:
                new_end = max(
                    new_end, float(retained_words[-1]["end"]) + word_guard)
            if timeline_end is not None:
                new_end = min(timeline_end, new_end)

        # Every original gap is a semantic deletion selected by the model.
        # A local energy edge may approach it, but may not cross the adjacent
        # retained span and silently merge that deletion away.
        if index > 0 and new_start <= original_rows[index - 1][1] + 1e-9:
            new_start = old_start
            start_activity = None
        if (index + 1 < len(original_rows)
                and new_end >= original_rows[index + 1][0] - 1e-9):
            new_end = old_end
            end_activity = None

        if new_end - new_start < minimum:
            new_start, new_end = old_start, old_end
        row[0], row[1] = new_start, new_end
        if abs(new_start - old_start) > 1e-9 or abs(new_end - old_end) > 1e-9:
            changes.append({
                "span_index": index,
                "before": [_round_time(old_start), _round_time(old_end)],
                "after": [_round_time(new_start), _round_time(new_end)],
                "start_activity": (dict(start_activity)
                                   if start_activity is not None else None),
                "end_activity": (dict(end_activity)
                                 if end_activity is not None else None),
                "source": "local_waveform_energy",
            })

    merged: List[List[float]] = []
    for start, end in sorted(rows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(_round_time(start), _round_time(end)) for start, end in merged], changes


def refine_span(
        semantic_span: Any, words: Optional[Iterable[Mapping[str, Any]]] = None,
        primary_activity: Optional[Iterable[Any]] = None,
        secondary_activity: Any = None,
        visual_boundaries: Optional[Iterable[Any]] = None,
        fps: float = 30.0, duration: Optional[float] = None,
        task_seed: Any = None,
        config: Optional[BoundaryConfig] = None,
        source_index: Optional[int] = None) -> Dict[str, Any]:
    """Refine one semantic span without ever shrinking its required content."""
    cfg = config or BoundaryConfig()
    if fps <= 0.0 or not math.isfinite(fps):
        raise ValueError("fps must be positive and finite")
    original_start, original_end = _bounds(semantic_span)
    duration_value = _finite(duration)
    if duration_value is not None and duration_value <= 0.0:
        raise ValueError("duration must be positive")
    timeline_end = duration_value if duration_value is not None else float("inf")
    semantic_conf = (_confidence(semantic_span, 0.75)
                     if isinstance(semantic_span, Mapping) else 0.75)

    selected_words: List[Dict[str, Any]] = []
    for word in words or []:
        try:
            start, end = _bounds(word)
        except ValueError:
            continue
        if end > original_start and start < original_end and word.get("required", True):
            selected_words.append({
                "start": start, "end": end,
                "text": str(word.get("text", word.get("word", ""))),
                "confidence": _confidence(word, 0.5),
                "track": str(word.get("track") or word.get("source_track") or "primary"),
            })
    selected_words.sort(key=lambda row: (row["start"], row["end"], row["text"]))

    if selected_words:
        core_start = min(original_start, selected_words[0]["start"])
        core_end = max(original_end, selected_words[-1]["end"])
        word_conf = sum(row["confidence"] for row in selected_words) / len(selected_words)
    else:
        core_start, core_end = original_start, original_end
        word_conf = 0.45

    search_start = max(0.0, min(original_start, core_start) - cfg.search_lead)
    search_end = min(timeline_end, max(original_end, core_end) + cfg.search_trail)
    protection_start = max(search_start, core_start - cfg.word_guard_before)
    protection_end = min(search_end, core_end + cfg.word_guard_after)
    combined_conf = min(1.0, max(0.0, 0.55 * semantic_conf + 0.45 * word_conf))
    low_confidence = combined_conf < cfg.low_confidence_threshold
    preroll = cfg.low_confidence_preroll if low_confidence else cfg.normal_preroll
    postroll = cfg.low_confidence_postroll if low_confidence else cfg.normal_postroll
    final_start = max(search_start, min(original_start, protection_start) - preroll)
    final_end = min(search_end, max(original_end, protection_end) + postroll)
    reasons = ["semantic_span_preserved", "word_core_protected" if selected_words else "word_evidence_missing"]
    if low_confidence:
        reasons.append("low_confidence_keep_more")

    primary = _normalise_activity(primary_activity, "primary", "speech")
    secondary = _normalise_secondary(secondary_activity)
    final_start, final_end, primary_used = _connected_union(
        final_start, final_end, primary, cfg.primary_join_gap, search_start, search_end)
    if primary_used:
        reasons.append("primary_audio_activity_preserved")
    secondary_eligible, secondary_rejected = _eligible_secondary_activity(
        secondary, final_start, final_end, search_start, search_end, cfg)
    final_start, final_end, secondary_used = _connected_union(
        final_start, final_end, secondary_eligible, cfg.secondary_join_gap,
        search_start, search_end)
    if secondary_used:
        kinds = {str(row["kind"]).lower() for row in secondary_used}
        reasons.append("audio_event_tail_preserved")
        if kinds.intersection({"breath", "laughter", "laugh", "emotion", "prosody"}):
            reasons.append("breath_laughter_or_emotion_preserved")
    if secondary_rejected:
        reasons.append("continuous_or_distant_secondary_audio_ignored")

    covering_primary = [row for row in primary
                        if row["start"] <= search_start + 1.0 / fps
                        and row["end"] >= search_end - 1.0 / fps]
    if covering_primary:
        final_start, final_end = search_start, search_end
        reasons.append("no_silence_conservative_fallback")

    visuals = [row for row in _visual_rows(visual_boundaries)
               if row["confidence"] >= cfg.visual_min_confidence]
    start_candidates = [row for row in visuals
                        if search_start <= row["time"] <= final_start
                        and final_start - row["time"] <= cfg.visual_snap_limit]
    end_candidates = [row for row in visuals
                      if final_end <= row["time"] <= search_end
                      and row["time"] - final_end <= cfg.visual_snap_limit]
    visual_used: List[Dict[str, Any]] = []
    if start_candidates:
        selected = max(start_candidates, key=lambda row: (row["time"], row["confidence"]))
        final_start = selected["time"]
        visual_used.append(selected)
        reasons.append("visual_start_boundary_kept")
    if end_candidates:
        selected = min(end_candidates, key=lambda row: (row["time"], -row["confidence"]))
        final_end = selected["time"]
        visual_used.append(selected)
        reasons.append("visual_end_boundary_kept")

    final_start = max(0.0, _frame_floor(min(final_start, protection_start, core_start), fps))
    final_end = min(timeline_end, _frame_ceil(max(final_end, protection_end, core_end), fps))
    if final_end <= final_start:
        raise ValueError("refined span collapsed after frame alignment")
    reasons.append("frame_safe_start_floor_end_ceil")

    evidence_confidences = [row["confidence"] for row in primary_used + secondary_used]
    evidence_confidences.extend(row["confidence"] for row in visual_used)
    if evidence_confidences:
        evidence_conf = sum(evidence_confidences) / len(evidence_confidences)
        boundary_conf = 0.7 * combined_conf + 0.3 * evidence_conf
    else:
        boundary_conf = combined_conf * 0.85

    return {
        "schema": "boundary_refinement_v1",
        "source_span_indices": [] if source_index is None else [int(source_index)],
        "semantic_span": [_round_time(original_start), _round_time(original_end)],
        "search_window": [_round_time(search_start), _round_time(search_end)],
        "immutable_semantic_core": [_round_time(core_start), _round_time(core_end)],
        "speech_protection_interval": [_round_time(protection_start), _round_time(protection_end)],
        "final_interval": [_round_time(final_start), _round_time(final_end)],
        "confidence": round(min(1.0, max(0.0, boundary_conf)), 6),
        "low_confidence": bool(low_confidence),
        "reasons": list(dict.fromkeys(reasons)),
        "evidence": {
            "required_words": selected_words,
            "primary_activity": [dict(row) for row in primary_used],
            "secondary_activity": [dict(row) for row in secondary_used],
            "visual_boundaries": [dict(row) for row in visual_used],
        },
        "fps": float(fps),
        "task_seed": None if task_seed is None else str(task_seed),
        "config": asdict(cfg),
    }


def _merge_rows(left: Dict[str, Any], right: Dict[str, Any], fps: float) -> Dict[str, Any]:
    merged = dict(left)
    for key in ("semantic_span", "search_window", "immutable_semantic_core",
                "speech_protection_interval", "final_interval"):
        merged[key] = [min(left[key][0], right[key][0]), max(left[key][1], right[key][1])]
    merged["final_interval"] = [
        _round_time(_frame_floor(merged["final_interval"][0], fps)),
        _round_time(_frame_ceil(merged["final_interval"][1], fps)),
    ]
    merged["source_span_indices"] = sorted(set(
        left.get("source_span_indices", []) + right.get("source_span_indices", [])))
    merged["confidence"] = round(min(left["confidence"], right["confidence"]), 6)
    merged["low_confidence"] = bool(left["low_confidence"] or right["low_confidence"])
    merged["reasons"] = sorted(set(left["reasons"] + right["reasons"] + [
        "overlapping_refined_spans_merged"]))
    merged["evidence"] = {
        key: left["evidence"].get(key, []) + right["evidence"].get(key, [])
        for key in ("required_words", "primary_activity", "secondary_activity", "visual_boundaries")
    }
    return merged


def refine_spans(
        semantic_spans: Iterable[Any], words: Optional[Iterable[Mapping[str, Any]]] = None,
        primary_activity: Optional[Iterable[Any]] = None,
        secondary_activity: Any = None,
        visual_boundaries: Optional[Iterable[Any]] = None,
        fps: float = 30.0, duration: Optional[float] = None,
        task_seed: Any = None,
        config: Optional[BoundaryConfig] = None) -> List[Dict[str, Any]]:
    """Refine and deterministically merge a timeline of semantic spans."""
    cfg = config or BoundaryConfig()
    word_rows = list(words or [])
    primary_rows = list(primary_activity or [])
    visual_rows = list(visual_boundaries or [])
    refined = [
        refine_span(span, words=word_rows, primary_activity=primary_rows,
                    secondary_activity=secondary_activity,
                    visual_boundaries=visual_rows, fps=fps, duration=duration,
                    task_seed=task_seed, config=cfg, source_index=index)
        for index, span in enumerate(semantic_spans)
    ]
    refined.sort(key=lambda row: (row["final_interval"][0], row["final_interval"][1],
                                  row["source_span_indices"]))
    merged: List[Dict[str, Any]] = []
    for row in refined:
        if merged and row["final_interval"][0] <= merged[-1]["final_interval"][1] + cfg.merge_gap:
            merged[-1] = _merge_rows(merged[-1], row, fps)
        else:
            merged.append(row)
    return merged


__all__ = [
    "BoundaryConfig", "WaveformEdgeConfig", "detect_activity_intervals",
    "snap_spans_to_activity_edges", "refine_span", "refine_spans",
]
