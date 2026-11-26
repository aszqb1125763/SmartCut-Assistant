# -*- coding: utf-8 -*-
"""Run the production SmartCut editorial pipeline against a Premiere timeline.

Premiere remains the timeline owner.  This module only asks the normal
analysis/writer/editor/boundary/director chain for final source-time keep spans,
then projects their complement into the *current* Premiere timeline.
"""
from __future__ import annotations

import hashlib
import json
import os
import time


MIN_DELETE_SECONDS = 0.15
MIN_KEEP_SECONDS = 0.10
TIME_TOLERANCE = 0.04


def _norm_path(value):
    return os.path.normcase(os.path.abspath(str(value or "")))


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _normalized_spans(rows):
    spans = []
    for row in rows or []:
        try:
            a, b = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if b <= a:
            continue
        spans.append([a, b])
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + TIME_TOLERANCE:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def canonical_keep_spans(rows):
    """Return keep spans that are safe to rebuild as Premiere clips.

    Model output can contain tiny residual ranges (often one video frame) when
    independently generated cuts almost touch.  Premiere treats every keep
    range as a real clip, so those residuals become visible flash frames on the
    rebuilt timeline.  Merge neighbouring ranges first, then discard spans too
    short to be an intentional edit.
    """
    return [
        [a, b] for a, b in _normalized_spans(rows)
        if b - a >= MIN_KEEP_SECONDS
    ]


