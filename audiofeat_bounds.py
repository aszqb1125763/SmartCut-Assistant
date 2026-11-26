# -*- coding: utf-8 -*-
"""用"能量谷"把每句话的起止重新定准（不靠文字时间戳）。

原理很简单，不需要模型看图：
    · 两句话之间，波形上一定有个"最安静的点"（能量谷）——那就是天然的切点
    · 一句话的开口，就是能量从谷底陡升的那一瞬；收口就是陡降的那一瞬
    · 文字的起止时间戳是模型按字猜的，语速一快一慢就会偏，切下去半个字

做法：对整个音轨算 10 毫秒一帧的能量（dB），然后
    起点：在 [原起点-0.35, 原起点+0.25] 内找上升最陡的帧，切在它前一帧
    终点：在 [原终点-0.25, 原终点+0.35] 内找下降最陡的帧，切在它后一帧
    并保证和邻句至少留 40 毫秒，不会重叠
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SR = 16000
HOP = 160          # 10 ms
WIN = 400          # 25 ms

CAPTION_REFINEMENT_VERSION = 3
CAPTION_REFINEMENT_ALGORITHM = "caption-waveform-local-v3"
CAPTION_SEARCH_SECONDS = 0.25
CAPTION_MAX_SHIFT_SECONDS = 0.25
CAPTION_WORD_GUARD_SECONDS = 0.005
CAPTION_PREROLL_SECONDS = 0.04
CAPTION_POSTROLL_SECONDS = 0.06


def _finite(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _track_ids(value):
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            try:
                track = int(item)
            except (TypeError, ValueError):
                continue
            if track >= 0 and track not in result:
                result.append(track)
        return result
    try:
        track = int(value)
    except (TypeError, ValueError):
        return []
    return [track] if track >= 0 else []


def segment_id_hash(evidence):
    """Stable identity for the exact ordered subtitle/evidence rows."""
    ids = []
    for row in evidence or []:
        if not isinstance(row, dict) or not str(row.get("segment_id") or ""):
            return ""
        ids.append(str(row["segment_id"]))
    if not ids:
        return ""
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def segment_timeline_hash(evidence):
    """Bind refinement to precise cue and first/last-word timestamps."""
    timeline = []
    for row in evidence or []:
        if not isinstance(row, dict) or not str(row.get("segment_id") or ""):
            return ""
        start, end = _finite(row.get("start")), _finite(row.get("end"))
        if start is None or end is None or end <= start:
            return ""
        words = _word_bounds(row)
        timeline.append([
            str(row["segment_id"]), round(start, 6), round(end, 6),
            round(words[0][0], 6) if words else None,
            round(words[-1][1], 6) if words else None,
        ])
    if not timeline:
        return ""
    encoded = json.dumps(
        timeline, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_refinement_metadata(source_identity, audio_stream_used, asr_profile,
                              evidence):
    """Describe exactly which source timeline the refined rows belong to."""
    profile = asr_profile if isinstance(asr_profile, dict) else {}
    return {
        "algorithm": CAPTION_REFINEMENT_ALGORITHM,
        "version": CAPTION_REFINEMENT_VERSION,
        "source_identity": source_identity,
        "audio_stream_used": audio_stream_used,
        "asr_profile_version": profile.get("version"),
        "segment_id_hash": segment_id_hash(evidence),
        "segment_timeline_hash": segment_timeline_hash(evidence),
        "segment_count": len(evidence or []),
        "search_seconds": CAPTION_SEARCH_SECONDS,
        "max_shift_seconds": CAPTION_MAX_SHIFT_SECONDS,
    }


def metadata_for_index(data):
    return build_refinement_metadata(
        data.get("source_identity"), data.get("audio_stream_used"),
        data.get("asr_profile"), data.get("segment_evidence") or [])


def _word_bounds(row):
    words = []
    for word in row.get("words") or [] if isinstance(row, dict) else []:
        if not isinstance(word, dict):
            continue
        start = _finite(word.get("start"))
        end = _finite(word.get("end"))
        if start is not None and end is not None and end >= start:
            words.append((start, end))
    return sorted(words)


def _local_activity(audio, start, end, sr):
    """Detect bounded local energy intervals without allocating a full-film matrix."""
    first = max(0, int(math.floor(float(start) * sr)))
    last = min(int(audio.size), int(math.ceil(float(end) * sr)))
    if last <= first:
        return []
    samples = np.asarray(audio[first:last], dtype=np.float32)
    try:
        import boundary_refiner
        rows = boundary_refiner.detect_activity_intervals(
            samples, sr, track="selected", kind="speech")
    except Exception:  # noqa: BLE001
        return []
    offset = first / float(sr)
    return [{**row,
             "start": float(row["start"]) + offset,
             "end": float(row["end"]) + offset}
            for row in rows]


def _activity_for_anchor(rows, anchor_start, anchor_end, edge, window_edge):
    candidates = [row for row in rows
                  if float(row["end"]) >= anchor_start - 0.08
                  and float(row["start"]) <= anchor_end + 0.08]
    if not candidates:
        return None
    chosen = min(candidates, key=lambda row: (
        abs(float(row[edge]) - (anchor_start if edge == "start" else anchor_end)),
        -float(row.get("confidence") or 0.0)))
    # The activity ran into the probe boundary, so the actual onset/offset was
    # not observed.  Keeping the ASR word edge is safer than following game
    # audio or a continuously-open microphone.
    if abs(float(chosen[edge]) - float(window_edge)) <= 0.025:
        return None
    return chosen


def refine_caption_segments(audio, segments, evidence, source_audio_track,
                            sr=SR, search=CAPTION_SEARCH_SECONDS,
                            max_shift=CAPTION_MAX_SHIFT_SECONDS):
    """Conservatively extend caption cues to clear local waveform edges.

    The function is deliberately caption-specific: it never merges or removes
    rows, never crosses a first/last word timestamp, and falls back whenever a
    local move would introduce more overlap than the original ASR timeline.
    """
    raw = []
    for segment in segments or []:
        try:
            start, end = float(segment[0]), float(segment[1])
        except (IndexError, TypeError, ValueError):
            return []
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            return []
        raw.append([start, end])
    if (not raw or audio is None or getattr(audio, "size", 0) == 0
            or len(raw) != len(evidence or [])):
        return [(row[0], row[1]) for row in raw]

    selected_tracks = set(_track_ids(source_audio_track))
    duration = float(audio.size) / float(sr)
    search = max(0.05, min(float(search), CAPTION_MAX_SHIFT_SECONDS))
    max_shift = max(0.05, min(float(max_shift), CAPTION_MAX_SHIFT_SECONDS))
    refined = [row[:] for row in raw]

    for index, (base, row) in enumerate(zip(raw, evidence)):
        if not isinstance(row, dict):
            continue
        row_tracks = set(_track_ids(
            row.get("source_audio_tracks") or row.get("source_audio_track")))
        if selected_tracks and row_tracks and not (selected_tracks & row_tracks):
            continue
        words = _word_bounds(row)
        if not words:
            continue
        start, end = base
        first_word, last_word = words[0], words[-1]

        start_lo, start_hi = max(0.0, start - search), min(duration, start + search)
        start_rows = _local_activity(audio, start_lo, start_hi, sr)
        onset = _activity_for_anchor(
            start_rows, first_word[0], first_word[1], "start", start_lo)
        new_start = start
        if onset is not None:
            candidate = max(0.0, float(onset["start"]) - CAPTION_PREROLL_SECONDS)
            candidate = min(candidate, first_word[0] - CAPTION_WORD_GUARD_SECONDS)
            if abs(candidate - start) <= max_shift + 1e-9:
                new_start = candidate

        end_lo, end_hi = max(0.0, end - search), min(duration, end + search)
        end_rows = _local_activity(audio, end_lo, end_hi, sr)
        offset = _activity_for_anchor(
            end_rows, last_word[0], last_word[1], "end", end_hi)
        new_end = end
        if offset is not None:
            candidate = min(duration, float(offset["end"]) + CAPTION_POSTROLL_SECONDS)
            candidate = max(candidate, last_word[1] + CAPTION_WORD_GUARD_SECONDS)
            if abs(candidate - end) <= max_shift + 1e-9:
                new_end = candidate

        if (new_end <= new_start
                or new_start > first_word[0] - CAPTION_WORD_GUARD_SECONDS + 1e-9
                or new_end < last_word[1] + CAPTION_WORD_GUARD_SECONDS - 1e-9):
            continue
        refined[index] = [new_start, new_end]

    # Never create additional cue overlap.  Revert the two touching edges to
    # their original ASR values; pre-existing source overlap is preserved.
    for index in range(len(refined) - 1):
        original_overlap = max(0.0, raw[index][1] - raw[index + 1][0])
        refined_overlap = max(0.0, refined[index][1] - refined[index + 1][0])
        if refined_overlap > original_overlap + 1e-9:
            refined[index][1] = raw[index][1]
            refined[index + 1][0] = raw[index + 1][0]

    output = []
    for base, current, row in zip(raw, refined, evidence):
        words = _word_bounds(row if isinstance(row, dict) else {})
        if (not words or current[1] <= current[0]
                or abs(current[0] - base[0]) > max_shift + 1e-9
                or abs(current[1] - base[1]) > max_shift + 1e-9
                or current[0] > words[0][0] - CAPTION_WORD_GUARD_SECONDS + 1e-9
                or current[1] < words[-1][1] + CAPTION_WORD_GUARD_SECONDS - 1e-9):
            current = base
        # Six decimal places keep serialization deterministic while preserving
        # the 5 ms word guard.  Millisecond rounding could move a legal edge
        # back across that guard and invalidate the entire refined timeline.
        output.append((round(current[0], 6), round(current[1], 6)))
    return output


def validated_refined_segments(data):
    """Return trusted v2 refined rows, otherwise an empty list."""
    segments = data.get("segments") or [] if isinstance(data, dict) else []
    evidence = data.get("segment_evidence") or [] if isinstance(data, dict) else []
    refined = data.get("segments_refined") or [] if isinstance(data, dict) else []
    metadata = data.get("segments_refined_metadata") or {} if isinstance(data, dict) else {}
    if (data.get("segments_refined_version") != CAPTION_REFINEMENT_VERSION
            or len(segments) != len(evidence) or len(refined) != len(segments)
            or not isinstance(metadata, dict)):
        return []
    expected = metadata_for_index(data)
    for key in ("algorithm", "version", "source_identity", "audio_stream_used",
                "asr_profile_version", "segment_id_hash",
                "segment_timeline_hash", "segment_count"):
        if metadata.get(key) != expected.get(key):
            return []

    result = []
    raw = []
    for segment, value, row in zip(segments, refined, evidence):
        try:
            start, end = float(segment[0]), float(segment[1])
            new_start, new_end = float(value[0]), float(value[1])
        except (IndexError, TypeError, ValueError):
            return []
        evidence_start = _finite(row.get("start")) if isinstance(row, dict) else None
        evidence_end = _finite(row.get("end")) if isinstance(row, dict) else None
        if (evidence_start is not None and evidence_end is not None
                and evidence_end > evidence_start):
            # index.json keeps display segments at 10 ms precision.  The
            # evidence rows retain the exact baseline used by refinement.
            if abs(evidence_start - start) > 0.011 or abs(evidence_end - end) > 0.011:
                return []
            base_start, base_end = evidence_start, evidence_end
        else:
            base_start, base_end = start, end
        if (not all(math.isfinite(item) for item in
                    (start, end, base_start, base_end, new_start, new_end))
                or end <= start or base_end <= base_start or new_end <= new_start
                or abs(new_start - base_start) > CAPTION_MAX_SHIFT_SECONDS + 1e-6
                or abs(new_end - base_end) > CAPTION_MAX_SHIFT_SECONDS + 1e-6):
            return []
        words = _word_bounds(row if isinstance(row, dict) else {})
        if words:
            start_moved = abs(new_start - base_start) > 1e-6
            end_moved = abs(new_end - base_end) > 1e-6
            if ((start_moved and
                 new_start > words[0][0] - CAPTION_WORD_GUARD_SECONDS + 1e-6)
                    or (end_moved and
                        new_end < words[-1][1] + CAPTION_WORD_GUARD_SECONDS - 1e-6)):
                return []
        elif (abs(new_start - base_start) > 1e-6
              or abs(new_end - base_end) > 1e-6):
            # Rows without word evidence are allowed only when left untouched.
            return []
        raw.append((base_start, base_end))
        result.append((new_start, new_end))
    for index in range(len(result) - 1):
        old_overlap = max(0.0, raw[index][1] - raw[index + 1][0])
        new_overlap = max(0.0, result[index][1] - result[index + 1][0])
        if new_overlap > old_overlap + 1e-6:
            return []
    return result


def frame_db_full(x: np.ndarray):
    """整条音频的 10ms 帧能量（dB），返回 (dB 数组, 每帧起始秒)。"""
    n = max(0, 1 + (len(x) - WIN) // HOP)
    if n <= 0:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    fr = x[idx]
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(np.maximum(rms, 1e-6)), np.arange(n) * HOP / SR


def refine_segments(audio: np.ndarray, segments, pad_on=0.04, pad_off=0.06,
                    lead=0.35, trail=0.25, min_gap=0.04):
    """返回 [(新起点, 新终点)]，长度与 segments 一致。"""
    db, tt = frame_db_full(np.asarray(audio, dtype=np.float32))
    if db.size == 0:
        return [(float(s[0]), float(s[1])) for s in segments]
    d = np.diff(db, prepend=db[0])          # 每帧的能量变化（dB）

    def fidx(t):
        return int(min(len(tt) - 1, max(0, round(t / (HOP / SR)))))

    n = len(segments)
    out = []
    for i, s in enumerate(segments):
        a, b = float(s[0]), float(s[1])
        i0, i1 = fidx(a), fidx(b)
        # 起点：向前后各看一点，找"上升最陡"的帧
        lo = fidx(max(0.0, a - lead))
        hi = fidx(a + trail)
        if hi > lo:
            k = lo + int(np.argmax(d[lo:hi]))
            na = tt[k] - pad_on
        else:
            na = a
        # 终点：找"下降最陡"的帧
        lo2 = fidx(max(0.0, b - trail))
        hi2 = fidx(b + lead)
        if hi2 > lo2:
            k2 = lo2 + int(np.argmin(d[lo2:hi2]))
            nb = tt[k2] + pad_off
        else:
            nb = b
        na = max(0.0, float(na))
        nb = float(nb)
        if nb <= na:
            na, nb = a, b
        out.append([na, nb])

    # 防重叠：邻句之间至少留 min_gap
    for i in range(n - 1):
        if out[i][1] > out[i + 1][0] - min_gap:
            mid = (out[i][1] + out[i + 1][0]) / 2.0
            out[i][1] = mid - min_gap / 2.0
            out[i + 1][0] = mid + min_gap / 2.0
    for i in range(n):
        if out[i][1] <= out[i][0]:
            out[i] = [float(segments[i][0]), float(segments[i][1])]
    return [(round(a, 3), round(b, 3)) for a, b in out]


def refine_index(idx_path: str, on_status=None):
    """给索引用：算精确边界，写进 segments_refined。"""
    import index as ix
    p = os.path.join(idx_path, "index.json") if os.path.isdir(idx_path) else idx_path
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    segs = data.get("segments") or []
    if not segs:
        print("没有台词")
        return 1
    st = on_status or print
    st("抽音轨…")
    import tempfile
    fd, wav = tempfile.mkstemp(suffix=".wav", dir=ix.temp_root())
    os.close(fd)
    try:
        source_track = data.get("audio_stream_used")
        if not _track_ids(source_track):
            raise ValueError("索引没有可用的 audio_stream_used，拒绝按未知音轨精修字幕")
        if not ix.extract_audio_wav(data["source"], wav, source_track):
            raise RuntimeError("无法抽取索引记录的字幕音轨")
        audio = ix.read_wav_mono(wav)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    evidence = data.get("segment_evidence") or []
    refine_segs = []
    for segment, row in zip(segs, evidence):
        start, end = _finite(segment[0]), _finite(segment[1])
        exact_start = _finite(row.get("start")) if isinstance(row, dict) else None
        exact_end = _finite(row.get("end")) if isinstance(row, dict) else None
        if (start is not None and end is not None
                and exact_start is not None and exact_end is not None
                and exact_end > exact_start
                and abs(exact_start - start) <= 0.011
                and abs(exact_end - end) <= 0.011):
            start, end = exact_start, exact_end
        refine_segs.append((start, end, segment[2]))
    st("用已选音轨局部波形微调每句起止（%d 句）…" % len(segs))
    ref = refine_caption_segments(
        audio, refine_segs, evidence, source_track, sr=SR)
    if len(ref) != len(segs):
        raise RuntimeError("字幕波形精修没有保持原字幕条数")
    moved = sum(1 for (a, b), s in zip(ref, refine_segs)
                if abs(a - float(s[0])) > 0.05 or abs(b - float(s[1])) > 0.05)
    data["segments_refined"] = [[a, b] for a, b in ref]
    data["segments_refined_version"] = CAPTION_REFINEMENT_VERSION
    data["segments_refined_metadata"] = metadata_for_index(data)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    st("完成：%d/%d 句边界有改动，已写入 %s" % (moved, len(segs), p))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python audiofeat.py --refine <索引目录>")
        raise SystemExit(2)
    raise SystemExit(refine_index(sys.argv[1]))
