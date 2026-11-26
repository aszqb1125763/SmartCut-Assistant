# -*- coding: utf-8 -*-
"""专项纠错模型：字符二元组索引 + 词频先验，专门修"拼音都对不上"的错字。

为什么需要：拼音纠错（terms.py）对同音错字 100% 有效，
但对"听错音节"的错（过充水晶 → 过的一水晶 / 过充水京）完全无效（实测 0%）。

模型（轻量、CPU、训练几秒）：
  训练：把 13 万物名词逐个切成"字符二元组"，建倒排索引；统计词频先验
  推理：拿一个疑似错词，取它的二元组去召回候选，按
        score = 二元组覆盖率 × 0.7 + 词频对数归一 × 0.3
        打分，超过阈值且长度差 ≤1 的才采纳
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict

_SOURCE_BASE = os.path.dirname(os.path.abspath(__file__))
if _SOURCE_BASE not in sys.path:
    sys.path.insert(0, _SOURCE_BASE)

from memory_config import app_home  # noqa: E402

BASE = app_home()

DICT = os.path.join(BASE, "kb", "术语_全量.txt")

_state = {"ready": False, "index": {}, "freq": {}, "terms": [], "norm": 1.0}


def _bigrams(t):
    t = "".join(ch for ch in t if ch.isalnum() or "\u4e00" <= ch <= "\u9fa5")
    return [t[i:i + 2] for i in range(max(0, len(t) - 1))] or [t]


def train(path=None, min_len=3, max_len=8):
    """建索引（几秒），等价于"训练"这个统计模型。"""
    p = path or DICT
    terms, freq = [], Counter()
    for line in open(p, encoding="utf-8"):
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if min_len <= len(t) <= max_len:
            terms.append(t)
            freq[t] += 1
    # 词频：这里没有语料，用"是否在多个模组出现"当先验，没有信息就取 1
    index = defaultdict(set)
    for t in terms:
        for g in set(_bigrams(t)):
            index[g].add(t)
    _state.update({"ready": True, "index": index, "freq": freq,
                   "terms": terms, "norm": math.log(1 + len(terms))})
    return _state


def candidates(token, topk=20):
    st = _state if _state["ready"] else train()
    grams = set(_bigrams(token))
    if not grams:
        return []
    hits = Counter()
    for g in grams:
        for t in st["index"].get(g, ()):
            hits[t] += 1
    out = []
    for t, c in hits.items():
        if abs(len(t) - len(token)) > 1:
            continue
        cov = c / float(len(grams))
        prior = math.log(1 + st["freq"].get(t, 1)) / st["norm"]
        out.append((cov * 0.7 + prior * 0.3, cov, t))
    out.sort(reverse=True)
    return out[:topk]


def correct_token(token, min_cov=0.8, min_score=0.6):
    """把一个疑似错词改成词典里的写法（找不到就原样返回）。"""
    if not token or len(token) < 3:
        return token, None
    st = _state if _state["ready"] else train()
    if token in st["freq"]:
        return token, None
    for score, cov, t in candidates(token, topk=8):
        if cov >= min_cov and score >= min_score:
            return t, (score, cov)
    return token, None


def main():
    import time
    t0 = time.time()
    st = train()
    print("训练完成：%d 个词 / %d 个二元组索引（%.1f 秒）"
          % (len(st["terms"]), len(st["index"]), time.time() - t0))
    for bad in ("过的一水晶", "过充水京", "乌妖电熔", "堕落之能", "离心几", "猪乌鸭之星"):
        good, info = correct_token(bad)
        print("  %-8s → %-8s %s" % (bad, good, ("score %.2f cov %.2f" % info) if info else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
