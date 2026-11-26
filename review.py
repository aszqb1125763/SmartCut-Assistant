# -*- coding: utf-8 -*-
"""逐刀复查：让本地模型看每一刀的两边，判断观众会不会看不懂。

为什么需要：现在是"章节建议 + 分数阈值"决定留哪些句子，它不看上下文，
所以会出现"话说到一半就切走"、"下一句依赖被剪掉的内容"这类随意感。
这一步只做一件事：在每一刀的边界上，把左右各两三句给模型看，
问它"剪掉中间之后，右边第一句还能独立看懂吗"；不能就往右多留 1~3 句。

成本：一刀一次调用，十几到几十刀，约 1~2 分钟；只在中/重度用它。
"""
from __future__ import annotations

import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SYS = "你是资深剪辑总监，只判断一件事：这一刀下去，观众会不会看不懂。"

PROMPT = """按时间顺序的台词，中间有一段被剪掉了。

【剪之前留着的内容，最后几句】
{left}

【被剪掉的部分，开头几句】
{cut}

【剪之后留下的内容，开头几句】
{right}

判断：剪掉中间之后，上面【剪之后】的第一句还能不能让观众看懂？
· 如果它依赖被剪掉的信息（比如"这个""刚才那个""继续上一步"），需要往右多留几句才接得上 → 回答 EXTEND 数字（1~4）
· 如果本来就是一个新话题、能独立看懂 → 回答 OK
· 如果被剪掉的这几句其实是交代关键前提、必须留 → 回答 BACK 数字（1~4）

只输出一行，例如：OK 或 EXTEND 2 或 BACK 1"""


def _ctx(segs, i0, i1, n):
    """取 [i0, i1] 范围内的 n 句（向 i0 方向）。"""
    out = []
    for i in range(max(0, i0 - n), min(len(segs), i1)):
        out.append("· %s" % (segs[i][2] or "")[:80])
    return "\n".join(out) or "（无）"


def review_boundaries(data, spans, on_log=None, max_calls=80, port=8136,
                      n_left=2, n_right=2):
    """逐刀复查，返回调整后的片段列表（只往右扩 / 往左补，不删内容）。"""
    import proofread as pf
    segs = data.get("segments") or []
    if not segs or not spans:
        return spans
    log = on_log or (lambda _m: None)
    spans = [(float(a), float(b)) for a, b in spans]

    def idx_at(t):
        for i, s in enumerate(segs):
            if float(s[0]) >= t - 0.01:
                return i
        return len(segs) - 1

    # 找出所有"剪掉的空档"（两段之间的间隔），逐个复查
    gaps = []
    for k in range(len(spans) - 1):
        g0, g1 = spans[k][1], spans[k + 1][0]
        if g1 - g0 >= 1.0:
            gaps.append((k, g0, g1))
    if not gaps:
        return spans
    log("逐刀复查：%d 处剪口，最多查 %d 处" % (len(gaps), max_calls))

    fixed = 0
    out = [list(s) for s in spans]
    try:
        with pf.Server(task="segment", port=port, ctx=4096, api_role="director") as srv:
            for n, (k, g0, g1) in enumerate(gaps[:max_calls], 1):
                i_cut0 = idx_at(g0)
                i_cut1 = max(i_cut0, idx_at(g1) - 1)
                left = _ctx(segs, i_cut0 - n_left, i_cut0, n_left)
                cut = _ctx(segs, i_cut0, min(i_cut1 + 1, i_cut0 + 3), 3)
                right = _ctx(segs, i_cut1 + 1, i_cut1 + 1 + n_right, n_right)
                try:
                    raw = srv.chat(SYS, PROMPT.format(left=left, cut=cut, right=right),
                                   max_tokens=20)
                except Exception as exc:  # noqa: BLE001
                    from llm_gateway import propagate_api_error
                    propagate_api_error(exc)
                    log("  第 %d 处复查失败：%s" % (n, str(exc)[:50]))
                    continue
                ans = (raw or "").strip().upper()
                if ans.startswith("EXTEND"):
                    m = re.search(r"(\d)", ans)
                    n_ext = min(4, max(1, int(m.group(1)) if m else 1))
                    end_i = min(len(segs) - 1, i_cut1 + n_ext)
                    out[k + 1][0] = min(out[k + 1][0], float(segs[end_i][0]))
                    fixed += 1
                    log("  第 %d 处：右边接不上 → 往前多留 %d 句（到 %.1f 秒）"
                        % (n, n_ext, out[k + 1][0]))
                elif ans.startswith("BACK"):
                    m = re.search(r"(\d)", ans)
                    n_bak = min(4, max(1, int(m.group(1)) if m else 1))
                    start_i = max(0, i_cut0 - n_bak)
                    out[k][1] = max(out[k][1], float(segs[start_i][1]))
                    fixed += 1
                    log("  第 %d 处：被剪的几句是必要前提 → 左边多留 %d 句"
                        % (n, n_bak))
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        log("逐刀复查跳过（模型没起来）：%s" % str(exc)[:80])
        return spans

    # 合并可能重叠的段
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    log("逐刀复查完成：调整 %d 处，%.1f → %.1f 分钟"
        % (fixed, sum(b - a for a, b in spans) / 60,
           sum(b - a for a, b in merged) / 60))
    return [(a, b) for a, b in merged]


if __name__ == "__main__":
    import json
    if len(sys.argv) < 2:
        print("用法：python review.py <索引目录>  （只做演示：读 剪辑计划 里的段）")
        raise SystemExit(2)
    d = sys.argv[1]
    p = os.path.join(d, "index.json") if os.path.isdir(d) else d
    data = json.load(open(p, encoding="utf-8"))
    plan = os.path.join(os.path.dirname(p), "剪辑计划.json")
    spans = []
    if os.path.isfile(plan):
        pd = json.load(open(plan, encoding="utf-8"))
        spans = [(float(c["start"]), float(c["end"])) for c in pd.get("pieces") or []]
    if not spans:
        print("没有可复查的片段（先跑一次剪辑生成 剪辑计划.json）")
        raise SystemExit(1)
    review_boundaries(data, spans, on_log=print)
