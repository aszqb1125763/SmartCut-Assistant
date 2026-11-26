# -*- coding: utf-8 -*-
"""体裁模板：游戏实况怎么剪（从子枫自己的成片里反推出来的）。

实测两期（第 11 期 33.5→14.7 分、第 10 期 60.1→14.1 分）：
  · 历史样本多在 14~15 分钟，但这只是参考，不是硬时长目标
  · 每段 **中位 11 秒**，最长 50 秒左右（一整套操作才不切），极短段很少
  · 删掉的空档很大：中位 5~15 秒，最长 137 秒 / 510 秒 —— 大段发呆、来回跑全砍
  · **每章都留一点**：6 个章节保留率 31/59/46/51/30/58%（第 11 期），
    26/28/14/40/12%（第 10 期）——没有哪一章被整章丢掉，最差的章也留一成多
  · 片头从第一句招呼开始（跳过前面 16.7 秒的空白/准备画面），第一段 6~30 秒
  · 结尾提前 30~285 秒收（不讲废话、不留黑屏）
  · **88~89% 的刀落在 ≥0.35 秒的停顿上**

所以"游戏实况模式"= 随素材复杂度浮动的软配额 + 按章分配（每章保底两成）+ 章内按分数挑连续块
+ 保留片头招呼 + 结尾提前收 + 切在停顿上。
"""
from __future__ import annotations

import numpy as np

PROFILES = {
    "gameplay": {
        "name": "游戏实况",
        # 子枫给的真实结构：0~20 秒系列高潮（冷开场）→ 20~40 秒自我介绍 → 正片到 15 分钟
        "teaser_sec": 18.0,          # 冷开场高潮长度
        "intro_sec": 20.0,           # 自我介绍长度（从第一句招呼起算）
        "target_min": 15.0,          # 旧配置兼容；实际预算由原片与章节动态计算
        "target_ratio": 0.42,
        "target_floor_min": 10.0,
        "target_cap_min": 28.0,
        "chapter_bonus_sec": 20.0,
        "story_overflow": 1.12,      # 为补全目标/结果/收尾允许适度超出软预算
        "chapter_floor": 0.22,       # 每章至少保留这个比例
        "chapter_cap": 0.55,         # 单章最多保留这个比例（避免一头沉）
        "span_target": 11.0,         # 单段目标长度（秒）
        "span_max": 50.0,            # 单段上限：一整套操作
        "span_split": 40.0,          # 超过这个长度才切（实测 40 秒最接近手剪：54 段/中位 11.7）
        "span_min": 4.0,             # 太碎的不要
        "head_keep": 24.0,           # 片头保留秒数（招呼 + 本集目标）
        "tail_drop": 0.0,            # 结尾允许直接黑屏：不额外收尾
    },
    "tutorial": {
        "name": "教程 / 整合包推荐",
        "target_min": 10.0,
        "target_band": (9.0, 12.0),
        "chapter_floor": 0.35,
        "chapter_cap": 0.80,
        "span_target": 14.0,
        "span_max": 60.0,
        "span_min": 5.0,
        "head_keep": 20.0,
        "tail_drop": 20.0,
    },
}


def profile(name=None):
    return PROFILES.get(name or "gameplay", PROFILES["gameplay"])


def suggest_budget(data, name="gameplay", level="medium"):
    """按原片长度和章节复杂度给软预算；剧情骨架优先于精确时长。"""
    prof = dict(profile(name))
    duration = float(data.get("duration") or 0.0)
    sections = (data.get("script") or {}).get("sections") or []
    ratio = float(prof.get("target_ratio", 0.0) or 0.0)
    floor_min = float(prof.get("target_floor_min", prof.get("target_min", 10.0)))
    cap_min = float(prof.get("target_cap_min", max(floor_min, prof.get("target_min", 10.0))))
    bonus = max(0, len(sections) - 4) * float(prof.get("chapter_bonus_sec", 0.0))
    if level == "heavy":
        ratio *= 0.64
        floor_min *= 0.68
        cap_min *= 0.58
        bonus *= 0.55
    elif level == "light":
        ratio = max(0.72, ratio * 1.65)
        floor_min *= 1.35
        cap_min *= 1.55
        bonus *= 1.20
    if duration <= 0 or ratio <= 0:
        return float(prof.get("target_min", 10.0)) * 60.0
    wanted = duration * ratio + bonus
    return max(floor_min * 60.0, min(cap_min * 60.0, wanted))


