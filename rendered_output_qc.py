# -*- coding: utf-8 -*-
"""Evidence-based quality review of the *rendered* SmartCut output.

The pre-render director reviews source spans.  Rendering, transitions, audio
mixing, subtitles, and BGM can still introduce defects, so this module samples
the actual output file through one reusable PCM media index.  It verifies
container streams and durations with ffprobe, listens to short random-access
windows, checks source word coverage at retained boundaries, and reuses
:mod:`visual_critic` for rendered-frame evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

import numpy as np

import audio_evidence_cache
import director_multimodal
import visual_critic


SCHEMA = "smartcut.rendered-output-qc"
SCHEMA_VERSION = 1
SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3}


class RenderedOutputCancelled(RuntimeError):
    pass


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _finite(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _finding(kind, modality, severity, message, refs=None, interval=None,
             veto=False, confidence=1.0, action="inspect"):
    interval = ([round(_finite(interval[0]), 6), round(_finite(interval[1]), 6)]
                if interval is not None else None)
    refs = sorted(set(str(value) for value in (refs or [])))
    token = "|".join((str(kind), str(modality), str(severity), str(message),
                      repr(interval), repr(refs)))
    return {
        "id": "render_finding_" + hashlib.sha1(
            token.encode("utf-8")).hexdigest()[:12],
        "type": str(kind), "modality": str(modality),
        "severity": str(severity), "message": str(message),
        "evidence_refs": refs, "suggested_interval": interval,
        "veto": bool(veto),
        "confidence": round(max(0.0, min(1.0, _finite(confidence))), 5),
        "action": str(action), "learnable": False,
    }


def _check_cancel(should_cancel):
    if should_cancel and should_cancel():
        raise RenderedOutputCancelled("渲染后导演复审已取消")


def _ffmpeg_path():
    try:
        import index
        return str(index.find_ffmpeg())
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _ffprobe_path(ffmpeg=None):
    ffmpeg = str(ffmpeg or _ffmpeg_path())
    sibling = os.path.join(os.path.dirname(ffmpeg),
                           "ffprobe.exe" if os.name == "nt" else "ffprobe")
    return sibling if os.path.isfile(sibling) else "ffprobe"


def probe_media(path, ffprobe=None):
    """Return exact stream metadata from ffprobe as a stable compact dict."""
    command = [str(ffprobe or _ffprobe_path()), "-v", "error",
               "-show_entries",
               "format=duration,size:stream=index,codec_type,codec_name,duration,"
               "sample_rate,channels,width,height,avg_frame_rate",
               "-of", "json", str(path)]
    result = subprocess.run(
        command, capture_output=True, timeout=60, creationflags=_no_window())
    if result.returncode != 0:
        message = (result.stderr or b"").decode("utf-8", "replace")[-1000:]
        raise RuntimeError("ffprobe 读取成片失败：%s" % message)
    try:
        raw = json.loads((result.stdout or b"{}").decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe 返回了无效 JSON") from exc
    streams = []
    for row in raw.get("streams") or []:
        item = {
            "index": int(row.get("index", len(streams))),
            "type": str(row.get("codec_type") or ""),
            "codec": str(row.get("codec_name") or ""),
            "duration": _finite(row.get("duration"), 0.0),
        }
        for key in ("sample_rate", "channels", "width", "height"):
            try:
                item[key] = int(row.get(key) or 0)
            except (TypeError, ValueError):
                item[key] = 0
        if row.get("avg_frame_rate"):
            item["avg_frame_rate"] = str(row["avg_frame_rate"])
        streams.append(item)
    format_row = raw.get("format") or {}
    return {
        "duration": _finite(format_row.get("duration"), 0.0),
        "size": int(_finite(format_row.get("size"), 0.0)),
        "streams": streams,
        "video_streams": [row for row in streams if row["type"] == "video"],
        "audio_streams": [row for row in streams if row["type"] == "audio"],
    }


def _timeline_rows(timeline):
    rows = []
    if isinstance(timeline, dict):
        seen = set()
        for piece in timeline.get("pieces") or []:
            mappings = piece.get("audio") or piece.get("video") or []
            for mapping in mappings:
                try:
                    output_start = float(mapping["output_start"])
                    source_start = float(mapping["source_start"])
                    duration = min(
                        float(mapping["output_end"]) - output_start,
                        float(mapping["source_end"]) - source_start)
                except (KeyError, TypeError, ValueError):
                    continue
                key = (round(output_start, 9), round(source_start, 9),
                       round(duration, 9))
                # The same source interval is repeated for every selected
                # audio track.  Coverage is temporal, so count it once.
                if duration <= 0 or key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "output_start": output_start,
                    "source_start": source_start,
                    "duration": duration,
                    "output_end": output_start + duration,
                    "source_end": source_start + duration,
                })
        return sorted(rows, key=lambda row: (
            row["output_start"], row["source_start"]))
    for item in timeline or []:
        try:
            output_start, source_start, duration = map(float, item[:3])
        except (TypeError, ValueError, IndexError):
            continue
        if duration <= 0:
            continue
        rows.append({"output_start": output_start, "source_start": source_start,
                     "duration": duration,
                     "output_end": output_start + duration,
                     "source_end": source_start + duration})
    return sorted(rows, key=lambda row: (row["output_start"], row["source_start"]))


def _even_sample(rows, count):
    rows = list(rows)
    count = max(0, int(count))
    if count <= 0:
        return []
    if len(rows) <= count:
        return rows
    if count == 1:
        return [rows[len(rows) // 2]]
    chosen = []
    for index in range(count):
        position = round(index * (len(rows) - 1) / float(count - 1))
        row = rows[int(position)]
        if row not in chosen:
            chosen.append(row)
    return chosen


def select_review_nodes(duration, timeline=None, max_nodes=12, delta=0.35):
    """Choose rendered-output start/end, edit seams, and timeline probes."""
    duration = max(0.0, _finite(duration))
    if duration <= 0:
        return []
    limit = max(2, int(max_nodes))
    delta = max(0.08, _finite(delta, 0.35))
    candidates = []

    def add(kind, time_value, priority, reason):
        time_value = max(0.0, min(duration, _finite(time_value)))
        if kind == "output_start":
            samples = [0.0, min(duration, 0.12), min(duration, delta)]
        elif kind == "output_end":
            samples = [max(0.0, duration - delta),
                       max(0.0, duration - 0.12), max(0.0, duration - 0.04)]
        else:
            samples = [max(0.0, time_value - delta), time_value,
                       min(duration, time_value + delta)]
        candidates.append({
            "kind": kind, "time": round(time_value, 6),
            "priority": int(priority), "reason": str(reason),
            "sample_times": [round(value, 6) for value in samples],
            "gap_seconds": None, "segment_index": None,
        })

    add("output_start", 0.0, 130, "实际成片开头")
    add("output_end", duration, 129, "实际成片结尾")
    rows = _timeline_rows(timeline)
    seam_times = []
    for previous, current in zip(rows, rows[1:]):
        time_value = current["output_start"]
        if time_value <= 0.05 or time_value >= duration - 0.05:
            continue
        if seam_times and abs(time_value - seam_times[-1]) < 0.20:
            continue
        seam_times.append(time_value)
    seams = []
    for time_value in seam_times:
        seams.append({"kind": "edit_boundary", "time": time_value,
                      "priority": 120, "reason": "实际成片接缝"})
    seam_budget = max(0, min(len(seams), (limit - 2 + 1) // 2))
    for row in _even_sample(seams, seam_budget):
        add(row["kind"], row["time"], row["priority"], row["reason"])

    for ratio, label in ((0.25, "前段"), (0.50, "中段"), (0.75, "后段")):
        if len(candidates) >= limit:
            break
        value = duration * ratio
        if any(abs(value - row["time"]) < 0.8 for row in candidates):
            continue
        add("timeline_probe", value, 50, "实际成片时间线探针：%s" % label)
    candidates = candidates[:limit]
    candidates.sort(key=lambda row: (row["time"], -row["priority"], row["kind"]))
    for index, row in enumerate(candidates, 1):
        row["id"] = "render_node_%03d" % index
    return candidates


def _merge_intervals(intervals, gap=0.03):
    rows = sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a))
    merged = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


_QC_WORD_SLACK = 0.025


def find_word_boundary_cuts(timeline, words, tolerance=None):
    """Find source words partially retained by the actual render timeline.

    最终交付复审使用 25ms 的独立容差。剪辑阶段可以为了避免咔字向外
    多留 100ms，但复审不能把“只保留了一个短词的一半”当成完整覆盖。
    小于 5ms 的浮点/贴边误差仍会在下面单独忽略。
    """
    tolerance = _QC_WORD_SLACK if tolerance is None else float(tolerance)
    rows = _timeline_rows(timeline)
    coverage = _merge_intervals(
        (row["source_start"], row["source_end"]) for row in rows)
    findings = []
    for word in director_multimodal.normalize_words(words):
        if not word.get("required", True):
            continue
        start, end = word["start"], word["end"]
        overlaps = [(left, right) for left, right in coverage
                    if right > start and left < end]
        if not overlaps:
            continue
        # 段边界正好压在字边界上时，浮点误差会留下 1e-14 秒的"重叠"。
        # 那不算切字——字是完整的，只是边界重合。必须真的压进字里才算。
        pressed = sum(max(0.0, min(end, right) - max(start, left))
                      for left, right in overlaps)
        if pressed <= max(0.005, tolerance * 0.01):
            continue
        fully_kept = any(left <= start + tolerance and right >= end - tolerance
                         for left, right in overlaps)
        if fully_kept:
            continue
        cut_source = min((max(start, left) for left, right in overlaps
                          if left > start + tolerance), default=start)
        output_time = None
        for row in rows:
            if row["source_start"] - tolerance <= cut_source <= row["source_end"] + tolerance:
                output_time = row["output_start"] + max(
                    0.0, min(row["duration"], cut_source - row["source_start"]))
                break
        interval = ([max(0.0, output_time - 0.25), output_time + 0.25]
                    if output_time is not None else None)
        findings.append(_finding(
            "render_word_boundary_cut", "audio", "critical",
            "实际成片时间线只保留了词语的一部分（提示：ASR 边界有 ±0.1 秒误差）",
            [word.get("id") or "word", "source:%.6f-%.6f" % (start, end)],
            interval, True, max(0.80, _finite(word.get("confidence"), 0.8)),
            "rebuild_source_boundary"))
    return findings


def _extract_audio_window_samples(path, start, end, ffmpeg=None, sample_rate=16000,
                                  audio_stream=0, should_cancel=None,
                                  audio_cache=None):
    """Legacy single-window reader.

    The production review path uses :class:`AudioEvidenceCache` so a rendered
    file is decoded once per audio stream.  Keep this helper for diagnostics
    and backwards-compatible callers; calling it in the node loop would reopen
    the same media container for every review point.
    """
    _check_cancel(should_cancel)
    start, end = max(0.0, _finite(start)), max(0.0, _finite(end))
    if end <= start:
        return np.zeros(0, dtype=np.float32), int(sample_rate)
    if audio_cache is not None:
        try:
            samples = audio_cache.review_samples(
                max(0, int(audio_stream)), start, end)
        except audio_evidence_cache.AudioEvidenceCancelled as exc:
            raise RenderedOutputCancelled(str(exc)) from exc
        _check_cancel(should_cancel)
        return samples, int(audio_cache.sample_rate)
    command = [str(ffmpeg or _ffmpeg_path()), "-hide_banner", "-loglevel", "error",
               "-ss", "%.6f" % start, "-t", "%.6f" % (end - start),
               "-i", str(path), "-map", "0:a:%d" % max(0, int(audio_stream)),
               "-vn", "-ac", "1",
               "-ar", str(int(sample_rate)), "-f", "s16le", "pipe:1"]
    result = subprocess.run(
        command, capture_output=True, timeout=90, creationflags=_no_window())
    _check_cancel(should_cancel)
    if result.returncode != 0:
        message = (result.stderr or b"").decode("utf-8", "replace")[-600:]
        raise RuntimeError("实际成片音频窗口读取失败：%s" % message)
    samples = np.frombuffer(result.stdout or b"", dtype="<i2").astype(np.float32)
    if samples.size:
        samples /= 32768.0
    return samples, int(sample_rate)


def _open_shared_audio_cache(path, stream_count, ffmpeg=None,
                             sample_rate=16000, should_cancel=None):
    """Create the Premiere-style shared PCM index used by rendered QC.

    A cache entry is keyed by the rendered file identity and audio ordinal.
    All review nodes then perform bounded random reads from the same PCM index
    instead of launching ``ffmpeg`` for ``nodes x tracks``.
    """
    return audio_evidence_cache.AudioEvidenceCache(
        path, tracks=range(max(0, int(stream_count))),
        sample_rate=int(sample_rate), ffmpeg=ffmpeg or _ffmpeg_path(),
        should_cancel=should_cancel)


def _rms_db(samples):
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not samples.size:
        return -120.0
    return 20.0 * math.log10(max(1e-8, float(np.sqrt(np.mean(samples * samples)))))


def analyze_rendered_audio_window(samples, sample_rate, node, time_offset,
                                  track_id="rendered_mix"):
    """Analyze one decoded output window and return evidence plus findings."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    sample_rate = max(1, int(sample_rate))
    node_time = _finite(node.get("time"))
    window_end = time_offset + samples.size / float(sample_rate)
    target = (max(time_offset, node_time - 0.22),
              min(window_end, node_time + 0.22))
    base = director_multimodal.analyze_audio_span(
        samples, sample_rate, target, words=[], track_id=str(track_id),
        role="mixed", time_offset=time_offset,
        check_start=False, check_end=False, check_content=False)
    findings = [dict(row) for row in base.get("findings") or []]
    rms_db = _rms_db(samples)
    kind = str(node.get("kind") or "timeline_probe")
    center = int(max(0, min(samples.size, round((node_time - time_offset) * sample_rate))))
    side = max(1, int(sample_rate * 0.18))
    before = samples[max(0, center - side):center]
    after = samples[center:min(samples.size, center + side)]
    before_db, after_db = _rms_db(before), _rms_db(after)
    # 只有两侧都"有声音"时，18dB 的差才叫突变；一侧已经是静音（气口）时，
    # 那是"说完一句安静下来"，不是跳变。阈值取 -50dB，把很轻的说话也算有声。
    if kind == "edit_boundary" and min(before_db, after_db) > -50.0 \
            and abs(before_db - after_db) >= 18.0:
        findings.append(_finding(
            "render_audio_level_jump", "audio", "major",
            "实际成片接缝两侧音量突变，可能产生明显听感跳变",
            [node.get("id")], [node_time - 0.25, node_time + 0.25],
            False, 0.88, "repair_audio_transition"))
    edge = max(1, int(sample_rate * 0.04))
    if kind == "output_start" and _rms_db(samples[:edge]) > -24.0:
        findings.append(_finding(
            "render_hot_audio_start", "audio", "minor",
            "实际成片第一帧音频已处于高活动，需确认是否硬切入发音",
            [node.get("id")], [0.0, min(window_end, 0.25)],
            False, 0.76, "inspect_output_start"))
    if kind == "output_end" and _rms_db(samples[-edge:]) > -24.0:
        findings.append(_finding(
            "render_hot_audio_end", "audio", "minor",
            "实际成片最后一帧音频仍处于高活动，需确认是否切掉尾音",
            [node.get("id")], [max(0.0, node_time - 0.25), node_time],
            False, 0.76, "inspect_output_end"))
    return {
        "node_id": node.get("id"), "kind": kind,
        "track_id": str(track_id),
        "window": [round(time_offset, 6), round(window_end, 6)],
        "sample_rate": sample_rate, "sample_count": int(samples.size),
        "actual_audio_inspected": bool(samples.size),
        "rms_db": round(rms_db, 4), "before_db": round(before_db, 4),
        "after_db": round(after_db, 4),
        "metrics": base.get("metrics") or {}, "findings": findings,
    }


