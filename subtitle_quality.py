# -*- coding: utf-8 -*-
"""Conservative, cached subtitle correction shared by PR and standalone ASR.

The language model may fix a homophone, but it may never summarize a sentence
or remove a spoken token.  Old indexes keep Whisper's original text and word
timings in ``segment_evidence``; unsafe historic corrections are restored from
that evidence before a new contextual pass is considered.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile


ALGORITHM_VERSION = "subtitle-context-preserve-v7"
CACHE_NAME = "subtitle_quality.json"
REPORT_NAME = "字幕校对报告.json"


def _visible_length(value):
    return len(re.sub(r"[\s\W_]+", "", str(value or ""), flags=re.UNICODE))


def _finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def alignment_issues(index_data):
    """Find unresolved ASR timing defects, including in legacy indexes."""
    segments = index_data.get("segments") or []
    evidence = index_data.get("segment_evidence") or []
    profile = index_data.get("asr_profile") or {}
    try:
        full_track_coverage_checked = int(profile.get("version") or 0) >= 4
    except (TypeError, ValueError):
        full_track_coverage_checked = False
    issues = []
    if len(segments) != len(evidence):
        issues.append({
            "indexes": [], "start": None, "end": None, "text": "",
            "reasons": ["segment_evidence_count_mismatch"],
            "segment_count": len(segments), "evidence_count": len(evidence),
        })
    for blocker in index_data.get("subtitle_alignment_blockers") or []:
        if not isinstance(blocker, dict):
            continue
        reason = str(blocker.get("reason") or "alignment_export_blocker")
        issues.append({
            "indexes": [],
            "start": _finite_number(blocker.get("start")),
            "end": _finite_number(blocker.get("end")),
            "text": "", "reasons": [reason],
        })
    parent_groups = {}
    for index, row in enumerate(evidence):
        if not isinstance(row, dict):
            issues.append({
                "indexes": [index], "start": None, "end": None, "text": "",
                "reasons": ["invalid_segment_evidence"],
            })
            continue
        start = _finite_number(row.get("start"))
        end = _finite_number(row.get("end"))
        text = str(row.get("text_original") or row.get("text") or "").strip()
        reasons = []
        if index < len(segments):
            segment = segments[index]
            try:
                seg_start = _finite_number(segment[0])
                seg_end = _finite_number(segment[1])
            except (IndexError, TypeError):
                seg_start = seg_end = None
            expected_id = "s%06d" % (index + 1)
            if str(row.get("segment_id") or "") != expected_id:
                reasons.append("segment_id_mismatch")
            if (start is None or end is None or seg_start is None or seg_end is None
                    or abs(start - seg_start) > 0.02
                    or abs(end - seg_end) > 0.02):
                reasons.append("segment_time_mismatch")
        flags = row.get("asr_flags") or []
        if isinstance(flags, str):
            flags = [flags]
        flag_set = {str(flag) for flag in flags}
        if "alignment_repair_failed" in flag_set:
            reasons.append("alignment_repair_failed")
        if "timestamp_alignment_suspected" in flag_set:
            reasons.append("timestamp_alignment_suspected")
        visible = _visible_length(text)
        if (start is not None and end is not None and end >= start and visible
                and end - start > max(6.0, visible * 2.5)):
            reasons.append("short_text_long_duration")

        words = []
        zero_word_count = 0
        word_probabilities = []
        for word in row.get("words") or []:
            if not isinstance(word, dict):
                continue
            word_start = _finite_number(word.get("start"))
            word_end = _finite_number(word.get("end"))
            if word_start is None or word_end is None or word_end < word_start:
                continue
            word_text = str(word.get("text", word.get("word", "")) or "")
            probability = _finite_number(word.get("probability"))
            if probability is not None:
                word_probabilities.append(probability)
            words.append((word_start, word_end, word_text))
            word_visible = _visible_length(word_text)
            if (word_visible and
                    word_end - word_start > max(2.5, word_visible * 1.5)):
                reasons.append("word_duration_outlier")
            if word_end - word_start < 0.015:
                zero_word_count += 1
        words.sort()
        if any(right[0] - left[1] > 8.0
               for left, right in zip(words, words[1:])):
            reasons.append("word_gap_outlier")
        duration = ((end - start) if start is not None and end is not None else 0.0)
        if (visible >= 4 and 0.0 < duration < 0.40
                and visible / max(duration, 1e-6) > 14.0
                and zero_word_count >= max(2, len(words) // 2)
                and word_probabilities and min(word_probabilities) < 0.05):
            reasons.extend(("impossible_speech_rate", "collapsed_word_timestamps"))
        compact_text = re.sub(r"\s+", "", text).lower()
        if (word_probabilities and min(word_probabilities) < 0.01
                and any(token in compact_text for token in (
                    "请不吝", "感谢观看", "字幕由", "中文字幕", "订阅转发",
                    "支持明镜", "语言为普通话", "amara.org"))):
            reasons.append("template_hallucination_suspected")

        parent_start = _finite_number(row.get("original_asr_segment_start"))
        parent_end = _finite_number(row.get("original_asr_segment_end"))
        if (not full_track_coverage_checked
                and parent_start is not None and parent_end is not None
                and parent_end > parent_start):
            tracks = row.get("source_audio_tracks")
            if not isinstance(tracks, (list, tuple)):
                tracks = [row.get("source_audio_track")]
            track_key = tuple(sorted(str(value) for value in tracks
                                     if value is not None))
            key = (track_key, round(parent_start, 6), round(parent_end, 6))
            group = parent_groups.setdefault(key, {
                "indexes": [], "words": [], "texts": [],
                "start": parent_start, "end": parent_end,
            })
            group["indexes"].append(index)
            group["words"].extend(words)
            if text:
                group["texts"].append(text)
        if reasons:
            issues.append({
                "indexes": [index], "start": start, "end": end, "text": text,
                "reasons": list(dict.fromkeys(reasons)),
            })

    # Consecutive cues from the same source track must not substantially
    # overlap.  A small rounding tolerance is harmless, but the real failure
    # that prompted this gate was a 0.59-second overlap after one stretched
    # single-character row.  Simultaneous speech on different source tracks is
    # legitimate and is deliberately checked independently.
    previous_by_track = {}
    for index, row in enumerate(evidence):
        if not isinstance(row, dict):
            continue
        start = _finite_number(row.get("start"))
        end = _finite_number(row.get("end"))
        if start is None or end is None or end <= start:
            continue
        tracks = row.get("source_audio_tracks")
        if not isinstance(tracks, (list, tuple)) or not tracks:
            tracks = [row.get("source_audio_track")]
        track_keys = tuple(sorted({str(value) for value in tracks
                                   if value is not None})) or ("unknown",)
        for track_key in track_keys:
            previous = previous_by_track.get(track_key)
            if previous is not None:
                overlap = float(previous["end"]) - start
                if overlap > 0.08:
                    issues.append({
                        "indexes": [int(previous["index"]), index],
                        "start": start,
                        "end": min(float(previous["end"]), end),
                        "text": "%s / %s" % (
                            str(previous.get("text") or "")[:80],
                            str(row.get("text_original") or row.get("text") or "")[:80]),
                        "reasons": ["segment_overlap"],
                        "overlap_seconds": overlap,
                        "source_audio_track": track_key,
                    })
            if previous is None or end > float(previous["end"]):
                previous_by_track[track_key] = {
                    "index": index, "start": start, "end": end,
                    "text": row.get("text_original") or row.get("text") or "",
                }

    for group in parent_groups.values():
        parent_duration = group["end"] - group["start"]
        words = sorted(group["words"])
        reasons = []
        if any(right[0] - left[1] > 8.0
               for left, right in zip(words, words[1:])):
            reasons.append("parent_word_gap_outlier")
        if parent_duration > 20.0:
            if not words:
                reasons.append("parent_words_missing")
            else:
                envelope = max(0.0, words[-1][1] - words[0][0])
                if envelope / parent_duration < 0.35:
                    reasons.append("parent_word_coverage_outlier")
        if reasons:
            issues.append({
                "indexes": sorted(set(group["indexes"])),
                "start": group["start"], "end": group["end"],
                "text": " ".join(group["texts"])[:160], "reasons": reasons,
            })

    unique = {}
    for issue in issues:
        key = (tuple(issue["indexes"]), issue.get("start"), issue.get("end"),
               tuple(issue["reasons"]))
        unique[key] = issue
    return sorted(unique.values(), key=lambda item: (
        item.get("start") if item.get("start") is not None else float("inf"),
        item.get("end") if item.get("end") is not None else float("inf"),
        item["indexes"]))


def _hash(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".subtitle-", suffix=".tmp",
                                dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def _backend_identity():
    import llm_config
    import llm_gateway

    if llm_gateway.api_enabled():
        return llm_gateway.backend_signature()
    path = llm_config.pick("subtitle")
    try:
        stat = os.stat(path)
        detail = [os.path.abspath(path), stat.st_size, stat.st_mtime_ns]
    except OSError:
        detail = [os.path.abspath(path) if path else "", None, None]
    return "local:" + _hash(detail)


def _baseline_segments(index_data, table, kb_all):
    """Return source-timed text and restore only demonstrably lossy old edits."""
    import proofread
    import subtitles

    rows = subtitles.get_segments(index_data)
    evidence = index_data.get("segment_evidence") or []
    evidence_texts = [str(row.get("text_original") or "")
                      for row in evidence if isinstance(row, dict)]
    current_texts = [str(row[2] or "") for row in rows]
    pairs = proofread.trusted_known_pairs(
        evidence_texts + current_texts, kb_all, table)
    restored = []
    result = []
    for i, (start, end, current) in enumerate(rows):
        text = str(current or "")
        raw = ""
        if i < len(evidence) and isinstance(evidence[i], dict):
            raw = str(evidence[i].get("text_original") or "").strip()
        # A legitimate glossary/homophone correction passes the preservation
        # gate.  Deletion, truncation and changed speaker/number do not.
        if raw and raw != text:
            problem = proofread.correction_violation(raw, text, pairs)
            if problem:
                restored.append({"index": i, "from": text, "to": raw,
                                 "reason": problem})
                text = raw
        result.append((float(start), float(end), text))
    return result, restored


def _resource_fingerprint(table, kb_all, texts):
    import proofread
    return _hash(proofread.relevant_known_pairs(texts, kb_all, table))


def _candidate_indexes(index_data, baseline, restored, table, kb_all):
    """Review every meaningful row; ASR confidence cannot prove spelling.

    High acoustic confidence only says the recognizer heard a stable sound. It
    does not distinguish same-sounding characters, so skipping all high-score
    rows permanently hides ordinary homophone errors. The expensive work stays
    bounded by batching these rows rather than invoking the model per sentence.
    """
    del index_data, restored, table, kb_all
    return [i for i, (_start, _end, text) in enumerate(baseline)
            if str(text or "").strip()]


def _correct_candidates(items, candidate_indexes, table, kb_all, server, log):
    """Correct selected rows in bounded batches with four-sentence context."""
    import proofread

    selected = [items[position] for position in candidate_indexes
                if 0 <= position < len(items)]
    if not selected:
        return {}, {}
    # Twenty-four short subtitle rows fit the 8k context and the 8B model's
    # bounded 1024-token response reliably. A batch of 48 caused every first
    # response on the real 1016-line recording to truncate, then retry as two
    # half-batches; 24 is both faster and quieter while keeping context.
    changes, lowconf = proofread.correct_texts(
        selected, table, server, batch=24, kb_all=kb_all, context=4,
        allow_context_homophones=True, progress=log)
    log("  字幕语义校对完成：%d 句，接受 %d 处，拒绝 %d 处"
        % (len(selected), len(changes), len(lowconf)))
    return changes, lowconf


def correct_source_segments(index_data, index_dir, log=print, use_model=True,
                            force=False):
    """Correct source-order utterances before timeline remapping.

    Returns ``(segments, report)`` and never mutates index.json.  API failures
    are propagated so an API-mode job cannot silently fall back to local AI.
    Local model unavailability retains the restored/original text with an
    explicit warning.
    """
    import llm_gateway
    import proofread

    table, _wrong_set, kb_all = proofread.load_resources()
    baseline, restored = _baseline_segments(index_data, table, kb_all)
    timing_issues = alignment_issues(index_data)
    identity = {
        "algorithm": ALGORITHM_VERSION,
        "source": [[round(a, 6), round(b, 6), text] for a, b, text in baseline],
        "timestamp_alignment_issues": timing_issues,
        "resources": _resource_fingerprint(
            table, kb_all, [text for _a, _b, text in baseline]),
        "backend": _backend_identity() if use_model else "disabled",
    }
    signature = _hash(identity)
    cache_path = os.path.join(index_dir, CACHE_NAME) if index_dir else ""
    if not force and cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cached = json.load(handle)
            rows = cached.get("segments") or []
            if cached.get("signature") == signature and len(rows) == len(baseline):
                result = [(float(row[0]), float(row[1]), str(row[2])) for row in rows]
                report = dict(cached.get("report") or {})
                report["cache_hit"] = True
                log("  字幕上下文校对：复用缓存（%d 句）" % len(result))
                return result, report
        except (OSError, ValueError, TypeError, IndexError):
            pass

    explicit_pairs = proofread.trusted_known_pairs(
        [text for _a, _b, text in baseline], kb_all, table)
    deterministic = {}
    for index, (_start, _end, text) in enumerate(baseline):
        fixed = proofread._canonical_terms(str(text), explicit_pairs)
        if fixed != text:
            deterministic[index] = fixed

    changes, lowconf, warning = dict(deterministic), {}, ""
    candidates = _candidate_indexes(index_data, baseline, restored, table, kb_all)
    if use_model and baseline:
        items = [(i, text) for i, (_a, _b, text) in enumerate(baseline)]
        try:
            with proofread.Server(task="subtitle", ctx=8192) as server:
                log("  字幕上下文复核：%d/%d 句，按 24 句批量并读取前后各 4 句语境…"
                    % (len(candidates), len(items)))
                model_changes, lowconf = _correct_candidates(
                    items, candidates, table, kb_all, server, log)
                changes.update(model_changes)
        except Exception as exc:  # noqa: BLE001
            llm_gateway.propagate_api_error(exc)
            warning = "本地字幕校对不可用，已保留原始识别文字：%s" % exc
            log("  " + warning)

    result = [(a, b, str(changes.get(i, text)))
              for i, (a, b, text) in enumerate(baseline)]
    report = {
        "algorithm": ALGORITHM_VERSION,
        "cache_hit": False,
        "sentences": len(result),
        "review_candidates": len(candidates),
        "historic_loss_restored": len(restored),
        "accepted_corrections": len(changes),
        "rejected_suggestions": len(lowconf),
        "timestamp_alignment_issue_count": len(timing_issues),
        "timestamp_alignment_issues": timing_issues,
        "warning": warning,
        "restored": restored,
        "accepted": [{"index": i, "from": baseline[i][2], "to": text}
                     for i, text in sorted(changes.items())],
        "rejected": [{"index": i, "from": baseline[i][2], "suggested": text}
                     for i, text in sorted(lowconf.items())],
    }
    if cache_path:
        payload = {"signature": signature, "identity": identity,
                   "segments": result, "report": report}
        _atomic_json(cache_path, payload)
        _atomic_json(os.path.join(index_dir, REPORT_NAME), report)
    return result, report


def correct_rows(rows, log=print, use_model=True):
    """Apply the same conservative contextual pass to temporary ASR rows."""
    import llm_gateway
    import proofread

    if not rows:
        return rows
    table, _wrong_set, kb_all = proofread.load_resources()
    items = [(i, str(row.get("text") or "")) for i, row in enumerate(rows)]
    changes = {}
    try:
        if use_model:
            with proofread.Server(task="subtitle", ctx=8192) as server:
                changes, lowconf = proofread.correct_texts(
                    items, table, server, batch=16, kb_all=kb_all, context=4,
                    progress=log)
                log("  字幕校对：接受 %d 处，拒绝 %d 处有损/低置信建议"
                    % (len(changes), len(lowconf)))
    except Exception as exc:  # noqa: BLE001
        llm_gateway.propagate_api_error(exc)
        log("  本地字幕校对不可用，保留原文：%s" % exc)
    for i, row in enumerate(rows):
        if i in changes:
            row["text"] = changes[i]
    return rows
