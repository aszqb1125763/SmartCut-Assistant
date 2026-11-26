# -*- coding: utf-8 -*-
"""语义相关性打分：每句和"本章目标"有多像（纯统计，不烧显存、可复现）。

为什么不用本地模型直接判：实测 Qwen3-8B 判"留/剪"和阈值打平，而且几乎不说剪。
改成算相似度就稳定了——相似度是连续分数，可以排序、可以调权重、可以复现。

做法：字符 2-gram TF-IDF（中文不用分词也够用），
    句子向量 vs 本章目标向量 → 余弦，就是"相关度"。
    另外算相邻句相似度（话题连续性），太低说明这里换话题了。
"""
from __future__ import annotations

import math
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


def _grams(text: str, n: int = 2):
    t = "".join(ch for ch in (text or "")
                if "\u4e00" <= ch <= "\u9fa5" or ch.isalnum())
    if len(t) < n:
        return [t] if t else []
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def _vec(grams, idf):
    c = Counter(grams)
    v = {g: (1.0 + math.log(n)) * idf.get(g, 1.0) for g, n in c.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    for g in v:
        v[g] /= norm
    return v


def _cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(g, 0.0) for g, x in a.items())


def relevance(data, goal_text=None):
    """返回 (每句相关度, 每句"话题连续性")，范围大致 0~1。"""
    segs = data.get("segments") or []
    if not segs:
        return [], []
    texts = [str(s[2] or "").strip() for s in segs]
    all_grams = [_grams(t) for t in texts]
    df = Counter()
    for gs in all_grams:
        for g in set(gs):
            df[g] += 1
    n = max(1, len(texts))
    idf = {g: math.log((1.0 + n) / (1.0 + c)) + 1.0 for g, c in df.items()}
    vecs = [_vec(gs, idf) for gs in all_grams]

    # 每句所属章节的目标文本
    script = data.get("script") or {}
    sections = script.get("sections") or []
    mainline = str(script.get("mainline") or "")
    goal_vecs = []
    for sec in sections:
        gt = "%s %s %s" % (sec.get("title", ""), sec.get("advice", ""), mainline)
        goal_vecs.append(((float(sec.get("start", 0)), float(sec.get("end", 0))),
                          _vec(_grams(gt), idf)))
    whole = _vec(_grams(mainline), idf) if mainline else None

    rel = []
    for s, v in zip(segs, vecs):
        t = float(s[0])
        gv = None
        for (a, b), gvv in goal_vecs:
            if a <= t <= b:
                gv = gvv
                break
        if gv is None:
            gv = whole
        rel.append(round(_cos(v, gv), 4) if gv else 0.5)

    # 话题连续性：和上一句的相似度
    cont = [1.0]
    for i in range(1, len(vecs)):
        cont.append(round(_cos(vecs[i - 1], vecs[i]), 4))
    return rel, cont


def attach(index_path: str):
    """写进索引（seg_relevance / seg_continuity），供剪辑用。"""
    import json
    p = index_path
    if os.path.isdir(p):
        p = os.path.join(p, "index.json")
    data = json.load(open(p, encoding="utf-8"))
    rel, cont = relevance(data)
    if not rel:
        print("没有台词")
        return 1
    data["seg_relevance"] = rel
    data["seg_continuity"] = cont
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    print("已写入 %d 句：相关度 %.2f~%.2f（中位 %.2f），连续性中位 %.2f"
          % (len(rel), min(rel), max(rel), sorted(rel)[len(rel) // 2],
             sorted(cont)[len(cont) // 2]))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python relevance.py <索引目录>")
        raise SystemExit(2)
    raise SystemExit(attach(sys.argv[1]))
