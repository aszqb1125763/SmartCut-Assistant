# -*- coding: utf-8 -*-
"""Build editable Simplified/Traditional caption sources for Premiere.

Premiere owns the final timeline.  SmartCut therefore writes two SRT files
whose timestamps already use the rebuilt sequence coordinates, then the CEP
bridge imports each file as a native caption track.  The SRT files stay beside
the generated .prproj so the project keeps a stable, inspectable media source.
"""
from __future__ import annotations

import json
import os
import re

import subtitles


_FALLBACK_S2T = str.maketrans({
    "后": "後", "发": "發", "里": "裡", "台": "臺",
    "这": "這", "个": "個", "们": "們", "为": "為", "么": "麼",
    "说": "說", "话": "話", "开": "開", "关": "關", "机": "機",
    "进": "進", "过": "過", "还": "還", "没": "沒", "时": "時",
    "间": "間", "长": "長", "来": "來", "动": "動", "画": "畫",
    "声": "聲", "轨": "軌", "辑": "輯", "体": "體", "简": "簡",
    "转": "轉", "换": "換", "录": "錄", "频": "頻", "导": "導",
    "见": "見", "现": "現", "应": "應", "经": "經", "实": "實",
    "从": "從", "与": "與", "并": "並", "删": "刪", "对": "對",
    "显": "顯", "当": "當", "种": "種", "样": "樣", "无": "無",
    "处": "處", "头": "頭", "点": "點", "边": "邊", "际": "際",
    "继": "繼", "续": "續", "设": "設", "备": "備", "阶": "階",
    "题": "題", "数": "數", "据": "據", "线": "線", "该": "該",
    "区": "區", "块": "塊", "认": "認", "备": "備", "准": "準",
})
_FALLBACK_T2S = str.maketrans({
    traditional: chr(simplified)
    for simplified, traditional in _FALLBACK_S2T.items()
})


