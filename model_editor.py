# -*- coding: utf-8 -*-
"""模型剪辑师：替掉旧软件的「剪辑师」岗位（workflow 的 editor 阶段）。

子枫 2026-11-24：
    「把旧软件中的模型换成这个新的，可以自己看图片听声音、文字识别能力更强的这个，
      让他来替代原有软件的剪辑师和剪辑。」

它只做一件事：给模型「以绝对时间轴为主干的逐轨听觉/台词/停顿/重叠事件 +
看得到的画面（OCR 文字）+ 章节框架（小脑算的边界）」，让它自己出剪辑单
（keep/delete + 理由）。
边界、导演、转场、渲染、收尾全部沿用原来的岗位，一行不动。

按 `_规范` 的规矩：
  · 框架（章节）由代码/小脑算，不由模型想（00_索引·下一步）；
  · 模型只给区间和理由，时长/压缩比/覆盖全由代码算（11·七、05·算术归计算层）；
  · 讲原理，不堆约束（05·原理优先于约束）；
  · 校验「覆盖 / 区间合法 / 开头有词 / 不删光」由代码做，模型不做约束校验（11·五）。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import time

import numpy as np

import autocut
import edit_modes
import edit_plan


def _application_base():
    """Return the writable application root in source and frozen builds."""
    configured = str(os.environ.get("SMARTCUT_HOME") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if getattr(sys, "frozen", False):
        executable = str(getattr(sys, "executable", "") or "").strip()
        if executable:
            return os.path.dirname(os.path.abspath(executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE = _application_base()
CACHE_DIR = os.path.join(BASE, "工作目录", "模型剪辑")
GX, GY = 16, 9
MAX_CHAPTERS = 12
MAX_BODY_CHARS = 2600
MIN_TIMELINE_GAP = 0.35

LEVEL_EDITOR_RULES = {
    "light": ("轻度：保留完整上下文与各章过程，只删明确空场、完全重复、失败重录；"
              "不能为了缩短时长省略剧情步骤。"),
    "medium": ("中度：可删重复铺垫、跑题和弱支线，但每章的目标、主要过程、结果、"
               "评价与过渡必须连贯。"),
    "heavy": ("重度：目标是最短的完整故事链，不是只留孤立高光；必须保留目标、关键过程、"
              "结果/评价与因果过渡，不能为了压时长删掉中间逻辑。"),
}

# 剪辑力度 = 台词保留比例的**范围**（不是精确值：差不多就行，E04 允许范围值）。
# light  保留 85%~100%：只删空场/完全重复
# medium 保留 70%~85% ：接近人工成片
# heavy  保留 35%~55% ：只留关键节点
# 代码只在"删过头"（低于下限）时才封顶，范围内由模型自由判断。
LEVEL_KEEP_RANGE = {
    "light": (0.85, 1.00),
    "medium": (0.70, 0.85),
    "heavy": (0.35, 0.55),
}


def _units_in(spans, units):
    """这些时间区间里覆盖了多少句台词（只数 U，不数空场）。

    用区间重叠而不是"起点是否落在段内"：起点判断会漏掉被整段跨过的句子，
    导致预算数得比实际删得少（实测差 10 句）。
    """
    if not spans or not units:
        return 0
    count = 0
    for unit in units:
        start = float(unit.get("a") or 0.0)
        end = float(unit.get("b") or start)
        if any(float(a) < end - 0.05 and float(b) > start + 0.05
               for a, b in spans):
            count += 1
    return count

SYS_PLAN = """你在剪视频。下面给你的不是“按文本顺序排列的台词”，而是一条按绝对时间展开的
时间轴事件流。U 是有台词事件，G 是真实存在的无台词区间；文本只是 U 事件挂载的一个属性。

每行是一个事件：U=有台词，G=未检测到台词的真实区间（不代表无声）；C=章节；t=绝对起止；
d=持续时间；gap=前空隙；ov-=与前面事件重叠；safe=按转写边界估算的候选切口余量，尚非声学认证；
ov+=与后续事件重叠；听=对应逐轨听觉标签；文=该时间区间的转写。数值单位均为秒。

**判断顺序（先时间、声音，后文字）：**
1. 先沿事件的绝对起止时间判断先后、持续、停顿、重叠和因果；文本在输入里的排列顺序
   不能代替真实时间关系；
2. 再看逐轨听觉状态。重叠表示声音同时发生，不是前后两句话；G 是无台词区间，可能仍有
   游戏声、音乐或音效。默认删除不推进当前剧本、也不帮助理解相邻台词的 G；只有存在明确
   关键画面证据、能直接补充当前剧本信息时才保留，而且不要把远距离台词之间的多个 G 合成
   一个几十秒或几分钟的连续保留段；
3. 最后结合文本语义和章节画面。删有台词区间会让观众少听一句话，除非它明显重复、跑题
   或按当前力度不应保留；
4. 优先考虑在 G 区间或 U 事件标出的 safe 候选余量内落切口；它只是 ASR 时间推算，最终
   切点还会经过声学边界精修。候选余量为 0 或存在重叠时，不要直接切断发音；
5. 同一件事讲了两遍，留更完整、更符合前后因果的一遍；视频开头第一个有效事件前的大段
   空场可以删除。

**你要做的：**
只用当前批“可写事件 ID”决定删除；前后只读上下文只能帮助理解因果，绝不能写进删除结果。
单个事件放进 delete_events；连续多个事件可用 delete_ranges 的起止 ID。不要自行填写秒数，
程序会把通过授权校验的 ID 映射成绝对时间区间。

