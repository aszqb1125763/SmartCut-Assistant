# -*- coding: utf-8 -*-
"""候选粗剪的全片导演审稿器（生产链：初剪后审片并局部返工）。

逐句价值模型回答“这一句像不像应该保留”，本模块回答另一类问题：
候选成片作为一整期游戏实况是否讲得通。它检查 hook / 目标 / 推进 /
解释 / 结果 / 收尾、章节覆盖、刀口连贯、重复、跑题和失败重录，并把问题
转换为可执行的增删反馈。时长是按力度设定的软目标；剧情链完整优先，
但只要还有非故事内容可删，导演就会继续收回预算。
"""
from __future__ import annotations

import math
import re

import genre
import llm_gateway
import source_timeline
import stage


HOOK_WORDS = tuple(stage.GREET) + ("这期", "本期", "今天")
GOAL_WORDS = tuple(stage.GOAL_WORDS) + ("先做", "准备做", "要弄", "要造")
PROGRESS_WORDS = ("接下来", "下一步", "首先", "然后", "开始", "放进去", "拿出来",
                  "连接", "合成", "制作", "搭建", "安装", "升级", "测试", "调整")
EXPLAIN_WORDS = ("因为", "所以", "原理", "原因", "也就是说", "意思是", "需要",
                 "必须", "用来", "这样可以", "这样就", "配方", "步骤", "方法",
                 "先把", "再把", "如果", "相当于", "区别")
RESULT_WORDS = tuple(stage.RESULT_WORDS) + ("终于", "获得", "解锁", "达成", "完成了")
CLOSING_WORDS = tuple(stage.TEASE_WORDS) + ("感谢观看", "这期就", "本期就", "到这里",
                                               "结束", "总结", "一键三连")
LOST_WORDS = ("在哪", "找不到", "找一下", "找找", "等一下", "等等", "看一下",
              "翻一下", "忘了", "记不清", "想不起来", "不知道放哪")
RETAKE_WORDS = ("重来", "再来一次", "重新来", "刚才不算", "说错了", "不对不对",
                "录错", "白录", "重新说", "从头来")
FILLER_WORDS = ("嗯", "啊", "呃", "额", "那个", "这个", "就是", "然后", "其实")
CONNECTOR_START = ("然后", "所以", "但是", "而且", "因为", "不过", "还有", "接着")
UNFINISHED_END = ("然后", "所以", "因为", "但是", "而且", "就是", "那个", "这个",
                  "的话", "还有", "接下来", "比如")

# 文字承接只在可安全补回的局部范围内成立。更长的停顿通常是游戏操作、
# 菜单或自然段落边界，不能让导演生成一个永远无法执行的“补上一句”任务。
BOUNDARY_GAP = 1.6
CONTEXT_MAX_GAP = 5.0

STAGE_ORDER = ("hook", "goal", "progress", "explain", "result", "closing")
STAGE_WEIGHTS = {
    "hook": 0.12,
    "goal": 0.17,
    "progress": 0.18,
    "explain": 0.13,
    "result": 0.23,
    "closing": 0.17,
}

LEVEL_POLICY = {
    "light": {"overflow": 0.25, "pass_score": 72.0,
              "story_min": 0.90, "continuity_min": 0.92},
    "medium": {"overflow": 0.12, "pass_score": 74.0,
               "story_min": 0.95, "continuity_min": 0.94},
    "heavy": {"overflow": 0.20, "pass_score": 74.0,
              "story_min": 0.98, "continuity_min": 0.94},
}


def _level_policy(level):
    return LEVEL_POLICY.get(level, LEVEL_POLICY["medium"])


def _norm(text):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or "")).lower()


def _grams(text, n=3):
    text = _norm(text)
    if len(text) < n:
        return set()
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def similarity(a, b):
    aa, bb = _grams(a), _grams(b)
    if not aa or not bb:
        return 0.0
    return 2.0 * len(aa & bb) / (len(aa) + len(bb))


def _has(text, words):
    clean = _norm(text)
    return any(_norm(word) in clean for word in words)


