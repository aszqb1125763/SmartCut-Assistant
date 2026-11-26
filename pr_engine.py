# -*- coding: utf-8 -*-
"""PR 里的 AI 剪辑：看序列+台词 → 输出可执行的时间区间删除单。

策略与软件侧同源（都读 coords.LEVEL_KEEP_RANGE）：
  · 力度预算：轻度保留 85%~100%、中度 70%~85%、重度 35%~55% 的台词；
    模型给的删除量超过"可删上限"时，代码按顺序截断（不交给模型自律）。
  · 开头保护：起点在开头 5 秒内的删除区间整条丢弃。
  · 换算：素材时间 → PR 时间轴由代码做，模型不碰秒数以外的任何东西。

输入：sync_server.build_view() 的结构（PR 片段 + 索引台词）。
输出：{"ranges": [[pr_start, pr_end], ...], "reason": str}
"""
from __future__ import annotations

import json
import os
import re


MIN_KEEP_SECONDS = 0.10

try:
    import coords as _coords
    LEVEL_KEEP_RANGE = dict(getattr(_coords, "LEVEL_KEEP_RANGE", {
        "light": (0.85, 1.00),
        "medium": (0.70, 0.85),
        "heavy": (0.35, 0.55),
    }))
except Exception:  # noqa: BLE001
    LEVEL_KEEP_RANGE = {
        "light": (0.85, 1.00),
        "medium": (0.70, 0.85),
        "heavy": (0.35, 0.55),
    }

SYS = """你在剪一条已经排好的视频。下面是片段的台词，格式「序号 起点~终点 文本」（素材时间）。

当前力度：{level}（目标保留台词 {keep_low}%~{keep_high}%，即最多删 {drop_pct}%）。
全片台词共 {speech_sec} 秒，本力度最多删 {budget_sec} 秒。

只找**最该删的 1~3 处**，只认这两种：
1. 同一件事明确说了两遍（保留讲得最完整的那一遍）；
2. 口误且后面马上自己纠正了（只删说错的那一次）。

其余一律不动——这是教程，每一步操作都有用，**宁可不删，也不要删错**。
不许删开头第一句，不许删到步骤的目标/过程/结果。

输出 JSON（不要多余文字）：
{{"delete_ranges": [[起点, 终点]], "reason": "一句话说明删了哪一处、为什么"}}

如果找不到明确的重复或口误，就输出 {{"delete_ranges": [], "reason": "没有明确该删的"}}。"""