输出 JSON：
{"delete_events": ["G0001", "U0008"],
 "delete_ranges": [["U0012", "G0009"]],
 "reason": "一句话说清你删了什么、为什么"}"""

SYS_POLISH = """下面是一段视频转写的原始台词，可能有错别字。
不要改写它，只回答：按上面的原理，这一段里哪些句子明显是在重复上一条说话内容？
如果看不出来就回答"没有"。两句话内。"""


# ---------------------------------------------------------------- 画面
_FRAME_CACHES = {}
_FRAME_CACHE_ORDER = []
_FRAME_EVIDENCE = {}


def _frame_cache_for(src, should_cancel=None):
    """Reuse one persistent review-proxy handle per unchanged source file."""
    try:
        stat = os.stat(src)
        identity = (os.path.normcase(os.path.abspath(src)), int(stat.st_size),
                    int(stat.st_mtime_ns))
    except OSError:
        return None
    cache = _FRAME_CACHES.get(identity)
    if cache is None:
        import video_frame_cache
        cache = video_frame_cache.VideoFrameCache(
            src, should_cancel=should_cancel)
        _FRAME_CACHES[identity] = cache
        _FRAME_CACHE_ORDER.append(identity)
        while len(_FRAME_CACHE_ORDER) > 4:
            expired = _FRAME_CACHE_ORDER.pop(0)
            _FRAME_CACHES.pop(expired, None)
    elif should_cancel is not None:
        cache.should_cancel = should_cancel
    return cache


def _remember_frame_evidence(key, value):
    _FRAME_EVIDENCE[key] = value
    while len(_FRAME_EVIDENCE) > 256:
        _FRAME_EVIDENCE.pop(next(iter(_FRAME_EVIDENCE)))


def _ffmpeg():
    path = os.path.join(BASE, "bin", "ffmpeg.exe")
    return path if os.path.isfile(path) else "ffmpeg"


def _frame_at(src, seconds, out):
    if not (os.path.isfile(out) and os.path.getsize(out) > 128):
        subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "%.2f" % max(0.0, float(seconds)), "-i", src,
             "-frames:v", "1", "-q:v", "2", out],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        import cv2
        return cv2.imdecode(np.fromfile(out, dtype=np.uint8),
                            cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def _grid(img):
    """亮度 0~9 + 颜色块（子枫定的"压缩画面"口径）。"""
    import cv2
    small = cv2.resize(img, (GX, GY), interpolation=cv2.INTER_AREA)
    lum = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(int)
    levels = np.clip((lum / 255 * 9).round().astype(int), 0, 9)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    names = ["红", "橙", "黄", "绿", "青", "蓝", "紫"]
    rows = []
    for row in range(GY):
        line = ""
        for col in range(GX):
            hue, sat, val = hsv[row, col]
            if val < 45:
                line += "黑"
            elif sat < 30 and val > 190:
                line += "白"
            elif sat < 30:
                line += "灰"
            else:
                line += names[min(int(hue / 180 * 7), 6)]
        rows.append(line)
    return levels, rows


def _frame_text(engine, src, seconds, log):
    """一帧 → 画面文字（OCR）。OCR 不可用时退回"亮/暗"一句话，不挡流程。"""
    meta = None
    img = None
    try:
        cache = _frame_cache_for(src)
        if cache is not None:
            img, meta = cache.frame_at(seconds)
    except Exception as exc:  # noqa: BLE001
        try:
            import video_frame_cache
            if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                raise autocut.RenderCancelled(str(exc)) from exc
        except ImportError:
            pass
        meta = {"precision": "legacy_exact_fallback", "reused": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
    if img is None:
        tmp_dir = CACHE_DIR
        os.makedirs(tmp_dir, exist_ok=True)
        token = hashlib.sha1((os.path.abspath(src) + "|%.6f" %
                              float(seconds)).encode("utf-8")).hexdigest()[:16]
        tmp = os.path.join(tmp_dir, "frame_%s.jpg" % token)
        existed = os.path.isfile(tmp) and os.path.getsize(tmp) > 128
        img = _frame_at(src, seconds, tmp)
        if meta is None:
            meta = {"precision": "legacy_exact_fallback", "reused": existed}
    _remember_frame_evidence(
        (os.path.normcase(os.path.abspath(src)), round(float(seconds), 6)),
        meta or {"precision": "unavailable", "reused": False})
    if img is None:
        return ""
    texts = []
    if engine is not None:
        try:
            result, _cost = engine(img)
            texts = [str(row[1]).strip() for row in (result or [])
                     if len(row) > 1 and str(row[1]).strip()]
        except Exception as exc:  # noqa: BLE001
            log("   画面文字识别失败（%s），这一帧只看明暗" % exc)
    if texts:
        # 去重、截断：一帧最多 8 条，太长会挤占上下文
        uniq = []
        for text in texts:
            if text not in uniq:
                uniq.append(text)
        return " / ".join(uniq[:8])
    levels, _rows = _grid(img)
    mean = float(np.mean(levels))
    return "（没有文字）画面平均亮度 %.1f/9" % mean


def _chapter_probe_times(start, end):
    """Sample the beginning, middle and end of a chapter for visual context."""
    start, end = float(start), float(end)
    span = max(0.0, end - start)
    if span <= 0.0:
        return [("中间", start)]
    margin = min(0.5, max(0.05, span / 10.0))
    candidates = [
        ("开头", start + margin),
        ("中间", (start + end) / 2.0),
        ("结尾", end - margin),
    ]
    result = []
    seen = set()
    for label, seconds in candidates:
        seconds = max(start, min(end, seconds))
        marker = round(seconds, 3)
        if marker in seen:
            continue
        seen.add(marker)
        result.append((label, seconds))
    return result


def _cancel_check(runtime):
    stopper = getattr(runtime, "should_cancel", None)
    if callable(stopper) and stopper():
        raise autocut.RenderCancelled("已取消")


def _collect_visual_evidence(runtime, chapters, engine):
    """Collect auditable visual evidence at three key points per chapter."""
    try:
        _frame_cache_for(runtime.src,
                         should_cancel=getattr(runtime, "should_cancel", None))
    except Exception as exc:  # noqa: BLE001
        try:
            import video_frame_cache
            if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                raise autocut.RenderCancelled(str(exc)) from exc
        except ImportError:
            pass
    frames = {}
    scene_notes = {}
    evidence = []
    for index, (start, end) in enumerate(chapters, 1):
        records = []
        for label, seconds in _chapter_probe_times(start, end):
            _cancel_check(runtime)
            evidence_key = (os.path.normcase(os.path.abspath(runtime.src)),
                            round(float(seconds), 6))
            _FRAME_EVIDENCE.pop(evidence_key, None)
            summary = _frame_text(
                engine, runtime.src, seconds, runtime.log) or ""
            row = {
                "chapter": index,
                "label": label,
                "time": round(float(seconds), 3),
                "summary": summary,
                "available": bool(summary),
                "frame_cache": dict(_FRAME_EVIDENCE.get(evidence_key) or {
                         "precision": "mock_or_legacy", "reused": False}),
                "visual_authority": "context_only_no_veto",
            }
            records.append(row)
            evidence.append(row)
        usable = sum(1 for row in records if row["available"])
        frames[index] = "；".join(
            "%s@%.1fs：%s" % (
                row["label"], row["time"], row["summary"] or "未取到画面")
            for row in records)
        scene_notes[index] = "关键帧 %d/%d 可用；采样 %s" % (
            usable, len(records),
            "/".join("%.1fs" % row["time"] for row in records))
        runtime.log("   第 %d 章画面：%s" %
                    (index, frames[index][:100]))
    return frames, scene_notes, evidence


# ---------------------------------------------------------------- 章节
def _chapters(units, shots, duration, min_len=6.0):
    """小脑式分章：长停顿 + 画面跳变，两处线索合并成边界点。"""
    points = [0.0]
    for prev, nxt in zip(units, units[1:]):
        gap = float(nxt["a"]) - float(prev["b"])
        if gap >= 1.5:                       # 长停顿 = 话题断点
            points.append((float(prev["b"]) + float(nxt["a"])) / 2.0)
    for at in (shots or []):
        try:
            at = float(at)
        except (TypeError, ValueError):
            continue
        if 0.0 < at < duration:
            points.append(at)
    points = sorted(set(round(p, 2) for p in points))
    merged = [points[0]]
    for p in points[1:]:
        if p - merged[-1] >= min_len:
            merged.append(p)
    chapters = []
    for index, start in enumerate(merged):
        end = merged[index + 1] if index + 1 < len(merged) else duration
        if end - start >= 2.0:
            chapters.append((start, end))
    if len(chapters) > MAX_CHAPTERS:         # 章太多就合并，模型看不完
        count = len(chapters)
        kept = []
        for i in range(MAX_CHAPTERS):
            # Integer half-open partitions cover every source chapter exactly
            # once.  The old inclusive end index repeated the boundary chapter
            # in adjacent groups, which duplicated its U/G timeline events.
            first = (i * count) // MAX_CHAPTERS
            stop = ((i + 1) * count) // MAX_CHAPTERS
            stop = max(first + 1, min(count, stop))
            a = chapters[first][0]
            b = chapters[stop - 1][1]
            kept.append((a, b))
        chapters = kept
    return chapters


def _track_audio(src, ordinal, sr=16000):
    """抽**指定音轨**（不是混音）：剪辑素材是多轨的，要听人声那一条。"""
    done = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", src,
         "-map", "0:a:%d" % int(ordinal), "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not done.stdout:
        return None
    return np.frombuffer(done.stdout, dtype="<i2").astype(np.float32) / 32768.0


def _voice_tags(units, audio):
    """小脑听音轨，并把合并画像按时间重叠映射回每个原始 unit。

    ``utterance.pack`` 会把间隔短于 0.45 秒的相邻 unit 合成一条画像，
    因此它的返回位置不能直接当作 unit 位置。这里始终用画像的 t0/t1
    与 unit 的绝对时间求最大重叠，保证后续句子不会因前面发生合并而
    整体错挂声音状态。
    """
    if audio is None or getattr(audio, "size", 0) == 0:
        return []
    try:
        import utterance
    except Exception:  # noqa: BLE001
        return []
    words = [{"start": float(u["a"]), "end": float(u["b"]),
              "word": str(u.get("text") or "")} for u in units]
    try:
        rows = utterance.pack(words, audio)
    except Exception:  # noqa: BLE001
        return []
    return _map_voice_rows(units, rows)


def _voice_portrait(row):
    return "%s·%s·%s" % (row.get("level") or "?",
                          row.get("speed") or "?",
                          row.get("shape") or "?")


def _map_voice_rows(units, rows):
    """Map packed audio portraits to source units by absolute-time overlap."""
    packed = []
    for position, row in enumerate(rows or []):
        try:
            start, end = float(row["t0"]), float(row["t1"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        packed.append((start, end, position, row))
    result = []
    for unit in units or []:
        try:
            start, end = float(unit["a"]), float(unit["b"])
        except (KeyError, TypeError, ValueError):
            result.append("")
            continue
        if end < start:
            start, end = end, start
        center = (start + end) / 2.0
        candidates = []
        for row_start, row_end, position, row in packed:
            overlap = max(0.0, min(end, row_end) - max(start, row_start))
            row_center = (row_start + row_end) / 2.0
            contains_center = row_start - 0.01 <= center <= row_end + 0.01
            candidates.append((overlap, int(contains_center),
                               -abs(center - row_center), -position, row))
        if not candidates:
            result.append("")
            continue
        best = max(candidates, key=lambda item: item[:-1])
        # Rounded t0/t1 can miss an extremely short unit by a few ms.  Permit
        # only a near-centre match; never pull an unrelated distant portrait.
        if best[0] <= 0.0 and -best[2] > 0.06:
            result.append("")
        else:
            result.append(_voice_portrait(best[-1]))
    return result


def _voice_tags_from_cache(units, cache, ordinal, cancel_check=None):
    """Analyse bounded utterance windows from a prepared PCM cache entry."""
    try:
        import utterance
        import audio_evidence_cache
    except Exception:  # noqa: BLE001
        return []
    words = [{"start": float(unit["a"]), "end": float(unit["b"]),
              "word": str(unit.get("text") or "")}
             for unit in units]
    try:
        groups = utterance.sentences(words)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for group in groups:
        if callable(cancel_check) and cancel_check():
            raise audio_evidence_cache.AudioEvidenceCancelled(
                "audio portrait analysis cancelled")
        start = float(group[0]["start"])
        end = float(group[-1]["end"])
        audio = cache.review_samples(ordinal, start, end)
        if audio is None or getattr(audio, "size", 0) == 0:
            continue
        local_words = [{"start": float(word["start"]) - start,
                        "end": float(word["end"]) - start,
                        "word": str(word.get("word") or "")}
                       for word in group]
        try:
            local_rows = utterance.pack(local_words, audio)
        except Exception:  # noqa: BLE001
            continue
        for row in local_rows:
            shifted = dict(row)
            shifted["t0"] = start + float(row.get("t0") or 0.0)
            shifted["t1"] = start + float(row.get("t1") or 0.0)
            rows.append(shifted)
    return _map_voice_rows(units, rows)


def _track_values(raw):
    """Normalise internal (zero-based) audio-track selections."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[,，;；\s]+", raw.strip()) if part]
    elif not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    result = []
    for value in raw:
        try:
            track = int(value)
        except (TypeError, ValueError):
            continue
        if track >= 0 and track not in result:
            result.append(track)
    return result