def classify_stages(data):
    """给每句一个全片结构角色；优先标记稀缺的首尾和结果角色。"""
    segs = data.get("segments") or []
    if not segs:
        return []
    duration = float(data.get("duration") or segs[-1][1] or 1.0)
    base = stage.label_sentences(data)
    labels = []
    for index, (a, _b, text) in enumerate(segs):
        a = float(a)
        clean = _norm(text)
        base_label = base[index] if index < len(base) else "progress"
        if (a <= min(75.0, duration * 0.08)
                and (base_label == "hook" or _has(clean, HOOK_WORDS))):
            labels.append("hook")
        elif (a <= min(210.0, duration * 0.18)
              and (base_label == "goal" or _has(clean, GOAL_WORDS))):
            labels.append("goal")
        elif (a >= max(0.0, duration - 210.0)
              and (base_label == "tease" or _has(clean, CLOSING_WORDS))):
            labels.append("closing")
        elif base_label == "result" or _has(clean, RESULT_WORDS):
            labels.append("result")
        elif len(clean) >= 6 and _has(clean, EXPLAIN_WORDS):
            labels.append("explain")
        else:
            labels.append("progress")
    return labels


def _scores_of(data, sentence_scores=None):
    segs = data.get("segments") or []
    if llm_gateway.api_enabled():
        return [0.5] * len(segs)
    source = sentence_scores if sentence_scores is not None else data.get("scores")
    if source is not None and len(source) == len(segs):
        return [max(0.0, min(1.0, float(value))) for value in source]
    out = []
    for a, b, text in segs:
        clean = _norm(text)
        duration = max(0.3, float(b) - float(a))
        value = 0.42
        value += 0.16 if 5 <= len(clean) <= 42 else -0.08
        value += 0.10 if 1.5 <= duration <= 10 else -0.05
        value += 0.13 if _has(clean, RESULT_WORDS + GOAL_WORDS) else 0.0
        value += 0.08 if _has(clean, EXPLAIN_WORDS + PROGRESS_WORDS) else 0.0
        value -= 0.20 if _has(clean, LOST_WORDS + RETAKE_WORDS) else 0.0
        out.append(max(0.0, min(1.0, value)))
    return out


def mask_from_spans(data, spans, overlap_ratio=0.5):
    mask = []
    for a, b, _text in data.get("segments") or []:
        a, b = float(a), float(b)
        length = max(0.2, b - a)
        overlap = sum(max(0.0, min(b, float(y)) - max(a, float(x)))
                      for x, y in spans)
        mask.append(overlap / length >= overlap_ratio)
    return mask


def spans_from_mask(data, mask, bridge_gap=1.6, min_span=0.7):
    segs = data.get("segments") or []
    out = []
    current = None
    previous_index = None
    for index, keep in enumerate(mask):
        if not keep or index >= len(segs):
            continue
        a, b = float(segs[index][0]), float(segs[index][1])
        # 只有相邻的保留句才允许桥接。若中间有明确拒绝的句子，即使它很短，
        # 也必须保持切口，否则 span 回算时会把该句重新判为保留。
        if (current and previous_index is not None and index == previous_index + 1
                and a - current[1] <= bridge_gap):
            current[1] = max(current[1], b)
        else:
            if current and current[1] - current[0] >= min_span:
                out.append(tuple(current))
            current = [a, b]
        previous_index = index
    if current and current[1] - current[0] >= min_span:
        out.append(tuple(current))
    return out


def _chapter_ranges(data):
    sections = (data.get("script") or {}).get("sections") or []
    if sections:
        return [(float(sec.get("start", 0)), float(sec.get("end", 0)),
                 str(sec.get("title") or "未命名章节"),
                 str(sec.get("advice") or "")) for sec in sections]
    duration = float(data.get("duration") or 0)
    if duration <= 0:
        return []
    count = 5
    return [(duration * i / count, duration * (i + 1) / count,
             "时间段%d" % (i + 1), "")
            for i in range(count)]


def _indices_in_range(segs, start, end):
    return [i for i, segment in enumerate(segs)
            if start <= (float(segment[0]) + float(segment[1])) / 2.0 <= end]


