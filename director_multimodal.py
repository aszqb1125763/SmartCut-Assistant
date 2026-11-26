# -*- coding: utf-8 -*-
"""导演多模态证据层。

本模块不决定剧情取舍，也不替代 ``director_critic``。它把导演真正需要
听到、看到的证据整理为稳定的 JSON 结构：

* 文字结构审稿与真实音频同为主证据，各占主证据分数的一半；
* 音频层检查词级保护区、切口附近的实际波形活动、多音轨尾音和数字削波；
* 视觉层只检查关键节点，并可对黑屏、结果画面缺失和明确断裂行使否决；
* 低置信度、证据缺失或仅由不确定模型给出的结论不能自动学习。

重模型是可选的。规则层只依赖 NumPy；调用方可以把 ``visual_critic`` 的
报告直接传给 :func:`combine_review`。生产链后续负责选择音轨并提取窗口，
本模块也提供不会弹出控制台的 ffmpeg 音频窗口提取函数。
"""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import wave
from pathlib import Path

import llm_gateway

import numpy as np


SCHEMA = "director_multimodal_v1"
SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3}
VISUAL_VETO_TYPES = {
    "black_or_blank",
    "missing_action_result",
    "incomplete_action",
    "scene_discontinuity",
}


def _no_window():
    return 0x08000000 if os.name == "nt" else 0