def _review_visual_nodes(path, nodes, output_dir, should_cancel=None,
                         use_vlm=False, vlm_tier="fast"):
    frames_dir = os.path.join(output_dir, "frames")
    frame_cache = None
    try:
        import video_frame_cache
        frame_cache = video_frame_cache.VideoFrameCache(
            path, should_cancel=should_cancel)
    except Exception:  # noqa: BLE001
        pass
    rows = []
    for node in nodes:
        _check_cancel(should_cancel)
        if frame_cache is not None:
            node["_frame_cache"] = frame_cache
        paths = visual_critic.extract_node_frames(path, node, frames_dir)
        row = visual_critic.analyze_node(node, paths)
        if use_vlm and any(issue.get("severity") in ("critical", "major")
                           for issue in row.get("issues") or []):
            sheet = visual_critic.make_contact_sheet(
                paths, os.path.join(output_dir, "%s.jpg" % node["id"]))
            row["vlm"] = visual_critic.describe_contact_sheet(
                sheet, tier=vlm_tier)
        rows.append(row)
    return rows


def _visual_findings(rows):
    findings = []
    for row in rows or []:
        exact_visual = str(row.get("frame_precision") or "") != \
            "review_proxy_nearest_0.5s"
        sample_times = row.get("sample_times") or []
        interval = ([min(sample_times), max(sample_times)]
                    if len(sample_times) >= 2 else None)
        for issue in row.get("issues") or []:
            kind = str(issue.get("type") or "render_visual_issue")
            veto = kind in director_multimodal.VISUAL_VETO_TYPES and exact_visual
            if row.get("kind") in ("output_start", "output_end") \
                    and kind == "black_or_blank":
                veto = False  # a short intentional fade is allowed but remains visible.
            severity = "critical" if veto or kind == "frame_missing" \
                else str(issue.get("severity") or "major")
            findings.append(_finding(
                kind, "visual", severity,
                issue.get("message") or "实际成片关键画面需要复核",
                ["visual:%s" % row.get("id")], interval,
                veto or kind == "frame_missing", 0.9 if veto else 0.82,
                issue.get("action") or "inspect_rendered_frame"))
        parsed = (row.get("vlm") or {}).get("parsed") or {}
        if (row.get("vlm") or {}).get("status") == "ok" and (
                parsed.get("risk") == "drop" or parsed.get("continuity") == "broken"):
            findings.append(_finding(
                "render_vlm_visual_veto" if exact_visual else "render_vlm_proxy_review",
                "visual", "critical" if exact_visual else "major",
                parsed.get("summary") or "视觉模型确认实际成片画面不连续",
                ["visual:%s" % row.get("id")], interval, exact_visual,
                0.84 if exact_visual else 0.68,
                "rebuild_render_interval" if exact_visual
                else "inspect_proxy_context"))
    return findings