def pick_teaser(data, scores, want_sec=18.0, min_start=60.0):
    """挑冷开场高潮：正片里评分最高的 15~20 秒连续块（优先"结果/成果"句）。"""
    segs = data.get("segments") or []
    if not segs:
        return None
    try:
        import stage as _stage
        labels = _stage.label_sentences(data)
    except Exception:  # noqa: BLE001
        labels = [""] * len(segs)
    best = None
    n = len(segs)
    for i in range(n):
        a = float(segs[i][0])
        if a < min_start:
            continue
        tot, j = 0.0, i
        while j < n and tot < want_sec:
            tot = float(segs[j][1]) - a
            j += 1
        if tot < want_sec * 0.7 or tot > want_sec * 1.8:
            continue
        b = float(segs[j - 1][1])
        inside = [k for k in range(i, j)]
        vals = [scores[k] if k < len(scores) else 0.5 for k in inside]
        bonus = 0.12 if any(labels[k] == "result" for k in inside) else 0.0
        sc = (sum(vals) / max(1, len(vals))) + bonus
        if best is None or sc > best[0]:
            best = (sc, a, b)
    return (best[1], best[2]) if best else None


def _sent_spans(data):
    return [(float(s[0]), float(s[1])) for s in (data.get("segments") or [])]


_PROCESS_WORDS = ("先", "然后", "接着", "再", "开始", "需要", "找到", "拿到",
                  "放上", "装上", "安装", "连接", "合成", "制作", "建造", "搭建",
                  "测试", "调整", "升级", "进入", "前往", "解决", "操作", "尝试",
                  "打掉", "打开", "关闭", "准备好")
_TRANSITION_WORDS = ("接下来", "然后我们", "下一步", "下一个", "回去", "继续",
                     "转到", "进入下一", "换到")
_RETAKE_WORDS = ("重来", "再来一次", "重新来", "刚才不算", "说错了", "不对不对",
                 "录错", "白录", "重新说", "从头来")


def _story_optional(section):
    """剧本明确标成可大幅删减时，重度允许整章省略。"""
    advice = str((section or {}).get("advice") or "").lower().replace(" ", "")
    return any(word in advice for word in (
        "可以大幅删减", "可大幅删减", "可以删", "可删", "省略", "略过",
        "optional", "dropchapter",
    ))


def _story_sections(data):
    """返回可用于章节级叙事保护的分段；没有剧本时按约五分钟切段。"""
    sections = (data.get("script") or {}).get("sections") or []
    if sections:
        return sections
    dur = float(data.get("duration") or 0.0)
    if dur <= 0:
        return []
    n = max(1, int(round(dur / 300.0)))
    return [{"start": dur * k / n, "end": dur * (k + 1) / n,
             "title": "第%d段" % (k + 1)} for k in range(n)]


