# -*- coding: utf-8 -*-
"""通用剪辑价值特征与分组平衡线性模型。

这个模块刻意只依赖台词和时间轴，使训练缓存、源码模式和打包模式可以
使用完全相同的特征。画面/音量仍由现有 16 维实验模型处理；没有可靠的
独立视觉标签前，不把它们混进本模型。
"""
from __future__ import annotations

import math
import re

import numpy as np

import completeness
import stage


FILLER = ("嗯", "啊", "呃", "额", "那个", "这个", "就是", "然后", "其实",
          "的话", "对吧", "是不是", "你看", "什么呢", "怎么讲", "我看看")
LOST = ("在哪", "找不到", "找一下", "找找", "等一下", "等等", "看下", "看一下",
        "翻一下", "翻翻", "忘了", "记不清", "想不起来")
RETAKE = ("重来", "再来一次", "重新来", "刚才不算", "说错了", "不对不对",
          "录错", "白录", "重新说", "从头来")
UNCERTAIN = ("可能", "应该", "大概", "不知道", "不确定", "好像", "也许", "算了")
ACTION = ("放", "装", "做", "造", "挖", "合成", "连接", "启动", "打开", "拿",
          "升级", "建", "拆", "换", "测试", "完成", "成功", "搞定")
UNFIN = completeness.TAIL_BAD

FEATURE_NAMES = [
    "cps", "local_cps", "log_duration", "log_chars",
    "prev_gap", "next_gap", "position", "start_2m", "end_2m",
    "filler_density", "lost", "retake", "uncertain", "question",
    "progress_density", "result_word", "goal_word", "tease_word", "greeting",
    "action_density", "has_number", "exclaim", "completeness", "unfinished",
    "duplicate_prev", "duplicate_next", "local_novelty",
    "local_cps_ratio", "local_len_ratio", "prev_connector", "next_connector",
    "stage_hook", "stage_goal", "stage_result", "stage_tease",
    "very_short", "long_sentence", "pause_both",
]


def _norm(text):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(text or ""))


