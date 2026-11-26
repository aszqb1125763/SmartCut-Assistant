# -*- coding: utf-8 -*-
"""接缝回执（coords_report）：把每处接缝的判定写成可核对的 JSON。

为什么要有它（2026-11-24）：
    以前判据算完只是"日志里一句话"，没人能回答三个问题：
        · 这一处到底多少 dB / 多少 Hz / 形状差多少？
        · 它触发了规范哪一条（M0x）？
        · 这一版比上一版好还是差？
    没有回执，判据就等于没判——因为无法核对、无法回归、无法追责。

**2026-11-24 起：判定全部走小脑（brain.Calculator），本模块只做"编排 + 落盘"。**

**两处标准（E03 / §17），别混用：**
    · 混音前（源片人声轨）：严格 5dB / 500Hz / 5dB —— 在定稿阶段判。
    · 出片前（成片混音）：**宽松** = 跳变落不落在"该片自己正常跳变的 P90"里。
    成片里游戏轨/音效在切口两侧本来就不连续，人声接得再好，混音也会超严格线。

规矩：
    · 每条接缝带 `color`（给模型）、`row`（条款矩阵行）、`compares`（给审计）
    · `summary` 给两套：`strict`（混音前口径）与 `relative`（出片前口径）
    · 闸门看 `relative` + P0；`strict` 只作参考
"""
from __future__ import annotations

import json
import os

import brain
import coords
import decision
from model_inbox import Inbox
from timeline import Timeline
import seam_picker as spc

SCHEMA = "smartcut.coords-report"
SCHEMA_VERSION = 3


def _percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round(q / 100.0 * (len(ordered) - 1)))))
    return ordered[index]


def seam_row(calc, at, p90_db=None):
    """一处接缝：严格三轴 + 相对跳变 + 尾音 + 颜色 + 条款 + 比较式。"""
    probe = calc.probe_seam(at)
    # 出片前口径：跳变幅度取"能量差 / 形状差"里更大的那个，
    # 跟该片自己正常跳变的 P90 比（E03：宽松，不是绝对阈值）。
    jump_db = max(abs(probe["values"]["energy_gap_db"]),
                  abs(probe["values"]["shape_rms_db"]))
    # E03 原文：跳变 ≤ 该片正常跳变的 P90。这里不用 2 倍边缘区——
    # 宽松线本身就是"人耳在这片里听不出来的量级"，再放宽一倍就没意义了。
    verdict = decision.judge(probe)
    if p90_db:
        if verdict.get("pause_edge"):
            # 切在气口：一侧在静音里，形状差天然很大，不按跳变判（M16/M17）
            relative = "ok"
        else:
            relative = "ok" if jump_db <= float(p90_db) else "over"
    else:
        relative = None
    color, label = brain.color_of(verdict.get("grade"))
    red = color == "red"
    gray = color == "gray"
    relative_red = relative == "over"
    return {
        "at": round(float(at), 2),
        "brain": probe.get("brain"),
        "color": color, "label": label,
        "energy_gap_db": probe["values"]["energy_gap_db"],
        "left_db": probe.get("left_db"), "right_db": probe.get("right_db"),
        "tone_gap_hz": probe["values"]["tone_gap_hz"],
        "shape_rms_db": probe["values"]["shape_rms_db"],
        "jump_db": round(jump_db, 1),
        "relative_grade": relative,
        "relative_over": relative_red,
        "pause_edge": verdict.get("pause_edge", False),
        "pause_note": verdict.get("note"),
        "grade": verdict.get("grade"),
        "row": verdict.get("row") or "M?",
        "severity": verdict.get("severity"),
        "compares": verdict.get("compares") or [],
        "tail_ms": probe.get("tail_ms"),
        "tail_shape": probe.get("tail_shape"),
        "tail_grade": probe.get("tail_grade"),
        "tail_chopped": bool(probe.get("tail_chopped")),
        "is_red": red,
        "is_gray": gray,
    }


def build(film, seams, ffmpeg=None, track=0, extra=None, should_cancel=None):
    """对成片逐缝出回执（小脑算，本模块只编排）。"""
    audio_source = brain.AudioSource(film, ffmpeg=ffmpeg or None, track=track,
                                     should_cancel=should_cancel)
    calc = brain.Calculator(film, ffmpeg=ffmpeg or None, track=track,
                            sources={"audio": audio_source})
    try:
        return _build_with_calculator(film, seams, calc, extra, should_cancel)
    finally:
        calc.close()


