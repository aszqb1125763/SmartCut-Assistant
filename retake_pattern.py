# -*- coding: utf-8 -*-
"""白录检测 v2：只看"长物件名"、去子串、限片头、要求"残局"特征。

v1 的问题：98 段 / 923 秒（把 堕落/堕落智/落智 当成三个物件，误报满地）。
v2 规则：
  · 物件名只取 3~6 字、且不是别的物件名的子串（过冲水晶 / 堕落智能 / 猪乌鸦之星）
  · 一句里出现 ≥2 个物件名才算"报菜名"
  · 只在片头（默认前 10 分钟）找
  · 两次报菜名之间必须有停顿（≥1.2 秒）+ 那段是"残局"：
    末尾那句很短（≤2.6 秒）或音量明显偏低（比该段平均低 5dB 以上）
  · 中间段长度 3~45 秒
"""
from __future__ import annotations

import re
from collections import Counter

STOP = set("的了是在我你们他她它这那和就不都有个什么怎么然后所以但是因为可以我们他们"
           "一下一个这个那个这些那些这里那里现在还是就是也要会能对呗吧啊嗯哦呃着过把"
           "先做看想要是没很太再又倒而且如果虽然当然其实应该可能东西一批对里外上下大小"
           "多少几每个两个三个四个五十六七九八零一二好来去去到回起开开关关做弄")

DISCOURSE = ("好", "那我们", "我们先", "然后", "接下来", "那么", "所以", "另外",
             "第一件事", "先看", "看一下", "先做", "来", "现在")


def _norm(t):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", t or "")


def _terms(segments, lo=3, hi_len=6, min_cnt=2, max_frac=0.12):
    """候选物件名：3~6 字、出现 ≥2 次、不超过 12% 的句子，且去掉被更长名包含的子串。"""
    texts = [_norm(str(s[2] or "")) for s in segments]
    n = max(1, len(texts))
    cnt = Counter()
    for t in texts:
        seen = set()
        for L in range(lo, hi_len + 1):
            for i in range(max(0, len(t) - L + 1)):
                g = t[i:i + L]
                if any(ch in STOP for ch in g):
                    continue
                seen.add(g)
        cnt.update(seen)
    # 短素材（几十句）时 n*0.12 只有个位数，出现两次的物件名会被误滤掉，
    # 所以给上限一个下限 3
    hi_cnt = max(3, int(n * max_frac))
    cand = [g for g, c in cnt.items() if min_cnt <= c <= hi_cnt]
    # 去掉子串：若某名字被另一个更长、次数相近的名字包含，就丢掉短的
    cand.sort(key=len, reverse=True)
    keep = []
    for g in cand:
        if any(g != k and g in k and cnt[k] >= cnt[g] * 0.6 for k in keep):
            continue
        keep.append(g)
    return set(keep)


def _entities(text, terms):
    """最长匹配：同一批字只算一个物件（避免 堕落/堕落智/落智 重复计）。"""
    t = _norm(text)
    found, i = set(), 0
    ordered = sorted(terms, key=len, reverse=True)
    while i < len(t):
        hit = None
        for g in ordered:
            if t.startswith(g, i):
                hit = g
                break
        if hit:
            found.add(hit)
            i += len(hit)
        else:
            i += 1
    return found


def find_aborted_attempts(data, span_limit=600.0, max_span=45.0, min_span=3.0,
                          min_gap=1.2, min_shared=2, quiet_db=5.0, lead_in=3.0,
                          verbose=None):
    segs = data.get("segments") or []
    if len(segs) < 8:
        return []
    au = data.get("seg_audio") or []
    feat = data.get("seg_feat") or []
    terms = _terms(segs)
    decl = []
    for i, s in enumerate(segs):
        ent = _entities(str(s[2] or ""), terms)
        if len(ent) >= min_shared and float(s[0]) <= span_limit:
            decl.append({"i": i, "t0": float(s[0]), "t1": float(s[1]), "ent": ent})
    if len(decl) < 2:
        return []
    drops = []
    for k in range(len(decl) - 1):
        a = decl[k]
        for j in range(k + 1, len(decl)):
            b = decl[j]
            if b["t0"] - a["t1"] > max_span:
                break
            gap = b["t0"] - a["t1"]
            if gap < min_gap or gap < min_span:
                continue
            shared = a["ent"] & b["ent"]
            if len(shared) < min_shared:
                continue
            # 残局特征：a 之后那句很短，或音量明显偏低
            ia, ib = a["i"], b["i"]
            tail = segs[ib - 1] if ib - 1 > ia else segs[ia]
            tail_len = float(tail[1]) - float(tail[0])
            quiet = False
            if au and len(au) == len(segs):
                lv = [float(au[x][0]) for x in range(ia + 1, ib) if x < len(au)]
                if lv:
                    quiet = (sum(lv) / len(lv)) <= -32.0
            elif feat and len(feat) == len(segs):
                lv = [float(feat[x][1]) for x in range(ia + 1, ib) if x < len(feat)]
                if lv:
                    quiet = (sum(lv) / len(lv)) <= -32.0
            if tail_len > 2.6 and not quiet:
                continue
            # 从"第一次报菜名那句话"开头就剪（再往前带 lead_in 秒的引子），
            # 不然第一遍的开场白会留在片子里，观众看到的就是"说一半又重来一遍"
            _start = max(0.0, a["t0"] - lead_in)
            drops.append({
                "start": _start, "end": b["t0"], "dur": b["t0"] - _start,
                "why": "白录：%s 报过之后跑偏 %.0f 秒，又重报同一批物件"
                       % ("/".join(sorted(shared)[:3]), gap),
            })
            if verbose:
                verbose("  白录：%.1f~%.1f（%.0f 秒）物件 %s"
                        % (_start, b["t0"], b["t0"] - _start,
                           "/".join(sorted(shared)[:3])))
            break
    return drops