def _grams(text, n=3):
    text = _norm(text)
    if len(text) < n:
        return set()
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def _sim(a, b):
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def feature_matrix(segments, duration=None):
    """返回稳定的逐句特征矩阵；训练和未来推理必须共用此函数。"""
    segs = [[float(s[0]), float(s[1]), str(s[2] or "")] for s in segments]
    if not segs:
        return np.zeros((0, len(FEATURE_NAMES)), dtype=np.float64)
    duration = float(duration or segs[-1][1] or 1.0)
    data = {"segments": segs, "duration": duration}
    stages = stage.label_sentences(data)
    complete, _flags = completeness.compute(data)
    cps = []
    chars = []
    for a, b, text in segs:
        n = len(_norm(text))
        chars.append(n)
        cps.append(n / max(0.2, b - a))

    rows = []
    for i, (a, b, text) in enumerate(segs):
        clean = _norm(text)
        nchar = chars[i]
        dur = max(0.2, b - a)
        lo, hi = max(0, i - 3), min(len(segs), i + 4)
        local_cps = sum(chars[lo:hi]) / max(
            0.2, sum(max(0.2, segs[k][1] - segs[k][0]) for k in range(lo, hi)))
        prev_gap = max(0.0, a - segs[i - 1][1]) if i else 0.0
        next_gap = max(0.0, segs[i + 1][0] - b) if i + 1 < len(segs) else 0.0
        fill = sum(clean.count(word) for word in FILLER) / max(1.0, nchar / 6.0)
        progress = sum(clean.count(word) for word in stage.RESULT_WORDS) / max(1.0, nchar / 8.0)
        action = sum(clean.count(word) for word in ACTION) / max(1.0, nchar / 8.0)

        dup_prev = max((_sim(clean, segs[j][2]) for j in range(max(0, i - 30), i)),
                       default=0.0)
        dup_next = max((_sim(clean, segs[j][2])
                        for j in range(i + 1, min(len(segs), i + 11))), default=0.0)
        neighbor_sim = max((_sim(clean, segs[j][2])
                            for j in range(max(0, i - 3), min(len(segs), i + 4))
                            if j != i), default=0.0)
        mean_cps = sum(cps[lo:hi]) / max(1, hi - lo)
        mean_len = sum(chars[lo:hi]) / max(1, hi - lo)
        prev_text = _norm(segs[i - 1][2]) if i else ""
        next_text = _norm(segs[i + 1][2]) if i + 1 < len(segs) else ""
        lab = stages[i] if i < len(stages) else "progress"

        rows.append([
            min(cps[i], 15.0) / 8.0,
            min(local_cps, 15.0) / 8.0,
            min(1.0, math.log1p(dur) / math.log(20.0)),
            min(1.5, math.log1p(nchar) / math.log(30.0)),
            min(prev_gap, 8.0) / 8.0,
            min(next_gap, 8.0) / 8.0,
            a / max(1.0, duration),
            1.0 if a < 120.0 else 0.0,
            1.0 if a >= max(0.0, duration - 120.0) else 0.0,
            min(1.0, fill),
            1.0 if any(word in clean for word in LOST) else 0.0,
            1.0 if any(word in clean for word in RETAKE) else 0.0,
            1.0 if any(word in clean for word in UNCERTAIN) else 0.0,
            1.0 if any(mark in text for mark in ("?", "？", "怎么", "为什么")) else 0.0,
            min(1.0, progress),
            1.0 if any(word in clean for word in stage.RESULT_WORDS) else 0.0,
            1.0 if any(word in clean for word in stage.GOAL_WORDS) else 0.0,
            1.0 if any(word in clean for word in stage.TEASE_WORDS) else 0.0,
            1.0 if any(word in clean for word in stage.GREET) else 0.0,
            min(1.0, action),
            1.0 if re.search(r"\d|[一二三四五六七八九十百千万]", clean) else 0.0,
            1.0 if any(mark in text for mark in ("!", "！", "哇", "卧槽", "我靠")) else 0.0,
            float(complete[i]) if i < len(complete) else 0.5,
            1.0 if clean.endswith(UNFIN) else 0.0,
            dup_prev,
            dup_next,
            1.0 - neighbor_sim,
            min(2.0, cps[i] / max(0.2, mean_cps)),
            min(2.0, nchar / max(1.0, mean_len)),
            1.0 if prev_text.endswith(UNFIN) else 0.0,
            1.0 if next_text.startswith(completeness.HEAD_BAD) else 0.0,
            1.0 if lab == "hook" else 0.0,
            1.0 if lab == "goal" else 0.0,
            1.0 if lab == "result" else 0.0,
            1.0 if lab == "tease" else 0.0,
            1.0 if nchar <= 3 or dur <= 0.7 else 0.0,
            1.0 if nchar >= 35 or dur >= 10.0 else 0.0,
            min(prev_gap, next_gap, 5.0) / 5.0,
        ])
    return np.asarray(rows, dtype=np.float64)


def sigmoid(values):
    return 1.0 / (1.0 + np.exp(-np.clip(values, -30.0, 30.0)))


def balanced_weights(labels, groups):
    """每部视频总权重相等，视频内正负类别各占一半。"""
    labels = np.asarray(labels, dtype=np.float64)
    groups = np.asarray(groups)
    out = np.zeros(len(labels), dtype=np.float64)
    unique = list(dict.fromkeys(groups.tolist()))
    for group in unique:
        idx = np.flatnonzero(groups == group)
        for value in (0.0, 1.0):
            part = idx[labels[idx] == value]
            if len(part):
                out[part] = 0.5 / (len(unique) * len(part))
    if out.sum() <= 0:
        out[:] = 1.0 / max(1, len(out))
    else:
        out /= out.sum()
    return out


def fit(X, y, groups, epochs=1400, lr=0.18, l2=0.04):
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    sample_w = balanced_weights(y, groups)
    mean = np.average(X, axis=0, weights=sample_w)
    var = np.average((X - mean) ** 2, axis=0, weights=sample_w)
    std = np.sqrt(var) + 1e-8
    Z = (X - mean) / std
    weights = np.zeros(Z.shape[1], dtype=np.float64)
    bias = 0.0
    for _ in range(int(epochs)):
        pred = sigmoid(Z @ weights + bias)
        err = (pred - y) * sample_w
        weights -= lr * (Z.T @ err + l2 * weights)
        bias -= lr * err.sum()
    return {"mean": mean, "std": std, "weights": weights, "bias": float(bias)}


def predict(model, X):
    X = np.asarray(X, dtype=np.float64)
    return sigmoid(((X - model["mean"]) / model["std"]) @ model["weights"]
                   + float(model["bias"]))

