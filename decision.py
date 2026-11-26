# -*- coding: utf-8 -*-
"""决策参数（权重/均衡）——**属于本地模型，不属于小脑**。

子枫 2026-11-24：
    「小脑不需要有思考能力，他就是被动的计算机，中立绝对的。」
    「所有要权重、均衡、思考的全部属于本地模型。」

所以：
    · 小脑只做中立计算（同一个输入永远同一个输出，不含任何偏好）；
    · 执行层（选点/闸门）**只读这里的参数**，不许在代码里写死权重；
    · 改决策 = 改 `conf/decision.json`，不用改代码。

这里只负责读参数、给默认值，不做任何判断。
"""
from __future__ import annotations

import json
import os

import coords
from memory_config import app_home

BASE = app_home()
PATH = os.path.join(BASE, "conf", "decision.json")

#: 文件缺失时的兜底（与 2026-11-24 的实测默认一致，保证行为可复现）
DEFAULTS = {
    "candidate_rank": {"semantic": 0.7, "axes": 0.3},
    "gate": {"max_p0": 2, "max_relative_over": 0.55, "max_gray_ratio": 0.40},
}


def load(path=None):
    target = path or PATH
    data = {}
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        data = {}
    merged = dict(DEFAULTS)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def candidate_rank(path=None):
    """候选排序的权重（语义 / 三轴）。"""
    row = load(path).get("candidate_rank") or {}
    semantic = float(row.get("semantic", DEFAULTS["candidate_rank"]["semantic"]))
    axes = float(row.get("axes", DEFAULTS["candidate_rank"]["axes"]))
    total = semantic + axes
    if total <= 0:
        return DEFAULTS["candidate_rank"]["semantic"], DEFAULTS["candidate_rank"]["axes"]
    return semantic / total, axes / total


def gate(path=None):
    return dict(load(path).get("gate") or DEFAULTS["gate"])


def judge(probe):
    """把"左右通道的数据"判成档位/条款——这一步属于决策，不属于小脑。"""
    values = dict(probe.get("values") or {})
    channels = probe.get("channels") or {}
    left = dict(channels.get("left") or {})
    right = dict(channels.get("right") or {})
    grade, note = coords.grade_seam(
        float(values.get("energy_gap_db") or 0.0),
        float(values.get("tone_gap_hz") or 0.0),
        float(values.get("shape_rms_db") or 0.0),
        left_silent=bool(left.get("silent")),
        right_silent=bool(right.get("silent")))
    state = dict(values)
    state.update({"left_silent": bool(left.get("silent")),
                  "right_silent": bool(right.get("silent")),
                  "axes_grade": grade})
    if probe.get("tail_chopped") and probe.get("tail_ms") is not None:
        state["tail_decay_ms"] = float(probe["tail_ms"])
    verdict = coords.judge(state)
    if verdict.get("severity") == "P0":
        grade = "over"
    return {"grade": grade, "note": note,
            "row": verdict.get("row"), "severity": verdict.get("severity"),
            "action": verdict.get("action"),
            "compares": verdict.get("compares") or [],
            "pause_edge": bool(left.get("silent") or right.get("silent"))}


def brief(snapshot):
    """三轴快照 → 模型用的三个数据（定性，不带精确原始值）。"""
    energy = snapshot.get("energy") or {}
    text = snapshot.get("text") or {}
    peak = energy.get("peak_db")
    # 六档：够区分情绪，又不至于把 dB 丢给模型
    ladder = (
        (coords.ENERGY_VERY_STRONG_DB, "极强"),
        (coords.ENERGY_STRONG_DB, "强"),
        (coords.ENERGY_UPPER_MID_DB, "中上"),
        (coords.ENERGY_MID_DB, "中"),
        (coords.ENERGY_WEAK_DB, "弱"),
    )
    if peak is None:
        level = "未知"
    else:
        level = "静音"
        for edge, label in ladder:
            if peak >= edge:
                level = label
                break
    segments = energy.get("segments") or []
    sudden = any(
        row.get("kind") in ("rise", "fall")
        and float(row.get("dur_s") or 0.0) <= coords.SUDDEN_SEGMENT_S
        and abs(float(row.get("db1") or 0.0) - float(row.get("db0") or 0.0))
        >= coords.CLIFF_DB_20MS
        for row in segments)
    change = "骤变" if sudden else "渐变"
    says = ""
    for row in (text.get("sentences") or [])[:3]:
        says += str(row.get("text") or "")
    return {
        "t0": snapshot.get("t0"),
        "t1": snapshot.get("t1"),
        "timeline": snapshot.get("timeline", "source"),
        # ① 文字是什么
        "text": {"says": says[:60],
                 "available": bool(says),
                 "duration_s": round(float(text.get("total_duration_s") or 0.0), 2)},
        # ② 在时间轴上持续多久（已含在 t1-t0 与 text.duration_s）
        "duration_s": round(float(snapshot.get("t1") or 0.0)
                            - float(snapshot.get("t0") or 0.0), 2),
        # ③ 能量怎么样（形态 / 强弱 / 突变）
        "energy": {"shape": energy.get("label") or "未知",
                   "level": level,
                   "level_path": energy.get("level_path") or level,
                   "level_swing": energy.get("level_swing"),
                   "stability": (
                       "平稳" if (energy.get("level_flat_ratio") or 0) >= 0.6
                       and (energy.get("level_mean_run_s") or 0) >= 1.2
                       else ("起伏" if (energy.get("level_flat_ratio") or 0) < 0.35
                             else "中等")),
                   "change": change},
        # 采样率是口径，不是测量值——放在外面，energy 里只留标签
        "energy_fps": energy.get("energy_fps"),
        "level_runs": energy.get("level_runs"),
        "note": "定性视图：模型据这三项判断情绪或声音割裂，不需要精确原始值",
    }
