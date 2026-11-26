# -*- coding: utf-8 -*-
"""文字小脑：只吃词级原始时间戳，输出"结构化文本"给模型看。

为什么要它（子枫 2026-11-24）：
    「本地大模型不允许接收任何原始数据，所有数据都要经过小脑计算输出。」
    所以模型不能再拿到裸台词；它能拿到的是这里整理过的：句子、标记、概念计数。

覆盖条款（第一版，只做能算的）：
    B02 词性/功能词标记（粗分：功能词 / 实词 / 语气词）
    B03 指代词（它/这/那/其/该）及"前 3 词内有没有实体"
    B06 强逻辑词（因为/所以/但是/然而/首先/其次/综上）
    B08 概念候选与出现次数（≥5 核心 / 2–4 次要 / 1 提及）
    B11 切口安全等级 S/A/B/C/D 的初版（句末 / 分句 / 实词间 / 虚词旁 / 禁切）
"""
from __future__ import annotations

import re

import coords

from .base import BrainPlugin

#: 功能词（虚词）：落在这些字旁边容易断在短语中间
FUNCTION_WORDS = set("的 了 是 在 和 跟 与 就 都 也 还 又 把 被 给 对 从 到 "
                     "这 那 一 个 之 而 或 但 因 所 以".split())
#: 黏着语素：绝对禁止切在它前面
STICKY = set("的地得着了过们第初")
#: 强逻辑词（B06：前后 10 字禁切）
LOGIC_WORDS = ("因为", "所以", "但是", "然而", "首先", "其次", "综上",
               "不过", "因此", "如果", "虽然")
#: 指代词（B03）
PRONOUNS = ("它", "这", "那", "其", "该", "这个", "那个", "这些", "那些")
#: 句末标点/语气（B11 的 S 级判断用）
SENTENCE_END = set("。！？…")
CLAUSE_END = set("，、；：,;:")


def _norm(text):
    return re.sub(r"\s+", "", str(text or ""))