def _build_with_calculator(film, seams, calc, extra, should_cancel):
    def check_cancel():
        if should_cancel is not None and should_cancel():
            from audio_evidence_cache import AudioEvidenceCancelled
            raise AudioEvidenceCancelled("成片回执检查已取消")

    check_cancel()
    # 音频读取失败必须交给调用方阻断交付，不能伪造 0 秒、0 问题的通过报告。
    duration = calc.duration
    # 该片自己的正常跳变分布（50ms 帧的 |diff|），E03 出片前宽松线的来源
    jump_stats = spc.normal_jump_stats(calc.audio)
    p90_db = float(jump_stats.get("p90") or coords.JUMP_P90_DB)
    points = [float(x) for x in seams if 0.4 < float(x) < duration - 0.4]
    rows = []
    for at in points:
        check_cancel()
        rows.append(seam_row(calc, at, p90_db=p90_db))
    # 文字小脑：给最差的几处标出"能挪到哪儿"（S/A 级切点）
    try:
        for row in sorted(rows, key=lambda row: -row["jump_db"])[:3]:
            row["safe_edges"] = calc.safe_edges(row["at"] - 3.0,
                                                 row["at"] + 3.0, limit=6)
    except Exception:  # noqa: BLE001
        pass
    # 三轴快照（给模型）：文字是什么+多久、能量长什么样，时间轴统一
    try:
        for row in sorted(rows, key=lambda row: -row["jump_db"])[:3]:
            snap = calc.axis_snapshot(row["at"] - 2.0, row["at"] + 2.0)
            row["axis"] = {
                "text_duration_s": snap["text"]["total_duration_s"],
                "text_sentences": len(snap["text"]["sentences"]),
                "energy_label": snap["energy"].get("label"),
                "energy_peak_db": snap["energy"].get("peak_db"),
                "energy_valley_db": snap["energy"].get("valley_db"),
                "energy_segments": len(snap["energy"].get("segments") or []),
            }
    except Exception:  # noqa: BLE001
        pass
    # 画面小脑：只抽最差的几处（每处一次抽帧，全量抽太慢）
    try:
        worst = sorted(rows, key=lambda row: -row["jump_db"])[:5]
        for row in worst:
            probe = calc.probe_picture_jump(row["at"])
            row["picture"] = {k: probe.get(k) for k in
                               ("bright_gap", "hist_corr", "edge_gap",
                                "grade", "row", "brain")}
    except Exception:  # noqa: BLE001
        pass
    # 时间轴 + 收件箱：小脑结果**推送**给模型（模型被动收，不用主动问）
    timeline = Timeline()
    inbox = Inbox()
    for row in rows:
        at = row["at"]
        timeline.push("audio", "seam", {
            "at": at, "color": row["color"], "grade": row["grade"],
            "row": row["row"], "severity": row["severity"],
            "energy_gap_db": row["energy_gap_db"],
            "tone_gap_hz": row["tone_gap_hz"],
            "shape_rms_db": row["shape_rms_db"],
            "jump_db": row["jump_db"],
            "tail_shape": row["tail_shape"],
            "tail_chopped": row["tail_chopped"],
        })
        try:
            safety = calc.check_cut_safety(at)
            timeline.push("speech", "cut_safety", {
                "at": at, "inside_word": safety.get("inside_word"),
                "grade": safety.get("grade")})
            edge = calc.edge_grade(at)
            timeline.push("text", "edge", {
                "at": at, "grade": edge.get("grade"),
                "reason": edge.get("reason")})
        except Exception:  # noqa: BLE001
            pass
        if row.get("axis"):
            timeline.push("axis", "snapshot", {
                "at": at, **row["axis"]})
        if row.get("picture"):
            timeline.push("picture", "jump", {
                "at": at, "grade": row["picture"].get("grade"),
                "bright_gap": row["picture"].get("bright_gap"),
                "hist_corr": row["picture"].get("hist_corr")})
    inbox.push_many(timeline.entries)
    check_cancel()
    total = len(rows)
    red = sum(1 for row in rows if row["is_red"])
    gray = sum(1 for row in rows if row["is_gray"])
    yellow = sum(1 for row in rows if row["color"] == "yellow")
    green = total - red - gray - yellow
    rel_over = sum(1 for row in rows if row["relative_over"])
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "spec_version": coords.SPEC_VERSION,
        "brain_schema": brain.SCHEMA_VERSION,
        "film": os.path.abspath(film),
        "film_seconds": round(duration, 1),
        "standards": {
            "pre_mix": {"energy_db": coords.ENERGY_OK_DB,
                        "tone_hz": coords.TONE_OK_HZ,
                        "shape_db": coords.SHAPE_OK_DB,
                        "edge_factor": coords.EDGE_FACTOR,
                        "note": "E03 混音前严格口径（在源片人声轨上判）"},
            "post_render": {"jump_p90_db": round(p90_db, 1),
                            "normal_mean_db": jump_stats.get("mean"),
                            "normal_max_db": jump_stats.get("max"),
                            "note": "E03 出片前宽松口径：跳变要落在这片自己的 P90 里"},
        },
        "summary": {
            "total": total,
            "green": green, "yellow": yellow, "red": red, "gray": gray,
            "ok": green, "edge": yellow, "over": red,
            "ok_ratio": round(green / float(total), 3) if total else 0.0,
            "over_ratio": round(red / float(total), 3) if total else 0.0,
            "gray_ratio": round(gray / float(total), 3) if total else 0.0,
            "p0": sum(1 for row in rows if row.get("severity") == "P0"),
            "hard_tail": sum(1 for row in rows if row.get("tail_chopped")),
            "strict": {
                "energy_ok": sum(1 for row in rows
                                 if row["energy_gap_db"] <= coords.ENERGY_OK_DB),
                "tone_ok": sum(1 for row in rows
                               if row["tone_gap_hz"] <= coords.TONE_OK_HZ),
                "shape_ok": sum(1 for row in rows
                                if row["shape_rms_db"] <= coords.SHAPE_OK_DB),
            },
            "relative": {
                "p90_db": round(p90_db, 1),
                "over": rel_over,
                "over_ratio": round(rel_over / float(total), 3) if total else 0.0,
                "ok": total - rel_over,
            },
            "picture_over": sum(1 for row in rows
                                 if (row.get("picture") or {}).get("grade")
                                 == "over"),
            "timeline": {
                "entries": len(timeline),
                "brains": sorted({row["brain"] for row in timeline.entries}),
                "aligned": all(timeline.is_aligned(row["at"]) for row in rows),
                "tolerance_s": timeline.tolerance,
            },
            "p50": {"energy_db": _percentile(
                        [r["energy_gap_db"] for r in rows], 50),
                    "tone_hz": _percentile([r["tone_gap_hz"] for r in rows], 50),
                    "shape_db": _percentile(
                        [r["shape_rms_db"] for r in rows], 50),
                    "jump_db": _percentile([r["jump_db"] for r in rows], 50)},
        },
        "worst": sorted(rows, key=lambda r: -r["jump_db"])[:10],
        "seams": rows,
    }
    if extra:
        report.update(extra)
    report["timeline_doc"] = timeline.to_json()
    report["inbox_doc"] = inbox.to_json()
    return report