def _finite(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _finding(kind, modality, severity, message, evidence_refs=None, veto=False,
             interval=None, confidence=1.0, action="inspect"):
    confidence = max(0.0, min(1.0, _finite(confidence, 0.0)))
    interval = ([round(_finite(interval[0]), 6), round(_finite(interval[1]), 6)]
                if interval is not None else None)
    token = "|".join((str(kind), str(modality), str(severity), str(message),
                      repr(interval), repr(sorted(evidence_refs or []))))
    return {
        "id": "finding_" + hashlib.sha1(token.encode("utf-8")).hexdigest()[:12],
        "type": str(kind),
        "modality": str(modality),
        "severity": str(severity),
        "message": str(message),
        "evidence_refs": sorted(str(value) for value in (evidence_refs or [])),
        "veto": bool(veto),
        "suggested_interval": interval,
        "confidence": round(confidence, 5),
        "action": str(action),
        "learnable": bool(confidence >= 0.80 and not veto),
    }


def normalize_words(words):
    """兼容不同 ASR 字段名，返回按时间排序的绝对词级证据。"""
    rows = []
    for index, word in enumerate(words or []):
        if isinstance(word, dict):
            start = word.get("start", word.get("start_time"))
            end = word.get("end", word.get("end_time"))
            text = word.get("word", word.get("text", ""))
            confidence = word.get("probability", word.get(
                "confidence", word.get("score", 1.0)))
            track = word.get("source_track", word.get("track_id"))
            ref = word.get("id") or "word_%06d" % index
        elif isinstance(word, (list, tuple)) and len(word) >= 3:
            start, end, text = word[:3]
            confidence = word[3] if len(word) > 3 else 1.0
            track = None
            ref = "word_%06d" % index
        else:
            continue
        start, end = _finite(start, -1.0), _finite(end, -1.0)
        if start < 0 or end <= start:
            continue
        rows.append({
            "id": str(ref), "text": str(text or ""),
            "start": start, "end": end,
            "confidence": max(0.0, min(1.0, _finite(confidence, 0.0))),
            "source_track": None if track is None else str(track),
        })
    rows.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    return rows


def select_review_windows(data, spans, words=None, max_windows=28, context_seconds=0.8):
    """选择导演需要实际听/看的少量高价值窗口。

    剪切入点和出点永远优先；其次是低置信度词、语义冲突、章节转折、
    高价值句和无对白保留区间。返回值只描述窗口，不读取媒体，因此可以先写
    入任务记忆，再由音频和视觉工作器按相同窗口取证。
    """
    duration = _finite(data.get("duration"), 0.0)
    segments = data.get("segments") or []
    raw_spans = sorted((_finite(a), _finite(b)) for a, b in (spans or [])
                       if _finite(b) > _finite(a))
    if duration <= 0:
        duration = max([0.0] + [end for _start, end in raw_spans] +
                       [_finite(row[1]) for row in segments if len(row) >= 2])
    clean_spans = [(max(0.0, start), min(duration, end))
                   for start, end in raw_spans
                   if min(duration, end) > max(0.0, start)]
    context = max(0.2, _finite(context_seconds, 0.8))
    candidates = []

    def add(priority, kind, time_value, reason, refs=None, modalities=None,
            span_index=None):
        time_value = _finite(time_value)
        is_boundary = kind in {"cut_start", "cut_end"}
        target_interval = None
        if not is_boundary:
            # Source transcript uncertainty is not a defect in the delivered
            # film when the editor removed that source interval.  Spend the
            # optional review budget only on retained content; missing story
            # context remains the text director's responsibility.
            if span_index is None:
                span_index = next((index for index, (left, right) in enumerate(clean_spans)
                                   if left <= time_value <= right), None)
            if span_index is None:
                return
            kept_start, kept_end = clean_spans[span_index]
            if not kept_start <= time_value <= kept_end:
                return
            target_interval = [max(kept_start, time_value - 0.30),
                               min(kept_end, time_value + 0.30)]
            if target_interval[1] <= target_interval[0]:
                return
        time_value = max(0.0, min(duration, time_value))
        candidate = {
            "priority": int(priority), "kind": str(kind), "time": time_value,
            "interval": [max(0.0, time_value - context),
                         min(duration, time_value + context)],
            "reason": str(reason), "evidence_refs": list(refs or []),
            "modalities": list(modalities or ["audio", "visual"]),
            "span_index": span_index,
        }
        if target_interval is not None:
            candidate["target_interval"] = target_interval
        candidates.append(candidate)

    for index, (start, end) in enumerate(clean_spans):
        add(120, "cut_start", start, "成片入点需要听首字并检查画面承接",
            ["span:%d" % index], ["text", "audio", "visual"], index)
        add(119, "cut_end", end, "成片出点需要听尾音并检查动作/结果是否完整",
            ["span:%d" % index], ["text", "audio", "visual"], index)
        has_dialogue = any(_finite(row[1]) > start and _finite(row[0]) < end
                           for row in segments if len(row) >= 2)
        if not has_dialogue:
            add(108, "no_dialogue_event", (start + end) / 2.0,
                "无对白保留区间需要用游戏声音和画面确认价值",
                ["span:%d" % index], ["audio", "visual"])

    for row in normalize_words(words):
        if row["confidence"] < 0.62:
            for span_index, (kept_start, kept_end) in enumerate(clean_spans):
                left = max(kept_start, row["start"])
                right = min(kept_end, row["end"])
                if right > left:
                    add(112, "low_confidence_word", (left + right) / 2.0,
                        "低置信度词需要同步复听和看口型/画面上下文",
                        [row["id"]], ["text", "audio", "visual"], span_index)
    for index, finding in enumerate(data.get("semantic_findings") or []):
        if isinstance(finding, dict):
            time_value = finding.get("time", finding.get("start", 0.0))
            add(114, "semantic_conflict", time_value,
                finding.get("message") or "文字语义和上下文存在冲突",
                ["semantic:%d" % index], ["text", "audio", "visual"])
    for index, section in enumerate((data.get("script") or {}).get("sections") or []):
        add(101, "chapter_transition", section.get("start", 0.0),
            "章节转折：%s" % (section.get("title") or "未命名章节"),
            ["section:%d" % index], ["text", "audio", "visual"])

    scores = [] if llm_gateway.api_enabled() else data.get("scores") or []
    if len(scores) == len(segments):
        for index in sorted(range(len(scores)), key=lambda i: (-_finite(scores[i]), i))[:5]:
            row = segments[index]
            add(90, "score_peak", (_finite(row[0]) + _finite(row[1])) / 2.0,
                "高价值语句需要确认声音情绪和结果画面",
                ["segment:%d" % index], ["text", "audio", "visual"])

    candidates.sort(key=lambda row: (-row["priority"], row["time"], row["kind"]))
    # ``max_windows`` is an extras budget, never a boundary budget.  Every
    # retained span has two audible edit edges and both are mandatory.  The
    # PCM cache makes this affordable even for long timelines; sampling a few
    # edges and calling the entire film "heard" would be a false pass.
    extra_limit = max(0, int(max_windows))
    boundaries = [row for row in candidates if row["kind"] in {"cut_start", "cut_end"}]
    extras = [row for row in candidates if row["kind"] not in {"cut_start", "cut_end"}]
    selected = list(boundaries)
    extras_added = 0
    for row in extras:
        if extras_added >= extra_limit:
            break
        protected = row["kind"] == "semantic_conflict"
        if not protected and any(abs(row["time"] - other["time"]) < 0.35
                                 and row["kind"] == other["kind"] for other in selected):
            continue
        selected.append(row)
        extras_added += 1
    selected.sort(key=lambda row: (row["time"], -row["priority"], row["kind"]))
    for index, row in enumerate(selected, 1):
        row["id"] = "review_%03d" % index
        row["time"] = round(row["time"], 6)
        row["interval"] = [round(value, 6) for value in row["interval"]]
        if "target_interval" in row:
            row["target_interval"] = [round(value, 6) for value in row["target_interval"]]
    return selected


def _frame_rms(samples, sample_rate, frame_ms=20.0, hop_ms=10.0):
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    frame = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    if samples.size < frame:
        if not samples.size:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        samples = np.pad(samples, (0, frame - samples.size))
    count = 1 + (samples.size - frame) // hop
    starts = hop * np.arange(count)
    indices = starts[:, None] + np.arange(frame)[None, :]
    frames = samples[indices]
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    return rms, starts.astype(np.float32) / float(sample_rate)


def _region_ratio(active, times, left, right):
    mask = (times >= left) & (times < right)
    return float(np.mean(active[mask])) if np.any(mask) else 0.0


def _merge_intervals(intervals, gap=0.05):
    clean = sorted((_finite(a), _finite(b)) for a, b in intervals if _finite(b) > _finite(a))
    out = []
    for start, end in clean:
        if out and start <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [[round(a, 6), round(b, 6)] for a, b in out]


def analyze_audio_span(samples, sample_rate, span, words=None, track_id="speech",
                       role="speech", context_seconds=0.35,
                       boundary_guard_seconds=0.08, time_offset=0.0,
                       check_start=True, check_end=True, check_content=True):
    """在一条真实音轨上审核一个成片区间。

    ``samples`` 必须是整条音轨（单声道浮点或可转为浮点）。检测不会仅凭
    ASR 时间戳下结论，而会比较切口前后真实波形活动。``role`` 为 ``speech``
    时首尾词和持续语音属于硬保护；其他音轨的持续事件会作为需保留的尾音。
    """
    sample_rate = int(sample_rate)
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须大于 0")
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    time_offset = max(0.0, _finite(time_offset, 0.0))
    duration = time_offset + samples.size / float(sample_rate)
    start = max(time_offset, min(duration, _finite(span[0])))
    end = max(start, min(duration, _finite(span[1])))
    context = max(0.12, _finite(context_seconds, 0.35))
    window_start, window_end = max(time_offset, start - context), min(duration, end + context)
    lo = int(math.floor((window_start - time_offset) * sample_rate))
    hi = int(math.ceil((window_end - time_offset) * sample_rate))
    lo, hi = max(0, lo), min(samples.size, hi)
    window = samples[lo:hi]
    rms, rel_times = _frame_rms(window, sample_rate)
    times = rel_times + window_start
    db = 20.0 * np.log10(np.maximum(rms, 1e-6)) if rms.size else np.zeros(0)
    if db.size:
        noise = float(np.percentile(db, 20))
        peak = float(np.percentile(db, 95))
        threshold = min(-24.0, max(-52.0, noise + max(7.0, (peak - noise) * 0.30)))
        active = db >= threshold
    else:
        noise = peak = threshold = -120.0
        active = np.zeros(0, dtype=bool)

    guard = max(0.04, _finite(boundary_guard_seconds, 0.08))
    pre_ratio = _region_ratio(active, times, max(window_start, start - 0.14), start)
    head_ratio = _region_ratio(active, times, start, min(end, start + guard))
    tail_ratio = _region_ratio(active, times, max(start, end - guard), end)
    post_ratio = _region_ratio(active, times, end, min(window_end, end + 0.14))
    inside_ratio = _region_ratio(active, times, start, end)
    # ASR words belong to their source track.  A word on the commentary track
    # must not turn a coincident game sound on another track into speech proof.
    # Older indexes do not name a track, so retain their unassigned words.
    word_rows = [row for row in normalize_words(words)
                 if row["source_track"] is None
                 or row["source_track"] == str(track_id)]
    overlap_words = [row for row in word_rows if row["end"] > start and row["start"] < end]
    # The caller may pass only 0.24 seconds on the kept side of a cut.  Look
    # across the inspected context for the nearest retained word as well, so a
    # completed word just outside that small target is still clearance proof.
    # It is not a boundary anchor unless the cut actually violates its guard.
    start_words = [row for row in word_rows
                   if row["end"] > start and row["start"] < window_end]
    end_words = [row for row in word_rows
                 if row["start"] < end and row["end"] > window_start]
    first_word = min(start_words, key=lambda row: (row["start"], row["end"])) \
        if start_words else None
    last_word = max(end_words, key=lambda row: (row["end"], row["start"])) \
        if end_words else None
    refs = ["audio:%s" % track_id]
    findings = []
    confidence_values = [row["confidence"] for row in overlap_words]
    word_confidence = (min(confidence_values) if confidence_values else
                       (0.62 if role == "speech" else 0.76))

    # 词级时间和波形必须相互印证。切口前仍有活动且入点一开始就有声，说明
    # 很可能已经切进辅音/元音起始；短首字（如“我”）尤其不能靠句级时间恢复。
    epsilon = 1e-6
    start_word_close = bool(first_word and first_word["start"] < start + guard - epsilon)
    end_word_close = bool(last_word and last_word["end"] > end - guard + epsilon)

    def boundary_finding(side, word, interval):
        word_refs = [word["id"]] if word else []
        finding = _finding(
            "speech_cut_%s" % side, "audio", "critical",
            ("入点可能切进首字或连续发音" if side == "start" else
             "出点可能切掉末字、呼吸或情绪尾音"),
            refs + word_refs, True, interval,
            max(0.72, word["confidence"]) if word else 0.72,
            "expand_%s" % side)
        finding.update({
            "boundary_side": side,
            "boundary_time": round(start if side == "start" else end, 6),
            "boundary_word_refs": word_refs,
            "evidence_kind": "word_guard" if word else "waveform_only",
            "word_guard_seconds": round(guard, 6),
            "boundary_anchor": ({
                "word_ref": word["id"], "start": word["start"],
                "end": word["end"], "source_track": word["source_track"],
            } if word else None),
        })
        return finding

    # Continuous environmental sound is not proof that an already complete
    # word still needs padding.  Only a violated word guard can be word-backed;
    # when there is no local word evidence, retain the conservative 0.72
    # waveform-only hint for one bounded repair attempt in the caller.
    suggested_start = max(time_offset, min(
        start - 0.16,
        (first_word["start"] - 0.12) if first_word else start - 0.12,
    ))
    if (check_start and role == "speech" and head_ratio >= 0.45
            and (pre_ratio >= 0.15 or start_word_close)
            and (start_word_close or first_word is None)
            and suggested_start < start - max(0.01, 1.0 / sample_rate)):
        findings.append(boundary_finding(
            "start", first_word if start_word_close else None,
            [suggested_start, end]))
    suggested_end = min(duration, max(
        end + 0.18,
        (last_word["end"] + 0.14) if last_word else end + 0.14,
    ))
    if (check_end and role == "speech" and tail_ratio >= 0.45
            and (post_ratio >= 0.15 or end_word_close)
            and (end_word_close or last_word is None)
            and suggested_end > end + max(0.01, 1.0 / sample_rate)):
        findings.append(boundary_finding(
            "end", last_word if end_word_close else None,
            [start, suggested_end]))
    # 持续音乐、环境底噪跨过切口时由转场的音频淡化/交叉淡化处理，不能把
    # 每一条非语音轨都记成重大问题。只有窗口里存在明显动态突起时，才把它
    # 当成需要向外保护的游戏音效或动作尾音。
    event_contrast_db = max(0.0, peak - noise)
    if (check_end and role != "speech" and tail_ratio >= 0.40
            and post_ratio >= 0.25 and event_contrast_db >= 6.0):
        findings.append(_finding(
            "important_audio_tail", "audio", "major", "非语音音轨在出点后仍有连续事件或游戏音效",
            refs, False, [start, min(duration, end + 0.30)], 0.84, "expand_end"))
    if (check_content and role == "speech" and end > start + 0.25
            and inside_ratio < 0.04):
        findings.append(_finding(
            "unexpected_silence", "audio", "major", "保留区间内几乎没有可听语音活动",
            refs, False, [start, end], 0.82, "inspect_track_or_span"))
    clipping_ratio = float(np.mean(np.abs(window) >= 0.995)) if window.size else 0.0
    abs_window = np.abs(window)
    crest_factor = (float(abs_window.max()) / max(1e-6, float(np.sqrt(
        np.mean(window * window) + 1e-12)))) if window.size else 0.0
    zero_crossing_rate = (float(np.mean(np.signbit(window[1:]) != np.signbit(window[:-1])))
                          if window.size > 1 else 0.0)
    dynamic_range_db = max(0.0, peak - noise)
    if clipping_ratio >= 0.003:
        findings.append(_finding(
            "digital_clipping", "audio", "major", "音频存在明显数字削波，听感和识别结果可能失真",
            refs, False, [start, end], min(0.99, 0.80 + clipping_ratio * 10.0),
            "inspect_audio"))

    score = max(0.0, 100.0 - sum(
        {"critical": 35.0, "major": 12.0, "minor": 4.0, "info": 0.0}[row["severity"]]
        for row in findings))
    return {
        "schema": "director_audio_evidence_v1",
        "track_id": str(track_id), "role": str(role),
        "time_offset": round(time_offset, 6),
        "checks": {"start": bool(check_start), "end": bool(check_end),
                   "content": bool(check_content)},
        "span": [round(start, 6), round(end, 6)],
        "window": [round(window_start, 6), round(window_end, 6)],
        "sample_rate": sample_rate, "sample_count": int(window.size),
        "actual_audio_inspected": bool(window.size),
        # Only words proving an actual boundary defect may promote a repair.
        # Other transcript words remain available as context, separately.
        "word_refs": sorted({ref for row in findings
                             for ref in row.get("boundary_word_refs") or []}),
        "context_word_refs": [row["id"] for row in overlap_words],
        "word_confidence": round(word_confidence, 5),
        "metrics": {
            "noise_floor_db": round(noise, 4), "peak_db": round(peak, 4),
            "activity_threshold_db": round(threshold, 4),
            "pre_activity_ratio": round(pre_ratio, 5),
            "head_activity_ratio": round(head_ratio, 5),
            "inside_activity_ratio": round(inside_ratio, 5),
            "tail_activity_ratio": round(tail_ratio, 5),
            "post_activity_ratio": round(post_ratio, 5),
            "clipping_ratio": round(clipping_ratio, 7),
            # 这些是可复现的韵律/音色代理值；不把它们伪装成情绪标签。
            "dynamic_range_db": round(dynamic_range_db, 4),
            "crest_factor": round(crest_factor, 5),
            "zero_crossing_rate": round(zero_crossing_rate, 6),
        },
        "score": round(score, 3), "findings": findings,
    }


def analyze_audio_tracks(tracks, sample_rate, spans, words=None):
    """按确定顺序审核多条音轨和多个 span。

    ``tracks`` 接受 ``{track_id: samples}`` 或
    ``{track_id: {samples, role, sample_rate}}``。词证据可以是公共列表，或按
    track id 分组的字典。
    """
    reports = []
    for track_id in sorted(tracks or {}, key=str):
        item = tracks[track_id]
        if isinstance(item, dict):
            samples = item.get("samples", [])
            role = item.get("role", "speech" if not reports else "game")
            sr = int(item.get("sample_rate") or sample_rate)
        else:
            samples, sr = item, int(sample_rate)
            role = "speech" if not reports else "game"
        track_words = words.get(track_id, []) if isinstance(words, dict) else (words or [])
        for span in sorted(((_finite(a), _finite(b)) for a, b in spans), key=lambda row: row):
            reports.append(analyze_audio_span(
                samples, sr, span, track_words, str(track_id), str(role)))
    return reports


def extract_audio_window(source, output, start, end, audio_stream=None,
                         sample_rate=16000, ffmpeg="ffmpeg"):
    """提取导演要听的真实 PCM 窗口；Windows 下不创建控制台窗口。"""
    start, end = max(0.0, _finite(start)), _finite(end)
    if end <= start:
        raise ValueError("音频窗口结束时间必须晚于开始时间")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
               "-ss", "%.6f" % start, "-t", "%.6f" % (end - start),
               "-i", str(source)]
    if audio_stream is not None:
        command += ["-map", "0:a:%d" % int(audio_stream)]
    command += ["-vn", "-ac", "1", "-ar", str(int(sample_rate)),
                "-c:a", "pcm_s16le", str(output)]
    result = subprocess.run(command, capture_output=True, timeout=120,
                            creationflags=_no_window())
    if result.returncode != 0 or not output.is_file():
        message = (result.stderr or b"").decode("utf-8", "replace")[-1000:]
        raise RuntimeError("音频窗口提取失败：%s" % message)
    return str(output)


