# -*- coding: utf-8 -*-
"""段落体检：切成片之后，章节还讲不讲得完、跳没跳段。

为什么单独做这一层：句级检查只看"一句话有没有说完"，接缝检查只看"这一刀
顺不顺"，但观众感觉到"段落有问题"通常是更大尺度的事——

  · 一个章节被删得太狠，讲了一半就跳到下一个话题
  · 两个保留段之间跨过了章节边界，等于"第一章没讲完就进了第二章"
  · 某章节整段被删掉，观众会觉得"刚才在说什么？怎么突然换话题了"

判据：
  · 每章节的保留比例（保留时长 / 原时长）
  · 跨章节边界的接缝（前一段在 A 章、后一段在 B 章）
  · 整段消失的章节

只报告，不改剪辑——它是"证据层"，用来判断该不该拦。
"""
from __future__ import annotations

import json

KEEP_LOW = 0.30        # 一章保留不到 30% → 大概率讲不完
KEEP_HIGH = 0.85       # 保留超过 85% → 基本没删
GAP_SECONDS = 60.0     # 章节内被跳过的总时长超过这个数 → 提醒
MAX_SILENT_CHAPTERS = 1


def _overlap(a1, a2, b1, b2):
    return max(0.0, min(a2, b2) - max(a1, b1))


def chapter_health(sections, spans, min_total=20.0):
    """每章节保留了多少。

    sections: [{"title": str, "start": float, "end": float}, ...]（源片时间）
    spans:    [(源片起点, 源片终点), ...]（成片实际保留的区间）
    """
    rows = []
    for index, sec in enumerate(sections or []):
        try:
            start = float(sec.get("start") or 0.0)
            end = float(sec.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        total = end - start
        if total < min_total:
            continue
        kept = 0.0
        for a, b in spans or []:
            kept += _overlap(start, end, float(a), float(b))
        ratio = kept / total if total > 0 else 0.0
        rows.append({
            "index": index + 1,
            "title": str(sec.get("title") or "")[:24],
            "start": round(start, 1),
            "end": round(end, 1),
            "total": round(total, 1),
            "kept": round(kept, 1),
            "ratio": round(ratio, 3),
            "verdict": ("讲不完" if ratio < KEEP_LOW
                        else ("基本没删" if ratio >= KEEP_HIGH else "正常")),
        })
    return rows


def cross_chapter_seams(sections, spans):
    """找出跨章节边界的接缝：前一段在 A 章、后一段在 B 章。

    这种地方是"换章"，观众对衔接最敏感；如果前一章话没说完就跳，
    会明显觉得"断"。
    """
    def chapter_of(t):
        for index, sec in enumerate(sections or []):
            try:
                if float(sec.get("start") or 0) <= t < float(sec.get("end") or 0):
                    return index + 1, str(sec.get("title") or "")[:20]
            except (TypeError, ValueError):
                continue
        return 0, ""

    rows = []
    for i in range(len(spans or []) - 1):
        left = float(spans[i][1])
        right = float(spans[i + 1][0])
        a_id, a_title = chapter_of(left)
        b_id, b_title = chapter_of(right)
        if a_id and b_id and a_id != b_id:
            rows.append({
                "seam": i + 1,
                "from_chapter": a_id, "from_title": a_title,
                "to_chapter": b_id, "to_title": b_title,
                "source_gap": round(right - left, 1),
            })
    return rows


def chapter_jumps(sections, spans, min_jump=25.0):
    """每章内部相邻保留段之间的"跳跃"（源片上跳过的秒数）。

    章节整体保留比例正常，不代表章节内部不跳：一章 120 秒留了 100 秒，
    但那 20 秒可能是"中间挖掉一大块"，观众会觉得"刚才说到哪了"。
    """
    out = []
    for index, sec in enumerate(sections or []):
        try:
            start = float(sec.get("start") or 0.0)
            end = float(sec.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        inside = sorted((float(a), float(b)) for a, b in spans or []
                        if float(b) > start and float(a) < end)
        jumps = []
        for k in range(len(inside) - 1):
            gap = inside[k + 1][0] - inside[k][1]
            if gap >= min_jump:
                jumps.append({
                    "after": round(inside[k][1], 1),
                    "before": round(inside[k + 1][0], 1),
                    "gap": round(gap, 1),
                })
        if jumps:
            out.append({
                "index": index + 1,
                "title": str(sec.get("title") or "")[:24],
                "jumps": jumps,
                "biggest": max(row["gap"] for row in jumps),
            })
    return out


def vanishing_chapters(rows, min_total=20.0):
    """整章几乎没保留（保留比例 < 5%）的章节。"""
    return [row for row in rows
            if row["total"] >= min_total and row["ratio"] < 0.05]


def summarize(sections, spans):
    rows = chapter_health(sections, spans)
    seams = cross_chapter_seams(sections, spans)
    jumps = chapter_jumps(sections, spans)
    gone = vanishing_chapters(rows)
    thin = [row for row in rows if row["verdict"] == "讲不完"]
    return {
        "chapters": rows,
        "cross_seams": seams,
        "jumps": jumps,
        "vanished": gone,
        "thin": thin,
        "ok": not gone and len(thin) <= MAX_SILENT_CHAPTERS,
    }


def to_text(report):
    lines = []
    rows = report.get("chapters") or []
    if rows:
        lines.append("段落体检：%d 章" % len(rows))
        for row in rows:
            lines.append("  第%d章「%s」原 %.0f 秒 / 留 %.0f 秒（%.0f%%）%s"
                         % (row["index"], row["title"], row["total"],
                            row["kept"], row["ratio"] * 100,
                            "" if row["verdict"] == "正常"
                            else " ← " + row["verdict"]))
    for seam in report.get("cross_seams") or []:
        lines.append("  跨章接缝：第%d章「%s」→ 第%d章「%s」"
                     % (seam["from_chapter"], seam["from_title"],
                        seam["to_chapter"], seam["to_title"]))
    for row in report.get("jumps") or []:
        lines.append("  章内跳跃：第%d章「%s」%d 处，最大 %.0f 秒"
                     % (row["index"], row["title"], len(row["jumps"]),
                        row["biggest"]))
    for row in report.get("vanished") or []:
        lines.append("  整章消失：第%d章「%s」" % (row["index"], row["title"]))
    return "\n".join(lines)


def main(argv=None):
    import argparse
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("index_dir")
    args = ap.parse_args(argv)
    path = os.path.join(args.index_dir, "index.json")
    data = json.load(open(path, encoding="utf-8"))
    sections = ((data.get("script") or {}).get("sections") or [])
    plans = sorted(
        [os.path.join(args.index_dir, "plans", n) for n in
         os.listdir(os.path.join(args.index_dir, "plans"))],
        key=os.path.getmtime, reverse=True)
    plan = json.load(open(plans[0], encoding="utf-8"))["plan"]
    spans = [(float(a), float(b)) for a, b in plan["final_render_spans"]]
    report = summarize(sections, spans)
    print(to_text(report))


if __name__ == "__main__":
    main()