def find_half_sentences(timeline, segments, tolerance=0.12, min_keep=0.05):
    """找出「话说到一半就断」的句子。

    为什么它是 P0：语言是强预期，一句说到一半停下，人脑会产生错误信号
    （观众立刻觉得"卡了"）；画面跳变只是"换场景"，几乎不触发这种反应。

    判据只看成片时间线，不引用原片做对照——观众看不到原片：
      · 整句都留了（差 ≤ tolerance）→ 正常
      · 整句都没留（≤ min_keep）→ 是删除，不是半句
      · 留了一部分 → 就是半句
    """
    rows = _timeline_rows(timeline)
    if not rows or not segments:
        return []
    coverage = _merge_intervals(
        (row["source_start"], row["source_end"]) for row in rows)
    findings = []
    for index, segment in enumerate(segments or []):
        try:
            start, end = float(segment[0]), float(segment[1])
        except (IndexError, TypeError, ValueError):
            continue
        length = end - start
        if length <= 0.12:
            continue
        kept = sum(max(0.0, min(end, right) - max(start, left))
                   for left, right in coverage)
        if kept <= min_keep * length or kept >= length - tolerance:
            continue
        # 定位到成片时间：落在哪一段的源区间里，就换算成成片第几秒。
        at = None
        for row in rows:
            overlap = (min(end, row["source_end"])
                       - max(start, row["source_start"]))
            if overlap <= 0:
                continue
            offset = max(0.0, min(row["duration"],
                                  max(start, row["source_start"])
                                  - row["source_start"]))
            at = row["output_start"] + offset
            break
        ratio = kept / length
        # 句子被切并不一定是切字，因此这里只保留为可定位的 major，
        # 不单凭 ASR 句界直接否决。真正切进词内部由词级门禁 veto；导演
        # 仍能据此联合切口音频判断是否需要补回上下文。
        findings.append(_finding(
            "render_half_sentence", "text", "major",
            "接缝落在句子中间，观众会听到话说一半（保留 %.0f%%）"
            % (ratio * 100.0),
            ["segment:%d" % (index + 1),
             "source:%.3f-%.3f" % (start, end),
             "kept:%.3f/%.3f" % (kept, length)],
            [at, at + 0.4] if at is not None else None,
            # veto=False：降为提示，不阻断出片。
            # 句子被切不等于“话说一半”——两端能级相似、语义接得上就是流畅
            # （子枫的例子：两个「层」字，切在层前面接上去听不出）。
            False, 0.95, "rebuild_sentence_boundary"))
    return findings