def read_pcm16_wav(path):
    """读取 :func:`extract_audio_window` 生成的单声道 PCM16 WAV。"""
    with wave.open(str(path), "rb") as handle:
        channels, width, sample_rate = (handle.getnchannels(),
                                        handle.getsampwidth(),
                                        handle.getframerate())
        if width != 2:
            raise ValueError("只支持 PCM16 WAV")
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _text_findings(report):
    findings = []
    for index, issue in enumerate((report or {}).get("issues") or []):
        severity = issue.get("severity", "major")
        severity = severity if severity in SEVERITY_RANK else "major"
        refs = ["text_issue:%d" % index]
        refs += ["segment:%s" % value for value in issue.get("indices", [])]
        event_ids = [str(value) for value in issue.get("event_ids") or []]
        refs += ["event:%s" % value for value in event_ids]
        source_revision = str((report or {}).get(
            "source_timeline_revision") or "")
        if source_revision:
            refs.append("source_timeline:%s" % source_revision[:16])
        finding = _finding(
            issue.get("type", "text_issue"), "text", severity,
            issue.get("message", "文字结构需要复核"), refs,
            severity == "critical", issue.get("interval"),
            issue.get("confidence", 0.90), issue.get("action", "inspect"))
        finding.update({
            "event_ids": event_ids,
            "coordinate_space": str(issue.get("coordinate_space") or "source"),
            "source_timeline_revision": source_revision or None,
        })
        findings.append(finding)
    return findings