def _selected_tracks(runtime, data):
    """Use the same zero-based track selection as rendering when available."""
    chooser = getattr(runtime, "selected_tracks", None)
    if callable(chooser):
        try:
            raw = chooser(data)
        except Exception:  # noqa: BLE001 - old runtimes have no stable API
            raw = None
    else:
        raw = None
    if raw is None:
        explicit = getattr(runtime, "tracks", None)
        if explicit is not None and _track_values(explicit):
            raw = explicit
        else:
            # Old/fake runtimes do not expose selected_tracks().  Their index
            # records the ASR-selected stream; it is the safest compatibility
            # fallback.  A list is accepted for newer multi-track indexes.
            raw = data.get("audio_stream_used")
            if raw is None:
                raw = [stream.get("ordinal", index)
                       for index, stream in enumerate(
                           data.get("audio_streams") or [])]
    tracks = _track_values(raw)
    count = len(data.get("audio_streams") or [])
    if count:
        tracks = [track for track in tracks if track < count]
    return tracks


def _multi_track_tags(runtime, data, units):
    """Listen to every selected stream through one reusable PCM cache.

    A real workflow runtime owns one cache for editor/boundary/director stages;
    older runtimes get one temporary cache for this call.  Direct ffmpeg decode
    remains only as a compatibility fallback when caching is unavailable.
    """
    tracks = _selected_tracks(runtime, data)
    per_sentence = [[] for _unit in units]
    reports = []
    cache = None
    owns_cache = False
    cache_source = "none"
    cache_error = None
    audio_cache_module = None
    if tracks:
        try:
            import audio_evidence_cache as audio_cache_module
            ensure = getattr(runtime, "ensure_audio_cache", None)
            if callable(ensure):
                cache = ensure(data)
                if cache is not None:
                    cache_source = "runtime"
            if cache is None:
                cache = audio_cache_module.AudioEvidenceCache(
                    runtime.src, tracks, ffmpeg=_ffmpeg(),
                    should_cancel=getattr(runtime, "should_cancel", None))
                owns_cache = True
                cache.prepare()
                cache_source = "global"
        except Exception as exc:  # noqa: BLE001
            if owns_cache and cache is not None:
                try:
                    cache.close()
                except Exception:  # noqa: BLE001
                    pass
            import workflow_engine
            if isinstance(exc, workflow_engine.WorkflowCancelled):
                raise
            if (audio_cache_module is not None
                    and isinstance(exc,
                                   audio_cache_module.AudioEvidenceCancelled)):
                raise workflow_engine.WorkflowCancelled(str(exc)) from exc
            cache, owns_cache = None, False
            cache_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            runtime.log("   听觉缓存暂不可用，改用兼容解码：%s" % cache_error)

    try:
        for ordinal in tracks:
            cancel_check = getattr(runtime, "should_cancel", None)
            if callable(cancel_check) and cancel_check():
                import workflow_engine
                raise workflow_engine.WorkflowCancelled(
                    "audio portrait analysis cancelled")
            entry = {}
            tags = []
            decoded = False
            used_cache = False
            audio = None
            if cache is not None:
                try:
                    entry = cache.entry(ordinal)
                    decoded = int(entry.get("sample_count") or 0) > 0
                    tags = _voice_tags_from_cache(
                        units, cache, ordinal,
                        cancel_check=getattr(runtime, "should_cancel", None))
                    used_cache = True
                except Exception as exc:  # noqa: BLE001
                    import workflow_engine
                    if isinstance(exc, workflow_engine.WorkflowCancelled):
                        raise
                    if (audio_cache_module is not None
                            and isinstance(
                                exc,
                                audio_cache_module.AudioEvidenceCancelled)):
                        raise workflow_engine.WorkflowCancelled(str(exc)) from exc
                    runtime.log("   第 %d 条缓存听觉失败（%s），改用兼容解码"
                                % (ordinal + 1, str(exc)[:120]))
            if not used_cache:
                audio = _track_audio(runtime.src, ordinal)
                decoded = bool(audio is not None and getattr(audio, "size", 0))
                tags = _voice_tags(units, audio)
            tagged_count = sum(1 for tag in tags if tag)
            for position, sentence_tags in enumerate(per_sentence):
                tag = tags[position] if position < len(tags) else ""
                sentence_tags.append(
                    "轨%d:%s" % (ordinal + 1, tag or "无可用标签"))
            reports.append({
                "track": int(ordinal),
                "display_track": int(ordinal) + 1,
                "decoded": decoded,
                "tagged_sentences": tagged_count,
                "cache_key": str(entry.get("key") or "") or None,
                "reused": bool(entry.get("reused")) if entry else False,
                "cache_source": cache_source if used_cache else "legacy",
                "cache_error": cache_error,
            })
            if tagged_count:
                cache_state = ("缓存%s" %
                               ("复用" if entry.get("reused") else "就绪")
                               if used_cache else "兼容解码")
                runtime.log(
                    "   听音轨：第 %d 条（%d 句带力度/语速/走势；%s）"
                    % (ordinal + 1, tagged_count, cache_state))
            elif decoded:
                runtime.log("   听音轨：第 %d 条已抽出，但听觉标签不可用"
                            % (ordinal + 1))
            else:
                runtime.log("   听音轨：第 %d 条没抽出来" % (ordinal + 1))
            # Compatibility fallback can still allocate one full float array;
            # release it before moving to the next selected stream.
            del audio
    finally:
        if owns_cache and cache is not None:
            cache.close()
    if not tracks:
        runtime.log("   听音轨：索引和运行设置里都没有可用音轨，这次只按文字判断")
    return ["；".join(parts) for parts in per_sentence], tracks, reports


def _chapter_number(seconds, chapters):
    """Map an absolute time to a chapter without changing the time itself."""
    if not chapters:
        return 1
    value = float(seconds)
    for index, (start, end) in enumerate(chapters, 1):
        if float(start) <= value < float(end):
            return index
    if value < float(chapters[0][0]):
        return 1
    return len(chapters)