def _limits():
    """闸门容忍度：**属于本地模型的决策参数**，从 conf/decision.json 读。"""
    import decision
    row = decision.gate()
    return {
        "relative_over": float(row.get("max_relative_over", 0.55)),
        "p0": int(row.get("max_p0", 2)),
        "gray": float(row.get("max_gray_ratio", 0.40)),
        "strict_over": 0.60,
    }


def verdict(report, **overrides):
    """闸门判据（棘轮）：P0 = 0、相对超标比例、看不清比例；严格口径只记不拦。

    为什么严格口径不拦：成片是混音，游戏轨/音效在切点两侧本来就不连续。
    E03 已经把"出片前"定成宽松（该片 P90），严格口径属于混音前那一关。
    """
    limits = _limits()
    # 兼容两种写法：max_p0=0 与 p0=0（老调用点用的是 max_ 前缀）
    alias = {"max_over_ratio": "strict_over", "max_p0": "p0",
             "max_gray": "gray", "max_relative_over": "relative_over"}
    for key, value in overrides.items():
        target = alias.get(key, key)
        if target in limits and value is not None:
            limits[target] = type(limits[target])(value)
    summary = report.get("summary") or {}
    relative = summary.get("relative") or {}
    rel_ratio = float(relative.get("over_ratio") or 0.0)
    gray_ratio = float(summary.get("gray_ratio") or 0.0)
    p0 = int(summary.get("p0") or 0)
    reasons = []
    if p0 > limits["p0"]:
        reasons.append("P0 %d 处 > %d" % (p0, limits["p0"]))
    if rel_ratio > limits["relative_over"]:
        reasons.append("相对超标 %.0f%% > %.0f%%"
                       % (rel_ratio * 100, limits["relative_over"] * 100))
    if gray_ratio > limits["gray"]:
        reasons.append("看不清 %.0f%% > %.0f%%"
                       % (gray_ratio * 100, limits["gray"] * 100))
    return {"passed": not reasons, "reasons": reasons,
            "reason": "；".join(reasons),
            "relative_over_ratio": rel_ratio,
            "strict_over_ratio": float(summary.get("over_ratio") or 0.0),
            "gray_ratio": gray_ratio, "p0": p0,
            "limits": limits}


def write(report, path):
    """原子写：先写 .tmp 再替换。写一半崩掉会留下坏 JSON（已经踩过一次）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    folder = os.path.dirname(path)
    # 同步落两份给模型侧：时间轴 + 收件箱（模型被动读这两份）
    for key, name in (("timeline_doc", "coords_timeline.json"),
                      ("inbox_doc", "model_inbox.json")):
        if report.get(key):
            side = os.path.join(folder, name)
            with open(side, "w", encoding="utf-8") as handle:
                json.dump(report[key], handle, ensure_ascii=False, indent=1)
    return path