def sequence_fingerprint(sequence):
    """Stable identity for the exact PR timeline that was planned."""
    payload = {
        "name": str((sequence or {}).get("name") or ""),
        "project_path": _norm_path((sequence or {}).get("project_path")),
        "tracks": [
            [str(row.get("kind") or ""), int(row.get("track") or 0),
             row.get("muted")]
            for row in (sequence or {}).get("tracks") or []
        ],
        "clips": [
            [str(row.get("kind") or ""), int(row.get("track") or 0),
             round(_number(row.get("start")), 6),
             round(_number(row.get("end")), 6),
             round(_number(row.get("in")), 6),
             round(_number(row.get("out")), 6),
             _norm_path(row.get("path"))]
            for row in (sequence or {}).get("clips") or []
        ],
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_sequence(sequence):
    video = list((sequence or {}).get("video") or [])
    if not video:
        raise ValueError("当前 PR 序列没有视频片段")
    sources = {_norm_path(row.get("path")) for row in video if row.get("path")}
    if len(sources) != 1:
        raise ValueError("第一版完整智能剪辑只支持一个原素材；当前序列包含多个素材")
    source_key = next(iter(sources))
    source = next(str(row.get("path") or "") for row in video
                  if _norm_path(row.get("path")) == source_key)
    if not os.path.isfile(source):
        raise ValueError("找不到 PR 序列引用的原素材：%s" % source)

    ordered = sorted(video, key=lambda row: (_number(row.get("start")),
                                             _number(row.get("end"))))
    previous_end = None
    for row in ordered:
        start, end = _number(row.get("start")), _number(row.get("end"))
        source_in, source_out = _number(row.get("in")), _number(row.get("out"))
        if end <= start or source_out <= source_in:
            raise ValueError("PR 时间轴存在无效片段，未执行智能剪辑")
        timeline_length = end - start
        source_length = source_out - source_in
        if abs(timeline_length - source_length) > max(0.08, source_length * 0.005):
            raise ValueError("当前序列包含变速/反向片段，第一版暂不自动剪，避免坐标错位")
        if previous_end is not None and start < previous_end - TIME_TOLERANCE:
            raise ValueError("当前序列存在重叠画面或多机位，第一版暂不自动剪")
        previous_end = max(previous_end or end, end)
    return source, ordered


def selected_audio_tracks(sequence, source, base_dir=None):
    """Resolve active PR audio to source stream indexes (zero based).

    ExtendScript exposes destination A-track numbers but not the original media
    stream mapping.  When the SmartCut audio picker has a saved source-stream
    selection, keep using that authoritative mapping; otherwise fall back to
    the destination track order for simple imports.
    """
    source_key = _norm_path(source)
    states = {
        int(row.get("track") or 0): row.get("muted")
        for row in (sequence or {}).get("tracks") or []
        if str(row.get("kind") or "").upper() == "A"
    }
    available = set()
    active = set()
    for clip in (sequence or {}).get("audio") or []:
        track = int(clip.get("track") or 0)
        if track <= 0:
            continue
        if _norm_path(clip.get("path")) != source_key:
            continue
        available.add(track - 1)
        if states.get(track) is True:
            continue
        active.add(track - 1)
    configured = []
    output_mode = "all"
    if base_dir:
        path = os.path.join(base_dir, "conf", "settings.json")
        try:
            with open(path, encoding="utf-8") as handle:
                settings = json.load(handle) or {}
            raw = str(settings.get("autocut_tracks") or "")
            configured = [int(value.strip()) - 1 for value in raw.replace("，", ",").split(",")
                          if value.strip().isdigit() and int(value.strip()) > 0]
            output_mode = str(settings.get("pr_output_audio_mode") or "all").lower()
        except (OSError, TypeError, ValueError):
            configured = []
    if configured:
        # An explicit source-stream choice is authoritative even when the
        # corresponding PR destination track is muted.  The model validates
        # it again against the media's real stream count.
        if output_mode == "selected":
            missing = [track for track in configured if track not in available]
            if missing:
                names = ",".join("A%d" % (track + 1) for track in missing)
                raise ValueError(
                    "只保留所选轨需要源音轨与 PR 目标轨同序；当前序列缺少 %s" % names)
        return configured
    if not active:
        raise ValueError("当前 PR 序列没有可用的未静音音轨")
    return sorted(active)


def delete_ranges_from_keep(video_clips, keep_spans):
    """Project source-time keep spans into per-clip PR delete ranges.

    A range is never allowed to cross an existing Premiere edit point.  This is
    what keeps Q/ripple-trim deterministic after the timeline has split into
    multiple clips.
    """
    keep = canonical_keep_spans(keep_spans)
    deletes = []
    for clip_no, clip in enumerate(video_clips):
        source_in = _number(clip.get("in"))
        source_out = _number(clip.get("out"))
        timeline_start = _number(clip.get("start"))
        timeline_end = _number(clip.get("end"))
        inside = []
        for a, b in keep:
            lo, hi = max(source_in, a), min(source_out, b)
            if hi > lo + TIME_TOLERANCE:
                inside.append([lo, hi])
        inside = _normalized_spans(inside)
        cursor = source_in
        gaps = []
        for a, b in inside:
            if a > cursor + MIN_DELETE_SECONDS:
                gaps.append([cursor, a])
            cursor = max(cursor, b)
        if source_out > cursor + MIN_DELETE_SECONDS:
            gaps.append([cursor, source_out])
        for a, b in gaps:
            ta = timeline_start + (a - source_in)
            tb = timeline_start + (b - source_in)
            ta = max(timeline_start, min(ta, timeline_end))
            tb = max(timeline_start, min(tb, timeline_end))
            if tb > ta + MIN_DELETE_SECONDS:
                deletes.append({
                    "start": round(ta, 3), "end": round(tb, 3),
                    "expected": round(tb - ta, 3), "clip": clip_no,
                })
    # The timeline shortens after every command.  Working back-to-front keeps
    # all remaining coordinates valid.
    deletes.sort(key=lambda row: (-row["start"], -row["end"]))
    return deletes


def operations_for_ranges(ranges, video_clips):
    ops = []
    for row in ranges:
        clip = video_clips[int(row["clip"])]
        start = float(row["start"])
        # If deletion starts exactly on an existing edit, another Ctrl+K is not
        # needed.  Otherwise create the A edit before trimming at B.
        if start > _number(clip.get("start")) + TIME_TOLERANCE:
            ops.append({"op": "seek", "t": round(start, 3)})
            ops.append({"op": "key", "k": "Ctrl+K"})
        ops.append({"op": "seek", "t": round(float(row["end"]), 3)})
        ops.append({"op": "key", "k": "Q", "expected": row["expected"]})
    return ops


def _settings(base_dir):
    path = os.path.join(base_dir, "conf", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        data = {}
    import cut_strength
    keep_ratio = cut_strength.ratio_from_settings(data)
    level = cut_strength.level_for_ratio(keep_ratio)
    return level, str(data.get("edit_mode") or "gameplay"), keep_ratio


def _audio_output_mode(base_dir):
    path = os.path.join(base_dir, "conf", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = str((json.load(handle) or {}).get(
                "pr_output_audio_mode") or "all").lower()
    except (OSError, ValueError, TypeError):
        value = "all"
    return value if value in ("all", "selected") else "all"


def write_editable_subtitle_tracks(index_dir, keep_spans, project_path,
                                   log=print, proofread=True):
    """Create final-timeline caption sources for the Premiere project.

    Kept ranges are still in source-media coordinates.  The helper remaps ASR
    sentences onto the compact rebuilt sequence and writes both language
    variants beside the .prproj, ready for CEP to create native caption tracks.
    """
    import pr_subtitles
    try:
        return pr_subtitles.write_editable_tracks(
            index_dir, keep_spans, project_path, proofread=proofread, log=log)
    except Exception as exc:  # noqa: BLE001 - captions must never block the edit
        warning = ("字幕生成失败，已跳过字幕轨但继续生成可编辑 PR 工程：%s"
                   % str(exc)[:200])
        log("  " + warning)
        return {
            "tracks": [], "count": 0, "converter": "",
            "quality": {"delivery_mode": "best_effort_nonblocking",
                        "fatal_subtitle_error": str(exc)},
            "warning": warning,
        }


def run(sequence, base_dir, index_dir=None, log=print, on_progress=None):
    """Run the normal SmartCut pipeline and return PR-native delete commands."""
    source, video = _validate_sequence(sequence)
    tracks = selected_audio_tracks(sequence, source, base_dir=base_dir)
    level, mode, keep_ratio = _settings(base_dir)
    audio_output_mode = _audio_output_mode(base_dir)
    if index_dir is None:
        stem = os.path.splitext(os.path.basename(source))[0]
        index_dir = os.path.join(base_dir, "工作目录", "索引", stem)
    output_dir = os.path.join(base_dir, "工作目录", "PR智能剪辑")
    os.makedirs(output_dir, exist_ok=True)

    import autopilot
    import workflow_engine

    log("PR 完整智能剪辑：%s / 保留 %.0f%% / 音轨 %s" %
        (mode, keep_ratio * 100.0,
         ",".join("A%d" % (item + 1) for item in tracks)))
    result = autopilot.run_one(
        source, level=level, mode=mode, tracks=tracks, use_bgm=False,
        index_dir=index_dir, output_dir=output_dir, burn_subs=False,
        loudness_norm=False, workflow=workflow_engine.PREMIERE_EDIT,
        model_editor=True, keep_ratio=keep_ratio,
        on_log=log, on_progress=on_progress,
        task_id="PR-%s-%d" % (hashlib.sha1(
            sequence_fingerprint(sequence).encode("ascii")).hexdigest()[:10],
            int(time.time())))
    # Use one canonical set everywhere: deletion projection, Premiere rebuild,
    # and subtitle remapping must never disagree about a one-frame residual.
    keep = canonical_keep_spans(result.get("spans") or [])
    if not keep:
        raise RuntimeError("完整智能剪辑没有返回可执行的保留时间轴")
    ranges = delete_ranges_from_keep(video, keep)
    return {
        "ok": True,
        "source": source,
        "index_dir": index_dir,
        "level": level,
        "keep_ratio": keep_ratio,
        "mode": mode,
        "tracks": tracks,
        "audio_track_numbers": [track + 1 for track in tracks],
        "audio_output_mode": audio_output_mode,
        "fingerprint": sequence_fingerprint(sequence),
        "keep_spans": keep,
        "ranges": ranges,
        "ops": operations_for_ranges(ranges, video),
        "count": len(ranges),
        "expected_shorten": round(sum(row["expected"] for row in ranges), 3),
        "task_id": result.get("task_id"),
        "plan_path": result.get("plan_path"),
        "director_passed": result.get("director_passed"),
        "director_debrief": result.get("director_debrief"),
    }
