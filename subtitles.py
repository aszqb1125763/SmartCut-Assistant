# -*- coding: utf-8 -*-
"""字幕：导出 SRT，或按固定样式烧录进画面。

为什么单独一个模块：
  字幕有两个完全不同的用途——「给平台用」（SRT 上传）和「烧进画面」（硬字幕）。
  前者只要文本+时间，后者还涉及字体、描边、位置、每行字数。

用法：
    python subtitles.py <索引目录> --srt 字幕.srt
    python subtitles.py <索引目录> --burn 输出.mp4 --font "微软雅黑" --size 54
    python subtitles.py --fonts                # 列出系统可用字体
"""


try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass

import argparse
import difflib
import json
import math
import os
import re
import subprocess
import sys

_SOURCE_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SOURCE_BASE)

import index as indexer  # noqa: E402
from memory_config import app_home  # noqa: E402

BASE = app_home()
CONF_DIR = os.path.join(BASE, "conf")
STYLE_PATH = os.path.join(CONF_DIR, "subtitle_style.json")

# 固定样式：一行最多多少字、位置、描边，都集中在这里，改文件即可
DEFAULT_STYLE = {
    "font": "思源黑体 CN Medium",
    "size": 54,
    "color": "#FFFFFF",          # 主色
    "outline": 3,                # 描边粗细
    "outline_color": "#101010",
    "shadow": 2,
    "shadow_color": "#000000",
    "align": 2,                  # 1左 2中 3右
    "margin_v": 46,              # 距底部像素
    "max_chars": 15,             # 一条约 15 字；语义分段后不再机械砍词
    "max_lines": 1,
    # 字幕先按完整句意分成连续 cue，再处理视觉换行。字数只负责兜底，
    # 不能把两个意思硬塞进同一条，也不能为了版式删除口播字。
    "semantic_target_chars": 15,
    "semantic_max_chars": 15,
    "outline_alpha": 0.0,
}


def load_style():
    st = dict(DEFAULT_STYLE)
    try:
        if os.path.isfile(STYLE_PATH):
            with open(STYLE_PATH, encoding="utf-8") as handle:
                st.update(json.load(handle))
    except Exception:  # noqa: BLE001
        pass
    return st


def save_style(st):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(STYLE_PATH, "w", encoding="utf-8") as handle:
        json.dump(st, handle, ensure_ascii=False, indent=2)


_ELLIPSIS_RE = re.compile(r"(?:…+|⋯+|\.{3,}|。{3,})")
_CAPTION_PUNCTUATION = set("。！？!?；;，,、：:")
_CAPTION_SENTENCE_END = set("。！？!?；;")
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SEMANTIC_FILLERS = {"嗯", "呃", "啊", "哦", "唉", "诶", "哎", "额"}
_SEMANTIC_STARTERS = (
    "但是", "不过", "可是", "然后", "所以", "而且", "另外", "接着", "结果",
    "其实", "反正", "如果", "因为", "这样的话", "那么", "现在", "后续",
    "我们", "你们", "他们", "它们", "这个", "那个", "这里", "那里", "这边",
    "那边", "上面", "下面", "左边", "右边", "物品", "材料", "行", "好",
)
_SEMANTIC_PRONOUN_STARTERS = (
    "我们", "你们", "他们", "它们", "这个", "那个", "这里", "那里", "这边",
    "那边", "上面", "下面", "左边", "右边", "物品", "材料",
)
_SEMANTIC_PREFIX_CONTINUATIONS = set("把给对向跟和与在从到用是的让叫请")


def clean_caption_text(text):
    """Remove ASR/model ellipses without dropping ordinary caption text."""
    cleaned_lines = []
    for source_line in str(text or "").splitlines() or [str(text or "")]:
        line = source_line
        while True:
            match = _ELLIPSIS_RE.search(line)
            if not match:
                break
            left = line[:match.start()].rstrip()
            right = line[match.end():].lstrip()
            previous = left[-1] if left else ""
            following = right[0] if right else ""
            joiner = ""
            if left and right:
                if previous in _CAPTION_PUNCTUATION and following in _CAPTION_PUNCTUATION:
                    if following in _CAPTION_SENTENCE_END:
                        left = left[:-1].rstrip()
                    else:
                        right = right[1:].lstrip()
                elif following in _CAPTION_PUNCTUATION \
                        or previous in _CAPTION_PUNCTUATION:
                    joiner = ""
                elif _CJK_CHAR_RE.fullmatch(previous) \
                        and _CJK_CHAR_RE.fullmatch(following):
                    joiner = "，"
                else:
                    joiner = " "
            line = left + joiner + right
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _load_index(path):
    if os.path.isdir(path):
        path = os.path.join(path, "index.json")
    return json.load(open(path, encoding="utf-8")), path


