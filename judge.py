# -*- coding: utf-8 -*-
"""把评分标准交给本地模型：让它按我的规则逐句判"留/剪"，再用你的成片当考卷。

为什么这么设计：我之前让模型"看这一刀会不会断"，它只会答"多留"（没有分辨力）。
问题不在模型，在题目太开放。这里改成：
  · 5 条明确标准（完整性/上下文依赖/节奏/主线相关/口水）
  · 每条标准给"反例 vs 正例"，例子直接从你的成片对齐结果里挖（真标签）
  · 强制输出 JSON（keep + 命中哪条规则），便于和你的成片对答案
"""
from __future__ import annotations

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SYS = """你是短视频剪辑的审片员。你的任务不是创作，而是按下面 5 条标准逐句判断"留还是剪"。

【标准】
1. 完整性：这句话自己是完整的（不是被切一半的半句）。不完整 → 剪。
2. 上下文依赖：如果剪掉它，下一句会看不懂（"这个""刚才那个""继续"）→ 留。
3. 节奏：单句过长（连续 30 秒以上没停顿地念）→ 留但标记 slow；过短（不到 1 秒的语气词）→ 剪。
4. 主线相关：和本章目标有关 → 留；明显跑题（聊吃的、聊弹幕、和本集无关的旧事）→ 剪。
5. 口水：纯语气词、自言自语找东西（"在哪呢""哦对""嗯…"）、重复上一句 → 剪。

【输出格式】只输出 JSON 数组，每项：
{"i": 句号, "keep": true/false, "rule": "1|2|3|4|5|none", "why": "不超过12字"}
不要输出任何解释性文字。

【例子】（来自真实成片，左边是留下、右边是剪掉）
留下：「我们把这三个压板机连起来，接上动力，它就能自己转了」（主线操作，完整）
剪掉：「嗯…那个…我看一下啊」（口水）
留下：「上一期我们把月球基地的地基打好了，这一期要接上氧气」（承上启下）
剪掉：「我刚吃了碗面，这个面还挺好吃的」（跑题）
"""

PROMPT = """本章目标：{goal}

下面是从原片里按时间顺序截出来的 {n} 句，已经带好编号（只判断是否保留，不改顺序）：

{lines}

按标准逐句判断，输出 JSON 数组。"""


def build_lines(segs, idxs):
    out = []
    for j, i in enumerate(idxs, 1):
        t = (segs[i][2] or "").strip()
        dur = float(segs[i][1]) - float(segs[i][0])
        out.append("%d. (%.1f秒) %s" % (j, dur, t))
    return "\n".join(out)


def judge_batch(srv, segs, idxs, goal=""):
    """返回 {句号(1-based 序号): (keep, rule, why)}"""
    raw = srv.chat(SYS, PROMPT.format(goal=goal or "（未说明）", n=len(idxs),
                                      lines=build_lines(segs, idxs)),
                   max_tokens=1200)
    txt = (raw or "").strip()
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            k = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        out[k] = (bool(item.get("keep")), str(item.get("rule", "none")),
                  str(item.get("why", ""))[:24])
    return out


def judge(data, indices=None, batch=25, port=8137, on_log=None):
    """对给定句子下判断，返回 {句子下标: (keep, rule, why)}"""
    import proofread as pf
    segs = data.get("segments") or []
    if indices is None:
        indices = list(range(len(segs)))
    log = on_log or (lambda _m: None)
    script = data.get("script") or {}
    res = {}
    with pf.Server(task="segment", port=port, ctx=4096, api_role="director") as srv:
        for k in range(0, len(indices), batch):
            chunk = indices[k:k + batch]
            goal = ""
            if chunk:
                t = float(segs[chunk[0]][0])
                for sec in script.get("sections") or []:
                    if float(sec.get("start", 0)) <= t <= float(sec.get("end", 0)):
                        goal = "%s（%s）" % (sec.get("title", ""), sec.get("advice", ""))
                        break
            got = judge_batch(srv, segs, chunk, goal)
            for j, i in enumerate(chunk, 1):
                if j in got:
                    res[i] = got[j]
            log("  判完 %d/%d 句" % (min(len(indices), k + batch), len(indices)))
    return res


if __name__ == "__main__":
    import autocut
    idx = sys.argv[1] if len(sys.argv) > 1 else r"D:\Media\cache\index.json"
    data = autocut.load_index(idx)
    got = judge(data, list(range(0, min(50, len(data["segments"])))),
                on_log=print)
    for i, (keep, rule, why) in sorted(got.items()):
        print("%3d %s [%s] %s | %s" % (i, "留" if keep else "剪", rule, why,
                                       (data["segments"][i][2] or "")[:30]))