def story_spine_indices(data, level="medium"):
    """生成章节级故事链：开场/局部目标 → 关键过程 → 结果 → 过渡。

    旧逻辑只认识全片开头的 goal 和全片结尾的 tease，后续章节在重度模式下
    容易只剩高分结果句。这里按每章的时间位置和局部关键词选锚点，分数只用于
    普通内容排序，不能决定一个故事阶段是否存在。
    """
    segs = data.get("segments") or []
    if not segs:
        return set()
    try:
        import stage as _stage
        labels = _stage.label_sentences(data)
        goal_words = tuple(getattr(_stage, "GOAL_WORDS", ()))
        result_words = tuple(getattr(_stage, "RESULT_WORDS", ()))
        tease_words = tuple(getattr(_stage, "TEASE_WORDS", ()))
    except Exception:  # noqa: BLE001
        labels = ["progress"] * len(segs)
        goal_words = result_words = tease_words = ()
    try:
        scores, _score_source = _scores(data)
    except Exception:  # noqa: BLE001
        scores = [0.5] * len(segs)

    def text_of(i):
        return "".join(str(segs[i][2] or "").split())

    def meaningful(i):
        text = text_of(i)
        return len(text) > 2 and (i >= len(labels) or labels[i] != "other")

    def contains(i, words):
        text = text_of(i)
        return bool(text) and any(word in text for word in words)

    def retake(i):
        return contains(i, _RETAKE_WORDS)

    protected = set()
    # 全片级骨架继续保留，但章节级骨架不再依赖这些标签是否命中。
    for i, lab in enumerate(labels):
        if lab in ("hook", "goal", "result", "tease"):
            protected.add(i)
            # 目标需要后一句展开，结果需要前一句交代过程；只带一侧避免保护区膨胀。
            if lab in ("hook", "goal") and i + 1 < len(segs) and meaningful(i + 1):
                protected.add(i + 1)
            elif lab == "tease" and i > 0 and meaningful(i - 1):
                protected.add(i - 1)

    for sec in _story_sections(data):
        if level == "heavy" and _story_optional(sec):
            continue
        a = float(sec.get("start", 0.0))
        b = float(sec.get("end", 0.0))
        if b - a < 2.0:
            continue
        idx = [i for i, seg in enumerate(segs)
               if a <= float(seg[0]) < b and meaningful(i)]
        if not idx:
            continue
        pos = {i: max(0.0, min(1.0, (float(segs[i][0]) - a) / (b - a)))
               for i in idx}
        anchors = []
        long_chapter = (b - a >= 90.0 and len(idx) >= 9)

        # 章节开场和局部目标。目标词即使出现在全片 150 秒以后也必须生效。
        anchors.append(idx[0])
        if long_chapter and len(idx) > 1:
            anchors.append(idx[1])
        local_goals = [i for i in idx if pos[i] <= 0.45 and contains(i, goal_words)]
        if local_goals:
            anchors.append(local_goals[0])

        # 长章节前、后半段各留一个关键操作，短章节只留一个中心操作。
        # 位置优先于模型分，避免高分支线把因果链中间步骤挤掉。
        process = [i for i in idx if 0.20 <= pos[i] <= 0.78
                   and not retake(i)
                   and not contains(i, tease_words + _TRANSITION_WORDS)
                   and (i >= len(labels) or labels[i] not in ("result", "tease"))]
        if process:
            buckets = []
            if long_chapter and len(process) >= 4:
                buckets = [[i for i in process if pos[i] < 0.5],
                           [i for i in process if pos[i] >= 0.5]]
            else:
                buckets = [process]
            for bucket in buckets:
                if not bucket:
                    continue
                target = 0.35 if bucket is buckets[0] and len(buckets) > 1 else (
                    0.65 if len(buckets) > 1 else 0.50)
                anchors.append(max(
                    bucket,
                    key=lambda i: (1 if contains(i, _PROCESS_WORDS) else 0,
                                   -abs(pos[i] - target),
                                   float(scores[i]) if i < len(scores) else 0.5)))

        # 优先显式成果；没有结果词时，也保留后段最接近收束位置的一句。
        results = [i for i in idx if (i < len(labels) and labels[i] == "result")
                   or contains(i, result_words)]
        if results:
            anchors.append(results[-1])
        else:
            tail = [i for i in idx if pos[i] >= 0.60 and not retake(i)]
            if tail:
                anchors.append(min(tail, key=lambda i: abs(pos[i] - 0.82)))

        # 章节末保留承接；明确过渡优先，否则保留最后一句避免突然跳章。
        transitions = [i for i in idx if contains(
            i, tease_words + _TRANSITION_WORDS) and not retake(i)]
        clean_tail = [i for i in idx if not retake(i)]
        anchors.append(transitions[-1] if transitions else clean_tail[-1])

        order = {i: p for p, i in enumerate(idx)}
        for anchor in dict.fromkeys(anchors):
            protected.add(anchor)
            p = order[anchor]
            # 长章节锚点带一条上下文；短章节保持最短完整链，避免几乎全保留。
            if long_chapter and anchor == idx[0] and p + 1 < len(idx):
                protected.add(idx[p + 1])
            elif long_chapter and p > 0:
                protected.add(idx[p - 1])

    return protected