def _crop_text_to_window(text, evidence, window_start, window_end,
                         segment_start, segment_end):
    """Crop a split utterance to words actually audible inside one kept span."""
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    current = compact(text)
    if not current:
        return ""
    words = evidence.get("words") or [] if isinstance(evidence, dict) else []
    aligned, offsets = "", []
    for word in words:
        try:
            value = compact(word.get("word", word.get("text", "")))
            start, end = float(word["start"]), float(word["end"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if not value or end < start:
            continue
        begin = len(aligned)
        aligned += value
        offsets.append((begin, len(aligned), start, end))
    original = compact(evidence.get("text_original") or evidence.get("text")) \
        if isinstance(evidence, dict) else ""
    if offsets and aligned == original and len(current) == len(original):
        chosen = [(lo, hi) for lo, hi, start, end in offsets
                  if window_start <= (start + end) / 2.0 < window_end]
        if not chosen:
            return ""
        return current[min(lo for lo, _hi in chosen):max(hi for _lo, hi in chosen)]
    # Old/third-party indexes may not have complete word alignment. Proportional
    # character cropping is safer than duplicating a whole sentence into every
    # tiny transition fragment.
    duration = max(1e-6, float(segment_end) - float(segment_start))
    left = max(0.0, min(1.0, (window_start - segment_start) / duration))
    right = max(left, min(1.0, (window_end - segment_start) / duration))
    lo = min(len(current), int(math.floor(left * len(current))))
    hi = min(len(current), int(math.ceil(right * len(current))))
    return current[lo:hi]


def timing_alignment_issue(start, end, text, evidence=None):
    """Return a reason when one source subtitle has impossible ASR timing.

    This is the export-side safety net for indexes created before the ASR
    alignment repair existed.  Whisper confidence describes token likelihood,
    not timestamp accuracy, so even a 0.99-confidence Chinese character must
    not be allowed to occupy tens of seconds on a Premiere caption track.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    flags = set(str(value) for value in (evidence.get("asr_flags") or []))
    for blocked in ("timestamp_alignment_suspected", "alignment_repair_failed"):
        if blocked in flags:
            return blocked

    try:
        duration = float(end) - float(start)
    except (TypeError, ValueError):
        return "invalid_time"
    visible = len(re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE))
    if visible and duration > max(6.0, visible * 2.5):
        return "short_text_long_duration"

    words = []
    zero_words = 0
    probabilities = []
    for word in evidence.get("words") or []:
        if not isinstance(word, dict):
            continue
        try:
            word_start = float(word["start"])
            word_end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        value = str(word.get("word", word.get("text", "")) or "")
        try:
            probability = float(word.get("probability"))
        except (TypeError, ValueError):
            probability = None
        if probability is not None and math.isfinite(probability):
            probabilities.append(probability)
        word_visible = len(re.sub(
            r"[\s\W_]+", "", value, flags=re.UNICODE))
        if (word_visible and
                word_end - word_start > max(2.5, word_visible * 1.5)):
            return "word_duration_outlier"
        if word_end - word_start < 0.015:
            zero_words += 1
        words.append((word_start, word_end))
    if (visible >= 4 and 0.0 < duration < 0.40
            and visible / max(duration, 1e-6) > 14.0
            and zero_words >= max(2, len(words) // 2)
            and probabilities and min(probabilities) < 0.05):
        return "collapsed_word_timestamps"
    compact_text = re.sub(r"\s+", "", str(text or "")).lower()
    if (probabilities and min(probabilities) < 0.01
            and any(token in compact_text for token in (
                "请不吝", "感谢观看", "字幕由", "中文字幕", "订阅转发",
                "支持明镜", "语言为普通话", "amara.org"))):
        return "template_hallucination_suspected"
    words.sort()
    if any(right[0] - left[1] > 8.0
           for left, right in zip(words, words[1:])):
        return "word_gap_outlier"
    return ""


def sanitize_source_segments(segments, word_segments=None):
    """Drop only proven timing-corrupt rows while keeping evidence aligned."""
    kept, kept_evidence, blocked = [], [], []
    has_evidence = isinstance(word_segments, (list, tuple))
    if has_evidence and len(segments or []) != len(word_segments):
        return [], [], [{
            "index": -1, "start": 0.0, "end": 0.0, "text": "",
            "reason": "segment_evidence_count_mismatch",
            "segment_count": len(segments or []),
            "evidence_count": len(word_segments),
        }]
    for index, segment in enumerate(segments or []):
        try:
            start, end, text = segment[:3]
        except (TypeError, ValueError):
            continue
        evidence = (word_segments[index]
                    if has_evidence and index < len(word_segments) else {})
        issue = timing_alignment_issue(start, end, text, evidence)
        if issue:
            blocked.append({
                "index": index,
                "start": float(start),
                "end": float(end),
                "text": str(text or ""),
                "reason": issue,
            })
            continue
        kept.append((float(start), float(end), str(text or "")))
        if has_evidence:
            kept_evidence.append(evidence)
    return kept, (kept_evidence if has_evidence else None), blocked


def assert_safe_index_alignment(data):
    """Refuse standalone subtitle export from a structurally unsafe index."""
    if not isinstance(data, dict):
        return
    import subtitle_quality
    issues = subtitle_quality.alignment_issues(data)
    if not issues:
        return
    first = issues[0]
    reasons = ", ".join(first.get("reasons") or ["unknown"])
    raise RuntimeError(
        "字幕时间轴质量检查未通过（%d 处，首处：%s）。"
        "请重新分析素材，已阻止导出可能错位或漏字的字幕。"
        % (len(issues), reasons))


def _corrected_char_words(text, evidence):
    """Project a length-changing glossary correction onto original word time."""
    if not isinstance(evidence, dict):
        return []
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    current = compact(text)
    original = compact(evidence.get("text_original") or evidence.get("text"))
    if not current or not original or current == original:
        return []
    source_chars = []
    aligned = ""
    for word in evidence.get("words") or []:
        try:
            value = compact(word.get("word", word.get("text", "")))
            start, end = float(word["start"]), float(word["end"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return []
        if not value or end < start:
            continue
        aligned += value
        for index, char in enumerate(value):
            source_chars.append({
                "text": char,
                "start": start + (end - start) * index / len(value),
                "end": start + (end - start) * (index + 1) / len(value),
                "probability": word.get("probability"),
            })
    if aligned != original or len(source_chars) != len(original):
        return []

    projected = []
    matcher = difflib.SequenceMatcher(None, original, current, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if j2 <= j1:
            continue
        if i2 > i1:
            start, end = source_chars[i1]["start"], source_chars[i2 - 1]["end"]
            probabilities = [row.get("probability") for row in source_chars[i1:i2]
                             if row.get("probability") is not None]
            probability = min(probabilities) if probabilities else None
        else:
            left = source_chars[i1 - 1]["end"] if i1 else source_chars[0]["start"]
            right = source_chars[i1]["start"] if i1 < len(source_chars) else left
            start = end = (left + right) / 2.0
            probability = None
        width = max(0.0, end - start)
        for offset, char in enumerate(current[j1:j2]):
            row = {
                "text": char,
                "start": start + width * offset / (j2 - j1),
                "end": start + width * (offset + 1) / (j2 - j1),
            }
            if probability is not None:
                row["probability"] = probability
            projected.append(row)
    return projected if "".join(row["text"] for row in projected) == current else []


def _remapped_word_evidence(evidence, text, source_start, source_end,
                            segment_start, segment_end, output_start,
                            output_end):
    """Map reliable source words into one compact-timeline subtitle row."""
    if not isinstance(evidence, dict):
        return {}
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    current = compact(text)
    original = compact(evidence.get("text_original") or evidence.get("text"))
    aligned, tokens = "", []
    for word in evidence.get("words") or []:
        try:
            value = compact(word.get("word", word.get("text", "")))
            start, end = float(word["start"]), float(word["end"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return {}
        if not value or end < start:
            continue
        aligned += value
        tokens.append((word, value, start, end))
    # Same-length homophone proofreading preserves acoustic character offsets.
    if not tokens or aligned != original:
        return {}
    if len(current) != len(original):
        projected = _corrected_char_words(text, evidence)
        if not projected:
            return {}
        tokens = [(word, word["text"], float(word["start"]), float(word["end"]))
                  for word in projected]
    selected = [token for token in tokens
                if source_start <= (token[2] + token[3]) / 2.0 < source_end]
    if len(current) != sum(len(token[1]) for token in selected):
        return {}
    mapped_words = []
    for word, value, start, end in selected:
        row = dict(word)
        row["text"] = str(word.get("text", word.get("word", value)) or value)
        row.pop("word", None)
        row["start"] = max(output_start, min(
            output_end, output_start + (max(start, source_start) - source_start)))
        row["end"] = max(row["start"], min(
            output_end, output_start + (min(end, source_end) - source_start)))
        mapped_words.append(row)
    if not mapped_words:
        return {}
    result = dict(evidence)
    result.update({"start": output_start, "end": output_end,
                   "text": str(text or ""),
                   "text_original": str(text or ""),
                   "words": mapped_words})
    return result


def remap(segs, timeline, word_segments=None, return_evidence=False):
    """把「原片时间轴」的字幕换成「成片时间轴」。

    剪辑是把原片一段段挑出来重排的，索引里的逐句时间轴是原片时间，
    直接烧到成片上会整片错位——这就是字幕功能一直封着的原因。

    timeline 是 autocut.render 记下来的片段清单：
        [(成片起点, 原片起点, 时长), ...]  按成片顺序
    一句台词如果跨了两个相邻片段（剪辑点落在句子中间），会被拆成两条，
    各自落在对应片段里，时间都按该片段的偏移平移。
    """
    if not segs or not timeline:
        return ([], []) if return_evidence else []
    segs, word_segments, _blocked = sanitize_source_segments(
        segs, word_segments)
    if not segs:
        return ([], []) if return_evidence else []
    if isinstance(timeline, dict):
        mapped_rows = []
        seen = set()
        for piece in timeline.get("pieces") or []:
            # 字幕跟“听到的内容”走。audio_bridge 的画面只来自后段，
            # 但左段尾音仍在交叉混合，不能把对应字幕映射丢掉。
            lanes = piece.get("audio") or piece.get("video") or []
            for row in lanes:
                key = (round(float(row.get("output_start", 0.0)), 6),
                       round(float(row.get("source_start", 0.0)), 6),
                       round(float(row.get("source_end", 0.0)) -
                             float(row.get("source_start", 0.0)), 6),
                       str(row.get("role") or ""))
                if key in seen or key[2] <= 0:
                    continue
                seen.add(key)
                mapped_rows.append(key[:3])
        timeline = mapped_rows
    out = []
    for o0, s0, dur in timeline:
        o1, s1 = float(o0), float(s0)
        e1 = s1 + float(dur)
        for segment_index, (a, b, text) in enumerate(segs):
            a, b = float(a), float(b)
            if b <= s1 or a >= e1:
                continue
            aa, bb = max(a, s1), min(b, e1)
            # 只丢掉"擦到一两个采样"的边（<40ms）；再短的单独台词也要留，
            # 否则"嗯""对"这种一声应答会整个消失（自测里暴露过）。
            if bb - aa < 0.04:
                continue
            oa = o1 + (aa - s1)
            # Keep the acoustic end exactly.  Artificially extending every
            # short response to 0.4 s created overlapping SRT cues; Premiere
            # may then move one native caption, making it look detached from
            # the waveform.  Readability must come from sentence/page grouping,
            # not from changing when the voice actually ends.
            ob = min(o1 + float(dur), o1 + (bb - s1))
            evidence = (word_segments[segment_index]
                        if word_segments is not None
                        and segment_index < len(word_segments) else {})
            value = text
            if aa > a + 0.02 or bb < b - 0.02:
                # Even a legacy page without word evidence must be cropped to
                # the kept interval.  Repeating the whole page on both sides of
                # a cut makes the caption appear to lead/lag the actual voice.
                # ``_crop_text_to_window`` falls back to conservative
                # proportional cropping when no complete word alignment exists.
                value = _crop_text_to_window(text, evidence, aa, bb, a, b)
            if ob > oa and str(value or "").strip():
                mapped_row = (round(oa, 3), round(ob, 3), value)
                mapped_evidence = _remapped_word_evidence(
                    evidence, value,
                    aa, bb, a, b, mapped_row[0], mapped_row[1])
                out.append((mapped_row, mapped_evidence))
    unique = {}
    for row, evidence in out:
        unique.setdefault(row, evidence)
    pairs = sorted(unique.items(), key=lambda item: item[0])
    rows = [row for row, _evidence in pairs]
    if return_evidence:
        return rows, [evidence for _row, evidence in pairs]
    return rows


def get_segments(data):
    """优先用逐句时间轴；老索引没有就退回卡片文本。"""
    segs = data.get("segments") or []
    corrected = data.get("segments_proofread") or []
    # v1 refinement did not record its source audio track and could silently
    # apply game-audio edges after the user changed tracks.  Only accept v2
    # rows whose source, selected track, ASR profile and ordered segment IDs all
    # still match, and whose movement cannot clip a word or add overlap.
    try:
        import audiofeat_bounds
        ref = audiofeat_bounds.validated_refined_segments(data)
    except Exception:  # noqa: BLE001
        ref = []
    if segs:
        out = []
        # 有能量谷定出来的精确边界就用它：字幕起止跟着真实开口/收口走，
        # 观众看到的"字还没念完字幕就没了"或"画面没声字幕先出"会少很多
        for i, s in enumerate(segs):
            if isinstance(s, (list, tuple)) and len(s) >= 3:
                if i < len(ref):
                    try:
                        a, b = float(ref[i][0]), float(ref[i][1])
                    except (TypeError, ValueError, IndexError):
                        a, b = float(s[0]), float(s[1])
                else:
                    a, b = float(s[0]), float(s[1])
                text = str(s[2])
                # A card title/summary is never a substitute for verbatim ASR.
                # Explicit proofreading stores a parallel, source-timed list.
                if len(corrected) == len(segs):
                    fixed = corrected[i]
                    try:
                        if (float(fixed[0]) == float(s[0])
                                and float(fixed[1]) == float(s[1])
                                and str(fixed[2]).strip()):
                            text = str(fixed[2])
                    except (TypeError, ValueError, IndexError):
                        pass
                out.append((a, b, text))
        if out:
            return out
    out = []
    for c in data.get("cards") or []:
        t = (c.get("text_fixed") or c.get("text") or "").strip()
        if t:
            out.append((float(c["start"]), float(c["end"]), t))
    return out


def _wrap(text, max_chars, max_lines):
    """Wrap without discarding text; ``paginate_segments`` enforces page height."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    max_chars = max(1, int(max_chars))
    lines = []
    while len(text) > max_chars:
        cut = max_chars
        # Prefer a phrase/word boundary near the line end. Keep punctuation.
        lower = max(1, max_chars // 2)
        for marks in ("。！？；.!?;", "，、：,: "):
            candidates = [i + 1 for i, ch in enumerate(text[:max_chars])
                          if ch in marks and i + 1 >= lower]
            if candidates:
                cut = candidates[-1]
                break
        lines.append(text[:cut].strip())
        text = text[cut:].lstrip()
    if text:
        lines.append(text)
    return lines


def _word_boundary_times(text, words, start, end):
    """Character offsets -> acoustic time, only for a complete word alignment."""
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    tokens = []
    for word in words or []:
        try:
            value = compact(word.get("word", word.get("text", "")))
            a, b = float(word["start"]), float(word["end"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        if value:
            if not math.isfinite(a + b) or b < a:
                return None
            tokens.append((value, max(start, min(end, a)), max(start, min(end, b))))
    # Context proofreading is only allowed to perform one-for-one homophone
    # substitutions unless an explicit glossary authorises a length change.
    # A same-length corrected sentence therefore still shares the original
    # acoustic character offsets even though the glyphs no longer match.
    # Requiring literal equality discarded those reliable word timestamps and
    # made long PR captions fall back to character-proportional timing.
    if not tokens or len("".join(row[0] for row in tokens)) != len(compact(text)):
        return None
    times = {0: start}
    offset = 0
    for i, (value, a, b) in enumerate(tokens):
        for k in range(1, len(value) + 1):
            times[offset + k] = a + (b - a) * k / len(value)
        offset += len(value)
        if i + 1 < len(tokens):
            times[offset] = (b + max(b, tokens[i + 1][1])) / 2
    times[offset] = end
    return times


def _compact_caption(value):
    return re.sub(r"\s+", "", str(value or ""))


def _semantic_phrases(text):
    """Return phrase-sized text pieces and whether each closes a sentence.

    Whisper's Chinese output commonly uses spaces for audible phrase pauses even
    when it omits punctuation.  Those pauses are useful semantic candidates, but
    they are not all mandatory cuts: the packer below rejoins short fragments so
    a filler such as ``呃`` never becomes a caption by itself.
    """
    value = re.sub(r"[\t\r\n ]+", " ", clean_caption_text(text)).strip()
    if not value:
        return []
    phrases = []
    buffer = []

    def flush(kind):
        piece = "".join(buffer).strip()
        buffer[:] = []
        if piece:
            phrases.append((piece, kind))

    for char in value:
        if char.isspace():
            flush("pause")
            continue
        buffer.append(char)
        if char in _CAPTION_SENTENCE_END:
            flush("sentence")
        elif char in (_CAPTION_PUNCTUATION - _CAPTION_SENTENCE_END):
            flush("phrase")
    flush("end")
    return phrases


def _split_oversized_semantic_phrase(text, target_chars, max_chars):
    """Split only an oversized unpunctuated phrase, preferring idea transitions."""
    value = str(text or "").strip()
    if not value:
        return []
    target_chars = max(4, int(target_chars or 15))
    max_chars = max(target_chars, int(max_chars or 15))
    pieces = []
    rest = value
    while len(_compact_caption(rest)) > max_chars:
        compact = _compact_caption(rest)
        minimum = min(max(4, target_chars // 2), max_chars)
        candidates = []
        for marker in _SEMANTIC_STARTERS:
            start = 0
            while True:
                position = compact.find(marker, start)
                if position < 0:
                    break
                if (position > 0 and marker in _SEMANTIC_PRONOUN_STARTERS
                        and compact[position - 1] in _SEMANTIC_PREFIX_CONTINUATIONS):
                    start = position + 1
                    continue
                if minimum <= position <= max_chars:
                    candidates.append(position)
                start = position + 1
        cut = (min(candidates, key=lambda item: (abs(item - target_chars), -item))
               if candidates else min(target_chars, max_chars))
        # Avoid leaving a one- or two-character orphan when the whole phrase is
        # only slightly over the hard limit.
        tail = len(compact) - cut
        if 0 < tail < 4 and len(compact) - 4 >= minimum:
            cut = len(compact) - 4
        if cut <= 0 or cut >= len(rest):
            cut = min(max_chars, len(rest) - 1)
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return [piece for piece in pieces if piece]


def _join_caption_phrases(parts):
    """Join ASR pause phrases without losing necessary Latin-word spacing."""
    out = ""
    for part in parts:
        value = str(part or "").strip()
        if not value:
            continue
        if (out and out[-1].isascii() and out[-1].isalnum()
                and value[0].isascii() and value[0].isalnum()):
            out += " "
        elif out and value in _SEMANTIC_FILLERS:
            out += " "
        elif out and _compact_caption(out) in _SEMANTIC_FILLERS:
            out += " "
        out += value
    return out


def semantic_caption_chunks(text, target_chars=15, max_chars=15, min_chars=6):
    """Split one ASR sentence into captions that each express one main idea.

    Sentence punctuation is mandatory.  Audible phrase pauses and subject/
    transition words are preferred boundaries.  Short fragments are joined to
    their neighbour, while a character limit is used only for an otherwise
    unbroken oversized phrase.  The compact output is guaranteed to contain the
    exact same characters as the cleaned input.
    """
    target_chars = max(4, int(target_chars or 15))
    max_chars = max(target_chars, int(max_chars or 15))
    min_chars = max(2, min(int(min_chars or 6), target_chars))
    phrases = []
    for phrase, kind in _semantic_phrases(text):
        split = _split_oversized_semantic_phrase(
            phrase, target_chars=target_chars, max_chars=max_chars)
        for index, piece in enumerate(split):
            piece_kind = kind if index == len(split) - 1 else "hard_limit"
            phrases.append((piece, piece_kind))
    if not phrases:
        return []

    chunks = []
    current = []
    current_length = 0
    for phrase, kind in phrases:
        visible = len(_compact_caption(phrase))
        if not visible:
            continue
        starter = _compact_caption(phrase).startswith(_SEMANTIC_STARTERS)
        filler = _compact_caption(phrase) in _SEMANTIC_FILLERS
        should_break_before = bool(
            current and not filler and current_length >= min_chars and (
                current_length + visible > max_chars
                or starter
                or current_length >= target_chars))
        if should_break_before:
            chunks.append(_join_caption_phrases(current))
            current, current_length = [], 0
        current.append(phrase)
        current_length += visible
        if kind in ("sentence", "hard_limit"):
            chunks.append(_join_caption_phrases(current))
            current, current_length = [], 0
    if current:
        chunks.append(_join_caption_phrases(current))
    chunks = [chunk for chunk in chunks if chunk]
    source = _compact_caption(clean_caption_text(text))
    if "".join(_compact_caption(chunk) for chunk in chunks) != source:
        return [clean_caption_text(text)] if source else []
    return chunks


def _slice_semantic_evidence(evidence, full_text, start_offset, end_offset,
                             piece_text, piece_start, piece_end):
    """Crop word evidence to one semantic cue, including cuts inside a token."""
    if not isinstance(evidence, dict):
        return {}
    tokens = []
    cursor = 0
    for word in evidence.get("words") or []:
        try:
            value = _compact_caption(word.get("word", word.get("text", "")))
            start, end = float(word["start"]), float(word["end"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return {}
        if not value or end < start:
            continue
        tokens.append((word, value, cursor, cursor + len(value), start, end))
        cursor += len(value)
    if cursor != len(_compact_caption(full_text)):
        return {}

    sliced = []
    for word, value, token_start, token_end, start, end in tokens:
        left = max(start_offset, token_start)
        right = min(end_offset, token_end)
        if right <= left:
            continue
        local_left = left - token_start
        local_right = right - token_start
        row = dict(word)
        row.pop("word", None)
        row["text"] = value[local_left:local_right]
        width = max(0.0, end - start)
        row["start"] = start + width * local_left / len(value)
        row["end"] = start + width * local_right / len(value)
        sliced.append(row)
    if "".join(row["text"] for row in sliced) != _compact_caption(piece_text):
        return {}
    result = dict(evidence)
    result.update({
        "start": float(piece_start), "end": float(piece_end),
        "text": str(piece_text or ""),
        "text_original": str(piece_text or ""),
        "words": sliced,
    })
    return result


def _merge_nonlexical_cues(rows, evidence_rows, max_gap=0.45):
    """Attach standalone fillers to an adjacent caption without deleting them."""
    merged_rows = []
    merged_evidence = []
    pending = None
    pending_evidence = None
    source_rows = list(rows or [])
    source_evidence = list(evidence_rows or [])
    for index, row in enumerate(source_rows):
        start, end, text = float(row[0]), float(row[1]), str(row[2] or "")
        evidence = source_evidence[index] if index < len(source_evidence) else {}
        compact = _compact_caption(text)
        if compact in _SEMANTIC_FILLERS:
            if merged_rows and start - merged_rows[-1][1] <= max_gap:
                old_start, _old_end, old_text = merged_rows[-1]
                merged_rows[-1] = (
                    old_start, end, _join_caption_phrases([old_text, text]))
                # The merged cue is already short enough not to need pagination;
                # discard partial evidence rather than claiming a false alignment.
                merged_evidence[-1] = {}
                continue
            next_start = (float(source_rows[index + 1][0])
                          if index + 1 < len(source_rows) else None)
            if next_start is not None and next_start - end <= max_gap:
                pending, pending_evidence = (start, end, text), evidence
                continue
        if pending is not None:
            start = pending[0]
            text = _join_caption_phrases([pending[2], text])
            evidence = {}
            pending = pending_evidence = None
        merged_rows.append((start, end, text))
        merged_evidence.append(evidence)
    if pending is not None:
        merged_rows.append(pending)
        merged_evidence.append(pending_evidence or {})
    return merged_rows, merged_evidence


def semantic_segment_cues(segments, word_segments=None, target_chars=15,
                          max_chars=15, min_chars=6, return_evidence=False):
    """Create source-timed, one-meaning caption cues before visual pagination."""
    rows = []
    evidence_rows = []
    for index, segment in enumerate(segments or []):
        try:
            start, end, text = float(segment[0]), float(segment[1]), str(segment[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not math.isfinite(start + end) or end <= start:
            continue
        cleaned = clean_caption_text(text)
        chunks = semantic_caption_chunks(
            cleaned, target_chars=target_chars,
            max_chars=max_chars, min_chars=min_chars)
        if not chunks:
            continue
        evidence = (word_segments[index]
                    if word_segments is not None and index < len(word_segments)
                    else {})
        words = evidence.get("words", []) if isinstance(evidence, dict) else []
        acoustic = _word_boundary_times(cleaned, words, start, end)
        total = len(_compact_caption(cleaned)) or 1
        records = []
        offset = 0
        for chunk in chunks:
            width = len(_compact_caption(chunk))
            records.append({"text": chunk, "start_offset": offset,
                            "end_offset": offset + width})
            offset += width

        # Collapse only boundaries that the word timestamps prove are too close
        # to represent safely.  Text is merged, never discarded.
        stable = []
        cursor_time = start
        for record in records:
            boundary_offset = record["end_offset"]
            boundary_time = (end if boundary_offset >= total else
                             acoustic.get(boundary_offset)
                             if acoustic is not None else None)
            if boundary_time is None:
                boundary_time = start + (end - start) * boundary_offset / total
            boundary_time = max(cursor_time, min(end, float(boundary_time)))
            if stable and boundary_time - cursor_time < 0.04:
                previous = stable[-1]
                previous["text"] = _join_caption_phrases(
                    [previous["text"], record["text"]])
                previous["end_offset"] = record["end_offset"]
                previous["end_time"] = boundary_time
                cursor_time = boundary_time
                continue
            record["start_time"] = cursor_time
            record["end_time"] = boundary_time
            stable.append(record)
            cursor_time = boundary_time
        if stable:
            stable[-1]["end_time"] = end

        for record in stable:
            cue_start = float(record["start_time"])
            cue_end = float(record["end_time"])
            if cue_end <= cue_start:
                continue
            rows.append((cue_start, cue_end, record["text"]))
            evidence_rows.append(_slice_semantic_evidence(
                evidence, cleaned, record["start_offset"], record["end_offset"],
                record["text"], cue_start, cue_end))
    rows, evidence_rows = _merge_nonlexical_cues(rows, evidence_rows)
    if return_evidence:
        return rows, evidence_rows
    return rows


def _balanced_pages(text, max_chars, max_lines):
    """Split a sentence into readable pages without a one-character orphan."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return []
    capacity = max(1, int(max_chars)) * max(1, int(max_lines))
    page_count = max(1, int(math.ceil(len(value) / capacity)))
    pages = []
    rest = value
    for page_index in range(page_count):
        remaining_pages = page_count - page_index
        if remaining_pages == 1:
            piece, rest = rest, ""
        else:
            ideal = int(math.ceil(len(rest) / remaining_pages))
            minimum = max(1, len(rest) - (remaining_pages - 1) * capacity)
            maximum = min(capacity, len(rest) - (remaining_pages - 1))
            cut = max(minimum, min(maximum, ideal))
            low = max(minimum, cut - max(2, int(capacity * 0.30)))
            high = min(maximum, cut + max(2, int(capacity * 0.20)))
            boundary = []
            for index, char in enumerate(rest[:high], 1):
                if index >= low and char in "。！？；.!?;，、：,: ":
                    boundary.append(index)
            if boundary:
                cut = min(boundary, key=lambda value: (abs(value - ideal), -value))
            piece, rest = rest[:cut].rstrip(), rest[cut:].lstrip()
        lines = _wrap(piece, max_chars, max_lines)
        # Rounding plus whitespace should never exceed the style's page height.
        if len(lines) > max_lines:
            overflow = "".join(lines[max_lines:])
            lines = lines[:max_lines]
            rest = overflow + ((" " + rest) if rest else "")
            page_count += 1
        pages.append("\n".join(lines))
    if rest:
        pages.extend("\n".join(_wrap(rest, max_chars, max_lines)[i:i + max_lines])
                     for i in range(0, len(_wrap(rest, max_chars, max_lines)), max_lines))
    return [page for page in pages if page]


def paginate_segments(segments, style=None, max_chars=None, word_segments=None):
    """Split long captions into consecutive cues, retaining every spoken character.

    Word timestamps take precedence when their text matches the whole sentence.
    Older/corrected sentences without exact alignment use character proportions;
    neither path invents missing speech or shortens the original time interval.
    """
    st = style or load_style()
    mc = max(1, int(max_chars or st.get("max_chars", 18)))
    ml = max(1, int(st.get("max_lines", 2)))
    out = []
    for i, (a, b, text) in enumerate(segments or []):
        a, b = float(a), float(b)
        if not math.isfinite(a + b) or b <= a:
            continue
        # Clean before pagination so a long ASCII ellipsis cannot be split
        # across two pages as ``..`` + ``.`` and evade the final write gate.
        text = clean_caption_text(text)
        if not text:
            continue
        pages = _balanced_pages(text, mc, ml)
        if not pages:
            continue
        # Count text characters, ignoring only layout whitespace.
        weights = [len(re.sub(r"\s+", "", page)) for page in pages]
        total = sum(weights) or 1
        words = []
        if word_segments is not None and i < len(word_segments):
            item = word_segments[i]
            words = item.get("words", []) if isinstance(item, dict) else item
        acoustic = _word_boundary_times(text, words, a, b)
        cursor, count = a, 0
        for j, page in enumerate(pages):
            count += weights[j]
            next_time = (b if j == len(pages) - 1 else
                         acoustic[count] if acoustic is not None and count in acoustic
                         else a + (b - a) * count / total)
            # Bad alignments must not create reversed/zero-time cues.
            if next_time <= cursor or next_time > b:
                acoustic = None
                next_time = a + (b - a) * count / total
            if next_time <= cursor:
                # Extremely short speech cannot fit millisecond-sized pages;
                # preserve its text in the previous cue instead of losing words.
                if out and out[-1][1] == cursor:
                    old_a, _old_b, old_text = out[-1]
                    out[-1] = (old_a, b, old_text + "\n" + page)
                else:
                    out.append((a, b, page))
                continue
            out.append((cursor, next_time, page))
            cursor = next_time
    return out


def normalize_caption_overlaps(segments, min_duration=0.04,
                               return_repairs=False):
    """Make one SRT track strictly non-overlapping without dropping text.

    Premiere may move or truncate overlapping caption cues on import.  Small
    ASR boundary overlaps are therefore resolved by clipping the earlier cue
    to the later cue's acoustic start.  If that would leave an unreadably tiny
    earlier cue, its text is folded into the later cue instead of being lost.
    """
    ordered = sorted(
        ((float(a), float(b), str(text or "")) for a, b, text in segments or []
         if math.isfinite(float(a) + float(b)) and float(b) > float(a)),
        key=lambda row: (row[0], row[1]))
    out = []
    repairs = 0
    minimum = max(0.001, float(min_duration))
    for start, end, text in ordered:
        if not text.strip():
            continue
        while out and start < out[-1][1] - 1e-9:
            previous_start, previous_end, previous_text = out[-1]
            repairs += 1
            if start - previous_start >= minimum:
                out[-1] = (previous_start, start, previous_text)
                break
            # The earlier cue would collapse.  Preserve every spoken character
            # by merging it into the later cue and extending that cue left.
            out.pop()
            start = min(start, previous_start)
            if previous_text.strip() and previous_text.strip() not in text:
                text = previous_text.rstrip() + "\n" + text.lstrip()
        if end <= start:
            continue
        out.append((start, end, text))
    return (out, repairs) if return_repairs else out


def _tc_srt(t):
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms = 999
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _tc_ass(t, fps=60):
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        cs = 99
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)


def write_srt(data, out_path, style=None, max_chars=None, segments=None,
              word_segments=None, paginated=False):
    """导出标准 SRT。平台上传、后期软件导入都用它。"""
    if segments is None:
        assert_safe_index_alignment(data)
    st = style or load_style()
    mc = max_chars or st.get("max_chars", 18)
    segs = get_segments(data) if segments is None else segments
    if not paginated:
        if word_segments is None and segments is None:
            word_segments = data.get("segment_evidence")
        segs = paginate_segments(segs, st, mc, word_segments)
    segs = normalize_caption_overlaps(segs)
    with open(out_path, "w", encoding="utf-8") as fh:
        n = 0
        for a, b, t in segs:
            lines = clean_caption_text(t).splitlines()
            if not lines:
                continue
            n += 1
            fh.write("%d\n" % n)
            fh.write("%s --> %s\n" % (_tc_srt(a), _tc_srt(b)))
            fh.write("\n".join(lines) + "\n\n")
    return out_path, n


def _hex_to_ass(color, alpha=0.0):
    """#RRGGBB → ASS 的 &HAABBGGRR（AA 是不透明度，00=不透明）。"""
    c = (color or "#FFFFFF").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = c[0:2], c[2:4], c[4:6]
    aa = int(round(max(0.0, min(1.0, 1.0 - float(alpha))) * 255))
    return "&H%02X%s%s%s" % (aa, b.upper(), g.upper(), r.upper())


def write_ass(data, out_path, style=None, width=1920, height=1080, fps=60,
              segments=None, word_segments=None):
    """生成 ASS。烧录字幕用它，因为能精确控制字体、描边、位置。"""
    if segments is None:
        assert_safe_index_alignment(data)
    st = style or load_style()
    segs = get_segments(data) if segments is None else segments
    mc = st.get("max_chars", 18)
    if word_segments is None and segments is None:
        word_segments = data.get("segment_evidence")
    segs = paginate_segments(segs, st, mc, word_segments)

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: %d
PlayResY: %d
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,%s,%d,%s,&H00FFFFFF,%s,%s,0,0,0,0,100,100,0,0,1,%s,%s,%d,40,40,%d,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" % (width, height,
       st.get("font", "微软雅黑"), int(st.get("size", 54)),
       _hex_to_ass(st.get("color", "#FFFFFF")),
       _hex_to_ass(st.get("outline_color", "#101010")),
       _hex_to_ass(st.get("shadow_color", "#000000"), 1.0),
       st.get("outline", 3), st.get("shadow", 2),
       int(st.get("align", 2)), int(st.get("margin_v", 46)))

    n = 0
    with open(out_path, "w", encoding="utf-8-sig") as fh:
        fh.write(header)
        for a, b, t in segs:
            lines = clean_caption_text(t).splitlines()
            if not lines:
                continue
            txt = "\\N".join(lines)
            fh.write("Dialogue: 0,%s,%s,Default,,0,0,0,,%s\n"
                     % (_tc_ass(a, fps), _tc_ass(b, fps), txt))
            n += 1
    return out_path, n


def burn(video, ass_path, out_path, font_dir=None, on_log=print):
    """把 ASS 烧进画面。用 ass 滤镜（比 subtitles 滤镜对样式支持好）。"""
    ff = indexer.find_ffmpeg()
    esc = ass_path.replace("\\", "/").replace(":", "\\:")
    vf = "ass='%s'" % esc
    if font_dir:
        fd = font_dir.replace("\\", "/").replace(":", "\\:")
        vf += ":fontsdir='%s'" % fd

    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", video,
           "-vf", vf, "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
           out_path]
    on_log("烧录字幕：%s" % os.path.basename(out_path))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=indexer.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("字幕烧录失败：%s" % (r.stderr or "")[-800:])
    return out_path


def _read_name_table(path):
    """读出字体的 (中文名, 英文名)。读不出来返回 (None, None)。

    原来只读文件前 2MB，结果微软雅黑/黑体这种大字体，
    name 表在 19MB 处，直接读不到 → 认不出中文名。
    现在先解析表目录拿到 name 表偏移，再跳过去读那一段。
    """
    import struct
    try:
        fh = open(path, "rb")
    except OSError:
        return None, None
    with fh:
        try:
            head = fh.read(65536)
            if len(head) < 12:
                return None, None
            base = 0
            if head[:4] == b"ttcf":
                num = struct.unpack(">I", head[8:12])[0]
                if num < 1:
                    return None, None
                base = struct.unpack(">I", head[12:16])[0]
                if base + 12 > len(head):
                    fh.seek(0)
                    head = fh.read(base + 4096)
            num_tables = struct.unpack(">H", head[base + 4:base + 6])[0]
            name_off = name_len = None
            for i in range(min(num_tables, 200)):
                rec = base + 12 + i * 16
                if rec + 16 > len(head):
                    break
                if head[rec:rec + 4] == b"name":
                    name_off = struct.unpack(">I", head[rec + 8:rec + 12])[0]
                    name_len = struct.unpack(">I", head[rec + 12:rec + 16])[0]
                    break
            if name_off is None:
                return None, None
            fh.seek(name_off)
            data = fh.read(name_len or 262144)
            if len(data) < 6:
                return None, None
            count = struct.unpack(">H", data[2:4])[0]
            str_off = struct.unpack(">H", data[4:6])[0]
            zh = en = None
            for i in range(min(count, 400)):
                r = 6 + i * 12
                if r + 12 > len(data):
                    break
                pid, eid, lang, nid, ln, off = struct.unpack(">HHHHHH", data[r:r + 12])
                if nid != 1:
                    continue
                raw = data[str_off + off: str_off + off + ln]
                if not raw:
                    continue
                try:
                    txt = raw.decode("utf-16-be") if pid == 3 else raw.decode("latin-1")
                except (UnicodeDecodeError, LookupError):
                    continue
                txt = txt.strip("\\x00 ").strip()
                if not txt:
                    continue
                if lang in (0x0804, 0x0404, 0x0C04) and zh is None:
                    zh = txt
                elif lang == 0x0409 and en is None:
                    en = txt
            return zh, en
        except (struct.error, IndexError, OSError):
            return None, None


def _covers_cjk(path):
    """查 cmap 里有没有「中」这个字。有就是能显示中文。

    比看语言标签可靠：有些中文字体的 name 表只有英文名，
    有些西文字体却带了中文标签，只有字形表不会骗人。
    """
    import struct
    try:
        fh = open(path, "rb")
    except OSError:
        return False
    with fh:
        try:
            head = fh.read(65536)
            if len(head) < 12:
                return False
            base = 0
            if head[:4] == b"ttcf":
                base = struct.unpack(">I", head[12:16])[0]
                if base + 12 > len(head):
                    fh.seek(0)
                    head = fh.read(base + 8192)
            num = struct.unpack(">H", head[base + 4:base + 6])[0]
            cmap_off = None
            for i in range(min(num, 200)):
                rec = base + 12 + i * 16
                if rec + 16 > len(head):
                    break
                if head[rec:rec + 4] == b"cmap":
                    cmap_off = struct.unpack(">I", head[rec + 8:rec + 12])[0]
                    break
            if cmap_off is None:
                return False
            fh.seek(cmap_off)
            data = fh.read(262144)
            if len(data) < 4:
                return False
            ntab = struct.unpack(">H", data[2:4])[0]
            for i in range(min(ntab, 32)):
                rec = 4 + i * 8
                if rec + 8 > len(data):
                    break
                pid, eid, off = struct.unpack(">HHI", data[rec:rec + 8])
                if (pid, eid) not in ((3, 1), (3, 10), (0, 3), (0, 4), (3, 0)):
                    continue
                sub = data[off:off + 262144]
                if len(sub) < 4:
                    continue
                fmt = struct.unpack(">H", sub[0:2])[0]
                target = 0x4E2D                     # 「中」
                if fmt == 4:
                    segx2 = struct.unpack(">H", sub[6:8])[0]
                    ends = [struct.unpack(">H", sub[14 + 2 * k:16 + 2 * k])[0]
                            for k in range(segx2 // 2)]
                    starts_off = 16 + segx2
                    starts = [struct.unpack(">H", sub[starts_off + 2 * k:
                                                       starts_off + 2 * k + 2])[0]
                              for k in range(segx2 // 2)]
                    for a, b in zip(starts, ends):
                        if a <= target <= b:
                            return True
                elif fmt == 12:
                    ngroups = struct.unpack(">I", sub[12:16])[0]
                    for k in range(min(ngroups, 5000)):
                        g = 16 + k * 12
                        if g + 12 > len(sub):
                            break
                        a, b, _gid = struct.unpack(">III", sub[g:g + 12])
                        if a <= target <= b:
                            return True
            return False
        except (struct.error, IndexError, OSError):
            return False


def default_font():
    """在本机挑一个能显示中文的字体，优先无衬线/黑体。

    不同电脑装的字体不一样，写死名字换台机器就找不到，
    所以每次都在本机现挑。
    """
    fonts = list_fonts(detail=True)
    good = [f for f in fonts if f.get("cjk")]
    if not good:
        return ""
    # 打分：无衬线加分、常规字重加分、极端字重和衬线扣分
    sans_keys = ("sans", "hei", "yahei", "雅黑", "黑体", "misans",
                 "harmony", "alibaba", "deng", "等线")
    serif_keys = ("serif", "song", "宋", "明体", "楷", "仿宋")
    heavy_keys = ("black", "heavy", "bold", "thin", "light", "semi",
                  "extra", "demi", "ultra")
    def _score(f):
        blob = (f["label"] + " " + f["family"]).lower()
        s = 0
        if any(k in blob for k in sans_keys):
            s += 6
        if any(k in blob for k in serif_keys):
            s -= 5
        if any(k in blob for k in heavy_keys):
            s -= 4
        if not any(k in blob for k in heavy_keys):
            s += 3                      # 常规字重最通用
        if len(f["family"]) <= 18:
            s += 1                      # 名字短的多半是主家族名
        return -s
    return sorted(good, key=_score)[0]["family"]


def font_exists(family):
    return any(f["family"] == family for f in list_fonts(detail=True))


_FONT_CACHE = {}


def list_fonts(detail=False):
    """列出系统字体。

    detail=False 时返回名字列表（兼容老调用）；
    detail=True 时返回 [{"label": 中文名, "family": ASS 用的名字, "path": 文件}]，
    按“有中文名、能显示中文”优先排序。
    """
    import glob
    files = []
    for pat in (r"C:\Windows\Fonts\*.ttf", r"C:\Windows\Fonts\*.otf",
                r"C:\Windows\Fonts\*.ttc",
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft",
                             "Windows", "Fonts", "*")):
        files.extend(glob.glob(pat))
    out = []
    seen = set()
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in (".ttf", ".otf", ".ttc"):
            continue
        key = os.path.basename(f).lower()
        if key in seen:
            continue
        seen.add(key)
        if f not in _FONT_CACHE:
            zh, en = _read_name_table(f)
            _FONT_CACHE[f] = (zh, en, _covers_cjk(f))
        zh, en, has_cjk = _FONT_CACHE[f]
        stem = os.path.splitext(os.path.basename(f))[0]
        family = en or stem
        label = zh or en or stem
        if zh and en and zh != en:
            label = "%s（%s）" % (zh, en)
        out.append({"label": label, "family": family, "path": f,
                    "cjk": bool(has_cjk), "stem": stem})

    prefer = ("思源", "Source Han", "微软雅黑", "Microsoft YaHei", "Noto",
              "Alibaba", "HarmonyOS", "MiSans", "OPPO", "方正", "汉仪", "黑体", "宋体")
    out.sort(key=lambda x: (0 if x["cjk"] else 1,
                            0 if any(p.lower() in (x["label"] or "").lower()
                                     for p in prefer) else 1,
                            x["label"]))
    # 同一家族可能装了多个字重/多份文件，按 family 去重
    uniq = []
    seen_fam = set()
    for x in out:
        if x["family"] in seen_fam:
            continue
        seen_fam.add(x["family"])
        uniq.append(x)
    if detail:
        return uniq
    return [x["label"] for x in uniq]


def family_for(label):
    """从下拉里选中的显示名找回 ASS 要用的家族名。"""
    for x in list_fonts(detail=True):
        if x["label"] == label or x["family"] == label:
            return x["family"]
    return label


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("index", nargs="?")
    ap.add_argument("--srt", default=None)
    ap.add_argument("--burn", default=None)
    ap.add_argument("--font", default=None)
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--fonts", action="store_true")
    args = ap.parse_args(argv)

    if args.fonts:
        for n in list_fonts()[:60]:
            print(" ", n)
        return 0
    if not args.index:
        ap.print_help()
        return 1

    data, _p = _load_index(args.index)
    st = load_style()
    if args.font:
        st["font"] = args.font
    if args.size:
        st["size"] = args.size
    if args.max_chars:
        st["max_chars"] = args.max_chars

    segs = get_segments(data)
    print("可用句子：%d" % len(segs))
    if not segs:
        print("这份索引没有台词，先跑识别。")
        return 1

    base = os.path.dirname(os.path.abspath(_p))
    stem = data.get("name") or "subtitle"
    if args.srt:
        p, n = write_srt(data, args.srt, st)
        print("已导出 SRT：%s（%d 条）" % (p, n))
    if args.burn:
        ass = os.path.join(base, stem + ".ass")
        p, n = write_ass(data, ass, st)
        print("已生成 ASS：%s（%d 条）" % (p, n))
        burn(data.get("source"), ass, args.burn)
        print("已烧录：%s" % args.burn)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# 样式库：内置几套 + 玩家自己保存的（存在 conf/subtitle_styles.json）
# ---------------------------------------------------------------------------
BUILTIN_STYLES = {
    "经典白字黑边": {"color": "#FFFFFF", "outline": 3, "outline_color": "#101010",
                 "shadow": 2, "shadow_color": "#000000", "align": 2,
                 "margin_v": 46, "max_chars": 18, "max_lines": 2, "size": 54},
    "醒目黄字": {"color": "#FFD84A", "outline": 4, "outline_color": "#20180B",
               "shadow": 2, "shadow_color": "#000000", "align": 2,
               "margin_v": 56, "max_chars": 14, "max_lines": 2, "size": 62},
    "大字压屏": {"color": "#FFFFFF", "outline": 5, "outline_color": "#000000",
               "shadow": 0, "shadow_color": "#000000", "align": 2,
               "margin_v": 90, "max_chars": 11, "max_lines": 2, "size": 78},
    "小字低调": {"color": "#E6EDF5", "outline": 2, "outline_color": "#0A0A0A",
               "shadow": 1, "shadow_color": "#000000", "align": 2,
               "margin_v": 30, "max_chars": 22, "max_lines": 2, "size": 42},
    "居中无描边": {"color": "#FFFFFF", "outline": 0, "outline_color": "#000000",
                "shadow": 3, "shadow_color": "#000000", "align": 5,
                "margin_v": 0, "max_chars": 16, "max_lines": 2, "size": 56},
}

STYLES_PATH = os.path.join(CONF_DIR, "subtitle_styles.json")


def load_styles():
    """返回 {名字: 样式}，内置的在前，玩家保存的在后（同名覆盖）。"""
    out = {k: dict(v) for k, v in BUILTIN_STYLES.items()}
    try:
        if os.path.isfile(STYLES_PATH):
            user = json.load(open(STYLES_PATH, encoding="utf-8"))
            for k, v in (user or {}).items():
                if isinstance(v, dict):
                    out[k] = v
    except (OSError, ValueError):
        pass
    return out


def save_named(name, st):
    """把当前样式存成一个名字，下次直接选。"""
    name = (name or "").strip()
    if not name:
        return False
    try:
        user = {}
        if os.path.isfile(STYLES_PATH):
            user = json.load(open(STYLES_PATH, encoding="utf-8")) or {}
        user[name] = dict(st or {})
        json.dump(user, open(STYLES_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return True
    except (OSError, ValueError):
        return False


def delete_named(name):
    """只删玩家自己存的，内置的不动。"""
    if name in BUILTIN_STYLES:
        return False
    try:
        if not os.path.isfile(STYLES_PATH):
            return False
        user = json.load(open(STYLES_PATH, encoding="utf-8")) or {}
        if name in user:
            user.pop(name)
            json.dump(user, open(STYLES_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            return True
    except (OSError, ValueError):
        pass
    return False