def _timeline_events(units, chapters, tags=None,
                     min_gap=MIN_TIMELINE_GAP):
    """Build one absolute-time event stream for every later model batch.

    Event ids are assigned before chapters are grouped or batches are split, so
    ``U0007`` always means the same source utterance.  Positive silence is kept
    on every speech event (``prev_gap``/``safe_after``); meaningful silent
    spans are additionally promoted to explicit ``G`` events.  Overlapping ASR
    units remain simultaneous events instead of being presented as sequential
    prose.
    """
    tags = tags or []
    chapters = list(chapters or [])
    ordered = []
    for source_pos, raw in enumerate(units or []):
        try:
            start = float(raw["a"])
            end = float(raw["b"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        ordered.append((start, end, source_pos, raw))
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))

    if chapters:
        timeline_start = float(chapters[0][0])
        timeline_end = float(chapters[-1][1])
    elif ordered:
        timeline_start = min(0.0, ordered[0][0])
        timeline_end = ordered[-1][1]
        chapters = [(timeline_start, timeline_end)]
    else:
        return []

    # Determine the first genuinely safe margin after each event.  An event
    # nested inside a longer overlapping utterance has no safe margin at its
    # own end; the event that reaches the overlap-cluster frontier owns the
    # silence before the next cluster.
    clusters = []
    for position, (start, end, _source_pos, _raw) in enumerate(ordered):
        if clusters and start <= clusters[-1]["end"] + 1e-6:
            clusters[-1]["end"] = max(clusters[-1]["end"], end)
            clusters[-1]["members"].append(position)
        else:
            clusters.append({"start": start, "end": end,
                             "members": [position]})
    safe_after = {}
    for cluster_pos, cluster in enumerate(clusters):
        next_start = (clusters[cluster_pos + 1]["start"]
                      if cluster_pos + 1 < len(clusters) else timeline_end)
        for member in cluster["members"]:
            member_end = ordered[member][1]
            safe_after[member] = (
                max(0.0, next_start - member_end)
                if member_end >= cluster["end"] - 1e-6 else 0.0)

    speech = []
    previous_frontier = timeline_start
    for position, (start, end, source_pos, raw) in enumerate(ordered):
        next_start = (ordered[position + 1][0]
                      if position + 1 < len(ordered) else timeline_end)
        before = start - previous_frontier
        after = next_start - end
        speech.append({
            "event_id": "U%04d" % (position + 1),
            "kind": "speech",
            "chapter": _chapter_number(start, chapters),
            "a": start,
            "b": end,
            "duration": max(0.0, end - start),
            "source_pos": source_pos,
            "text": str(raw.get("text") or ""),
            "audio": str(tags[source_pos] or "")
            if source_pos < len(tags) else "",
            "prev_gap": max(0.0, before),
            "prev_overlap": max(0.0, -before),
            "safe_after": safe_after.get(position, max(0.0, after)),
            "next_overlap": max(0.0, -after),
        })
        previous_frontier = max(previous_frontier, end)

    # Turn meaningful uncovered spans into first-class timeline events.  Work
    # chapter by chapter so a gap never loses its chapter identity, but compute
    # coverage from *all* units so cross-chapter speech is not mislabelled as
    # silence.
    gaps = []
    threshold = max(0.0, float(min_gap))
    for chapter, (raw_start, raw_end) in enumerate(chapters, 1):
        chapter_start, chapter_end = float(raw_start), float(raw_end)
        coverage = []
        for start, end, _source_pos, _raw in ordered:
            left = max(chapter_start, start)
            right = min(chapter_end, end)
            if right > left:
                coverage.append((left, right))
        coverage.sort()
        merged = []
        for left, right in coverage:
            if merged and left <= merged[-1][1] + 1e-6:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        cursor = chapter_start
        for left, right in merged:
            if left - cursor >= threshold:
                gaps.append((cursor, left, chapter))
            cursor = max(cursor, right)
        if chapter_end - cursor >= threshold:
            gaps.append((cursor, chapter_end, chapter))

    gap_events = []
    for position, (start, end, chapter) in enumerate(
            sorted(gaps, key=lambda item: (item[0], item[1], item[2])), 1):
        gap_events.append({
            "event_id": "G%04d" % position,
            "kind": "gap",
            "chapter": chapter,
            "a": start,
            "b": end,
            "duration": max(0.0, end - start),
            "source_pos": None,
            "text": "",
            "audio": "未检测到台词（非静音结论）",
            "prev_gap": 0.0,
            "prev_overlap": 0.0,
            "safe_after": max(0.0, end - start),
            "next_overlap": 0.0,
        })

    events = speech + gap_events
    events.sort(key=lambda event: (
        float(event["a"]),
        0 if event["kind"] == "gap" else 1,
        float(event["b"]),
        event["event_id"],
    ))
    return events


def _chapter_lines(units, chapters, tags=None):
    """Group the already-numbered absolute timeline events by chapter."""
    grouped = {index: [] for index in range(1, len(chapters) + 1)}
    for event in _timeline_events(units, chapters, tags=tags):
        chapter = int(event["chapter"])
        grouped.setdefault(chapter, []).append((event["source_pos"], event))
    return [(index, a, b, grouped.get(index, []))
            for index, (a, b) in enumerate(chapters, 1)]


def _event_from_row_item(chapter, pos, raw):
    """Accept pre-v149 tuple rows in tests/old callers without changing ids."""
    if raw.get("event_id") and raw.get("kind"):
        return raw
    try:
        start, end = float(raw["a"]), float(raw["b"])
    except (KeyError, TypeError, ValueError):
        start = end = 0.0
    source_pos = pos if isinstance(pos, int) and pos >= 0 else None
    return {
        "event_id": "U%04d" % (source_pos + 1 if source_pos is not None else 1),
        "kind": "speech",
        "chapter": int(chapter),
        "a": start,
        "b": end,
        "duration": max(0.0, end - start),
        "source_pos": source_pos,
        "text": str(raw.get("text") or ""),
        "audio": "",
        "prev_gap": 0.0,
        "prev_overlap": 0.0,
        "safe_after": 0.0,
        "next_overlap": 0.0,
    }


def _event_line(chapter, pos, raw, annotated=None, tags=None):
    """Render one event identically in writable and read-only contexts."""
    annotated = annotated or []
    tags = tags or []
    event = _event_from_row_item(chapter, pos, raw)
    if event["kind"] == "gap":
        return ("  %s C%d t%.2f-%.2f d%.2f G｜无台词候选区%.2f"
                % (event["event_id"], event["chapter"], event["a"],
                   event["b"], event["duration"], event["duration"]))
    text = ""
    source_pos = event.get("source_pos")
    if isinstance(source_pos, int) and source_pos < len(annotated):
        text = str(annotated[source_pos].get("text") or "")
    if not text:
        text = str(event.get("text") or "")
    text = " ".join(text.split()).strip() or "（无可用转写）"
    tag = str(event.get("audio") or "")
    if not tag and isinstance(source_pos, int) and source_pos < len(tags):
        tag = str(tags[source_pos] or "")
    tag = " ".join(tag.split()).strip() or "未取得逐轨标签"
    return (
        "  %s C%d t%.2f-%.2f d%.2f gap=%.2f ov-=%.2f safe=%.2f "
        "ov+=%.2f 听[%s] 文[%s]"
        % (event["event_id"], event["chapter"], event["a"], event["b"],
           event["duration"], event.get("prev_gap", 0.0),
           event.get("prev_overlap", 0.0), event.get("safe_after", 0.0),
           event.get("next_overlap", 0.0), tag, text))


def _body(rows, frames, scene_notes, annotated=None, tags=None):
    """Render compact timeline records; prose text is only one event field."""
    annotated = annotated or []
    tags = tags or []
    parts = []
    for index, a, b, inside in rows:
        head = "【第 %d 章】片段绝对 %.2f→%.2fs｜片段长 %.2fs" % (
            index, a, b, b - a)
        note = frames.get(index) or ""
        if note:
            head += "\n  画面文字：%s" % note
        extra = scene_notes.get(index) or ""
        if extra:
            head += "\n  画面：%s" % extra
        parts.append(head)
        if not inside:
            parts.append("  （本章没有可用时间轴事件）")
            continue
        parts.append("  时间轴事件（绝对时间优先；文本只是区间属性）：")
        for pos, raw in inside:
            parts.append(_event_line(index, pos, raw, annotated, tags))
    return "\n".join(parts)