def chapter_story_chain_indices(data, scores=None, labels=None, level="medium"):
    """统一名称：供导演审稿和预算阶段复用同一套章节故事链。"""
    # scores/labels 参数为兼容调用者预计算结果保留；当前实现内部统一计算，
    # 避免不同调用阶段传入不同来源后又出现多套保护标准。
    return story_spine_indices(data, level=level)


def span_has_story(data, a, b, protected=None, level="medium"):
    """片段是否覆盖章节故事链锚点，供所有预算阶段统一判断。"""
    segs = data.get("segments") or []
    protected = protected if protected is not None else story_spine_indices(data, level)
    return any(i < len(segs) and a <= float(segs[i][0]) < b for i in protected)


def allocate(data, conf, level="medium", name="gameplay", budget_sec=None):
    """按体裁模板分配时间：返回 [(start, end)]。

    步骤：
      1. 章节切分（没有剧本就按时间均分）
      2. 每章先保底 chapter_floor
      3. 剩余配额按章节分数加权分配
      4. 章内按句子分数从高到低挑，挑选结果合并成连续块（相邻句间隔 ≤2.5 秒）
      5. 片头招呼单独保留；结尾按 tail_drop 收
    """
    prof = dict(profile(name))
    # 三档必须在参数上就分开：重度仍明显更短，但不能靠删空章节过程来压时长。
    if level == "heavy":
        prof.update({"target_min": 10.0, "chapter_floor": 0.18,
                     "chapter_cap": 0.50, "span_split": 32.0})
    elif level == "light":
        prof.update({"target_min": prof["target_min"] * 1.6,
                     "chapter_floor": 0.55, "chapter_cap": 0.95})
    segs = data.get("segments") or []
    if not segs:
        return []
    dur = float(data.get("duration") or 0)
    scores, _src = _scores(data)
    sections = _story_sections(data)

    budget = float(budget_sec) if budget_sec is not None else suggest_budget(
        data, name=name, level=level)
    # 短素材：配额不能超过原片本身，而且三档要各自分开
    #   medium：原片 78%（别把 9 分钟的片子只砍 10%）
    #   heavy ：原片 55% 且不超过说话时长的 55%；故事链不足时允许软超出
    if dur and dur < budget * 1.15:
        if level == "heavy":
            _spoken = sum(float(s[1]) - float(s[0]) for s in segs)
            budget = min(budget, dur * 0.55, (_spoken or dur) * 0.55)
        else:
            # 很短的片子（<5 分钟）中度留 70%，否则三档会挤在一起
            budget = min(budget, dur * 0.70 if dur < 300 else dur * 0.78)
    if level == "light":
        budget = max(budget, dur * 0.75)
    budget_limit = budget * float(prof.get("story_overflow", 1.0))

    # 每章：候选句、分数、时长
    chap = []
    for k, sec in enumerate(sections):
        a, b = float(sec.get("start", 0)), float(sec.get("end", 0))
        if b - a < 2:
            continue
        idx = [i for i, s in enumerate(segs) if a <= float(s[0]) < b]
        if not idx:
            continue
        vals = np.array([scores[i] if i < len(scores) else 0.5 for i in idx])
        chap.append({"k": k, "a": a, "b": b, "idx": idx, "vals": vals,
                     "dur": b - a, "score": float(vals.mean()) if len(vals) else 0.5,
                     "title": str(sec.get("title") or "")})
    if not chap:
        return []

    # 配额：先保底，再按分数分配剩余
    quota = {}
    floor_sum = 0.0
    for c in chap:
        q = min(c["dur"] * prof["chapter_floor"], c["dur"])
        quota[id(c)] = q
        floor_sum += q
    remain = max(0.0, budget - floor_sum)
    # 分数权重压平：子枫是“每章都留一点”，不是“好章留满、差章丢光”
    wsum = sum(max(0.05, c["score"]) ** 0.6 for c in chap)
    for c in chap:
        share = remain * (max(0.05, c["score"]) ** 0.6 / wsum) if wsum else 0.0
        cap = c["dur"] * prof["chapter_cap"]
        quota[id(c)] = min(cap, quota[id(c)] + share)

    # 同一套章节故事链会贯穿分配、补骨架和最终预算复核。
    spine_idx = story_spine_indices(data, level=level)

    # 章内挑句：先放骨架，再按分数降序凑到配额
    chosen = []
    for c in chap:
        order = np.argsort(-c["vals"])
        got, take = 0.0, []
        for i in c["idx"]:
            if i in spine_idx:
                take.append(i)
                got += float(segs[i][1]) - float(segs[i][0])
        for pos in order:
            i = c["idx"][int(pos)]
            if i in spine_idx:
                continue
            d = float(segs[i][1]) - float(segs[i][0])
            if got + d > quota[id(c)] + 1.0:
                continue
            take.append(i)
            got += d
        take.sort()
        chosen += [(float(segs[i][0]), float(segs[i][1])) for i in take]

    chosen.sort()
    # 合并成连续块（间隔 ≤2.5 秒算连着）
    blocks = []
    for a, b in chosen:
        if blocks and a - blocks[-1][1] <= 3.0:
            blocks[-1][1] = max(blocks[-1][1], b)
        else:
            blocks.append([a, b])

    # 片头：从第一句开始保留 head_keep 秒（招呼 + 本集目标）
    first = float(segs[0][0])
    head_end = first + prof["head_keep"]
    merged = []
    for a, b in blocks:
        merged.append([a, b])
    if not merged or merged[0][0] > first + 1.0:
        merged.insert(0, [first, min(head_end, dur)])
    else:
        merged[0][0] = min(merged[0][0], first)
        merged[0][1] = max(merged[0][1], min(head_end, dur))

    # 结尾提前收
    if merged and merged[-1][1] > dur - prof["tail_drop"]:
        merged[-1][1] = max(merged[-1][0] + 2.0, dur - prof["tail_drop"])

    # 合并会把间隔填回来，容易超预算：按分数从低到高整块砍，直到不超
    def _block_score(a, b):
        vals = [scores[i] if i < len(scores) else 0.5 for i, s in enumerate(segs)
                if a <= float(s[0]) < b]
        return float(np.mean(vals)) if vals else 0.5
    def _has_spine(a, b):
        return span_has_story(data, a, b, spine_idx, level=level)

    while sum(x - y for y, x in merged) > budget_limit and len(merged) > 1:
        # 骨架块一律不砍：宁可超一点预算，也不把剧情骨架剪断
        cand = [(_block_score(a, b), k) for k, (a, b) in enumerate(merged)
                if not _has_spine(a, b)]
        if not cand:
            break
        cand.sort()
        merged.pop(cand[0][1])
    merged.sort()

    # 去掉太碎的块
    out = [(a, b) for a, b in merged if b - a >= 1.0]
    if not out:
        return out

    # ---- 组装成片子枫的真实结构：高潮 → 自我介绍 → 正片 ----
    teaser = pick_teaser(data, scores, want_sec=prof.get("teaser_sec", 18.0))
    first = float(segs[0][0])
    intro_end = first + float(prof.get("intro_sec", 20.0))
    intro = []
    rest = []
    for a, b in out:
        if a < intro_end:
            intro.append([a, min(b, intro_end)])
            if b > intro_end:
                rest.append([intro_end, b])
        else:
            rest.append([a, b])
    if not intro:
        intro = [[first, min(intro_end, dur)]]
    ordered = []
    if teaser:
        ordered.append(teaser)
    ordered += [(a, b) for a, b in intro if b - a >= 0.5]
    ordered += [(a, b) for a, b in rest if b - a >= 0.5]

    # 补骨架：不管预算怎么变，开场/目标/结果/预告不能丢
    try:
        ordered = ensure_spine(data, ordered, budget_limit, level=level)
    except Exception:  # noqa: BLE001
        pass

    # 章内节奏（第一遍：吸附前先切碎，避免被后面的合并逻辑并回大块）
    try:
        import rhythm as _rhythm
        _head = ordered[:1] if ordered else []
        _mid = ordered[1:]
        _split = _rhythm.split_blocks(data, _mid,
                                      target=float(prof.get("span_target", 11.0)),
                                      max_len=float(prof.get("span_split", 40.0)))
        ordered = _head + _split
    except Exception:  # noqa: BLE001
        pass
    return ordered