def _filler_density(text):
    clean = _norm(text)
    count = sum(clean.count(_norm(word)) for word in FILLER_WORDS)
    return count / max(1.0, len(clean) / 6.0)


def _issue(kind, severity, message, indices=None, action="inspect", issue_key=None):
    row = {
        "type": kind,
        "severity": severity,
        "message": message,
        "indices": list(indices or []),
        "action": action,
    }
    if issue_key is not None:
        row["issue_key"] = str(issue_key)
    return row


def review(data, spans, sentence_scores=None, target_seconds=None, level="medium"):
    """审查整条候选时间线并返回结构化问题和 0~100 分。"""
    level = level if level in LEVEL_POLICY else "medium"
    policy = _level_policy(level)
    segs = data.get("segments") or []
    scores = _scores_of(data, sentence_scores)
    use_local_scores = not llm_gateway.api_enabled()
    labels = classify_stages(data)
    mask = mask_from_spans(data, spans)
    picked = [i for i, value in enumerate(mask) if value]
    issues = []

    # Director findings must be attached to the source timeline, not merely to
    # a transcript-array position.  ASR normally emits chronological rows; an
    # out-of-order index is unsafe and is rejected rather than silently judged
    # as prose order.
    chronological = sorted(
        range(len(segs)),
        key=lambda index: (float(segs[index][0]), float(segs[index][1]), index))
    timeline_monotonic = chronological == list(range(len(segs)))
    if not timeline_monotonic:
        issues.append(_issue(
            "timeline_out_of_order", "critical",
            "逐句索引没有按绝对源时间排列，不能按文本数组顺序审片",
            chronological, "rebuild_timeline"))

    stage_rows = {}
    weighted_coverage = 0.0
    available_weight = 0.0
    for kind in STAGE_ORDER:
        source = [i for i, label in enumerate(labels) if label == kind]
        selected = [i for i in source if mask[i]]
        # progress 很多，不要求全留；稀缺角色至少命中一句，结果要求命中两句或 35%。
        if not source:
            required = 0
            if kind in ("hook", "goal", "result", "closing"):
                coverage = 0.0
                available_weight += STAGE_WEIGHTS[kind]
                severity = "info"
                issues.append(_issue(
                    "missing_source_stage", severity,
                    "原素材中未识别到%s，无法确认整片结构完整" % kind,
                    [], "inspect"))
            else:
                coverage = 1.0
        else:
            required = 2 if kind == "result" and len(source) >= 4 else 1
            if kind in ("progress", "explain", "result"):
                desired = max(required, int(math.ceil(len(source) *
                                                     {"progress": 0.16,
                                                      "explain": 0.22,
                                                      "result": 0.35}[kind])))
                coverage = min(1.0, len(selected) / max(1, desired))
            else:
                coverage = min(1.0, len(selected) / required)
            available_weight += STAGE_WEIGHTS[kind]
            weighted_coverage += STAGE_WEIGHTS[kind] * coverage
        stage_rows[kind] = {
            "source": len(source), "selected": len(selected),
            "required": required, "coverage": round(coverage, 4),
        }
        if source and len(selected) < required:
            severity = "critical" if kind in ("goal", "result", "closing") else "major"
            issues.append(_issue("missing_stage", severity,
                                 "缺少%s（素材中有 %d 句，候选命中 %d 句）"
                                 % (kind, len(source), len(selected)),
                                 source, "add_stage"))

    stage_score = weighted_coverage / available_weight if available_weight else 1.0

    story_required = genre.chapter_story_chain_indices(data, level=level)
    chapters = []
    chapter_scores = []
    for start, end, title, advice in _chapter_ranges(data):
        source = _indices_in_range(segs, start, end)
        selected = [i for i in source if mask[i]]
        coverage = len(selected) / max(1, len(source))
        required_story = [i for i in source if i in story_required]
        selected_story = [i for i in required_story if mask[i]]
        story_coverage = (len(selected_story) / len(required_story)
                          if required_story else (1.0 if selected else 0.0))
        optional = (level == "heavy" and genre._story_optional({"advice": advice}))
        chapters.append({"title": title, "start": start, "end": end,
                         "source": len(source), "selected": len(selected),
                          "retention": round(coverage, 4), "advice": advice,
                          "optional": optional,
                          "required_story": len(required_story),
                          "selected_story": len(selected_story),
                          "story_coverage": round(story_coverage, 4)})
        if not optional:
            chapter_scores.append(story_coverage)
        if required_story and len(selected_story) < len(required_story):
            missing = [i for i in required_story if not mask[i]]
            severity = "critical" if story_coverage < 0.55 else "major"
            issues.append(_issue(
                "story_chain_gap", severity,
                "章节故事链不完整：%s（%d/%d）" % (
                    title, len(selected_story), len(required_story)),
                missing, "add_story_chain",
                issue_key="text:story_chain:%.6f:%.6f" % (start, end)))
        if source and not selected and not optional:
            issues.append(_issue("chapter_gap", "critical", "整章缺失：%s" % title,
                                 source, "add_chapter"))
        elif source and coverage < 0.055 and not optional:
            issues.append(_issue("chapter_thin", "major", "章节过薄：%s（仅保留 %.1f%%）"
                                 % (title, coverage * 100), source, "add_chapter"))
    chapter_score = (sum(chapter_scores) / len(chapter_scores)) if chapter_scores else 1.0
    selected_story_all = sum(1 for i in story_required if i < len(mask) and mask[i])
    story_score = selected_story_all / max(1, len(story_required))

    bad_boundaries = 0
    boundary_count = 0
    def same_retained_span(left_index, right_index):
        left_end = float(segs[left_index][1])
        right_start = float(segs[right_index][0])
        return any(float(start) <= left_end + 0.08
                   and float(end) >= right_start - 0.08
                   for start, end in spans)

    for i in picked:
        text = _norm(segs[i][2])
        previous_gap = (float(segs[i][0]) - float(segs[i - 1][1])
                        if i > 0 else 0.0)
        next_gap = (float(segs[i + 1][0]) - float(segs[i][1])
                    if i + 1 < len(segs) else 0.0)
        starts_span = (i == 0 or not mask[i - 1]
                       or (BOUNDARY_GAP < previous_gap <= CONTEXT_MAX_GAP
                           and not same_retained_span(i - 1, i)))
        ends_span = (i == len(mask) - 1 or not mask[i + 1]
                     or (BOUNDARY_GAP < next_gap <= CONTEXT_MAX_GAP
                         and not same_retained_span(i, i + 1)))
        if starts_span:
            boundary_count += 1
            if (i > 0 and previous_gap <= CONTEXT_MAX_GAP
                    and (text.startswith(tuple(_norm(x) for x in CONNECTOR_START))
                         or _norm(segs[i - 1][2]).endswith(
                             tuple(_norm(x) for x in UNFINISHED_END)))):
                bad_boundaries += 1
                issues.append(_issue("abrupt_start", "major", "片段从承接词或半句话中间开始",
                                     [i - 1, i], "add_previous"))
        if ends_span:
            boundary_count += 1
            if (next_gap <= CONTEXT_MAX_GAP
                    and (text.endswith(tuple(_norm(x) for x in UNFINISHED_END))
                    or (i + 1 < len(segs)
                        and _norm(segs[i + 1][2]).startswith(
                            tuple(_norm(x) for x in CONNECTOR_START))))):
                bad_boundaries += 1
                issues.append(_issue("abrupt_end", "major", "片段在未说完或强承接处结束",
                                     [i, min(i + 1, len(segs) - 1)], "add_next"))
    continuity_score = 1.0 - bad_boundaries / max(1, boundary_count)

    duplicate_indices = []
    last_selected = []
    for i in picked:
        duplicate_of = None
        for j in last_selected[-45:]:
            if similarity(segs[i][2], segs[j][2]) >= 0.84:
                duplicate_of = j
                break
        if duplicate_of is not None and labels[i] not in ("hook", "goal", "result", "closing"):
            duplicate_indices.append(i if scores[i] <= scores[duplicate_of] else duplicate_of)
        last_selected.append(i)
    duplicate_indices = sorted(set(duplicate_indices))
    if duplicate_indices:
        issues.append(_issue("duplicate", "major", "候选中有 %d 句近重复内容" % len(duplicate_indices),
                             duplicate_indices, "drop"))
    unique_score = 1.0 - len(duplicate_indices) / max(1, len(picked))

    retake_indices = []
    offtopic_indices = []
    for i in picked:
        text = segs[i][2]
        protected = labels[i] in ("hook", "goal", "result", "closing")
        if not protected and _has(text, RETAKE_WORDS):
            retake_indices.append(i)
        elif (use_local_scores and not protected and scores[i] < 0.52
              and (_has(text, LOST_WORDS) or _filler_density(text) >= 0.75)):
            offtopic_indices.append(i)
    if retake_indices:
        issues.append(_issue("retake", "critical", "仍保留 %d 句失败重录信号" % len(retake_indices),
                             retake_indices, "drop"))
    if offtopic_indices:
        issues.append(_issue("offtopic", "major", "仍保留 %d 句找东西/低信息口水" % len(offtopic_indices),
                             offtopic_indices, "drop"))
    relevance_score = 1.0 - (len(retake_indices) + len(offtopic_indices)) / max(1, len(picked))

    kept_seconds = sum(float(b) - float(a) for a, b in spans)
    average_sentence_score = (sum(scores[i] for i in picked) / len(picked)
                              if picked else 0.0)
    budget_score = 1.0
    if target_seconds and target_seconds > 0:
        overflow = max(0.0, kept_seconds - float(target_seconds))
        allowance = float(target_seconds) * float(policy["overflow"])
        budget_score = max(0.0, 1.0 - overflow / max(1.0, allowance))
        if overflow > max(5.0, allowance):
            issues.append(_issue("over_budget", "major", "超过候选时长预算 %.1f 秒" % overflow,
                                 [], "trim_budget"))

    raw_score = 100.0 * (
        0.27 * stage_score + 0.18 * chapter_score + 0.20 * story_score
        + 0.15 * continuity_score + 0.08 * unique_score
        + 0.08 * relevance_score + 0.04 * budget_score
    )
    critical = sum(issue["severity"] == "critical" for issue in issues)
    major = sum(issue["severity"] == "major" for issue in issues)
    # 单项平均分很容易掩盖“有十几个小断口”的问题，因此对可执行问题额外扣分。
    # 扣分设上限，避免长视频仅因句子多而天然低分。
    issue_penalty = min(18.0, critical * 5.0 + major * 0.8)
    score = raw_score - issue_penalty
    within_budget = (not target_seconds or target_seconds <= 0
                     or kept_seconds <= float(target_seconds)
                     * (1.0 + float(policy["overflow"])) + 5.0)
    passed = (score >= float(policy["pass_score"]) and critical == 0 and major <= 8
              and story_score >= float(policy["story_min"])
              and chapter_score >= float(policy["story_min"])
              and continuity_score >= float(policy["continuity_min"])
              and within_budget)
    source_events = source_timeline.build_events(
        segs, duration=data.get("duration_exact", data.get("duration")))
    event_by_source = {
        row["source_index"]: row for row in source_events
        if row.get("kind") == "speech"
        and isinstance(row.get("source_index"), int)}
    for issue in issues:
        event_rows = [event_by_source[index]
                      for index in issue.get("indices") or []
                      if index in event_by_source]
        issue["event_ids"] = [row["event_id"] for row in event_rows]
        issue["coordinate_space"] = "source"
        if event_rows:
            issue["interval"] = [
                round(min(float(row["start"]) for row in event_rows), 3),
                round(max(float(row["end"]) for row in event_rows), 3),
            ]
    selected_event_ids = [event_by_source[index]["event_id"]
                          for index in picked if index in event_by_source]
    return {
        "score": round(max(0.0, min(100.0, score)), 3),
        "passed": passed,
        "selected_sentences": len(picked),
        "selected_seconds": round(kept_seconds, 3),
        "average_sentence_score": round(average_sentence_score, 4),
        "sentence_scores_source": "local" if use_local_scores else "api_neutral",
        "stage_score": round(stage_score, 4),
        "chapter_score": round(chapter_score, 4),
        "story_score": round(story_score, 4),
        "continuity_score": round(continuity_score, 4),
        "unique_score": round(unique_score, 4),
        "relevance_score": round(relevance_score, 4),
        "budget_score": round(budget_score, 4),
        "issue_penalty": round(issue_penalty, 3),
        "stages": stage_rows,
        "chapters": chapters,
        "issues": issues,
        "level": level,
        "timeline_schema": source_timeline.SCHEMA,
        "source_timeline_revision": source_timeline.revision(source_events),
        "timeline_monotonic": timeline_monotonic,
        "selected_event_ids": selected_event_ids,
    }