class TextBrain(BrainPlugin):
    name = "text"
    accepts = ("words",)
    emits = ("sentences", "clauses", "concepts", "safe_edges")

    # -- 句子/分句 -----------------------------------------------------------
    def sentences(self, gap=None, max_chars=None):
        """按停顿把词拼成句子（没有标点，只能靠停顿与长度）。"""
        gap = coords.TEXT_SENTENCE_GAP_S if gap is None else float(gap)
        max_chars = coords.TEXT_MAX_CHARS if max_chars is None else int(max_chars)
        out = []
        current = []
        for word in self.src["words"].words:
            text = _norm(word.get("word"))
            if not text:
                continue
            if current:
                silence = float(word["start"]) - float(current[-1]["end"])
                length = sum(len(_norm(w.get("word"))) for w in current)
                if silence >= gap or length >= max_chars:
                    out.append(current)
                    current = []
            current.append(word)
        if current:
            out.append(current)
        return [{"start": round(float(rows[0]["start"]), 3),
                 "end": round(float(rows[-1]["end"]), 3),
                 "text": "".join(_norm(row.get("word")) for row in rows),
                 "words": len(rows)} for rows in out]

    def clauses(self, gap=0.28):
        """更细的停顿切分（分句级，B11 的 A 级用）。"""
        return self.sentences(gap=gap)

    # -- 标记 ---------------------------------------------------------------
    def mark(self, text):
        """给一句文本打标记：功能词/黏着/逻辑词/指代。"""
        text = _norm(text)
        return {
            "text": text,
            "starts_with_function": bool(text[:1] and text[:1] in FUNCTION_WORDS),
            "ends_with_function": bool(text[-1:] and text[-1:] in FUNCTION_WORDS),
            "sticky": [ch for ch in text[:1] + text[-1:]
                       if ch in STICKY],
            "logic_hits": [w for w in LOGIC_WORDS if w in text],
            "pronouns": [w for w in PRONOUNS if w in text],
            "length": len(text),
        }

    def concept_counts(self, limit=80):
        """概念候选：2–6 字的名词性片段，按出现次数分级（B08 初版）。"""
        counts = {}
        for sentence in self.sentences():
            text = sentence["text"]
            for size in (4, 3, 2):
                for index in range(0, max(0, len(text) - size + 1)):
                    piece = text[index:index + size]
                    if any(ch in FUNCTION_WORDS or ch in STICKY for ch in piece):
                        continue
                    counts[piece] = counts.get(piece, 0) + 1
        rows = [{"concept": key, "count": value,
                 "grade": ("core" if value >= coords.TEXT_CONCEPT_CORE
                           else ("minor" if value >= 2 else "mention"))}
                for key, value in counts.items()
                if value >= coords.TEXT_CONCEPT_MIN]
        rows.sort(key=lambda row: -row["count"])
        return rows[:limit]

    # -- 切口安全等级（B11 初版）-------------------------------------------
    def edge_grade(self, at, tolerance=0.12):
        """某个时间点的切口等级：S/A/B/C/D。"""
        at = float(at)
        words = self.src["words"].words
        inside = None
        for word in words:
            start = float(word["start"])
            end = float(word["end"])
            if start + 1e-6 < at < end - 1e-6:
                inside = word
                break
        if inside is not None:
            return {"grade": "D", "reason": "cut_inside_word",
                    "word": _norm(inside.get("word"))[:8]}
        before = None
        after = None
        for word in words:
            if float(word["end"]) <= at + 1e-6:
                before = word
            elif after is None:
                after = word
        if before is None or after is None:
            return {"grade": "D", "reason": "edge_of_material"}
        prev_text = _norm(before.get("word"))
        next_text = _norm(after.get("word"))
        gap = float(after["start"]) - float(before["end"])
        # S：句末（停顿够长，像一句话说完）
        if gap >= coords.TEXT_SENTENCE_GAP_S:
            return {"grade": "S", "reason": "sentence_end",
                    "gap": round(gap, 3)}
        # A：分句（有停顿）
        if gap >= coords.TEXT_CLAUSE_GAP_S:
            return {"grade": "A", "reason": "clause_end", "gap": round(gap, 3)}
        # D：紧挨黏着词/逻辑词后
        if prev_text[-1:] in STICKY or next_text[:1] in STICKY:
            return {"grade": "D", "reason": "sticky_morpheme",
                    "prev": prev_text[-4:], "next": next_text[:4]}
        for logic in LOGIC_WORDS:
            if prev_text.endswith(logic[:1]) and next_text.startswith(logic[1:2]):
                return {"grade": "D", "reason": "logic_word_split",
                        "logic": logic}
        # C：虚词旁
        if prev_text[-1:] in FUNCTION_WORDS or next_text[:1] in FUNCTION_WORDS:
            return {"grade": "C", "reason": "function_word_adjacent",
                    "prev": prev_text[-4:], "next": next_text[:4]}
        # B：实词之间的词边界
        return {"grade": "B", "reason": "content_word_boundary",
                "prev": prev_text[-4:], "next": next_text[:4]}

    def safe_edges(self, start, end, wanted=("S", "A"), limit=40):
        """区间内所有 S/A 级切点（给模型的"能切哪儿"）。"""
        rows = []
        seen = set()
        for word in self.src["words"].words:
            at = round(float(word["end"]), 3)
            if not (float(start) < at < float(end)) or at in seen:
                continue
            seen.add(at)
            grade = self.edge_grade(at)
            if grade["grade"] in wanted:
                rows.append({"at": at, **grade})
        return rows[:limit]

    def span(self, start, end, limit=12):
        """文字轴给模型的两件事：**是什么** + **在时间轴上持续多久**。"""
        start, end = float(start), float(end)
        rows = []
        for sentence in self.sentences():
            # 句子只归"它开头所在"的那一段，否则相邻两段会重复同一句
            if not (start <= float(sentence["start"]) < end):
                continue
            s0 = float(sentence["start"])
            s1 = min(float(sentence["end"]), end)
            if s1 - s0 < 0.05:
                continue
            duration = round(s1 - s0, 3)
            chars = len(sentence["text"])
            rows.append({
                "start": round(s0, 3), "end": round(s1, 3),
                "duration_s": duration, "text": sentence["text"],
                "clipped": s0 > float(sentence["start"]) + 1e-6
                           or s1 < float(sentence["end"]) - 1e-6,
                "chars": chars,
                "cps": round(chars / duration, 2) if duration > 0 else 0.0,
            })
            if len(rows) >= limit:
                break
        return {"probe": "text_span", "brain": self.name,
                "start": round(start, 3), "end": round(end, 3),
                "sentences": rows,
                "total_duration_s": round(sum(r["duration_s"] for r in rows), 3),
                "clause": ["B01", "B11"], **self.meta()}

    def context(self, start, end, limit=12):
        """给模型的文字包：**全部是文字小脑的输出**（句子 + 标记 + 概念）。

        模型不许直接吃台词；它拿到的是这里整理过的：
        句子边界、功能词/逻辑词/指代标记、概念分级。
        """
        start, end = float(start), float(end)
        rows = []
        for sentence in self.sentences():
            if sentence["end"] <= start or sentence["start"] >= end:
                continue
            rows.append({
                "start": sentence["start"], "end": sentence["end"],
                "text": sentence["text"],
                "mark": self.mark(sentence["text"]),
            })
            if len(rows) >= limit:
                break
        return {"probe": "text_context", "brain": self.name,
                "start": round(start, 3), "end": round(end, 3),
                "sentences": rows,
                "concepts": self.concept_counts(limit=8),
                **self.meta()}

    def probe(self, start, end):
        return {"probe": "text", "brain": self.name,
                "sentences": [s for s in self.sentences()
                              if s["start"] < float(end)
                              and s["end"] > float(start)],
                "edges": self.safe_edges(start, end),
                **self.meta()}