def _visual_findings(report):
    findings = []
    for node in (report or {}).get("nodes") or []:
        node_id = str(node.get("id") or "visual_unknown")
        # Old reports predate precision metadata and were source-seek frames;
        # only the explicitly labelled sparse proxy loses veto authority.
        exact_visual = str(node.get("frame_precision") or "") != \
            "review_proxy_nearest_0.5s"
        sample_times = node.get("sample_times") or []
        interval = ([min(sample_times), max(sample_times)] if len(sample_times) >= 2 else
                    [max(0.0, _finite(node.get("time")) - 1.0),
                     _finite(node.get("time")) + 1.0])
        for issue in node.get("issues") or []:
            kind = str(issue.get("type") or "visual_issue")
            # 2fps 代理只给导演补充上下文。它可能把 0.18 秒两侧折叠到
            # 同一帧，不能据此自动否决或移动刀口；只有源时间精确帧有此权限。
            veto = kind in VISUAL_VETO_TYPES and exact_visual
            severity = "critical" if veto else str(issue.get("severity") or "major")
            severity = severity if severity in SEVERITY_RANK else "major"
            confidence = issue.get("confidence", 0.88 if veto else 0.78)
            findings.append(_finding(
                kind, "visual", severity, issue.get("message", "关键画面需要复核"),
                ["visual:%s" % node_id], veto, interval, confidence,
                issue.get("action", "inspect_visual")))
        vlm = node.get("vlm") or {}
        parsed = vlm.get("parsed") or {}
        if vlm.get("status") == "ok" and parsed:
            veto = exact_visual and (parsed.get("risk") == "drop"
                                     or parsed.get("continuity") == "broken")
            if parsed.get("scene_type") == "loading_black":
                veto = exact_visual
            if veto or (not exact_visual and
                        (parsed.get("risk") == "drop"
                         or parsed.get("continuity") == "broken"
                         or parsed.get("scene_type") == "loading_black")):
                findings.append(_finding(
                    "vlm_visual_veto" if veto else "vlm_proxy_review",
                    "visual", "critical" if veto else "major",
                    parsed.get("summary") or "视觉模型确认关键画面不可用或不连续",
                    ["visual:%s" % node_id, "vlm:%s" % node_id], veto, interval,
                    0.82 if veto else 0.68,
                    "rebuild_interval" if veto else "inspect_proxy_context"))
    return findings