def _add_context(mask, index, segs, before=1, after=1):
    lo = max(0, index - before)
    hi = min(len(mask), index + after + 1)
    for i in range(lo, hi):
        # 只补相邻语句，不跨越明显长停顿。
        if (i < index and float(segs[i + 1][0]) - float(segs[i][1])
                > CONTEXT_MAX_GAP):
            continue
        if (i > index and float(segs[i][0]) - float(segs[i - 1][1])
                > CONTEXT_MAX_GAP):
            continue
        mask[i] = True


def _protected_indices(labels, mask, story_indices=None):
    protected = set()
    for kind in ("hook", "goal", "result", "closing"):
        selected = [i for i, label in enumerate(labels) if label == kind and mask[i]]
        if selected:
            protected.add(selected[0])
        if kind == "result" and len(selected) > 1:
            protected.add(max(selected, key=lambda i: i))
    for i in story_indices or ():
        if 0 <= i < len(mask) and mask[i]:
            protected.add(i)
    return protected


def _apply_feedback(data, mask, report, scores, target_seconds, level="medium"):
    segs = data.get("segments") or []
    labels = classify_stages(data)
    policy = _level_policy(level)
    story_indices = genre.chapter_story_chain_indices(data, level=level)
    changed = {"added": [], "dropped": [], "reasons": []}
    for issue in report["issues"]:
        indices = [i for i in issue.get("indices", []) if 0 <= i < len(mask)]
        action = issue.get("action")
        if action == "add_stage" and indices:
            best = max(indices, key=lambda i: scores[i] +
                       (0.16 if labels[i] in ("goal", "result", "closing") else 0.0))
            before = 1 if labels[best] in ("goal", "explain", "result") else 0
            after = 1 if labels[best] in ("goal", "result") else 0
            old = list(mask)
            _add_context(mask, best, segs, before=before, after=after)
            changed["added"].extend(i for i in range(len(mask)) if mask[i] and not old[i])
            changed["reasons"].append("补回%s" % labels[best])
        elif action == "add_chapter" and indices:
            best = max(indices, key=lambda i: scores[i]
                       + (0.18 if labels[i] in ("goal", "result", "explain") else 0.0))
            old = list(mask)
            _add_context(mask, best, segs, before=1, after=1)
            changed["added"].extend(i for i in range(len(mask)) if mask[i] and not old[i])
            changed["reasons"].append("补回章节关键节点")
        elif action == "add_story_chain" and indices:
            for i in indices:
                if not mask[i]:
                    mask[i] = True
                    changed["added"].append(i)
            changed["reasons"].append("补回章节目标/过程/结果/过渡")
        elif action in ("add_previous", "add_next") and indices:
            candidate = indices[0] if action == "add_previous" else indices[-1]
            if not mask[candidate]:
                mask[candidate] = True
                changed["added"].append(candidate)
                changed["reasons"].append("修复刀口断句")

    protected = _protected_indices(labels, mask, story_indices)
    for issue in report["issues"]:
        if issue.get("action") != "drop":
            continue
        for i in issue.get("indices", []):
            if 0 <= i < len(mask) and i not in protected and mask[i]:
                mask[i] = False
                changed["dropped"].append(i)
                changed["reasons"].append("移除%s" % issue["type"])

    # 在不牺牲骨架和各章唯一锚点的前提下收回原候选预算。
    if not llm_gateway.api_enabled() and target_seconds and target_seconds > 0:
        chapters = _chapter_ranges(data)

        def kept_seconds():
            # 预算按最终片段计时，包含为连贯而桥接的短停顿；只累计句长会低估成片。
            return sum(b - a for a, b in spans_from_mask(data, mask))

        guard = 0
        soft_limit = target_seconds * (1.0 + float(policy["overflow"]))
        while kept_seconds() > soft_limit and guard < len(mask):
            protected = _protected_indices(labels, mask, story_indices)
            for start, end, _title, advice in chapters:
                if level == "heavy" and genre._story_optional({"advice": advice}):
                    continue
                selected = [i for i in _indices_in_range(segs, start, end) if mask[i]]
                if len(selected) == 1:
                    protected.add(selected[0])
            removable = [i for i, keep in enumerate(mask) if keep and i not in protected
                         and labels[i] not in ("goal", "result", "closing")]
            if not removable:
                break
            drop = min(removable, key=lambda i: scores[i]
                       + (0.12 if labels[i] == "explain" else 0.0))
            mask[drop] = False
            changed["dropped"].append(drop)
            changed["reasons"].append("预算内替换低价值句")
            guard += 1

    changed["added"] = sorted(set(changed["added"]))
    changed["dropped"] = sorted(set(changed["dropped"]))
    changed["reasons"] = sorted(set(changed["reasons"]))
    return changed


