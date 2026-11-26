# -*- coding: utf-8 -*-
"""删掉“没有说话的停顿空段”——说话轨上的长静音。

为什么（子枫 2026-11-23）：
  「重要的是所选音轨的三坐标系，并且要在三坐标系的基础上延伸删掉
    没有说话的停顿空段。」

  他说话时会停顿（想、换气、看画面），那些 3~10 秒的空白留着，
  节奏就是拖沓的。但停顿分两种：
    · 换气/短停顿（< 1.2 秒）—— 自然的语言节奏，必须留
    · 长空白（≥ 1.2 秒）—— 该删，两端各留 0.2 秒气口

判据用两条同时成立（避免删掉“轻声说的话”）：
  · 能量轴：连续低于 −45dB
  · 文字轴：这段时间里没有词

不要用它删“情绪拖音”——拖音是有声音的（能量不低），不在这个判据里。
"""
from __future__ import annotations

import subprocess

import numpy as np

SR = 16000
SILENCE_DB = -45.0        # 低于这个算“几乎没声”
MIN_SILENCE = 1.2         # 连续静音超过这么长才删
EDGE_PAD = 0.20           # 删除时两端各留的气口
MIN_REMAIN = 0.5          # 删完每段至少还剩这么长
MERGE_GAP = 0.10          # 两段静音之间 <0.1 秒有声的，合并成一段
NO_DIALOGUE_PAD = 0.25    # 无对白长段两端保留的自然气口
WORD_GUARD = 0.15         # 能量静音碰到边界词时，只保护词本身，不豁免整段


def _track_audio(src, ffmpeg, track, ss=None, dur=None, sr=SR):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-i", src, "-map", "0:a:%d" % track, "-ac", "1",
            "-ar", str(sr), "-f", "s16le", "-"]
    done = subprocess.run(cmd, capture_output=True)
    return (np.frombuffer(done.stdout, dtype="<i2").astype(np.float32)
            / 32768.0)


def _env(audio, sr=SR, win=0.02):
    step = max(1, int(sr * win))
    n = audio.size // step
    if not n:
        return np.zeros(0), win
    frames = audio[:n * step].reshape(n, step)
    return (20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)),
                                     1e-9)), win)


def find_silence(audio, words=None, sr=SR, silence_db=SILENCE_DB,
                 min_silence=MIN_SILENCE):
    """找“长静音”区间 [(start, end), ...]（秒）。"""
    env, win = _env(audio, sr)
    if env.size == 0:
        return []
    quiet = env < silence_db
    spans = []
    start = None
    for k, flag in enumerate(quiet):
        if flag and start is None:
            start = k
        elif not flag and start is not None:
            spans.append((start * win, k * win))
            start = None
    if start is not None:
        spans.append((start * win, env.size * win))
    # 合并“中间只有 0.1 秒有声”的相邻静音
    merged = []
    for s in spans:
        if merged and s[0] - merged[-1][1] <= MERGE_GAP:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(s)
    out = []
    for s, e in merged:
        pieces = [(s, e)]
        # A word at one edge used to exempt the entire quiet range.  In real
        # footage that preserved 80-170 seconds of silence after one tail word.
        # Subtract only guarded word intervals and keep evaluating the rest.
        for word in words or []:
            try:
                word_start = float(word.get("start", 0.0)) - WORD_GUARD
                word_end = float(word.get("end", 0.0)) + WORD_GUARD
            except (AttributeError, TypeError, ValueError):
                continue
            if word_end <= s or word_start >= e:
                continue
            next_pieces = []
            for left, right in pieces:
                if word_end <= left or word_start >= right:
                    next_pieces.append((left, right))
                    continue
                if word_start > left:
                    next_pieces.append((left, min(right, word_start)))
                if word_end < right:
                    next_pieces.append((max(left, word_end), right))
            pieces = next_pieces
            if not pieces:
                break
        for left, right in pieces:
            if right - left >= min_silence:
                out.append((round(left, 3), round(right, 3)))
    return out