def _split_row_for_budget(row, frames, scene_notes, annotated, tags,
                          max_chars):
    """Split an oversized chapter between whole timeline events, never chars."""
    index, start, end, inside = row
    rendered = _body([row], frames, scene_notes, annotated, tags)
    if len(rendered) <= max_chars or not inside:
        return [{"row": row, "start": float(start), "end": float(end)}]

    groups = []
    current = []
    current_frontier = None
    for item in inside:
        candidate = current + [item]
        candidate_row = (index, start, end, candidate)
        candidate_text = _body(
            [candidate_row], frames, scene_notes, annotated, tags)
        event = item[1]
        event_start = float(event["a"])
        overlaps_current = (current_frontier is not None
                            and event_start < current_frontier - 1e-6)
        if current and len(candidate_text) > max_chars and not overlaps_current:
            groups.append(current)
            current = [item]
            current_frontier = float(event["b"])
        else:
            current = candidate
            event_end = float(event["b"])
            current_frontier = (event_end if current_frontier is None
                                else max(current_frontier, event_end))
    if current:
        groups.append(current)

    atoms = []
    for position, group in enumerate(groups):
        if position == 0:
            left = float(start)
        else:
            previous = groups[position - 1][-1][1]
            left = (float(previous["b"]) + float(group[0][1]["a"])) / 2.0
        if position + 1 == len(groups):
            right = float(end)
        else:
            following = groups[position + 1][0][1]
            right = (float(group[-1][1]["b"]) +
                     float(following["a"])) / 2.0
        left = max(float(start), min(float(end), left))
        right = max(left, min(float(end), right))
        atoms.append({
            # The header must describe only the fragment whose events are
            # actually present.  Keeping the full chapter range here would
            # make omitted neighbouring events look like an unexplained gap.
            "row": (index, left, right, group),
            "start": left,
            "end": right,
        })
    return atoms


def _batch_from_atoms(atoms, frames, scene_notes, annotated, tags):
    body = _body([item["row"] for item in atoms], frames,
                 scene_notes, annotated, tags)
    chapter_ids = []
    event_items = []
    for item in atoms:
        chapter = int(item["row"][0])
        if chapter not in chapter_ids:
            chapter_ids.append(chapter)
        for pos, raw in item["row"][3]:
            event_items.append((chapter, pos,
                                _event_from_row_item(chapter, pos, raw)))
    events = [event for _chapter, _pos, event in event_items]
    event_ids = [str(event.get("event_id") or "") for event in events
                 if event.get("event_id")]
    return {
        "body": body,
        "start": float(atoms[0]["start"]),
        "end": float(atoms[-1]["end"]),
        "chapters": chapter_ids,
        "event_ids": event_ids,
        "core_event_ids": list(event_ids),
        "events": events,
        "event_items": event_items,
    }


def _readonly_event_line(chapter, pos, event, annotated, tags):
    line = _event_line(chapter, pos, event, annotated, tags)
    return line if len(line) <= 360 else line[:357] + "…"


def _attach_neighbor_context(batches, expected_ids, annotated, tags,
                             context_events=2):
    """Add small read-only edges while proving core events are exact-once."""
    actual_ids = [event_id for batch in batches
                  for event_id in batch.get("core_event_ids") or []]
    if expected_ids and actual_ids != expected_ids:
        raise RuntimeError("时间轴分批发生事件重复或遗漏，拒绝交给模型")
    if len(actual_ids) != len(set(actual_ids)):
        raise RuntimeError("时间轴分批出现重复 event_id，拒绝交给模型")

    limit = max(0, int(context_events))
    for index, batch in enumerate(batches):
        before = (batches[index - 1].get("event_items") or [])[-limit:] \
            if limit and index else []
        after = (batches[index + 1].get("event_items") or [])[:limit] \
            if limit and index + 1 < len(batches) else []
        before_ids = [event["event_id"] for _chapter, _pos, event in before]
        after_ids = [event["event_id"] for _chapter, _pos, event in after]
        batch["readonly_before_ids"] = before_ids
        batch["readonly_after_ids"] = after_ids
        batch["readonly_event_ids"] = before_ids + after_ids
        batch["readonly_events"] = [event for _chapter, _pos, event
                                    in before + after]
        batch["context_before"] = "\n".join(
            _readonly_event_line(chapter, pos, event, annotated, tags)
            for chapter, pos, event in before)
        batch["context_after"] = "\n".join(
            _readonly_event_line(chapter, pos, event, annotated, tags)
            for chapter, pos, event in after)
    return batches


def _batch_rows(rows, frames, scene_notes, annotated=None, tags=None,
                max_chars=MAX_BODY_CHARS, context_events=2):
    """Pack complete events and attach adjacent read-only chronology."""
    annotated = annotated or []
    tags = tags or []
    max_chars = max(1, int(max_chars))
    atoms = []
    for row in rows:
        atoms.extend(_split_row_for_budget(
            row, frames, scene_notes, annotated, tags, max_chars))

    batches = []
    current = []
    for atom in atoms:
        candidate = current + [atom]
        text = _body([item["row"] for item in candidate], frames,
                     scene_notes, annotated, tags)
        if current and len(text) > max_chars:
            batches.append(_batch_from_atoms(
                current, frames, scene_notes, annotated, tags))
            current = [atom]
        else:
            current = candidate
    if current:
        batches.append(_batch_from_atoms(
            current, frames, scene_notes, annotated, tags))
    expected_ids = [str(raw.get("event_id") or "")
                    for _chapter, _start, _end, inside in rows
                    for _pos, raw in inside if raw.get("event_id")]
    return _attach_neighbor_context(
        batches, expected_ids, annotated, tags,
        context_events=context_events)


