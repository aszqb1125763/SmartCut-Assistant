# -*- coding: utf-8 -*-
"""剪辑导演：让本地大模型按上下文决定留哪些句子，而不是套阈值。

为什么改：
  · 关键词/阈值是"我猜子枫的标准"，一换素材就失灵
  · 模型分只按单句特征打分，看不到"这段在讲什么、前后有没有铺垫"
  · 大模型能读一小段台词，判断"这段是重复啰嗦还是在讲关键步骤"

做法：
  把台词按 40 句一批送进模型（带前后各 5 句上下文），
  让模型直接输出"该留哪些行号"，再由程序换成时间段。
  结果存进 index.json 的 director 字段，重复剪辑不用重算。
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

SYSTEM = ("你是资深游戏实况剪辑师。你的观众想看操作、进展、关键讲解，"
          "不想看重复啰嗦、找东西、卡壳。只按格式输出行号。")

PROMPT = """下面是《我的世界》实况的逐句台词，每行开头是编号。

任务：挑出应该保留的行。判断标准：
  · 有信息量：讲清了在做什么、为什么这么做、做出了什么
  · 有进展：造好了东西、打完了怪、发现了新东西
  · 有反应：惊讶、吐槽、情绪爆发（这些是看点）
  · 不要留：重复已经说过的、纯找东西、卡壳重试、长时间的废话铺垫
  · 宁可少留，不要凑数：观众宁可看 10 分钟干货，也不想看 30 分钟注水

注意：
  · 前后要连贯，别把一个步骤从中间截断
  · 第一段如果有自我介绍，必须留

只输出要保留的编号，一行一个，例如：
3
4
5
12

台词：
%s

只输出编号。"""


def _parse(raw, n):
    keep = set()
    for line in (raw or "").splitlines():
        m = re.match(r"^\s*(\d{1,4})\s*$", line.strip())
        if m:
            i = int(m.group(1))
            if 1 <= i <= n:
                keep.add(i - 1)
    return keep


def _numbered_lines(segs, lo, hi, start, batch):
    lines = []
    for i in range(lo, hi):
        mark = ">" if start <= i < start + batch else " "
        lines.append("%d. %s%s" % (i - lo + 1, mark, segs[i][2]))
    return "\n".join(lines)


def _parse_review(raw, n):
    """解析段落复查；NOT OK 不能因为包含 OK 而被当成通过。"""
    additions = set()
    removals = set()
    for action, value in re.findall(r"\b(ADD|DROP)\s+(\d{1,4})\b",
                                    raw or "", re.IGNORECASE):
        index = int(value)
        if 1 <= index <= n:
            (additions if action.upper() == "ADD" else removals).add(index - 1)
    return additions, removals


def plan(data, batch=40, ctx=5, on_log=print, model=None):
    """返回 [(start, end)]：模型决定要保留的区间。"""
    segs = data.get("segments") or []
    if not segs:
        return []
    import proofread as pf
    puts = getattr(pf, "_log_silent", None)
    keeps = [False] * len(segs)
    n = len(segs)
    got_total = 0
    with pf.Server(task="segment", port=8131, api_role="director",
                   model=(model or None)) as srv:
        for start in range(0, n, batch):
            lo = max(0, start - ctx)
            hi = min(n, start + batch + ctx)
            body = _numbered_lines(segs, lo, hi, start, batch)
            try:
                raw = srv.chat(SYSTEM, PROMPT % body, max_tokens=800)
            except Exception as exc:  # noqa: BLE001
                from llm_gateway import propagate_api_error
                propagate_api_error(exc)
                on_log("  第 %d 批失败：%s" % (start // batch + 1, exc))
                continue
            keep_local = _parse(raw, hi - lo)
            got_total += len(keep_local)
            for k in keep_local:
                gi = lo + k
                if start <= gi < start + batch:
                    keeps[gi] = True
            on_log("  导演判断 %d/%d（累计保留 %d 句）"
                   % (min(start + batch, n), n, sum(keeps)))
    if not got_total:
        return []
    # 合并连续句子成区间
    out = []
    cur = None
    for i, k in enumerate(keeps):
        if k:
            a, b = float(segs[i][0]), float(segs[i][1])
            if cur and a - cur[1] <= 1.5:
                cur[1] = b
            else:
                if cur:
                    out.append(tuple(cur))
                cur = [a, b]
    if cur:
        out.append(tuple(cur))
    return out


def apply(index_path, batch=40, on_log=print):
    data = json.load(open(index_path, encoding="utf-8"))
    spans = plan(data, batch=batch, on_log=on_log)
    if not spans:
        on_log("模型没给出结果，未写入")
        return 0
    data["director"] = [[round(a, 2), round(b, 2)] for a, b in spans]
    data["director_info"] = {"sentences": len(data.get("segments") or []),
                             "kept_spans": len(spans),
                             "kept_minutes": round(sum(b - a for a, b in spans) / 60, 1)}
    from llm_gateway import backend_signature
    data["director_info"]["backend_signature"] = backend_signature()
    json.dump(data, open(index_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    on_log("已写入 director：%d 段，共 %.1f 分钟"
           % (len(spans), data["director_info"]["kept_minutes"]))
    return len(spans)


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else ""
    if os.path.isdir(p):
        p = os.path.join(p, "index.json")
    if not p or not os.path.isfile(p):
        print("用法：python director.py <索引目录或 index.json>")
        sys.exit(1)
    apply(p)

# ---------------------------------------------------------------------------
# 按自然段落剪：切到话题断点就回看一次（不强求固定时长）
# ---------------------------------------------------------------------------
SYS_SEG = ("你是资深游戏实况剪辑师。你按话题段落工作："
           "剪完一个话题就回头检查一遍。只按格式输出。")

DECIDE = """这一段（{cur}~{end} 秒，共 {n} 句）的台词在下面。{goal}