def _has_actionable_issues(report):
    """Return whether another local rebuild can still improve this cut.

    A report may reach the numeric pass threshold while retaining concrete
    ``add_previous``/``add_next``/``drop`` instructions.  Stopping only because
    ``passed`` is true leaves those known bad seams in the plan, so the
    multimodal director later has nothing left to do except reject the task.
    """
    actionable = {
        "add_stage", "add_chapter", "add_story_chain",
        "add_previous", "add_next", "drop",
    }
    return any(
        issue.get("severity") in ("critical", "major")
        and issue.get("action") in actionable
        for issue in (report or {}).get("issues") or []
    )


def rebuild(data, spans, sentence_scores=None, target_seconds=None, max_rounds=2,
            level="medium"):
    """按审稿反馈重建候选；只接受导演总分上升的迭代。"""
    level = level if level in LEVEL_POLICY else "medium"
    policy = _level_policy(level)
    scores = _scores_of(data, sentence_scores)
    if target_seconds is None:
        target_seconds = sum(float(b) - float(a) for a, b in spans)
    current_spans = [(float(a), float(b)) for a, b in spans]
    current_report = review(data, current_spans, scores, target_seconds, level=level)
    history = [{"round": 0, "accepted": True, "report": current_report,
                "changes": {"added": [], "dropped": [], "reasons": []}}]
    for round_no in range(1, max(0, int(max_rounds)) + 1):
        if current_report["passed"] and not _has_actionable_issues(current_report):
            break
        mask = mask_from_spans(data, current_spans)
        changes = _apply_feedback(
            data, mask, current_report, scores, target_seconds, level=level)
        if not changes["added"] and not changes["dropped"]:
            break
        candidate_spans = spans_from_mask(data, mask)
        candidate_report = review(
            data, candidate_spans, scores, target_seconds, level=level)
        old_critical = sum(x["severity"] == "critical"
                           for x in current_report["issues"])
        new_critical = sum(x["severity"] == "critical"
                           for x in candidate_report["issues"])
        old_serious = sum(
            x["severity"] in ("critical", "major") and
            x.get("action") in {
                "add_stage", "add_chapter", "add_story_chain",
                "add_previous", "add_next", "drop",
            }
            for x in current_report["issues"])
        new_serious = sum(
            x["severity"] in ("critical", "major") and
            x.get("action") in {
                "add_stage", "add_chapter", "add_story_chain",
                "add_previous", "add_next", "drop",
            }
            for x in candidate_report["issues"])
        duration_ok = (not target_seconds or target_seconds <= 0
                       or candidate_report["selected_seconds"]
                       <= float(target_seconds) * (1.0 + float(policy["overflow"])) + 0.5)
        if not duration_ok:
            # 预算是软目标。若已经只剩章节故事链和必要首尾，不能为了命中时长
            # 再把目标、过程或结果删掉；有普通内容可删时仍严格执行软上限。
            candidate_mask = mask_from_spans(data, candidate_spans)
            labels = classify_stages(data)
            protected = genre.chapter_story_chain_indices(data, level=level)
            protected.update(i for i, label in enumerate(labels)
                             if label in ("hook", "goal", "result", "closing"))
            non_story = [i for i, keep in enumerate(candidate_mask)
                         if keep and i not in protected]
            duration_ok = not non_story
        structural_rescue = (
            new_critical < old_critical
            and candidate_report["story_score"] > current_report["story_score"] + 0.10
            and candidate_report["continuity_score"] >= 0.80
            and candidate_report["relevance_score"] >= current_report["relevance_score"] - 0.20
        )
        continuity_ok = (
            candidate_report["continuity_score"] >= current_report["continuity_score"] - 0.01
            or (candidate_report["story_score"] > current_report["story_score"] + 0.10
                and candidate_report["continuity_score"] >= 0.90)
            or structural_rescue
        )
        relevance_ok = (
            candidate_report["relevance_score"] >= current_report["relevance_score"] - 0.01
            or structural_rescue
        )
        accepted = (
            (candidate_report["score"] > current_report["score"] + 0.05
             or new_critical < old_critical
             or new_serious < old_serious)
            and candidate_report["story_score"] >= current_report["story_score"]
            and candidate_report["chapter_score"] >= current_report["chapter_score"]
            and continuity_ok
            and relevance_ok
            and duration_ok
        )
        history.append({"round": round_no, "accepted": accepted,
                        "report": candidate_report, "changes": changes})
        if not accepted:
            break
        current_spans, current_report = candidate_spans, candidate_report
    return current_spans, {
        "initial": history[0]["report"],
        "final": current_report,
        "history": history,
        "improved": current_report["score"] > history[0]["report"]["score"] + 0.05,
        "target_seconds": round(float(target_seconds), 3),
        "level": level,
    }