def _audio_findings(reports):
    """Collect audio findings without multiplying one event by track count."""
    findings = []
    grouped_tails = {}
    for report in reports or []:
        window_id = str(report.get("review_window_id") or "")
        for item in report.get("findings") or []:
            row = dict(item)
            refs = list(row.get("evidence_refs") or [])
            if window_id:
                refs.append("review:%s" % window_id)
            row["evidence_refs"] = sorted(set(str(value) for value in refs))
            if row.get("type") != "important_audio_tail":
                findings.append(row)
                continue
            interval = row.get("suggested_interval") or []
            key = window_id or ("%.3f" % _finite(interval[0]) if interval else row.get("id"))
            bucket = grouped_tails.setdefault(key, {
                "intervals": [], "refs": [], "confidence": 0.0,
                "message": row.get("message"), "action": row.get("action"),
                "severity": row.get("severity") or "major",
                "learnable": bool(row.get("learnable", False)),
            })
            if len(interval) == 2:
                bucket["intervals"].append(interval)
            bucket["refs"].extend(row["evidence_refs"])
            bucket["confidence"] = max(bucket["confidence"], _finite(
                row.get("confidence"), 0.0))
            if SEVERITY_RANK.get(row.get("severity"), 0) > \
                    SEVERITY_RANK.get(bucket["severity"], 0):
                bucket["severity"] = row.get("severity")
            bucket["learnable"] = bucket["learnable"] and bool(
                row.get("learnable", False))
    for key in sorted(grouped_tails):
        bucket = grouped_tails[key]
        merged = _merge_intervals(bucket["intervals"])
        interval = ([min(row[0] for row in merged), max(row[1] for row in merged)]
                    if merged else None)
        findings.append(_finding(
            "important_audio_tail", "audio", bucket["severity"],
            bucket["message"] or "非语音音轨在出点后仍有连续事件或游戏音效",
            sorted(set(bucket["refs"])), False, interval,
            bucket["confidence"], bucket["action"] or "expand_end"))
        findings[-1]["learnable"] = bucket["learnable"]
    return findings


