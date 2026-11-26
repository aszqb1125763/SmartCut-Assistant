# -*- coding: utf-8 -*-
"""把 E:\本地大模型 的中立转写接到剪辑软件的索引上。

只做"取词"这一步胶水，选择和候选全部用 E 盘原函数：
  · 干净正文（把握 < 0.45 才列候选）→ `neutral_text.render`（原样）
  · 带候选的台词（把握 < 0.70，最多 16 个候选）→ `homophones.format_with_alternatives`（原样）

规矩不变（子枫 2026-11-24）：
    「语音转文字输出的是绝对结果，这违反了绝对中立的设定。
      语音文本如果碰到了同音字，不能自己做选择。」
"""
from __future__ import annotations

import homophones
import neutral_text

THRESHOLD = neutral_text.THRESHOLD


def _segment_words(row):
    """segment_evidence 里的一句 → 词表（neutral_text / homophones 要的格式）。"""
    out = []
    for word in row.get("words") or []:
        prob = word.get("probability")
        text = str(word.get("text") or "")
        if prob is None or not text.strip():
            continue
        out.append({"word": text, "prob": float(prob),
                    "start": float(word.get("start") or 0.0),
                    "end": float(word.get("end") or 0.0)})
    return out


def words_of(data):
    """索引里的逐词证据 → neutral_text 要的词表（只留带概率的词）。"""
    import index as indexer

    rows = []
    for row in indexer.get_segment_evidence(data):
        rows.extend(_segment_words(row))
    return rows


def annotated_segments(data, low_prob=None):
    """逐句交出「带同音候选」的写法：山[焰|艳|燕]火。

    用的是 E 盘 homophones.format_with_alternatives 原函数（0.70 / 16 候选）。
    """
    import index as indexer

    threshold = homophones.LOW_PROB if low_prob is None else float(low_prob)
    table = homophones.load_index()
    rows = []
    for row in indexer.get_segment_evidence(data):
        words = _segment_words(row)
        if not words:
            continue
        text, concerns = homophones.format_with_alternatives(
            words, table, low_prob=threshold)
        rows.append({"start": float(row.get("start") or 0.0),
                     "end": float(row.get("end") or 0.0),
                     "text": text, "concerns": concerns})
    return rows


def build(data):
    """返回索引里的 neutral 块；没有逐词证据返回 None。"""
    words = words_of(data)
    if not words:
        return None
    text, concerns = neutral_text.render(words)
    return {"text": text, "concerns": concerns, "words": len(words),
            "threshold": THRESHOLD,
            "low_prob": homophones.LOW_PROB,
            "segments": annotated_segments(data)}


def notes(data, limit=40):
    """给剧本/模型看的附注：哪一处没把握、候选有哪些（不是结论）。"""
    neutral = data.get("neutral") or {}
    concerns = neutral.get("concerns") or []
    if not concerns:
        return ""
    lines = ["转写不确定 %d 处（把握 < %.2f；下面是同音候选，不是结论）："
             % (len(concerns), neutral.get("threshold") or THRESHOLD)]
    for item in concerns[:limit]:
        lines.append("  %.2fs「%s」%.2f → 可能：%s"
                     % (item["t"], item["char"], item["prob"],
                        " / ".join(item["candidates"])))
    if len(concerns) > limit:
        lines.append("  …还有 %d 处" % (len(concerns) - limit))
    return "\n".join(lines)


def report_text(data):
    """正文（不带清单），给字幕/报告用。"""
    return (data.get("neutral") or {}).get("text") or ""
