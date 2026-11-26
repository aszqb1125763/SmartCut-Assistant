# -*- coding: utf-8 -*-
"""片头重录检测：同一话题在开头连着讲了好几遍 → 只留最完整的一次。

为什么单独做：原来的 dedupe 比的是"整句字面重合"，而且要求两段相隔 ≥45 秒；
但重录通常就挨着（隔 1~3 秒）、而且说法不一样（"先把过冲水晶做下" vs "我们先看堕落智能"），
字面重合只有 0.2 左右，永远抓不到。

做法（只在片头 5 分钟内）：
  1. 把台词按 ≥1.6 秒停顿切成"说话段"
  2. 相邻段的关键词重合 ≥0.20 且都在讲同一件事 → 归成一簇
  3. 一簇里只留最长的那个（信息最全），其余标记为"重录"
  4. 只丢比保留段短 30% 以上的，避免误伤
"""
from __future__ import annotations

import re

STOP = set("的了是在我你们他她它这那和就不都有个什么怎么然后所以但是因为可以我们他们"
           "一下一个这个那个这些那些这里那里现在还是就是也要会能对呗吧啊嗯哦呃着过把")


def _norm(t):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", t or "")


def _keywords(t):
    t = _norm(t)
    gs = {t[i:i + 2] for i in range(max(0, len(t) - 1))}
    return {g for g in gs if not any(ch in STOP for ch in g)}


def _overlap(a, b):
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / float(min(len(ka), len(kb)))


def speech_runs(segments, min_gap=1.6):
    """把句子合并成说话段。"""
    runs, cur = [], []
    for s in segments:
        a, b = float(s[0]), float(s[1])
        t = str(s[2] or "")
        if cur and a - cur[-1][1] > min_gap:
            runs.append(cur)
            cur = []
        cur.append((a, b, t))
    if cur:
        runs.append(cur)
    return [{"t0": r[0][0], "t1": r[-1][1], "dur": r[-1][1] - r[0][0],
             "txt": "".join(x[2] for x in r)} for r in runs]


def find_retakes(data, span_limit=300.0, max_gap=8.0, min_overlap=0.20,
                 min_span=6.0, shorter_ratio=0.70, verbose=None):
    """返回要丢掉的重录段 [{"start","end","why",...}]（只在前 span_limit 秒内找）。"""
    segs = data.get("segments") or []
    if not segs:
        return []
    runs = speech_runs(segs)
    idx = [i for i, r in enumerate(runs) if r["t0"] < span_limit and r["dur"] >= min_span]
    if len(idx) < 2:
        return []
    # 相邻段连成一簇（关键词重合够高）
    clusters, cur = [], [idx[0]]
    for i in idx[1:]:
        a, b = runs[cur[-1]], runs[i]
        if b["t0"] - a["t1"] <= max_gap and _overlap(a["txt"], b["txt"]) >= min_overlap:
            cur.append(i)
        else:
            clusters.append(cur)
            cur = [i]
    clusters.append(cur)

    drops = []
    for cl in clusters:
        if len(cl) < 2:
            continue
        # 只列候选，不替人做决定：一段里哪一遍更好，机器判断不了（实测会判反）
        for x in range(len(cl) - 1):
            a, b = runs[cl[x]], runs[cl[x + 1]]
            drops.append({"a_start": a["t0"], "a_end": a["t1"], "a_dur": a["dur"],
                          "b_start": b["t0"], "b_end": b["t1"], "b_dur": b["dur"],
                          "why": "疑似同一段讲了两遍（间隔 %.1f 秒）" % (b["t0"] - a["t1"])})
            if verbose:
                verbose("  疑似重录：%.1f~%.1f（%.0f 秒） vs %.1f~%.1f（%.0f 秒）"
                        % (a["t0"], a["t1"], a["dur"], b["t0"], b["t1"], b["dur"]))
    return drops
