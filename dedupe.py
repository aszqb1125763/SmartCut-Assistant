# -*- coding: utf-8 -*-
"""重复录制识别：同一段内容说了两遍时，自动留更好的那一版。

为什么会重复：录播经常是「这段没说好，重来一遍」，或者分段录制时前后接了一段
已经录过的内容。人眼在 33 分钟里很难发现，但它会拉长成片、还让观众觉得
"这段我看过了"。

四步：
  1. 台词按 7 句一窗滑动，去掉标点做 4-gram 倒排 → 候选窗口对
  2. 相似度取「Jaccard 与 包含度」里高的那个：
     重录时常会多说几个口头禅（"然后…那个…"），Jaccard 会被稀释，
     但内容其实还是同一段，用包含度能救回来
  3. 同一处重复会命中很多窗口，按「两段的句子偏移」聚成一簇，
     簇里至少 3 个窗口才算数（一两个窗口对上多半是巧合，实测会误报）
  4. 找到之后**向两侧扩段**：按时间去扩，只要扩完还像就继续，
     否则就停在上一轮。这样才是整段 100 秒的重复，而不是窗口那 20 秒
  5. 两版比质量，留分高的：完整度（句尾不是半句）+ 干净（没有 noise）
     + 语速合理 + 后录的一版略加分（通常是重录修正版）

用法：
    python dedupe.py <索引目录>            # 只报告
    python dedupe.py <索引目录> --json    # 机读
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))

# 半句收尾：说了这些词通常话没说完
UNFINISHED = ("然后", "就是", "那个", "这个", "所以", "因为", "但是", "而且",
              "的话", "的时候", "对了", "还有", "以及", "比如")


def _norm(t: str) -> str:
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", t or "")


def _grams(t: str, n: int = 4) -> set:
    return {t[k:k + n] for k in range(max(0, len(t) - n + 1))}


def _similarity(ta: str, tb: str) -> float:
    """Jaccard 和包含度取高。两段都很短时返回 0，避免误配。"""
    if len(ta) < 12 or len(tb) < 12:
        return 0.0
    ga, gb = _grams(ta), _grams(tb)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    union = len(ga) + len(gb) - inter
    jac = inter / float(union) if union else 0.0
    cont = inter / float(min(len(ga), len(gb)))
    return max(jac, 0.85 * cont)


def _load(index):
    path = os.path.join(index, "index.json") if os.path.isdir(index) else index
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _span_text(texts, segs, a: float, b: float) -> str:
    return "".join(texts[i] for i, s in enumerate(segs)
                   if s[0] >= a - 0.01 and s[1] <= b + 0.01)


def _score_span(segs, sem, a: float, b: float):
    """给一段内容打分，越大越值得留。返回 (分数, 明细)。"""
    idxs = [i for i, s in enumerate(segs) if s[0] >= a - 0.01 and s[1] <= b + 0.01]
    if not idxs:
        return 0.0, {}
    pick = [segs[i] for i in idxs]
    total_dur = max(0.1, b - a)
    chars = sum(len(_norm(s[2])) for s in pick)

    unfinished = sum(1 for s in pick if _norm(s[2]).endswith(UNFINISHED))
    completeness = 1.0 - unfinished / len(pick)

    noise = sum(1 for i in idxs
                if i < len(sem) and (sem[i] or {}).get("kind") == "noise")
    clean = 1.0 - noise / len(pick)

    cps = chars / total_dur
    if cps <= 0:
        rate = 0.0
    elif 2.5 <= cps <= 6.5:
        rate = 1.0
    elif cps < 2.5:
        rate = max(0.0, cps / 2.5)
    else:
        rate = max(0.0, 1.0 - (cps - 6.5) / 6.5)

    score = 0.45 * completeness + 0.30 * clean + 0.25 * rate
    detail = {"完整度": round(completeness, 2), "干净": round(clean, 2),
              "语速": round(cps, 1), "句数": len(pick)}
    return score, detail


def _block_sim(texts, i, j, k=3):
    """比较 i、j 各往前 k 句的文本相似度（单句太短不可靠）。"""
    a = "".join(texts[max(0, i - k + 1):i + 1])
    b = "".join(texts[max(0, j - k + 1):j + 1])
    return _similarity(a, b)


def _extend(texts, i, j, step, win: int = 6, min_s: float = 0.30,
            patience: int = 5, max_steps: int = 600):
    """从 (i, j) 往 step 方向逐句对齐，返回匹配上的 [(i, j), ...]。

    比的是「包含当前句的 6 句窗口」，不是单句：重录里总有一两句被听岔，
    单句比会被一两句坏数据卡死，窗口比就稳得多。
    """
    out, miss = [], 0
    n = len(texts)
    for k in range(1, max_steps + 1):
        ii, jj = i + step * k, j + step * k
        # 窗口永远朝「走的方向」看：往回走看前面 6 句，往前走看后面 6 句。
        # 之前往回走也取「后面 6 句」，窗口会越过重复段边界吃到无关内容，
        # 实测多删了 40 秒真实内容。
        if step < 0:
            a_lo, b_lo = ii - win + 1, jj - win + 1
            a_hi, b_hi = ii + 1, jj + 1
        else:
            a_lo, b_lo = ii, jj
            a_hi, b_hi = ii + win, jj + win
        if a_lo < 0 or b_lo < 0 or a_hi > n or b_hi > n:
            break
        a = "".join(texts[a_lo:a_hi])
        b = "".join(texts[b_lo:b_hi])
        if _similarity(a, b) >= min_s:
            out.append((ii, jj))
            miss = 0
        else:
            miss += 1
            if miss >= patience:
                break
    return out


def find_duplicates(data, win: int = 7, min_sim: float = 0.30,
                    min_gap: float = 5.0, min_span: float = 2.5,
                    min_cluster: int = 2, later_bonus: float = 0.06,
                    expand_sim: float = 0.35, verbose=None):
    """找出「同一段内容出现两遍」，返回 [{keep, drop, sim, why, ...}]。"""
    segs = [(float(a), float(b), str(t)) for a, b, t in (data.get("segments") or [])]
    if len(segs) < win * 2:
        return []
    dur = float(data.get("duration") or segs[-1][1])
    sem = data.get("semantic") or []
    texts = [_norm(s[2]) for s in segs]

    wins = []
    for i in range(0, len(segs) - win + 1):
        chunk = "".join(texts[i:i + win])
        if len(chunk) < 20:
            continue
        wins.append({"i0": i, "t0": segs[i][0],
                     "t1": segs[i + win - 1][1], "txt": chunk})
    if len(wins) < 2:
        return []

    inv = defaultdict(list)
    for wi, w in enumerate(wins):
        for g in _grams(w["txt"]):
            inv[g].append(wi)

    shared_cnt = defaultdict(int)
    for _g, lst in inv.items():
        if len(lst) > 40:          # 太常见的片段没有区分度
            continue
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                shared_cnt[(lst[x], lst[y])] += 1

    cands = []
    for (a, b), shared in shared_cnt.items():
        if shared < 3:
            continue
        wa, wb = wins[a], wins[b]
        if abs(wa["t0"] - wb["t0"]) < min_gap:
            continue
        sim = _similarity(wa["txt"], wb["txt"])
        if sim >= min_sim:
            cands.append({"a": wa, "b": wb, "sim": sim,
                          "off": wb["t0"] - wa["t0"]})
    if not cands:
        return []

    # 按偏移聚簇：同一处重复，窗口对的时间偏移几乎一样
    cands.sort(key=lambda x: x["off"])
    clusters = []
    for c in cands:
        if clusters and abs(clusters[-1]["off"] - c["off"]) <= 12.0:
            g = clusters[-1]
            g["items"].append(c)
            g["off"] = (g["off"] * (len(g["items"]) - 1) + c["off"]) / len(g["items"])
            g["sim"] = max(g["sim"], c["sim"])
        else:
            clusters.append({"off": c["off"], "sim": c["sim"], "items": [c]})

    out = []
    for g in clusters:
        if len(g["items"]) < min_cluster:
            continue
        # 取簇里最像的一对当锚点，再按句子索引向两侧对齐扩段
        best = max(g["items"], key=lambda x: x["sim"])
        ia, ib = best["a"]["i0"], best["b"]["i0"]
        back = _extend(texts, ia, ib, -1)
        fwd = _extend(texts, ia + win - 1, ib + win - 1, +1)
        idxs = [x for x, _ in back] + list(range(ia, ia + win)) + [x for x, _ in fwd]
        jdxs = [y for _, y in back] + list(range(ib, ib + win)) + [y for _, y in fwd]
        a0, a1 = segs[min(idxs)][0], segs[max(idxs)][1]
        b0, b1 = segs[min(jdxs)][0], segs[max(jdxs)][1]
        # 两段不能重叠（重叠说明是同一段被当成了两段）
        if not (a1 <= b0 + 0.5 or b1 <= a0 + 0.5):
            continue
        if min(a1 - a0, b1 - b0) < min_span:
            continue
        # 时长要差不多：同一件事说两遍，差出两倍以上就不是"重录"了
        if not (0.55 <= (a1 - a0) / max(0.1, b1 - b0) <= 1.8):
            continue
        # 文字量也要够：50 个字以内"对上"多半是口头禅撞车
        if min(len(_span_text(texts, segs, a0, a1)),
               len(_span_text(texts, segs, b0, b1))) < 50:
            continue
        sa, da = _score_span(segs, sem, a0, a1)
        sb, db = _score_span(segs, sem, b0, b1)
        sa2 = sa + (0.0 if a0 < b0 else later_bonus)
        sb2 = sb + (0.0 if b0 < a0 else later_bonus)
        if sa2 >= sb2:
            keep, drop, kd, dd = (a0, a1), (b0, b1), da, db
        else:
            keep, drop, kd, dd = (b0, b1), (a0, a1), db, da
        out.append({"keep": (round(keep[0], 2), round(keep[1], 2)),
                    "drop": (round(drop[0], 2), round(drop[1], 2)),
                    "sim": round(g["sim"], 3),
                    "keep_detail": kd, "drop_detail": dd,
                    "why": "同一段内容出现两遍（相似 %.0f%%），保留%s"
                           % (g["sim"] * 100,
                              "前面那版" if keep[0] < drop[0] else "后面那版")})
        if verbose:
            verbose("  重复：留 %.1f~%.1f（%.1fs），丢 %.1f~%.1f（%.1fs），相似 %.0f%%"
                    % (keep[0], keep[1], keep[1] - keep[0],
                       drop[0], drop[1], drop[1] - drop[0], g["sim"] * 100))
    # 同一处重复会被不同偏移的窗口各命中一次 → 合并成一条（保留相似度高的）
    def _ov(p, q):
        lo, hi = max(p[0], q[0]), min(p[1], q[1])
        return max(0.0, hi - lo) / max(0.1, min(p[1] - p[0], q[1] - q[0]))

    merged = []
    for it in sorted(out, key=lambda x: -x["sim"]):
        if any(_ov(it["keep"], m["keep"]) > 0.5 or _ov(it["drop"], m["drop"]) > 0.5
               for m in merged):
            continue
        merged.append(it)
    merged.sort(key=lambda x: x["drop"][0])
    return merged


def drop_spans(dups):
    return [(float(d["drop"][0]), float(d["drop"][1])) for d in dups]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sim", type=float, default=0.30)
    args = ap.parse_args(argv)
    data = _load(args.index)
    print("索引：%s（%d 句）" % (args.index, len(data.get("segments") or [])))
    dups = find_duplicates(data, min_sim=args.sim, verbose=lambda m: print(m))
    if args.json:
        print(json.dumps(dups, ensure_ascii=False, indent=1))
        return 0
    if not dups:
        print("没发现重复内容。")
        return 0
    total = sum(b - a for a, b in drop_spans(dups))
    print("\n共 %d 组重复，可去掉 %.1f 分钟" % (len(dups), total / 60))
    for d in dups:
        print("  留 %7.1f~%7.1f ；丢 %7.1f~%7.1f   相似 %.0f%%"
              % (d["keep"][0], d["keep"][1], d["drop"][0], d["drop"][1],
                 d["sim"] * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def find_short_retakes(data, max_gap=30.0, min_sim=0.42,
                       max_gap_sentences=12, verbose=None):
    """找【短时间录了两遍】——同一段内容在几十秒内说两次。

    为什么单独写（子枫 2026-11-23）：
      原来的 find_duplicates 用「7 句拼接」比相似度，适合找整段重复；
      但重录往往是**单句**重复（第一次没说好，立刻重录），拼进 7 句后
      相似度被稀释，永远够不到阈值。

    重录例子（相隔 7 秒）：
      「我们把最后一组材料全部处理完」
      「这里我们把最后一组材料处理完」
    """
    segs = [(float(a), float(b), str(t))
            for a, b, t in (data.get("segments") or [])]
    if len(segs) < 2:
        return []
    texts = [_norm(s[2]) for s in segs]
    out = []
    used = set()
    for i in range(len(segs)):
        if i in used:
            continue
        for j in range(i + 1, min(i + max_gap_sentences + 1, len(segs))):
            if j in used:
                continue
            ta, tb = texts[i], texts[j]
            if len(ta) < 4 or len(tb) < 4:
                continue
            if segs[j][0] - segs[i][0] > max_gap:
                continue
            sim = _similarity(ta, tb)
            if sim < min_sim:
                continue
            out.append({
                "keep": [segs[j][0], segs[j][1]],
                "drop": [segs[i][0], segs[i][1]],
                "sim": round(sim, 3), "why": "short_retake",
                "text_keep": segs[j][2][:30],
                "text_drop": segs[i][2][:30],
                "gap": round(segs[j][0] - segs[i][0], 1),
            })
            used.add(i)
            break
    if verbose:
        for row in out:
            verbose("【重录】%.1f 秒「%s」→ 保留 %.1f 秒「%s」（相似 %.2f，隔 %.0f 秒）"
                    % (row["drop"][0], row["text_drop"][:16],
                       row["keep"][0], row["text_keep"][:16],
                       row["sim"], row["gap"]))
    return out