def _dedupe_findings(findings):
    chosen = {}
    for finding in findings:
        key = (finding["type"], tuple(finding["evidence_refs"]),
               tuple(finding["suggested_interval"] or []))
        previous = chosen.get(key)
        if previous is None or SEVERITY_RANK[finding["severity"]] > \
                SEVERITY_RANK[previous["severity"]]:
            chosen[key] = finding
    return sorted(chosen.values(), key=lambda row: (
        -SEVERITY_RANK[row["severity"]], row["modality"], row["type"], row["id"]))


def combine_review(text_report=None, audio_reports=None, visual_report=None,
                   require_audio=True, require_visual=False,
                   learning_confidence=0.80, text_applicable=True,
                   expected_audio_windows=None,
                   expected_timeline_revision=None):
    """合并导演证据并给出否决、返工区间和自动学习资格。

    有对白时文字与音频的主证据分数固定各占 50%；确认无对白时音频承担
    主证据。视觉不用于“平均掉”听觉或文字问题，而是作为次级扣分和确定性
    否决。未提供重模型时视觉规则仍可用。
    """
    audio_reports = list(audio_reports or [])
    findings = _text_findings(text_report)
    findings.extend(_audio_findings(audio_reports))
    findings.extend(_visual_findings(visual_report))

    revision_mismatches = []
    if expected_timeline_revision:
        expected_revision = str(expected_timeline_revision)
        if text_report is not None and str(
                text_report.get("timeline_revision") or "") != expected_revision:
            revision_mismatches.append("text")
        for report in audio_reports:
            if str(report.get("timeline_revision") or "") != expected_revision:
                revision_mismatches.append("audio")
                break
        if visual_report is not None and str(
                visual_report.get("timeline_revision") or "") != expected_revision:
            revision_mismatches.append("visual")
        if revision_mismatches:
            findings.append(_finding(
                "timeline_revision_mismatch", "container", "critical",
                "文字/听觉/视觉没有审同一版最终时间轴：%s" %
                ",".join(sorted(set(revision_mismatches))),
                ["timeline:%s" % expected_revision[:16]], True, None, 1.0,
                "repeat_multimodal_review"))

    expected_pairs = set()
    for window in expected_audio_windows or []:
        if window.get("kind") not in {"cut_start", "cut_end"}:
            continue
        window_id = str(window.get("id") or "")
        for track in window.get("expected_audio_tracks") or []:
            expected_pairs.add((window_id, str(track)))
    reviewed_pairs = {
        (str(row.get("review_window_id") or ""), str(row.get("track_id") or ""))
        for row in audio_reports if row.get("actual_audio_inspected")
    }
    reviewed_required = expected_pairs.intersection(reviewed_pairs)
    audio_coverage = {
        "expected_edge_tracks": len(expected_pairs),
        "reviewed_edge_tracks": len(reviewed_required),
        "missing_edge_tracks": len(expected_pairs - reviewed_pairs),
        "coverage": (len(reviewed_required) / float(len(expected_pairs))
                     if expected_pairs else
                     (1.0 if any(row.get("actual_audio_inspected")
                                 for row in audio_reports) else 0.0)),
    }
    coverage_complete = (not expected_pairs or
                         audio_coverage["missing_edge_tracks"] == 0)

    evidence_status = {
        "text": ("not_applicable" if not text_applicable else
                 ("available" if text_report is not None else "missing")),
        "audio": "available" if (coverage_complete and
                                      any(row.get("actual_audio_inspected")
                                          for row in audio_reports)) else "missing",
        "visual": "available" if (visual_report or {}).get("nodes") else "not_requested",
    }
    if require_visual and evidence_status["visual"] != "available":
        evidence_status["visual"] = "missing"
    if text_applicable and text_report is None:
        findings.append(_finding(
            "text_evidence_missing", "text", "critical", "导演没有收到文字/剧本主证据",
            [], True, None, 1.0, "collect_text_evidence"))
    if require_audio and evidence_status["audio"] != "available":
        if expected_pairs and not coverage_complete:
            findings.append(_finding(
                "audio_boundary_coverage_incomplete", "audio", "critical",
                "导演没有听完全部切口与所选音轨（%d/%d）" % (
                    audio_coverage["reviewed_edge_tracks"],
                    audio_coverage["expected_edge_tracks"]),
                ["audio_edge_coverage"], True, None, 1.0,
                "collect_all_boundary_audio"))
        else:
            findings.append(_finding(
                "audio_evidence_missing", "audio", "critical",
                "导演没有收到可听的真实音频窗口",
                [], True, None, 1.0, "collect_audio_evidence"))
    if require_visual and evidence_status["visual"] != "available":
        findings.append(_finding(
            "visual_evidence_missing", "visual", "major", "高风险节点没有可用关键画面",
            [], False, None, 1.0, "collect_visual_evidence"))

    findings = _dedupe_findings(findings)
    text_score = max(0.0, min(100.0, _finite(
        (text_report or {}).get("score"), 0.0 if text_report is None else 100.0)))
    audio_score = (sum(_finite(row.get("score"), 0.0) for row in audio_reports)
                   / len(audio_reports)) if audio_reports else 0.0
    weights = ({"text": 0.5, "audio": 0.5} if text_applicable
               else {"text": 0.0, "audio": 1.0})
    primary_score = weights["text"] * text_score + weights["audio"] * audio_score
    visual_penalty = sum({"critical": 18.0, "major": 7.0, "minor": 2.0, "info": 0.0}
                         [row["severity"]] for row in findings
                         if row["modality"] == "visual")
    score = max(0.0, primary_score - min(25.0, visual_penalty))
    vetoes = [row for row in findings if row["veto"]]
    critical = sum(row["severity"] == "critical" for row in findings)
    major = sum(row["severity"] == "major" for row in findings)
    rework = _merge_intervals(
        row["suggested_interval"] for row in findings
        if row["suggested_interval"] is not None
        and SEVERITY_RANK[row["severity"]] >= SEVERITY_RANK["major"])
    low_confidence = [row["id"] for row in findings
                      if row["confidence"] < float(learning_confidence)]
    evidence_complete = (evidence_status["text"] in ("available", "not_applicable")
                         and (not require_audio or evidence_status["audio"] == "available")
                         and (not require_visual or evidence_status["visual"] == "available"))
    # 通过线按"口播/实况剪辑"的现实定：硬切是默认手段，切点两侧音量和画面
    # 本来就会变（剪映《衔接与转场配方》：「口播和快节奏内容默认硬切」）。
    # 真正该硬拦的是 veto / critical（半句、切字），这些在别处已经判了。
    # 原来 score >= 72 是按电影级标准定的，会用"背景声不同"把正常片子卡死
    # （实测 66 分、0 否决、0 critical，却因为 2 个 major 不出片）。
    passed = bool(evidence_complete and not vetoes and critical == 0
                  and major <= 4 and score >= 55.0)
    auto_learning_eligible = bool(
        passed and not low_confidence and all(row.get("learnable", False)
                                              for row in findings))
    return {
        "schema": SCHEMA,
        "timeline_revision": (str(expected_timeline_revision)
                              if expected_timeline_revision else None),
        "evidence_status": evidence_status,
        "audio_coverage": audio_coverage,
        "primary_weights": weights,
        "scores": {
            "text": round(text_score, 3), "audio": round(audio_score, 3),
            "primary": round(primary_score, 3), "final": round(score, 3),
        },
        "passed": passed, "vetoed": bool(vetoes),
        "veto_count": len(vetoes), "critical_issues": critical,
        "major_issues": major, "findings": findings,
        "rework_intervals": rework,
        "low_confidence_findings": low_confidence,
        "auto_learning_eligible": auto_learning_eligible,
        "learning_policy": {
            "minimum_confidence": float(learning_confidence),
            "unverified_observations_are_promoted": False,
        },
    }