def _speech_intervals(segments, bridge=0.35):
    """Normalize ASR segments into merged speech coverage intervals."""
    rows = []
    for item in segments or []:
        try:
            if isinstance(item, dict):
                start = float(item.get("start", item.get("a", 0.0)))
                end = float(item.get("end", item.get("b", 0.0)))
            else:
                start, end = float(item[0]), float(item[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if end > start:
            rows.append((start, end))
    rows.sort()
    merged = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + float(bridge):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def find_no_dialogue(spans, segments, min_gap=4.0):
    """Find retained ranges with no detected dialogue, regardless of loudness.

    Game audio and music are often continuously loud, so energy-only silence
    detection cannot remove long sections where nobody speaks.  This pass uses
    the transcript coverage as the authority and only targets gaps already
    inside selected spans.
    """
    speech = _speech_intervals(segments)
    threshold = max(0.5, float(min_gap))
    gaps = []
    for raw_start, raw_end in spans or []:
        start, end = float(raw_start), float(raw_end)
        if end <= start:
            continue
        cursor = start
        for speech_start, speech_end in speech:
            if speech_end <= start:
                continue
            if speech_start >= end:
                break
            left = max(start, speech_start)
            right = min(end, speech_end)
            if left - cursor >= threshold:
                gaps.append((cursor, left))
            cursor = max(cursor, right)
        if end - cursor >= threshold:
            gaps.append((cursor, end))
    return [(round(start, 3), round(end, 3)) for start, end in gaps]


def apply(spans, silences, pad=EDGE_PAD, min_remain=MIN_REMAIN):
    """把长静音从保留段里挖掉，两端各留 pad 秒气口。返回新 spans（源片坐标）。"""
    if not silences or not spans:
        return list(spans or [])
    out = []
    for a, b in spans:
        cuts = []
        for s, e in silences:
            cs, ce = max(a, s + pad), min(b, e - pad)
            if ce - cs > 0.05:
                cuts.append((cs, ce))
        if not cuts:
            out.append((a, b))
            continue
        cuts.sort()
        cur = a
        for cs, ce in cuts:
            if cs > cur + min_remain:
                out.append((cur, cs))
            cur = max(cur, ce)
        if b > cur + min_remain:
            out.append((cur, b))
    # 合并间隔 <0.15 秒的相邻段（切太碎没意义）
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] < 0.15:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a > 0.1]


def trim(src, ffmpeg, spans, track, words=None, log=None,
         min_silence=MIN_SILENCE, pad=EDGE_PAD, audio=None,
         sample_rate=SR):
    """一步到位：读说话轨 → 找长静音 → 从 spans 里挖掉。

    ``audio`` 是时间轴运行时已经建立好的单轨 PCM。生产链必须优先传它，
    这样切点重选、停顿检测和导演复听共用同一份媒体索引；仅旧调用方没有
    PCM 时才保留直接调用 ffmpeg 的兼容路径。这里绝不混合多条音轨。
    """
    sample_rate = int(sample_rate or SR)
    if audio is None:
        audio = _track_audio(src, ffmpeg, track, sr=sample_rate)
    else:
        audio = np.asarray(audio, dtype=np.float32)
    if audio.size < sample_rate:
        return list(spans or []), []
    silences = find_silence(audio, words=words, sr=sample_rate,
                            min_silence=min_silence)
    if not silences:
        return list(spans or []), []
    total = sum(e - s for s, e in silences)
    new_spans = apply(spans, silences, pad=pad)
    if log:
        log("   停顿空段：%d 段，共 %.1f 秒（两端各留 %.1f 秒气口）"
            % (len(silences), total, pad))
        # 只保留“真的被删掉”的（落在保留段内的）
        removed = 0.0
        for s, e in silences:
            for a, b in spans or []:
                lo, hi = max(a, s + pad), min(b, e - pad)
                if hi > lo:
                    removed += hi - lo
        if log:
            log("   实际删掉 %.1f 秒（成片 %.1f → %.1f 秒）"
                % (removed, sum(b - a for a, b in spans or []),
                   sum(b - a for a, b in new_spans)))
    return new_spans, silences


def trim_no_dialogue(spans, segments, min_gap=4.0, pad=NO_DIALOGUE_PAD,
                     log=None):
    """Remove long transcript-free ranges even when background audio is loud."""
    gaps = find_no_dialogue(spans, segments, min_gap=min_gap)
    if not gaps:
        return list(spans or []), []
    new_spans = apply(spans, gaps, pad=pad)
    before = sum(float(end) - float(start) for start, end in spans or [])
    after = sum(float(end) - float(start) for start, end in new_spans)
    if log and before - after > 0.0:
        log("   无对白长段：%d 段，删掉 %.1f 秒（阈值 %.1f 秒）"
            % (len(gaps), before - after, float(min_gap)))
    return new_spans, gaps