def review_rendered_output(path, *, timeline=None, words=None,
                           segments=None, skip_ranges=(),
                           sequence_timeline=None,
                           expected_duration=None, selected_tracks=None,
                           expect_audio=True, output_dir=None, max_nodes=12,
                           should_cancel=None, ffmpeg=None, ffprobe=None,
                           use_vlm=False, vlm_tier="fast"):
    """Review the actual output file and return a deterministic QC report."""
    _tl = list(timeline or [])
    source_timeline = sequence_timeline or timeline
    path = os.path.abspath(os.fspath(path))
    output_dir = os.path.abspath(os.fspath(
        output_dir or os.path.join(os.path.dirname(path),
                                   ".%s_render_qc" % Path(path).stem)))
    os.makedirs(output_dir, exist_ok=True)
    findings = []
    _check_cancel(should_cancel)
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        findings.append(_finding(
            "render_file_missing", "container", "critical",
            "找不到有效的实际成片文件", [], None, True, 1.0,
            "rerender"))
        return _finish_report(path, {}, [], [], findings, expected_duration,
                              selected_tracks)

    try:
        probe = probe_media(path, ffprobe=ffprobe or _ffprobe_path(ffmpeg))
    except Exception as exc:  # noqa: BLE001
        findings.append(_finding(
            "render_probe_failed", "container", "critical",
            str(exc), [], None, True, 1.0, "rerender_or_repair_container"))
        return _finish_report(path, {}, [], [], findings, expected_duration,
                              selected_tracks)
    duration = _finite(probe.get("duration"), 0.0)
    if not probe.get("video_streams"):
        findings.append(_finding(
            "render_video_missing", "container", "critical",
            "实际成片没有可解码视频轨", [], None, True, 1.0, "rerender"))
    if expect_audio and not probe.get("audio_streams"):
        findings.append(_finding(
            "render_audio_missing", "audio", "critical",
            "实际成片没有可解码音轨", [], None, True, 1.0, "rerender_audio"))
    if duration <= 0:
        findings.append(_finding(
            "render_duration_invalid", "container", "critical",
            "实际成片时长无效", [], None, True, 1.0, "rerender"))
    if expected_duration is None:
        if isinstance(sequence_timeline, dict):
            expected_duration = _finite(sequence_timeline.get("duration"), 0.0) or None
        else:
            expected_duration = sum(row["duration"] for row in _timeline_rows(timeline)) or None
    if expected_duration and duration > 0:
        delta = abs(duration - float(expected_duration))
        tolerance = max(0.35, float(expected_duration) * 0.01)
        if delta > tolerance:
            severe = delta > max(1.0, float(expected_duration) * 0.03)
            findings.append(_finding(
                "render_duration_mismatch", "container",
                "critical" if severe else "major",
                "实际成片时长与最终时间线不一致（差 %.3f 秒）" % delta,
                [], [0.0, duration], severe, 0.98,
                "inspect_render_timeline"))
    video_durations = [row["duration"] for row in probe.get("video_streams") or []
                       if row.get("duration")]
    audio_durations = [row["duration"] for row in probe.get("audio_streams") or []
                       if row.get("duration")]
    if video_durations and audio_durations:
        av_delta = abs(max(video_durations) - max(audio_durations))
        if av_delta > max(0.35, duration * 0.005):
            findings.append(_finding(
                "render_av_duration_mismatch", "container", "major",
                "实际成片音轨与画面时长不一致（差 %.3f 秒）" % av_delta,
                [], [0.0, duration], False, 0.96, "repair_audio_video_sync"))

    nodes = select_review_nodes(duration, timeline=timeline, max_nodes=max_nodes)
    # 章节卡是故意做的黑场，不该被当成"黑屏缺陷"。跳过落在这些区间里的抽帧点。
    if skip_ranges:
        nodes = [node for node in nodes
                 if not any(float(left) - 0.15 <= float(node["time"])
                            <= float(right) + 0.15
                            for left, right in skip_ranges)]
    audio_reports = []
    audio_cache_report = {
        "status": "not_needed", "mode": "shared_pcm_index",
        "sample_rate": 16000, "tracks": [],
    }
    if expect_audio and probe.get("audio_streams"):
        streams = list(probe.get("audio_streams") or [])
        shared_cache = None
        cache_entries = {}
        try:
            shared_cache = _open_shared_audio_cache(
                path, len(streams), ffmpeg=ffmpeg, sample_rate=16000,
                should_cancel=should_cancel)
            cache_entries = shared_cache.prepare()
            audio_cache_report = {
                "status": "ready", "mode": "shared_pcm_index",
                "sample_rate": int(shared_cache.sample_rate),
                "tracks": [{
                    "ordinal": int(track),
                    "cache_key": str(entry.get("key") or ""),
                    "duration": _finite(entry.get("duration")),
                    "reused": bool(entry.get("reused")),
                } for track, entry in sorted(cache_entries.items())],
            }
        except audio_evidence_cache.AudioEvidenceCancelled as exc:
            raise RenderedOutputCancelled(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            audio_cache_report = {
                "status": "failed", "mode": "shared_pcm_index",
                "sample_rate": 16000, "tracks": [], "error": str(exc),
            }
            findings.append(_finding(
                "render_audio_cache_failed", "audio", "major",
                "实际成片共享音频索引建立失败：%s" % exc,
                ["rendered_audio_cache"], [0.0, duration], False, 1.0,
                "retry_audio_review"))

        try:
            if shared_cache is not None and cache_entries:
                for audio_ordinal, stream in enumerate(streams):
                    track_id = "rendered_audio_%d" % audio_ordinal
                    entry = cache_entries.get(audio_ordinal) or {}
                    for node in nodes:
                        _check_cancel(should_cancel)
                        start = max(0.0, float(node["time"]) - 0.55)
                        end = min(duration, float(node["time"]) + 0.55)
                        if node["kind"] == "output_start":
                            start, end = 0.0, min(duration, 0.9)
                        elif node["kind"] == "output_end":
                            start, end = max(0.0, duration - 0.9), duration
                        try:
                            samples = shared_cache.review_samples(
                                audio_ordinal, start, end)
                            report = analyze_rendered_audio_window(
                                samples, shared_cache.sample_rate, node, start,
                                track_id=track_id)
                        except audio_evidence_cache.AudioEvidenceCancelled as exc:
                            raise RenderedOutputCancelled(str(exc)) from exc
                        except RenderedOutputCancelled:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            report = {
                                "node_id": node["id"], "kind": node["kind"],
                                "track_id": track_id,
                                "actual_audio_inspected": False,
                                "rms_db": -120.0,
                                "findings": [_finding(
                                    "render_audio_window_failed", "audio", "major",
                                    str(exc), [node["id"], "audio:%s" % track_id],
                                    [start, end], False, 1.0,
                                    "retry_audio_review")],
                            }
                        report["audio_stream_ordinal"] = audio_ordinal
                        report["container_stream_index"] = stream.get("index")
                        report["cache_key"] = entry.get("key")
                        report["cache_reused"] = bool(entry.get("reused"))
                        audio_reports.append(report)
                        findings.extend(report.get("findings") or [])
        finally:
            if shared_cache is not None:
                shared_cache.close()
        inspected = [row for row in audio_reports if row.get("actual_audio_inspected")]
        if inspected and all(_finite(row.get("rms_db"), -120.0) < -55.0
                             for row in inspected):
            findings.append(_finding(
                "render_audio_silent", "audio", "critical",
                "实际成片抽样音频全部接近静音", [], [0.0, duration],
                True, 0.98, "rerender_audio"))
    findings.extend(find_word_boundary_cuts(source_timeline, words or []))
    if segments:
        # P0：话说到一半就断，是观众最敏感的问题（比画面、声音问题都严重）。
        findings.extend(find_half_sentences(source_timeline, segments))

    try:
        visual_rows = _review_visual_nodes(
            path, nodes, output_dir, should_cancel=should_cancel,
            use_vlm=use_vlm, vlm_tier=vlm_tier)
        findings.extend(_visual_findings(visual_rows))
    except RenderedOutputCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        visual_rows = []
        findings.append(_finding(
            "render_visual_review_failed", "visual", "major",
            str(exc), [], None, False, 1.0, "retry_visual_review"))
    report = _finish_report(path, probe, audio_reports, visual_rows, findings,
                          expected_duration, selected_tracks, nodes=nodes,
                          expect_audio=expect_audio,
                          audio_cache=audio_cache_report)
    if isinstance(sequence_timeline, dict):
        report["sequence_revision"] = sequence_timeline.get("sequence_revision")
        report["sequence_timeline_schema"] = sequence_timeline.get("schema")
    return report


def _finish_report(path, probe, audio_reports, visual_rows, findings,
                   expected_duration, selected_tracks, nodes=None,
                   expect_audio=True, audio_cache=None):
    chosen = {}
    for row in findings:
        key = (row.get("type"), tuple(row.get("evidence_refs") or []),
               tuple(row.get("suggested_interval") or []))
        old = chosen.get(key)
        if old is None or SEVERITY_RANK.get(row.get("severity"), 0) > \
                SEVERITY_RANK.get(old.get("severity"), 0):
            chosen[key] = row
    findings = sorted(chosen.values(), key=lambda row: (
        -SEVERITY_RANK.get(row.get("severity"), 0),
        row.get("modality", ""), row.get("type", ""), row.get("id", "")))
    vetoes = [row for row in findings if row.get("veto")]
    # 兜底宽松（子枫 2026-11-23）：
    # 「剩下实在不能解决的，那就挑一个最不极端的，这也算我说的宽松。」
    # 下面这一类是“边界重合”型误报——切点正好压在字/句的端点上，
    # 统计上会被算成“保留了一部分”，但人耳听不出任何截断。
    # ASR 边界本身有 ±0.1 秒误差，这类不该阻断出片。
    def _edge_false_alarm(row):
        kind = str(row.get("type") or "")
        if kind not in ("render_word_boundary_cut", "render_half_sentence"):
            return False
        msg = str(row.get("message") or "")
        # “保留 88%”这种接近整句的，算边界误差，不算真半句
        for token in ("保留 8", "保留 9"):
            if token in msg:
                return True
        # 词级检测本身已经有 25ms 容差并过滤 5ms 浮点接触。能走到这里
        # 说明时间线真实切进了词内部，不能再一律降级，否则半个首字/尾字
        # 会被交付门禁放过。
        return False
    relaxed = [row for row in vetoes if _edge_false_alarm(row)]
    if relaxed:
        for row in relaxed:
            row["veto"] = False
            row["severity"] = "minor"
            row["relaxed"] = "边界重合误报，按宽松标准放过"
        vetoes = [row for row in findings if row.get("veto")]
    critical = sum(row.get("severity") == "critical" for row in findings)
    major = sum(row.get("severity") == "major" for row in findings)
    minor = sum(row.get("severity") == "minor" for row in findings)
    # 扣分权重按“观众会不会察觉”定（子枫 2026-11-23：出片前审片要宽松，
    # 机器精度远超人耳）：
    #   critical（半句/切字）  35 分 —— 观众直接觉得“话断了”
    #   major（画面跳切）      11 分 —— 观众会觉得“跳了一下”
    #   minor（停滞画面等）     1 分 —— 观众基本无感，不该按 3 分扣
    score = max(0.0, 100.0 - min(100.0, critical * 35.0 + major * 11.0 + minor * 1.0))
    evidence = {
        "container": "available" if probe else "missing",
        "audio": ("available" if any(row.get("actual_audio_inspected")
                                      for row in audio_reports or []) else "missing"),
        "visual": "available" if visual_rows else "missing",
    }
    evidence_ready = bool(evidence["visual"] == "available" and (
        not expect_audio or evidence["audio"] == "available"))
    # 通过线按"口播/实况剪辑"的现实定（剪映《衔接与转场配方》：「口播和快节奏
    # 内容默认硬切」）：硬切处音量和画面本来就会变，不该按电影级 72 分卡死。
    # 真正该硬拦的是 veto / critical（半句、切字），那些在别处已经判了。
    passed = bool(probe and evidence_ready and not vetoes and critical == 0
                  and major <= 4 and score >= 50.0)
    return {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "output": os.path.abspath(path), "probe": probe,
        "expected_duration": expected_duration,

        "selected_source_tracks": list(selected_tracks or []),
        "audio_cache": dict(audio_cache or {
            "status": "not_needed", "mode": "shared_pcm_index",
            "tracks": [],
        }),
        "nodes": list(nodes or []), "audio_reports": list(audio_reports or []),
        "visual_nodes": list(visual_rows or []), "evidence_status": evidence,
        "required_evidence_available": evidence_ready,
        "findings": findings, "passed": passed, "vetoed": bool(vetoes),
        "veto_count": len(vetoes), "critical_issues": critical,
        "major_issues": major, "minor_issues": minor,
        "score": round(score, 3), "auto_learning_eligible": False,
    }


__all__ = [
    "RenderedOutputCancelled", "analyze_rendered_audio_window",
    "find_word_boundary_cuts", "probe_media", "review_rendered_output",
    "select_review_nodes",
]