# ---------------------------------------------------------------- 模型
def _parse_json(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    body = re.sub(r"//[^\n]*", "", match.group(0))
    body = re.sub(r",\s*([}\]])", r"\1", body)
    try:
        return json.loads(body)
    except ValueError:
        return None


def _ask_plan(llm, body, log):
    """一次问清剪辑单；模型输出不合法就再问一次（只重复一次，不纠缠）。"""
    legacy_retry = False
    for attempt in (1, 2):
        prompt = body
        if legacy_retry:
            prompt += ("\n\n上次使用了自由秒数，不能直接执行。请重新输出当前批可写的 "
                       "delete_events/delete_ranges 事件 ID。")
        answer = llm.chat(SYS_PLAN, prompt, max_tokens=1200)
        plan = _parse_json(answer)
        event_shape = (plan and
                       (isinstance(plan.get("delete_events"), list)
                        or isinstance(plan.get("delete_ranges"), list)))
        legacy_shape = plan and isinstance(plan.get("delete"), list)
        if event_shape or legacy_shape:
            if legacy_shape and not event_shape and attempt == 1:
                legacy_retry = True
                log("   模型仍在使用自由秒数，要求改用 event_id 再答一次")
                continue
            count = (len(plan.get("delete_events") or [])
                     + len(plan.get("delete_ranges") or []))
            log("   模型给出 %d 个事件删除决定%s"
                % (count, "（旧秒数兼容）" if legacy_shape and not event_shape
                   else ""))
            return plan
        log("   第 %d 次输出不是合法剪辑单，再问一次" % attempt)
    return None


def _clamp_batch_delete(raw, start, end, duration):
    """Keep a model batch from deleting material it was never shown."""
    lower = max(0.0, min(float(start), float(duration)))
    upper = max(lower, min(float(end), float(duration)))
    result = []
    for item in raw or []:
        try:
            left, right = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        left = max(lower, min(left, upper))
        right = max(lower, min(right, upper))
        if right - left >= 0.2:
            result.append((left, right))
    return result


def _canonical_event_id(value):
    match = re.fullmatch(r"\s*([UGug])(\d{1,8})\s*", str(value or ""))
    if not match:
        return ""
    return "%s%04d" % (match.group(1).upper(), int(match.group(2)))


def _decision_ids(raw):
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    result = []
    for value in values:
        if isinstance(value, str):
            found = re.findall(r"[UGug]\d{1,8}", value)
        else:
            found = [value]
        for item in found:
            event_id = _canonical_event_id(item)
            if event_id and event_id not in result:
                result.append(event_id)
    return result


def _decision_range_ids(raw):
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            left = (item.get("start_event") or item.get("from")
                    or item.get("start"))
            right = (item.get("end_event") or item.get("to")
                     or item.get("end"))
            ids = _decision_ids([left, right])
        else:
            ids = _decision_ids(item)
        if len(ids) >= 2:
            result.append((ids[0], ids[1]))
        elif len(ids) == 1:
            result.append((ids[0], ids[0]))
    return result


def _project_legacy_seconds(raw, batch, duration):
    """Project old free-second decisions onto complete writable events.

    No raw second boundary is ever executed.  A range that cannot touch at
    least one current-core event is ambiguous and makes the batch fail closed.
    Read-only neighbour events are recorded but never included in projection.
    """
    events = list(batch.get("events") or [])
    readonly_events = list(batch.get("readonly_events") or [])
    lower = max(0.0, min(float(batch["start"]), float(duration)))
    upper = max(lower, min(float(batch["end"]), float(duration)))
    accepted_ids = []
    rejected_context_ids = []
    projected_rows = []
    unmapped = []
    spans = []
    for item in raw or []:
        try:
            original_left, original_right = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            unmapped.append(list(item) if isinstance(item, (list, tuple))
                            else [str(item)])
            continue
        if original_right < original_left:
            original_left, original_right = original_right, original_left
        for event in readonly_events:
            event_left, event_right = float(event["a"]), float(event["b"])
            if original_left < event_right and original_right > event_left:
                event_id = str(event.get("event_id") or "")
                if event_id and event_id not in rejected_context_ids:
                    rejected_context_ids.append(event_id)
        left = max(lower, min(original_left, upper))
        right = max(lower, min(original_right, upper))
        if right - left < 0.2:
            unmapped.append([original_left, original_right])
            continue
        selected = [event for event in events
                    if left < float(event["b"]) and right > float(event["a"])]
        if not selected:
            unmapped.append([original_left, original_right])
            continue
        projected_left = min(float(event["a"]) for event in selected)
        projected_right = max(float(event["b"]) for event in selected)
        ids = [str(event["event_id"]) for event in selected]
        for event_id in ids:
            if event_id not in accepted_ids:
                accepted_ids.append(event_id)
        spans.append((projected_left, projected_right))
        projected_rows.append({
            "seconds": [round(original_left, 3), round(original_right, 3)],
            "event_ids": ids,
            "projected": [round(projected_left, 3),
                          round(projected_right, 3)],
        })
    return {
        "delete": _clean_delete(spans, duration),
        "accepted_event_ids": accepted_ids,
        "rejected_context_ids": rejected_context_ids,
        "projected_from_seconds": projected_rows,
        "unmapped_seconds": unmapped,
        "projection_failed": bool(unmapped),
    }


def _resolve_batch_decisions(plan, batch, duration):
    """Authorise model decisions against writable core event IDs only."""
    events = list(batch.get("events") or [])
    lookup = {str(event.get("event_id") or ""): event for event in events
              if event.get("event_id")}
    order = {event_id: position for position, event_id in enumerate(lookup)}
    readonly = set(batch.get("readonly_event_ids") or [])
    has_ids = ("delete_events" in plan or "delete_ranges" in plan)
    if not has_ids:
        projected = _project_legacy_seconds(
            plan.get("delete"), batch, duration)
        return {
            "delete": projected["delete"],
            "decision_mode": "legacy_seconds_projected",
            "requested_event_ids": [],
            "accepted_event_ids": projected["accepted_event_ids"],
            "accepted_ranges": [],
            "rejected_context_ids": projected["rejected_context_ids"],
            "rejected_unknown_ids": [],
            "projected_from_seconds": projected["projected_from_seconds"],
            "projection_failed": projected["projection_failed"],
            "unmapped_seconds": projected["unmapped_seconds"],
        }

    requested = _decision_ids(plan.get("delete_events") or [])
    requested_ranges = _decision_range_ids(plan.get("delete_ranges") or [])
    accepted_ids = []
    accepted_ranges = []
    rejected_context = []
    rejected_unknown = []
    spans = []

    def reject(event_id):
        target = rejected_context if event_id in readonly else rejected_unknown
        if event_id not in target:
            target.append(event_id)

    for event_id in requested:
        event = lookup.get(event_id)
        if event is None:
            reject(event_id)
            continue
        if event_id not in accepted_ids:
            accepted_ids.append(event_id)
        spans.append((float(event["a"]), float(event["b"])))

    for left_id, right_id in requested_ranges:
        if left_id not in lookup or right_id not in lookup:
            if left_id not in lookup:
                reject(left_id)
            if right_id not in lookup:
                reject(right_id)
            continue
        first, last = sorted((order[left_id], order[right_id]))
        selected = events[first:last + 1]
        left = min(float(event["a"]) for event in selected)
        right = max(float(event["b"]) for event in selected)
        spans.append((left, right))
        accepted_ranges.append([events[first]["event_id"],
                                events[last]["event_id"]])
        for event in selected:
            event_id = str(event["event_id"])
            if event_id not in accepted_ids:
                accepted_ids.append(event_id)

    return {
        "delete": _clean_delete(spans, duration),
        "decision_mode": "event_ids",
        "requested_event_ids": requested,
        "requested_ranges": [list(pair) for pair in requested_ranges],
        "accepted_event_ids": accepted_ids,
        "accepted_ranges": accepted_ranges,
        "rejected_context_ids": rejected_context,
        "rejected_unknown_ids": rejected_unknown,
    }


def _clean_script_text(value, limit):
    return " ".join(str(value or "").split()).strip()[:limit]


def _script_context(script, start, end, requested_level):
    """Return the writer's global mainline and batch-local section advice."""
    script = script if isinstance(script, dict) else {}
    mainline = _clean_script_text(script.get("mainline"), 120) or "（未提供主线）"
    script_level = _clean_script_text(script.get("cut_level"), 24) or "未知"
    strategy = _clean_script_text(script.get("strategy"), 220)
    matched = []
    for index, section in enumerate(script.get("sections") or [], 1):
        try:
            section_start = float(section.get("start"))
            section_end = float(section.get("end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if section_end <= float(start) or section_start >= float(end):
            continue
        matched.append({
            "index": index,
            "start": section_start,
            "end": section_end,
            "title": _clean_script_text(section.get("title"), 80)
            or "（未命名）",
            "advice": _clean_script_text(section.get("advice"), 100)
            or "（未给取舍建议）",
        })
    lines = ["【编剧剧本（全局只读目标）】",
             "主线：%s" % mainline,
             "当前设置力度：%s；剧本产出力度：%s" %
             (requested_level, script_level)]
    if strategy:
        lines.append("力度策略：%s" % strategy)
    if matched:
        for section in matched[:8]:
            lines.append("剧本章S%d %.2f-%.2f｜%s｜取舍=%s"
                         % (section["index"], section["start"], section["end"],
                            section["title"], section["advice"]))
    else:
        lines.append("本批没有匹配到剧本章节；仍须服从全局主线和当前力度。")
    return "\n".join(lines), matched


def _ask_batches(llm, batches, level, duration, log, cancel_check=None,
                 script=None, units=None, target_keep_ratio=None):
    """Ask every batch and combine absolute-time deletion decisions."""
    if not batches:
        raise RuntimeError("没有可供模型判断的章节")
    delete = []
    reasons = []
    audit = []
    total = len(batches)
    level_note = ("当前剪辑力度：%s。%s" %
                  (level, LEVEL_EDITOR_RULES.get(
                      level, LEVEL_EDITOR_RULES["medium"])))
    # 台词预算：按力度的**保留范围**折算成"最多能删几句"。
    # 范围内由模型自己判断，只有删过头（低于保留下限）才由代码封顶。
    units = list(units or [])
    keep_low, keep_high = LEVEL_KEEP_RANGE.get(
        level, LEVEL_KEEP_RANGE["medium"])
    if target_keep_ratio is not None:
        target_keep_ratio = max(0.15, min(1.0, float(target_keep_ratio)))
        keep_low = max(0.15, target_keep_ratio - 0.05)
        keep_high = min(1.0, target_keep_ratio + 0.05)
        level_note += (
            "\n全片目标约保留 %.0f%%，允许为保证语义完整浮动约 ±5 个百分点。"
            "重要内容可以多留，重复和跑题可以少留，但全片合计应尽量接近目标。"
            % (target_keep_ratio * 100.0))
    max_drop = float(1.0 - keep_low)
    budget = max(1, int(round(len(units) * max_drop))) if units else 0
    used = 0
    if budget:
        log("   台词预算：%d 句，%s 目标保留 %d%%~%d%%（最多删 %d 句）"
            % (len(units), level, keep_low * 100, keep_high * 100, budget))
    for number, batch in enumerate(batches, 1):
        if callable(cancel_check) and cancel_check():
            raise autocut.RenderCancelled("已取消")
        if budget and used >= budget:
            # 预算已被前几批用完：剩下的批不再问，省掉无意义的模型调用
            log("   台词预算已用完（%d/%d 句）：剩余 %d 批不再向模型要删除"
                % (used, budget, total - number + 1))
            break
        start, end = float(batch["start"]), float(batch["end"])
        script_note, script_sections = _script_context(
            script, start, end, level)
        core_ids = list(batch.get("core_event_ids")
                        or batch.get("event_ids") or [])
        before_ids = list(batch.get("readonly_before_ids") or [])
        after_ids = list(batch.get("readonly_after_ids") or [])
        before = batch.get("context_before") or "（无）"
        after = batch.get("context_after") or "（无）"
        budget_note = ""
        if budget:
            remain_low = max(0, int(round(len(units) * keep_low)))
            budget_note = ("台词预算：全片 %d 句台词。本力度目标保留 %d%%~%d%%"
                           "（差不多就行，不用精确）；已删 %d 句，"
                           "最多还能删 %d 句，删过头的内容不会被执行。\n"
                           % (len(units), keep_low * 100, keep_high * 100,
                              used, max(0, budget - used)))
        prompt = (
            "%s\n%s\n%s"
            "这是全片第 %d/%d 批，当前批核心范围 %.2f~%.2fs。\n"
            "【前一批只读上下文｜不可删除：%s】\n%s\n"
            "【当前批可写事件｜只允许这些 ID：%s】\n%s\n"
            "【后一批只读上下文｜不可删除：%s】\n%s\n"
            "只能用当前批可写 ID 填 delete_events/delete_ranges；"
            "只读 ID 与自行填写的秒数一律不会执行。"
            % (level_note, script_note, budget_note, number, total, start, end,
               ",".join(before_ids) or "无", before,
               ",".join(core_ids) or "无", batch["body"],
               ",".join(after_ids) or "无", after))
        log("   模型分批：%d/%d（%.1f~%.1fs，第 %s 章）"
            % (number, total, start, end,
               ",".join(str(value) for value in batch["chapters"])))
        plan = _ask_plan(llm, prompt, log)
        if not plan:
            raise RuntimeError(
                "模型第 %d/%d 批没有给出可用剪辑单（两次都不是 JSON）"
                % (number, total))
        decision = _resolve_batch_decisions(plan, batch, duration)
        if decision.get("projection_failed"):
            raise RuntimeError(
                "模型第 %d/%d 批仍使用无法安全映射的自由秒数，已拒绝执行"
                % (number, total))
        batch_delete = decision["delete"]
        if budget and batch_delete:
            # 代码核对预算（11·五：约束不能交给模型自律）：
            # 按时间顺序接纳删除，超出台词预算的区间直接丢弃并记账。
            allowed = []
            for a, b in batch_delete:
                count = _units_in([(a, b)], units)
                if used + count <= budget:
                    allowed.append((a, b))
                    used += count
                else:
                    log("   预算封顶：丢弃一段删除（%.1f~%.1fs，含 %d 句台词；"
                        "已用 %d/%d 句）"
                        % (float(a), float(b), count, used, budget))
            if len(allowed) != len(batch_delete):
                log("   本批删除被力度预算压到 %d 段（原 %d 段）"
                    % (len(allowed), len(batch_delete)))
            batch_delete = allowed
        reason = str(plan.get("reason") or "").strip()
        if reason:
            reasons.append("第%d批：%s" % (number, reason))
        delete.extend(batch_delete)
        audit.append({
            "batch": number,
            "start": round(start, 3),
            "end": round(end, 3),
            "chapters": list(batch["chapters"]),
            "event_ids": list(batch.get("event_ids") or []),
            "readonly_before_ids": before_ids,
            "readonly_after_ids": after_ids,
            "decision_mode": decision["decision_mode"],
            "requested_event_ids": decision.get("requested_event_ids") or [],
            "requested_ranges": decision.get("requested_ranges") or [],
            "accepted_event_ids": decision.get("accepted_event_ids") or [],
            "accepted_ranges": decision.get("accepted_ranges") or [],
            "rejected_context_ids": decision.get("rejected_context_ids") or [],
            "rejected_unknown_ids": decision.get("rejected_unknown_ids") or [],
            "projected_from_seconds": decision.get(
                "projected_from_seconds") or [],
            "unmapped_seconds": decision.get("unmapped_seconds") or [],
            "script_mainline": _clean_script_text(
                (script or {}).get("mainline") if isinstance(script, dict) else "",
                120),
            "script_level": str((script or {}).get("cut_level") or "")
            if isinstance(script, dict) else "",
            "script_sections": script_sections,
            "body_chars": len(batch["body"]),
            "delete": [[round(a, 3), round(b, 3)]
                       for a, b in batch_delete],
            "reason": reason,
        })
    return {
        "delete": delete,
        "reason": "；".join(reasons),
        "reasons": reasons,
        "batches": audit,
    }


def _configured_keep_ratio(runtime, conf):
    """Read the user's exact keep target without re-running level rules."""
    value = None
    try:
        value = getattr(runtime, "keep_ratio", None)
        if value is None:
            value = (getattr(runtime, "director_conf", None) or {}).get(
                "keep_ratio")
        if value is None:
            settings_path = os.path.join(BASE, "conf", "settings.json")
            if os.path.isfile(settings_path):
                with open(settings_path, encoding="utf-8") as handle:
                    payload = json.load(handle) or {}
                if isinstance(payload, dict):
                    value = payload.get("keep_ratio")
        if value is None:
            value = (conf or {}).get("keep_ratio")
        if value is None:
            return None
        return max(0.15, min(1.0, float(value)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- 校验
def _clean_delete(raw, duration):
    """区间合法化：夹到片内、去掉太短的、合并重叠。"""
    spans = []
    for item in raw or []:
        try:
            a, b = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        a = max(0.0, min(a, duration))
        b = max(0.0, min(b, duration))
        if b - a >= 0.2:
            spans.append((a, b))
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _voiced_start(data, floor_db=-45.0):
    """开头第一个"真的有人声"的位置。

    不认 ASR 的词起点：ASR 时间戳比真实发音早 0.1~0.5 秒，按它裁完，
    成片开头几秒仍是静音（实测 0~1 秒 -76.9dB，第 3 秒才起声）。
    这里用索引里已经算好的每秒音量（analysis.audio_1s）判。
    """
    levels = (data.get("analysis") or {}).get("audio_1s") or []
    for index, value in enumerate(levels):
        try:
            if float(value) > floor_db:
                return max(0.0, float(index) - 0.15)   # A21：首字前 150ms
        except (TypeError, ValueError):
            continue
    segments = data.get("segments") or []
    try:
        return float(segments[0][0]) if segments else 0.0
    except (TypeError, ValueError, IndexError):
        return 0.0


def _first_voiced_near(src, start, track=0, window=6.0, sr=16000,
                       floor_db=-45.0, need_s=0.12):
    """从 start 起往后找第一个"持续有人声"的位置（20ms 包络，认能量不认 ASR）。

    索引里的 analysis.audio_1s 是每秒折叠，粒度太粗：原片第 4 秒里有说话，
    整秒就被判成"有声"，而切点实际落在 4.75 秒的静音上。
    """
    start = max(0.0, float(start))
    done = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-ss", "%.3f" % start, "-t", "%.3f" % window, "-i", src,
         "-map", "0:a:%d" % int(track), "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not done.stdout:
        return start
    audio = np.frombuffer(done.stdout, dtype="<i2").astype(np.float32) / 32768.0
    frame = int(sr * 0.02)
    count = audio.size // frame
    if count < 1:
        return start
    frames = audio[:count * frame].reshape(count, frame)
    db = 20 * np.log10(np.maximum(np.abs(frames).max(axis=1), 1e-9))
    need = max(1, int(round(need_s / 0.02)))
    run = 0
    for index, value in enumerate(db):
        if float(value) > floor_db:
            run += 1
            if run >= need:
                return start + max(0.0, (index - need + 1) * 0.02)
        else:
            run = 0
    return start


def _enforce_opening(keep, units, log, data=None, src=None, track=0):
    """开头必须是人在说话：第一个保留段之前没人声的部分去掉。

    文档（使用说明·七 / 11·五）要求"开头 3 秒内必须有人说话"；
    判据必须用**能量**，不能只看 ASR 词起点——这是之前片头无声的原因。
    """
    if not keep:
        return keep
    if src:
        first_word = _first_voiced_near(src, keep[0][0], track=track)
    elif data is not None:
        first_word = _voiced_start(data)
    else:
        first_word = float(units[0]["a"]) if units else 0.0
    a, b = keep[0]
    if a >= first_word - 0.2:
        return keep
    if first_word - a <= 0.5:            # 已经很近了，不用动
        return keep
    head_cut = min(first_word, b)
    if head_cut - a < 0.3:
        return keep
    keep = [(head_cut, b)] + list(keep[1:])
    log("   开头修正：删掉 %.1f~%.1fs 的无声/空场，让第一句人声提前出现"
        % (a, head_cut))
    return keep


def _coverage_report(keep, units, duration, log):
    kept = sum(float(b) - float(a) for a, b in keep)
    dropped = duration - kept
    log("   代码核对：保留 %d 段 / %.1f 秒（原 %.1f 秒，删掉 %.1f 秒 = %.0f%%）"
        % (len(keep), kept, duration, dropped,
           (dropped / duration * 100) if duration else 0))
    if not units:
        return
    # 按区间重叠算（不能用"起点是否落在段内"：边界差 0.02 秒会误判成没覆盖）
    covered = 0
    for unit in units:
        ua, ub = float(unit["a"]), float(unit["b"])
        if any(ua < float(b) - 0.05 and ub > float(a) + 0.05 for a, b in keep):
            covered += 1
    log("   台词存活：%d/%d 句（%.0f%%）"
        % (covered, len(units), covered / float(len(units)) * 100))


# ---------------------------------------------------------------- 入口
def editor_by_model(runtime, ctx):
    """editor 阶段的替代实现，产出与原 editor 完全相同的 payload。"""
    t0 = time.time()
    idx = runtime.payload(ctx, "writer").get("index_dir") or runtime.idx
    data = autocut.load_index(idx)
    duration = float(data["duration"])
    conf = edit_modes.apply_to_conf(__import__("copy").deepcopy(
        autocut.load_conf()), runtime.mode)
    conf.setdefault("render", {})["scale"] = (
        conf.get("render", {}).get("scale") or "1920:-2")

    units = autocut.load_units(data)
    if not units:
        raise RuntimeError("索引里没有逐句台词，模型剪辑师无从判断（先跑分析）")

    runtime.log("③ 剪辑师（模型）：先看画面、听声音，再自己出剪辑单…")

    # 章节：长停顿 + 画面跳变（小脑算边界，模型只起名/判断）
    shots = []
    try:
        shots = autocut.shot_cuts_of(
            data, conf, should_cancel=getattr(runtime, "should_cancel", None)) or []
    except Exception as exc:  # noqa: BLE001
        runtime.log("   画面跳变读不出来（%s），只用停顿分章" % exc)
    chapters = _chapters(units, shots, duration)
    if not chapters:
        chapters = [(0.0, duration)]
    runtime.log("   章节：%d 章（长停顿 + 画面跳变）" % len(chapters))

    # 画面：每章开头/中间/结尾各抽一帧，证据既给模型也写入剪辑单。
    engine = None
    try:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
    except Exception as exc:  # noqa: BLE001
        runtime.log("   没有 OCR 引擎（%s），画面只看明暗" % exc)
    frames, scene_notes, visual_evidence = _collect_visual_evidence(
        runtime, chapters, engine)

    annotated = (data.get("neutral") or {}).get("segments") or []
    # 每条所选音轨独立解码和分析，只合并轻量标签，不混音、不同时占内存。
    tags, selected_tracks, audio_evidence = _multi_track_tags(
        runtime, data, units)
    # 先生成一次全片绝对时间轴，再按完整章节分批；单章过长才在完整
    # 事件之间拆批。事件 id 和前后时间关系不会因分批重新计算。
    rows = _chapter_lines(units, chapters, tags=tags)
    whole_body = _body(rows, frames, scene_notes, annotated, tags)
    # 给每批提示和力度说明预留上下文，不再用字符截断丢掉后半片。
    batches = _batch_rows(
        rows, frames, scene_notes, annotated, tags,
        max_chars=max(400, MAX_BODY_CHARS - 240))
    runtime.log("   时间轴：%d 字（章 + 事件），分 %d 批完整判断"
                % (len(whole_body), len(batches)))
    target_keep_ratio = _configured_keep_ratio(runtime, conf)
    if target_keep_ratio is None:
        runtime.log("   力度：%s（告诉模型删多少，最终由它判断）" % runtime.level)
    else:
        runtime.log("   力度：%s / 目标保留 %.0f%%（模型按内容价值分配）"
                    % (runtime.level, target_keep_ratio * 100.0))
    if len(whole_body) > MAX_BODY_CHARS:
        runtime.log("   时间轴超过 %d 字，已按章拆成 %d 批（没有截掉事件）"
                    % (MAX_BODY_CHARS, len(batches)))

    # 中立转写：台词里已经带同音候选（E 盘 homophones.format_with_alternatives），
    # 这里只报一句个数，不再重复贴清单。
    if annotated:
        runtime.log("   中立转写：%d 句台词带同音候选（把握 < %.2f）"
                    % (len(annotated), (data.get("neutral") or {}).get("low_prob")
                       or 0.70))

    from ui.llm_bridge import LLM
    plan_raw = None
    llm = LLM(base_dir=BASE)
    try:
        llm.start()
        plan_raw = _ask_batches(
            llm, batches, runtime.level, duration, runtime.log,
            cancel_check=getattr(runtime, "should_cancel", None),
            script=data.get("script") or {}, units=units,
            target_keep_ratio=target_keep_ratio)
    finally:
        llm.stop()
    if not plan_raw:
        raise RuntimeError("模型没有给出可用的剪辑单（两次输出都不是 JSON）")

    delete = _clean_delete(plan_raw.get("delete"), duration)
    keep = autocut.subtract([(0.0, duration)], delete)
    keep = [(round(float(a), 2), round(float(b), 2)) for a, b in keep
            if float(b) - float(a) >= 0.3]
    speech_tracks = []
    chooser = getattr(runtime, "speech_tracks", None)
    if callable(chooser):
        try:
            speech_tracks = list(chooser(data, selected_tracks) or [])
        except Exception:  # noqa: BLE001
            speech_tracks = []
    opening_track = int((speech_tracks or selected_tracks or
                         [data.get("audio_stream_used") or 0])[0])
    keep = _enforce_opening(
        keep, units, runtime.log, data=data, src=runtime.src,
        track=opening_track)
    if not keep:
        raise RuntimeError("模型把片子删光了（keep 为空），拒绝出片")
    _coverage_report(keep, units, duration, runtime.log)
    # The user percentage means retained information, not raw wall-clock time.
    # Long no-dialogue gaps are removed later and must never count as content.
    actual_keep_ratio = (_units_in(keep, units) / float(len(units))
                         if units else 0.0)
    timeline_keep_ratio = (sum(float(b) - float(a) for a, b in keep)
                           / duration if duration > 0 else 0.0)
    runtime.log("   模型理由：%s" % str(plan_raw.get("reason") or "")[:120])

    plan = edit_plan.build_plan(
        source=runtime.source_identity(), mode=runtime.mode, level=runtime.level,
        config=conf, semantic_spans=keep, versions=runtime.ARTIFACTS,
        evidence={"index_schema": data.get("schema") or {},
                  "model_editor": {"delete": delete,
                                   "backend_signature": __import__("llm_gateway").backend_signature(),
                                   "chapters": len(chapters),
                                   "batch_count": len(plan_raw.get("batches") or []),
                                   "batches": plan_raw.get("batches") or [],
                                   "reason": str(plan_raw.get("reason") or ""),
                                   "target_keep_ratio": target_keep_ratio,
                                   "actual_keep_ratio": actual_keep_ratio,
                                   "timeline_keep_ratio": timeline_keep_ratio,
                                   "keep_ratio_delta": (
                                       actual_keep_ratio - target_keep_ratio
                                       if target_keep_ratio is not None else None),
                                   "audio_tracks": selected_tracks,
                                   "audio_evidence": audio_evidence,
                                   "visual_evidence": visual_evidence,
                                   "reviewed_by": (
                                       "ai-api" if bool(getattr(
                                           getattr(llm, "api", None),
                                           "enabled", False))
                                       else "local-llm")}})
    plan_path = runtime.write_plan("editor", plan, "cut_plan")
    runtime.done("初剪（模型）", t0)
    return __import__("workflow_engine").StageResult(
        runtime.artifact("editor"),
        {"index_dir": idx, "duration": duration, "conf": conf,
         "spans": [[a, b] for a, b in keep], "logs": [],
         "plan": plan, "plan_path": plan_path,
         "plan_sha256": plan["plan_sha256"],
         "task_seed": plan["task_seed"]},
        {"artifact_type": "cut_plan", "segments": len(keep),
         "plan_sha256": plan["plan_sha256"], "by": "model_editor"})