def target_for_level(data, level="medium", spoken_seconds=None, genre_name="gameplay"):
    """返回导演审核使用的档位软时长；剧情链可以在容差内优先。"""
    level = level if level in LEVEL_POLICY else "medium"
    if level == "light":
        return 0.0
    segs = data.get("segments") or []
    spoken = (float(spoken_seconds) if spoken_seconds is not None else
              sum(float(seg[1]) - float(seg[0]) for seg in segs))
    suggested = genre.suggest_budget(data, name=genre_name, level=level)
    if level == "heavy":
        return min(suggested, spoken * 0.55, 11.0 * 60.0)
    return min(suggested, spoken * 0.80, 15.5 * 60.0)


def review_and_rebuild(data, spans, level="medium", target_seconds=None,
                       max_rounds=2, on_log=None):
    """生产链入口：全片审稿；轻度只记录，中/重度可按反馈重建。"""
    log = on_log or (lambda _message: None)
    level = level if level in LEVEL_POLICY else "medium"
    if target_seconds is None:
        target_seconds = target_for_level(data, level=level)
    initial = review(data, spans, target_seconds=target_seconds, level=level)
    log("导演审片：%s %.1f 分，剧情链 %.0f%%，章节 %.0f%%，连贯 %.0f%%"
        % ("通过" if initial["passed"] else "需修复", initial["score"],
           initial["story_score"] * 100, initial["chapter_score"] * 100,
           initial["continuity_score"] * 100))
    if (level == "light" or
            (initial["passed"] and not _has_actionable_issues(initial))):
        return [(float(a), float(b)) for a, b in spans], {
            "initial": initial, "final": initial, "history": [],
            "improved": False, "target_seconds": float(target_seconds or 0),
            "level": level,
        }
    rebuilt, result = rebuild(
        data, spans, target_seconds=target_seconds, max_rounds=max_rounds,
        level=level)
    final = result["final"]
    accepted = sum(1 for row in result["history"][1:] if row.get("accepted"))
    log("导演重建：接受 %d 轮，%.1f → %.1f 分，剧情链 %.0f%%，成片 %.1f 分钟"
        % (accepted, initial["score"], final["score"],
           final["story_score"] * 100, final["selected_seconds"] / 60.0))
    return rebuilt, result