def _scores(data):
    try:
        import autocut
        return autocut.scores_of(data)
    except Exception:  # noqa: BLE001
        n = len(data.get("segments") or [])
        return [0.5] * n, "fallback"


def ensure_spine(data, spans, budget, level="medium"):
    """保证每章故事链不丢；预算是软目标，只允许裁掉非故事链内容。"""
    segs = data.get("segments") or []
    protected = story_spine_indices(data, level=level)
    if not protected or not spans:
        return spans
    spans = [[float(a), float(b)] for a, b in spans]

    def covered(t):
        return any(a <= t <= b for a, b in spans)

    added = 0
    for i in sorted(protected):
        if i >= len(segs):
            continue
        t = float(segs[i][0])
        if covered(t):
            continue
        a = float(segs[i][0])
        b = float(segs[i][1])
        spans.append([a, b])
        added += 1
    if added:
        spans.sort()
        merged = []
        for a, b in spans:
            if merged and a - merged[-1][1] <= 1.2:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        spans = merged

    # 超预算：只裁非骨架块（从低分开始）
    def has_spine(a, b):
        return span_has_story(data, a, b, protected, level=level)

    try:
        import autocut as _ac
        scores, _src = _ac.scores_of(data)
    except Exception:  # noqa: BLE001
        scores = [0.5] * len(segs)

    def blk_score(a, b):
        vals = [scores[i] for i, sg in enumerate(segs)
                if a <= float(sg[0]) < b and i < len(scores)]
        return sum(vals) / len(vals) if vals else 0.5

    guard = 0
    while sum(b - a for a, b in spans) > budget and len(spans) > 1 and guard < 400:
        cand = [(blk_score(a, b), k) for k, (a, b) in enumerate(spans)
                if not has_spine(a, b)]
        if not cand:
            break
        cand.sort()
        spans.pop(cand[0][1])
        guard += 1
    return [(a, b) for a, b in spans]