def _read_strength(base_dir):
    """Read exact user percentage and derive the legacy strategy."""
    path = os.path.join(base_dir, "conf", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            conf = json.load(handle)
        import cut_strength
        ratio = cut_strength.ratio_from_settings(conf)
        return cut_strength.level_for_ratio(ratio), ratio
    except (OSError, ValueError):
        pass
    return "medium", 0.75


def _parse_json(text):
    if not text:
        return None
    match = re.search(r"\{.*\}", str(text), re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _event_lines(clips):
    """事件流：每句台词一条，带序号、素材时间和能量档（响/中/轻）。"""
    rows = []
    number = 0
    for clip in clips:
        for seg in clip.get("segments") or []:
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            number += 1
            level = str(seg.get("level") or "").strip()
            head = "%04d %.2f~%.2f" % (number, float(seg["a"]), float(seg["b"]))
            if level:
                head += " [%s]" % level
            rows.append(head + " " + text)
    return rows


def plan(view, base_dir, log=print, level=None, keep_ratio=None):
    """返回 {"ok": True, "ranges": [...], "reason": str, "level": str}。"""
    if not view or not view.get("ok"):
        return {"ok": False, "error": "没有可读的 PR 序列"}
    clips = view.get("clips") or []
    if not clips:
        return {"ok": False, "error": "序列里没有视频片段"}
    # 没有台词索引时模型什么都看不见，只会建议"整条删"——直接拒绝，不动序列
    total_segments = sum(len(c.get("segments") or []) for c in clips)
    if total_segments == 0:
        return {"ok": False,
                "error": "这条素材还没有索引（模型看不到台词），先建索引再剪"}

    rows = _event_lines(clips)
    if not rows:
        return {"ok": False, "error": "这条素材没有台词（索引里没有内容）"}

    import cut_strength
    stored_level, stored_ratio = _read_strength(base_dir)
    keep_ratio = cut_strength.normalize_ratio(
        keep_ratio, fallback=stored_ratio)
    level = cut_strength.level_for_ratio(keep_ratio)
    keep_low = max(0.15, keep_ratio - 0.05)
    keep_high = min(1.0, keep_ratio + 0.05)
    speech = 0.0
    for clip in clips:
        for seg in clip.get("segments") or []:
            speech += max(0.0, float(seg["b"]) - float(seg["a"]))
    budget = max(0.0, speech * (1.0 - keep_low))

    system = SYS.format(
        level={"light": "轻度", "medium": "中度", "heavy": "重度"}.get(level, level),
        keep_low=int(keep_low * 100), keep_high=int(keep_high * 100),
        drop_pct=int(round((1.0 - keep_low) * 100)),
        speech_sec="%.1f" % speech, budget_sec="%.1f" % budget)

    from ui.llm_bridge import LLM
    llm = LLM(base_dir=base_dir, task="editor")
    # 静音轨提示：用户在 PR 里静音的轨，说明不想用它的声音
    muted = [t for t in (view.get("tracks") or []) if t.get("muted")]
    muted_note = ""
    if muted:
        names = "、".join("%s%d" % (str(t["kind"]), int(t["track"]))
                          for t in muted)
        muted_note = ("当前 PR 里被静音的轨：%s（用户不想用这些轨的声音，"
                      "判断时不要把它们当有效内容）。\n" % names)

    # 分批：本地模型上下文 8192 token，一次塞 276 句会被 llama-server 回 400。
    BATCH_SIZE = 120
    batches = [rows[i:i + BATCH_SIZE]
               for i in range(0, len(rows), BATCH_SIZE)] or [rows]
    answers = []
    try:
        llm.start()
        log("   AI 看 %d 句台词（分 %d 批），目标保留 %.0f%%（最多删 %.1f 秒）…"
            % (len(rows), len(batches), keep_ratio * 100.0, budget))
        for index, batch in enumerate(batches, 1):
            note = ("" if len(batches) == 1
                    else "\n（这是第 %d/%d 批，只判断这一批里的内容。）"
                         % (index, len(batches)))
            user_text = ("目标保留约%.0f%%有效内容；全片有效台词%.1f秒，"
                         "最多删除%.1f秒。长时间无台词的等待不计入保留比例。\n"
                         % (keep_ratio * 100.0, speech, budget))
            user_text += muted_note + note + "\n" + "\n".join(batch)
            answers.append(llm.chat(system, user_text, max_tokens=1200))
    finally:
        try:
            llm.stop()
        except Exception:  # noqa: BLE001
            pass

    raw_ranges = []
    reasons = []
    usable = 0
    for answer in answers:
        plan_data = _parse_json(answer)
        if not plan_data or not isinstance(plan_data.get("delete_ranges"), list):
            continue
        usable += 1
        reason = str(plan_data.get("reason") or "").strip()
        if reason:
            reasons.append(reason[:80])
        for item in plan_data.get("delete_ranges") or []:
            try:
                a, b = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if b > a:
                raw_ranges.append((a, b))
    if usable <= 0:
        return {"ok": False, "error": "模型没给出可用的删除区间",
                "raw": str(answers[0] if answers else "")[:300]}

    # 开头保护：起点在开头 5 秒内的整条丢弃（宁可不删，也不删开头）
    raw_ranges = [(a, b) for a, b in raw_ranges if a >= 5.0]
    # 单段上限 30 秒，防止"一段 100 秒"把整章吞掉
    raw_ranges = [(a, min(b, a + 30.0)) for a, b in raw_ranges]

    # 边界对齐：端点吸到最近的句子边界（偏差 ≤0.6 秒才动），避免切半句
    seg_bounds = set()
    for clip in clips:
        for seg in clip.get("segments") or []:
            seg_bounds.add(round(float(seg["a"]), 2))
            seg_bounds.add(round(float(seg["b"]), 2))

    def _snap(value):
        best, best_d = None, 0.6
        for bound in seg_bounds:
            delta = abs(bound - value)
            if delta < best_d:
                best, best_d = bound, delta
        return best if best is not None else value

    raw_ranges = [(_snap(a), _snap(b)) for a, b in raw_ranges]
    raw_ranges = [(a, b) for a, b in raw_ranges if b - a > 0.3]

    # 力度预算：按顺序接纳，累计超过 budget 的部分丢弃
    kept = []
    used = 0.0
    for a, b in raw_ranges:
        if used + (b - a) > budget + 0.01:
            continue
        kept.append((a, b))
        used += (b - a)

    # 换算：素材时间 → PR 时间轴
    out_ranges = []
    for clip in clips:
        clip_in = float(clip["in"])
        clip_out = float(clip["out"])
        clip_start = float(clip["start"])
        for a, b in kept:
            lo, hi = max(a, clip_in), min(b, clip_out)
            if hi - lo < 0.3:
                continue
            out_ranges.append([
                round(clip_start + (lo - clip_in), 3),
                round(clip_start + (hi - clip_in), 3),
            ])

    # 重建序列用的「保留段」（素材时间）：从每条 clip 的 in/out 里扣掉删除区间。
    # 这样插件不用 razor（实测不可靠），改成"清空 + 按保留段重新插入"。
    keep_segments = []
    for clip in clips:
        clip_in = float(clip["in"])
        clip_out = float(clip["out"])
        cuts = []
        for a, b in kept:
            lo, hi = max(a, clip_in), min(b, clip_out)
            if hi > lo:
                cuts.append((lo, hi))
        cursor = clip_in
        for a, b in sorted(cuts):
            # Do not rebuild accidental one-frame residuals as real PR clips.
            if a - cursor >= MIN_KEEP_SECONDS:
                keep_segments.append([round(cursor, 3), round(a, 3)])
            cursor = max(cursor, b)
        if clip_out - cursor >= MIN_KEEP_SECONDS:
            keep_segments.append([round(cursor, 3), round(clip_out, 3)])

    return {
        "ok": True,
        # 从后往前删：删除会 ripple 前移，降序能保证前面区间的时间不失效
        "ranges": sorted(out_ranges, key=lambda item: -item[0]),
        # 重建用：素材路径 + 素材时间上的保留段
        "source": str(clips[0].get("path") or ""),
        "keep_segments": keep_segments,
        "count": len(out_ranges),
        "level": level,
        "speech_sec": round(speech, 1),
        "budget_sec": round(budget, 1),
        "used_sec": round(used, 1),
        "reason": "；".join(reasons)[:200],
        "segments_seen": len(rows),
    }


STEP_SYS = """你在剪 Premiere Pro 的时间轴。下面给出当前序列。

你只需要判断「哪一段该删」，给出区间的起点和终点（时间轴秒数）。
程序会自动这样执行：
  1. 把播放头移到起点，切一刀（Ctrl+K）；
  2. 把播放头移到终点，做「波纹修剪上一个编辑点到播放头」——
     也就是把起点到终点这段删掉，后面的内容自动补上。

输出 JSON（不要多余文字）：
  {"delete_ranges": [[起点秒, 终点秒], ...], "reason": "一句话说明为什么删这段"}
或者已经剪完：
  {"done": true, "reason": "说明为什么剪完了"}

规则：
· 只删你确定的内容（重复、口误、跑题、明显废话）；
· 不要删开头第一句；
· 一次可以给多个区间，程序会从后往前删（避免坐标错位）。"""


def _sequence_digest(view):
    """把序列压成给模型看的摘要。"""
    lines = []
    for clip in view.get("clips") or []:
        lines.append(
            "片段%d：时间轴 %.2f~%.2f 秒，素材 %.2f~%.2f 秒，%d 句台词"
            % (clip.get("index", 0), float(clip.get("start") or 0),
               float(clip.get("end") or 0), float(clip.get("in") or 0),
               float(clip.get("out") or 0), len(clip.get("segments") or [])))
        for seg in (clip.get("segments") or [])[:8]:
            text = str(seg.get("text") or "")[:36]
            db = seg.get("db")
            gap = seg.get("gap")
            extra = ""
            if db is not None:
                extra += " %5.1fdB" % float(db)
            if gap is not None:
                extra += " 空隙%.2fs" % float(gap)
            lines.append("   %.2f~%.2f%s %s" % (float(seg["a"]), float(seg["b"]),
                                                extra, text))
        if len(clip.get("segments") or []) > 8:
            lines.append("   …（还有 %d 句）" % (len(clip["segments"]) - 8))
    return "\n".join(lines)


def step(view, base_dir, log=print):
    """模型看当前序列 → 给出要删的区间 → 转成 PR 可执行操作。"""
    if not view or not view.get("ok"):
        return {"ok": False, "error": "没有可读的 PR 序列"}
    clips = view.get("clips") or []
    if not clips:
        return {"ok": False, "error": "序列里没有视频片段"}
    # 没有台词索引时模型什么都看不见，只会建议"整条删"——直接拒绝，不动序列
    total_segments = sum(len(c.get("segments") or []) for c in clips)
    if total_segments == 0:
        return {"ok": False,
                "error": "这条素材还没有索引（模型看不到台词），先建索引再剪"}

    digest = _sequence_digest(view)
    total_len = max((float(c.get("end") or 0) for c in clips), default=0.0)
    digest = "序列总长 %.2f 秒。时间坐标必须落在 0~%.2f 之间。\n%s" % (
        total_len, total_len, digest)
    from ui.llm_bridge import LLM
    llm = LLM(base_dir=base_dir, task="editor")
    answer = None
    try:
        llm.start()
        log("   AI 看当前序列（%d 个片段）…" % len(clips))
        answer = llm.chat(STEP_SYS, digest, max_tokens=600)
    finally:
        try:
            llm.stop()
        except Exception:  # noqa: BLE001
            pass

    plan = _parse_json(answer)
    if not plan:
        return {"ok": False, "error": "模型输出不是 JSON",
                "raw": str(answer)[:300]}
    if plan.get("done"):
        return {"ok": True, "done": True,
                "reason": str(plan.get("reason") or "")[:200], "ops": []}

    # 收集区间（时间轴秒数），并夹到序列真实长度内（模型偶尔会给越界值）
    total = max((float(c.get("end") or 0) for c in clips), default=0.0)
    ranges = []
    for item in plan.get("delete_ranges") or []:
        try:
            a, b = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        a = max(0.0, min(a, total))
        b = max(0.0, min(b, total))
        if b > a + 0.5:
            ranges.append((a, b))
    if not ranges:
        return {"ok": True, "done": False, "ops": [], "reason": "",
                "note": "模型没给出有效区间（或区间越界被丢弃）",
                "clips_seen": len(clips)}

    # 从后往前删：先删后面的区间，前面区间的坐标才不会错位
    ranges.sort(key=lambda item: -item[0])
    ops = []
    for a, b in ranges:
        ops.append({"op": "seek", "t": round(a, 3)})
        ops.append({"op": "key", "k": "Ctrl+K"})   # 起点切一刀
        ops.append({"op": "seek", "t": round(b, 3)})
        ops.append({"op": "key", "k": "Q"})        # 终点波纹修剪到上一个编辑点
    return {"ok": True, "done": False, "ops": ops,
            "ranges": [[round(a, 3), round(b, 3)] for a, b in ranges],
            "reason": str(plan.get("reason") or "")[:200],
            "clips_seen": len(clips)}
