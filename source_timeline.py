# -*- coding: utf-8 -*-
"""Canonical source-coordinate event timeline used by model-facing roles.

The transcript list is evidence attached to a time interval; it is not the
timeline itself.  This module turns speech rows and uncovered source ranges
into stable ``U`` (utterance) and ``G`` (no-transcript gap) events.  All model
roles can therefore reason about the same absolute source coordinates without
depending on array order or inventing free-form seconds.
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata


SCHEMA = "smartcut.source-events.v1"
EPSILON = 1e-6


def _number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _normalise_text(value):
    return " ".join(unicodedata.normalize(
        "NFKC", str(value or "")).split()).strip()


def _normalise_segments(segments):
    rows = []
    for source_index, raw in enumerate(segments or []):
        if isinstance(raw, dict):
            start = raw.get("start", raw.get("a", 0.0))
            end = raw.get("end", raw.get("b", start))
            text = raw.get("text", raw.get("word", ""))
        else:
            try:
                start, end = raw[0], raw[1]
                text = raw[2] if len(raw) > 2 else ""
            except (TypeError, IndexError):
                continue
        start, end = _number(start), _number(end)
        if end < start:
            start, end = end, start
        rows.append({
            "source_index": int(source_index),
            "start": max(0.0, start),
            "end": max(0.0, end),
            "text": str(text or ""),
        })
    rows.sort(key=lambda row: (
        row["start"], row["end"], row["source_index"]))
    return rows


def _normalise_chapters(chapters, timeline_start, timeline_end):
    rows = []
    for raw in chapters or []:
        try:
            start, end = _number(raw[0]), _number(raw[1])
        except (TypeError, IndexError):
            continue
        if end < start:
            start, end = end, start
        start = max(timeline_start, min(timeline_end, start))
        end = max(start, min(timeline_end, end))
        if end - start > EPSILON:
            rows.append((start, end))
    rows.sort()
    if not rows and timeline_end > timeline_start:
        rows = [(timeline_start, timeline_end)]
    return rows


def _chapter_at(seconds, chapters):
    if not chapters:
        return 1
    value = _number(seconds)
    for index, (start, end) in enumerate(chapters, 1):
        if start <= value < end or (index == len(chapters)
                                    and abs(value - end) <= EPSILON):
            return index
    if value < chapters[0][0]:
        return 1
    return len(chapters)


def build_events(segments, *, duration=None, chapters=None, audio_tags=None,
                 min_gap=0.35):
    """Return a stable absolute-time event stream.

    ``audio_tags`` is indexed by the original source transcript index.  Event
    ids are assigned only after chronological sorting, so even an out-of-order
    transcript input cannot trick a model into using list order as chronology.
    """
    speech_rows = _normalise_segments(segments)
    duration_value = _number(duration, -1.0)
    inferred_end = max((row["end"] for row in speech_rows), default=0.0)
    timeline_start = 0.0
    timeline_end = max(inferred_end, duration_value if duration_value >= 0 else 0.0)
    chapter_rows = _normalise_chapters(
        chapters, timeline_start, timeline_end)
    tags = list(audio_tags or [])

    # Build overlap clusters.  Only a speech row that reaches the active
    # cluster frontier owns a genuinely safe gap after it.
    clusters = []
    for position, row in enumerate(speech_rows):
        if clusters and row["start"] <= clusters[-1]["end"] + EPSILON:
            clusters[-1]["end"] = max(clusters[-1]["end"], row["end"])
            clusters[-1]["members"].append(position)
        else:
            clusters.append({"start": row["start"], "end": row["end"],
                             "members": [position]})
    safe_after = {}
    for cluster_index, cluster in enumerate(clusters):
        next_start = (clusters[cluster_index + 1]["start"]
                      if cluster_index + 1 < len(clusters)
                      else timeline_end)
        for member in cluster["members"]:
            row = speech_rows[member]
            safe_after[member] = (
                max(0.0, next_start - row["end"])
                if row["end"] >= cluster["end"] - EPSILON else 0.0)

    speech_events = []
    previous_frontier = timeline_start
    for position, row in enumerate(speech_rows):
        next_start = (speech_rows[position + 1]["start"]
                      if position + 1 < len(speech_rows) else timeline_end)
        before = row["start"] - previous_frontier
        after = next_start - row["end"]
        source_index = row["source_index"]
        speech_events.append({
            "schema": SCHEMA,
            "coordinate_space": "source",
            "event_id": "U%04d" % (position + 1),
            "kind": "speech",
            "chapter": _chapter_at(row["start"], chapter_rows),
            "start": row["start"],
            "end": row["end"],
            "a": row["start"],
            "b": row["end"],
            "duration": max(0.0, row["end"] - row["start"]),
            "source_index": source_index,
            "source_pos": source_index,
            "text": row["text"],
            "audio": str(tags[source_index] or "")
            if source_index < len(tags) else "",
            "prev_gap": max(0.0, before),
            "prev_overlap": max(0.0, -before),
            "next_gap": max(0.0, after),
            "next_overlap": max(0.0, -after),
            "safe_after": safe_after.get(position, max(0.0, after)),
        })
        previous_frontier = max(previous_frontier, row["end"])

    # A transcript gap is first-class chronology, but is deliberately *not*
    # labelled silence: game audio, music or an effect may still be present.
    coverage = []
    for row in speech_rows:
        if row["end"] > row["start"]:
            coverage.append((row["start"], row["end"]))
    merged = []
    for start, end in coverage:
        if merged and start <= merged[-1][1] + EPSILON:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    uncovered = []
    cursor = timeline_start
    threshold = max(0.0, _number(min_gap))
    for start, end in merged:
        if start - cursor >= threshold:
            uncovered.append((cursor, start))
        cursor = max(cursor, end)
    if timeline_end - cursor >= threshold:
        uncovered.append((cursor, timeline_end))

    gap_events = []
    for position, (start, end) in enumerate(uncovered, 1):
        gap_events.append({
            "schema": SCHEMA,
            "coordinate_space": "source",
            "event_id": "G%04d" % position,
            "kind": "gap",
            "chapter": _chapter_at(start, chapter_rows),
            "start": start,
            "end": end,
            "a": start,
            "b": end,
            "duration": max(0.0, end - start),
            "source_index": None,
            "source_pos": None,
            "text": "",
            "audio": "未检测到台词；不等于静音",
            "prev_gap": 0.0,
            "prev_overlap": 0.0,
            "next_gap": 0.0,
            "next_overlap": 0.0,
            "safe_after": max(0.0, end - start),
        })

    events = speech_events + gap_events
    events.sort(key=lambda row: (
        row["start"], 0 if row["kind"] == "gap" else 1,
        row["end"], row["event_id"]))
    return events


def event_map(events):
    return {str(row.get("event_id") or ""): row for row in events
            if row.get("event_id")}


def revision(events):
    """Stable identity for the exact model-visible source timeline."""
    payload = [{
        "id": row.get("event_id"),
        "kind": row.get("kind"),
        "start": round(_number(row.get("start", row.get("a"))), 6),
        "end": round(_number(row.get("end", row.get("b"))), 6),
        "source_index": row.get("source_index", row.get("source_pos")),
        # Transcript and per-event listening labels are evidence attached to
        # this coordinate.  A correction at identical timestamps must still
        # invalidate a cached model decision.
        "text": _normalise_text(row.get("text")),
        "audio": _normalise_text(row.get("audio")),
    } for row in events or []]
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()