剪辑力度：{level_hint}
挑出该留的行号：
只输出行号，一行一个。

台词：
{lines}

只输出行号。"""

REVIEW = """你刚剪的这一段，保留的行号是：{picked}

这一段所属章节的目标：{goal}
当前剪辑力度：{level_hint}

回头检查：
1. 有没有把一句话剪断（保留的行前后本来连着）
2. 有没有漏掉关键步骤、或留下啰嗦重复
3. 剪出来的内容能不能独立看懂，并符合当前力度的剧情完整度

没问题就只输出：OK
有问题就逐行输出修改，补回格式：ADD 12；删除格式：DROP 12

台词：
{lines}"""


def _windows(data, min_sec=35.0, max_sec=150.0):
    """按话题断点切工作单元：语义段落优先，太长/太短时兜底。"""
    segs = data.get("segments") or []
    sem = data.get("semantic") or []
    if not segs:
        return []
    wins = []
    start_i = 0
    for i, sg in enumerate(segs):
        a0 = float(segs[start_i][0])
        span = float(sg[1]) - a0
        brk = bool(sem[i].get("para_break")) if i < len(sem) else False
        last = (i == len(segs) - 1)
        if last or (brk and span >= min_sec) or span >= max_sec:
            wins.append((start_i, i))
            start_i = i + 1
    return wins


LEVEL_HINT = {
    "light": "轻度粗剪：保留完整上下文，只删明显重复、卡壳和纯等待；宁可多留",
    "medium": "中度：保留各章目标、主要过程、结果和过渡，删除重复铺垫与弱支线",
    "heavy": "重度：保留最短完整故事链（目标→关键过程→结果/评价→过渡），"
             "优先删重复、等待和支线，不能为了更短删掉因果步骤",
}


def _level_hint(level=None):
    """优先使用调用者显式力度；旧命令行调用才回退读取设置。"""
    if level in LEVEL_HINT:
        return LEVEL_HINT[level]
    try:
        import json
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "conf", "settings.json")
        if os.path.isfile(p):
            lv = str(json.load(open(p, encoding="utf-8"))
                     .get("cut_level", "light"))
            return LEVEL_HINT.get(lv, LEVEL_HINT["light"])
    except Exception:  # noqa: BLE001
        pass
    return LEVEL_HINT["light"]


def _goal_for(script, t):
    for sec in (script.get("sections") or []):
        if float(sec.get("start", 0)) <= t <= float(sec.get("end", 0)):
            return "本章目标：%s（%s）" % (sec.get("title", ""), sec.get("advice", ""))
    ml = script.get("mainline") or ""
    return ("整期主线：%s" % ml) if ml else ""


def _spans_from_keeps(segs, keeps, bridge_gap=1.6, min_span=1.5):
    """把保留句转成片段，只桥接自然停顿，不复活已拒绝的内容。"""
    out, cur = [], None
    for i, keep in enumerate(keeps):
        if not keep or i >= len(segs):
            continue
        a, b = float(segs[i][0]), float(segs[i][1])
        if cur and a - cur[1] <= bridge_gap:
            cur[1] = max(cur[1], b)
        else:
            if cur:
                out.append(tuple(cur))
            cur = [a, b]
    if cur:
        out.append(tuple(cur))
    return [(a, b) for a, b in out if b - a >= min_span]


def plan_by_segment(data, on_log=print, use_review=True, min_sec=35.0, max_sec=150.0,
                    bridge_gap=1.6, level="medium", mode="gameplay",
                    target_seconds=None):
    """按话题段落剪 + 每段自查，返回 [(start, end)]。"""
    import re
    import proofread as pf
    segs = data.get("segments") or []
    wins = _windows(data, min_sec, max_sec)
    if not wins:
        return []
    script = data.get("script") or {}
    level = level if level in LEVEL_HINT else "medium"
    level_hint = _level_hint(level)
    if target_seconds and target_seconds > 0:
        level_hint += "；整片软目标约 %.1f 分钟，剧情链完整优先于精确命中时长" % (
            float(target_seconds) / 60.0)
    keeps = [False] * len(segs)
    with pf.Server(task="segment", port=8134, api_role="director") as srv:
        for k, (i0, i1) in enumerate(wins, 1):
            idxs = list(range(i0, i1 + 1))
            a1, b1 = float(segs[i0][0]), float(segs[i1][1])
            lines = "\n".join("%d. %s" % (j + 1, segs[i][2])
                              for j, i in enumerate(idxs))
            goal = _goal_for(script, a1)
            try:
                raw = srv.chat(SYS_SEG,
                               DECIDE.format(cur=int(a1), end=int(b1),
                                             n=len(idxs), goal=goal,
                                              level_hint=level_hint,
                                             lines=lines),
                               max_tokens=500)
            except Exception as exc:  # noqa: BLE001
                from llm_gateway import propagate_api_error
                propagate_api_error(exc)
                on_log("  第 %d 段(%d~%d 秒) 判断失败：%s" % (k, a1, b1, str(exc)[:50]))
                continue
            picked = [int(x) for x in re.findall(r"^\s*(\d{1,4})\s*$", raw or "", re.M)]
            picked = [x for x in picked if 1 <= x <= len(idxs)]
            for x in picked:
                keeps[idxs[x - 1]] = True

            if use_review and picked and goal:
                try:
                    raw2 = srv.chat(SYS_SEG,
                                    REVIEW.format(picked=",".join(map(str, picked)),
                                                  goal=goal,
                                                  level_hint=level_hint,
                                                  lines=lines),
                                    max_tokens=300)
                    adds, drops = _parse_review(raw2, len(idxs))
                    for x in adds:
                        keeps[idxs[x]] = True
                    for x in drops:
                        keeps[idxs[x]] = False
                    if adds or drops:
                        on_log("    复查补回 %d 句、删除 %d 句"
                               % (len(adds), len(drops)))
                except Exception as exc:  # noqa: BLE001
                    from llm_gateway import propagate_api_error
                    propagate_api_error(exc)
                    pass
            on_log("  第 %d/%d 段（%d~%d 秒）：留 %d/%d 句"
                   % (k, len(wins), a1, b1, len(picked), len(idxs)))

    # 自我介绍是"这期是谁、讲什么"的入口，观众全靠它认人。
    # 模型按信息量打分时它往往很低（都是客套话），所以这里绕过模型强制保留，
    # 而且要把整段留住（后续 1~2 句一起），别只留半句。
    try:
        import autocut as _ac
        # 先让模型判断（认得出各种说法），失败再退回关键词
        _intro = _ac.find_intro_ai(data, on_log=on_log)
        if _intro is None:
            _intro = _ac.find_intro(data)
        if _intro:
            kept_n = 0
            for ia, ib in _intro:
                for i, sg in enumerate(segs):
                    mid = (float(sg[0]) + float(sg[1])) / 2
                    if ia <= mid <= ib:
                        keeps[i] = True
                        kept_n += 1
            on_log("  片头：自我介绍 %d 句强制保留（%.0f~%.0f 秒）"
                   % (kept_n, _intro[0][0], _intro[0][1]))
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        on_log("  片头识别跳过：%s" % str(exc)[:50])

    if not any(keeps):
        return []
    return _spans_from_keeps(segs, keeps, bridge_gap=bridge_gap)