def _load_index(index_dir):
    path = index_dir
    if os.path.isdir(path):
        path = os.path.join(path, "index.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _converter(config="s2t"):
    try:
        from opencc import OpenCC
        return OpenCC(config)
    except Exception:  # noqa: BLE001 - packaged fallback must not block export
        return None


def to_traditional(text, converter=None):
    """Convert one caption to Traditional Chinese.

    The packaged application includes OpenCC.  The small character fallback is
    deliberately kept so a source checkout without that optional dependency
    can still export a usable project instead of losing both caption tracks.
    """
    value = str(text or "")
    converter = converter if converter is not None else _converter()
    if converter is not None:
        try:
            return converter.convert(value)
        except Exception:  # noqa: BLE001
            pass
    return value.translate(_FALLBACK_S2T)


def to_simplified(text, converter=None):
    """Normalize the Simplified track even when source ASR contains 繁體字."""
    value = str(text or "")
    converter = converter if converter is not None else _converter("t2s")
    if converter is not None:
        try:
            return converter.convert(value)
        except Exception:  # noqa: BLE001
            pass
    return value.translate(_FALLBACK_T2S)


def _timeline(keep_spans):
    cursor = 0.0
    rows = []
    for row in keep_spans or []:
        try:
            start, end = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        rows.append((cursor, start, end - start))
        cursor += end - start
    return rows, cursor


def kept_unresolved_speech(index_data, keep_spans, min_overlap=0.04):
    """Return unresolved VAD speech windows that must block delivery.

    只认 ``subtitle_alignment_blockers``（>=1.5 秒的连续人声无台词）。
    ``subtitle_alignment_diagnostics`` 里的短窗口（<1.5 秒）通常是"呃/嗯/哦"
    之类的语气词，重听后确认没有有效台词，只作诊断，不能阻塞导出——
    否则一条 1 秒的叹气就能让整部片子出不了工程。
    """
    spans = []
    for row in keep_spans or []:
        try:
            start, end = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if end > start:
            spans.append((start, end))
    issues = []
    for row in index_data.get("subtitle_alignment_blockers") or []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        overlap = max((max(0.0, min(end, right) - max(start, left))
                       for left, right in spans), default=0.0)
        if overlap >= float(min_overlap):
            issues.append(dict(row, kept_overlap=overlap))
    return issues


def mapped_segments(index_dir, keep_spans, source_segments=None,
                    source_evidence="index", return_evidence=False,
                    return_diagnostics=False):
    """Return source ASR remapped onto the compact Premiere sequence."""
    data = _load_index(index_dir)
    if source_segments is None:
        source_segments = subtitles.get_segments(data)
    if source_evidence == "index":
        source_evidence = data.get("segment_evidence")
    source_segments, source_evidence, blocked = subtitles.sanitize_source_segments(
        source_segments, source_evidence)
    timeline, total = _timeline(keep_spans)
    mapped, mapped_evidence = subtitles.remap(
        source_segments, timeline, word_segments=source_evidence,
        return_evidence=True)
    cleaned = []
    cleaned_evidence = []
    seen = set()
    for mapped_index, (start, end, text) in enumerate(mapped):
        start = max(0.0, float(start))
        end = min(float(total), float(end))
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value or end - start < 0.04:
            continue
        key = (round(start, 3), round(end, 3), value)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((key[0], key[1], value))
        cleaned_evidence.append(
            mapped_evidence[mapped_index]
            if mapped_index < len(mapped_evidence) else {})
    paired = sorted(zip(cleaned, cleaned_evidence),
                    key=lambda item: (item[0][0], item[0][1], item[0][2]))
    cleaned = [row for row, _evidence in paired]
    cleaned_evidence = [evidence for _row, evidence in paired]
    diagnostics = {"timing_rows_blocked": len(blocked),
                   "blocked_rows": blocked}
    if return_evidence and return_diagnostics:
        return cleaned, cleaned_evidence, diagnostics
    if return_evidence:
        return cleaned, cleaned_evidence
    if return_diagnostics:
        return cleaned, diagnostics
    return cleaned


def write_editable_tracks(index_dir, keep_spans, project_path, proofread=True,
                          log=print, force_proofread=False):
    """Write Simplified and Traditional SRTs beside a generated .prproj."""
    warnings = []
    # One bad three-second ASR row must not discard a completed 100-minute
    # analysis.  Re-hear only the flagged source-track window, atomically save
    # it after the normal timing gate passes, then proofread the repaired text.
    import index as indexer
    try:
        repair = indexer.repair_index_before_delivery(
            index_dir, log=lambda message: log("  " + str(message)))
    except Exception as exc:  # noqa: BLE001 - subtitles are best-effort delivery
        repair = {"changed": False, "remaining_issues": []}
        warning = ("字幕局部重听未能完成：%s；已继续生成 PR 工程，"
                   "异常字幕会被跳过并保留诊断。" % str(exc)[:160])
        warnings.append(warning)
        log("  " + warning)
    if repair.get("changed"):
        log("  字幕时间轴已局部自愈，继续生成可编辑 PR 字幕轨。")
    low_confidence_review = {}
    try:
        low_confidence_review = indexer.repair_low_confidence_before_delivery(
            index_dir, log=lambda message: log("  " + str(message)))
    except Exception as exc:  # noqa: BLE001 - uncertainty must not block delivery
        warning = ("字幕低置信词局部重听未能完成：%s；已保留原始 ASR，"
                   "不影响 PR 工程生成。" % str(exc)[:160])
        warnings.append(warning)
        log("  " + warning)
    if low_confidence_review.get("corrected_count"):
        log("  字幕低置信词已局部复听修正 %d 处。" %
            int(low_confidence_review["corrected_count"]))
    if low_confidence_review.get("unresolved_count"):
        warning = ("仍有 %d 个低置信词窗口两次复听结果不一致；"
                   "已保留原字幕并继续生成工程。" %
                   int(low_confidence_review["unresolved_count"]))
        warnings.append(warning)
        log("  " + warning)
    data = _load_index(index_dir)
    import subtitle_quality

    timing_issues = subtitle_quality.alignment_issues(data)
    quality = {}
    if proofread:
        try:
            source_rows, quality = subtitle_quality.correct_source_segments(
                data, index_dir if os.path.isdir(index_dir) else os.path.dirname(index_dir),
                log=log, force=force_proofread)
        except Exception as exc:  # noqa: BLE001 - preserve ASR when 8B is unavailable
            source_rows = subtitles.get_segments(data)
            warning = ("字幕上下文校对未完成：%s；已保留原始 ASR 字幕，"
                       "不影响 PR 工程生成。" % str(exc)[:160])
            warnings.append(warning)
            quality = {"warning": warning, "proofread_fallback": "source_asr"}
            log("  " + warning)
    else:
        source_rows = subtitles.get_segments(data)
        quality = {
            "timestamp_alignment_issue_count": len(timing_issues),
            "timestamp_alignment_issues": timing_issues,
        }
    timing_issues = quality.get("timestamp_alignment_issues") or timing_issues
    quality["low_confidence_relisten"] = dict(low_confidence_review or {})
    if timing_issues:
        first = timing_issues[0]
        start = first.get("start")
        end = first.get("end")
        where = ("%.2f~%.2f 秒" % (float(start), float(end))
                 if start is not None and end is not None else "未知时间")
        text = str(first.get("text") or "").strip()[:24]
        detail = "“%s”" % text if text else ""
        warning = (
            "检测到 %d 处 ASR 时间疑点，首处位于 %s%s；已继续生成 PR 工程，"
            "这些字幕只在通过安全过滤后才会写入字幕轨。"
            % (len(timing_issues), where, detail))
        warnings.append(warning)
        log("  " + warning)
    unresolved = kept_unresolved_speech(data, keep_spans)
    if unresolved:
        first = unresolved[0]
        warning = (
            "自动局部重听后仍有 %d 处连续人声没有可靠字幕，首处位于原片 "
            "%.2f~%.2f 秒；语气词、笑声和呼吸不计入。工程继续生成，"
            "疑点留在质量报告中供后续补听。"
            % (len(unresolved), float(first["start"]), float(first["end"])))
        warnings.append(warning)
        log("  " + warning)
    # Remap both text and its word anchors, then paginate on the compact PR
    # timeline.  This keeps acoustic boundaries even when a cut starts inside
    # what later becomes one visual caption page.
    rows, mapped_evidence, timing = mapped_segments(
        index_dir, keep_spans, source_segments=source_rows,
        return_evidence=True, return_diagnostics=True)
    quality = dict(quality or {})
    quality.update(timing)
    if timing["timing_rows_blocked"]:
        examples = "、".join(
            "%.2f~%.2f秒 %s" % (
                row["start"], row["end"], row["reason"])
            for row in timing["blocked_rows"][:3])
        warning = (
            "已跳过 %d 条时间证据异常的字幕（%s）；视频与 PR 工程继续生成。"
            % (timing["timing_rows_blocked"], examples))
        warnings.append(warning)
        log("  " + warning)
    if not rows:
        warnings.append("定稿时间轴没有可安全写入的台词，已跳过字幕轨，不影响工程。")
        return {"tracks": [], "count": 0,
                "quality": dict(quality, delivery_mode="best_effort_nonblocking"),
                "warning": "; ".join(dict.fromkeys(warnings))}

    directory = os.path.dirname(os.path.abspath(project_path))
    os.makedirs(directory, exist_ok=True)
    stem = os.path.splitext(os.path.basename(project_path))[0]
    simplified_path = os.path.join(directory, stem + "_简体中文字幕.srt")
    traditional_path = os.path.join(directory, stem + "_繁体中文字幕.srt")

    converter = _converter()
    simplified_converter = _converter("t2s")
    caption_style = subtitles.load_style()
    simplified_rows = [
        (start, end, to_simplified(text, converter=simplified_converter))
        for start, end, text in rows
    ]
    mapped_count = len(simplified_rows)
    simplified_rows, semantic_evidence = subtitles.semantic_segment_cues(
        simplified_rows, word_segments=mapped_evidence,
        target_chars=caption_style.get("semantic_target_chars", 15),
        max_chars=caption_style.get("semantic_max_chars", 15),
        return_evidence=True)
    quality["semantic_caption_source_rows"] = mapped_count
    quality["semantic_caption_rows"] = len(simplified_rows)
    quality["semantic_caption_splits"] = max(0, len(simplified_rows) - mapped_count)
    quality["delivery_mode"] = "best_effort_nonblocking"
    quality["delivery_warnings"] = list(dict.fromkeys(warnings))
    simplified_rows = subtitles.paginate_segments(
        simplified_rows, style=caption_style, word_segments=semantic_evidence)
    simplified_rows, overlap_repairs = subtitles.normalize_caption_overlaps(
        simplified_rows, return_repairs=True)
    quality["caption_overlap_repairs"] = overlap_repairs
    # Translate the same acoustically timed pages so both native PR tracks
    # retain identical boundaries.
    traditional_rows = [
        (start, end, to_traditional(text, converter=converter))
        for start, end, text in simplified_rows
    ]
    _, simplified_count = subtitles.write_srt(
        {}, simplified_path, segments=simplified_rows, paginated=True)
    _, traditional_count = subtitles.write_srt(
        {}, traditional_path, segments=traditional_rows, paginated=True)
    if simplified_count != traditional_count:
        raise RuntimeError("简体和繁体字幕条数不一致")

    return {
        "count": simplified_count,
        "converter": "opencc-s2t" if converter is not None else "fallback",
        "quality": quality,
        "warning": "; ".join(value for value in (
            "; ".join(dict.fromkeys(warnings)),
            str(quality.get("warning") or ""),
            ("" if converter is not None and simplified_converter is not None
             else "OpenCC 转换字典不可用，简繁字幕仅使用基础字表转换，"
                  "请检查字幕文字或重新安装依赖")) if value),
        "tracks": [
            {"name": "简体中文", "language": "zh-Hans",
             "path": simplified_path, "count": simplified_count},
            {"name": "繁体中文", "language": "zh-Hant",
             "path": traditional_path, "count": traditional_count},
        ],
    }