def describe(data, spans, name="gameplay", level="medium"):
    """给日志用：报成片时长、段数、每章保留率。"""
    prof = profile(name)
    dur = float(data.get("duration") or 0)
    kept = sum(b - a for a, b in spans)
    lens = [b - a for a, b in spans]
    reference = suggest_budget(data, name=name, level=level) / 60.0
    lines = ["体裁=%s 软预算约 %.1f 分钟（剧情完整优先）→ 实得 %.1f 分钟（原片 %.1f 分，保留 %.0f%%），%d 段"
             % (prof["name"], reference, kept / 60, dur / 60,
                kept / dur * 100 if dur else 0, len(spans))]
    if spans:
        lines.append("  结构：冷开场高潮 %.1f 秒（原片 %.1f~%.1f）→ 自我介绍 %.1f 秒 → 正片"
                     % (spans[0][1] - spans[0][0], spans[0][0], spans[0][1],
                        spans[1][1] - spans[1][0] if len(spans) > 1 else 0))
    if lens:
        lines.append("  段长：中位 %.1f 秒 / 最长 %.1f 秒" % (float(np.median(lens)), max(lens)))
    for k, sec in enumerate((data.get("script") or {}).get("sections") or [], 1):
        a, b = float(sec.get("start", 0)), float(sec.get("end", 0))
        if b - a < 2:
            continue
        got = sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)
        lines.append("    第 %d 章保留 %3.0f%%  %s"
                     % (k, got / (b - a) * 100, str(sec.get("title") or "")[:26]))
    return "\n".join(lines)
