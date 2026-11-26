# -*- coding: utf-8 -*-
"""句子价值模型：一句台词值不值得留。

为什么不用现有的模型分：实测它区分不开人工留/丢的句子
（留下但人工丢掉的 258 句平均分 0.49，人工也留的平均分 0.48）。

这里用手工特征 + 逻辑回归，权重从"人工成片 vs 原片"的配对里学：
    filler  口头禅密度（嗯/啊/那个/就是/然后…）
    lost    "找东西/卡壳"类词（在哪/找不到/等一下/看下/再来一次…）
    unfin   句尾是半句（然后/就是/那个…）
    sem     语义类型（filler/noise 扣分，laugh 加分）
    cps     语速（太快像赶、太慢像卡壳）
    dur     句长
    score   现有模型分
    motion  画面变化（有动作的多半有内容）
    level   音量
    dup     和前面 30 句里某句很像（重复啰嗦）
    pos     在素材里的相对位置
"""
from __future__ import annotations

import json
import math
import os
import re

from memory_config import app_home

BASE = app_home()
WEIGHTS_PATH = os.path.join(BASE, "conf", "cut_value.json")

FILLER = ("嗯", "啊", "呃", "额", "那个", "这个", "就是", "然后", "其实",
          "的话", "对吧", "是不是", "你看", "什么呢", "怎么讲")
LOST = ("在哪", "找不到", "找一下", "找找", "等一下", "等等", "看下", "看一下",
        "翻一下", "翻翻", "回头", "再来一次", "重来", "算了", "无所谓",
        "想起来", "忘了", "记不清")
UNFIN = ("然后", "就是", "那个", "这个", "所以", "因为", "但是", "而且",
         "的话", "的时候", "还有", "以及")

FEATURES = ["filler", "lost", "unfin", "sem_bad", "sem_laugh", "cps_dev",
            "dur", "score", "motion", "level", "dup", "pos", "const"]

DEFAULT_W = {k: 0.0 for k in FEATURES}
DEFAULT_W["const"] = 0.5


def _norm(t):
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(t or ""))


def _grams(t, n=3):
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _sim(a, b):
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / float(len(ga) + len(gb) - inter)


def features(data):
    """逐句算特征，返回 [[f1..fN], ...]（顺序与 data['segments'] 一致）。"""
    segs = [(float(a), float(b), _norm(t)) for a, b, t in (data.get("segments") or [])]
    n = len(segs)
    sem = data.get("semantic") or []
    sc = data.get("scores") or []
    feat = data.get("seg_feat") or []
    dur_all = float(data.get("duration") or (segs[-1][1] if segs else 1)) or 1.0
    rows = []
    for i, (a, b, t) in enumerate(segs):
        d = max(0.2, b - a)
        cps = len(t) / d
        fill = sum(t.count(w) for w in FILLER) / max(1.0, len(t) / 6.0)
        lost = 1.0 if any(w in t for w in LOST) else 0.0
        unfin = 1.0 if t.endswith(UNFIN) else 0.0
        kind = (sem[i].get("kind") if i < len(sem) and isinstance(sem[i], dict) else "")
        bad = 1.0 if kind in ("filler", "noise") else 0.0
        laugh = 1.0 if kind == "laugh" else 0.0
        # 语速偏离 2.5~6.5 字/秒越远越差（0 最好，1 最差）
        if cps <= 0:
            cps_dev = 1.0
        elif 2.5 <= cps <= 6.5:
            cps_dev = 0.0
        else:
            cps_dev = min(1.0, abs(cps - (4.5 if cps > 6.5 else 2.5)) / 4.5)
        dup = 0.0
        for j in range(max(0, i - 30), i):
            if len(segs[j][2]) >= 6 and _sim(t, segs[j][2]) >= 0.80:
                dup = 1.0
                break
        mo = float(feat[i][0]) if i < len(feat) and len(feat[i]) > 0 else 0.0
        lv = float(feat[i][1]) if i < len(feat) and len(feat[i]) > 1 else -40.0
        rows.append([
            min(1.0, fill), lost, unfin, bad, laugh, cps_dev,
            min(1.0, math.log1p(d) / math.log(20)),
            float(sc[i]) if i < len(sc) else 0.5,
            min(1.0, mo / 6.0), max(0.0, min(1.0, (lv + 50) / 40.0)),
            dup, a / dur_all, 1.0,
        ])
    return rows


def predict(rows, weights=None):
    w = weights or load_weights()
    vec = [float(w.get(k, 0.0)) for k in FEATURES]
    out = []
    for r in rows:
        z = sum(v * x for v, x in zip(vec, r))
        out.append(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))))
    return out


def has_trained() -> bool:
    """有没有真正训练过的权重（没有就用索引里的模型分，别拿兜底常数当真）。"""
    try:
        import json as _j
        d = _j.load(open(WEIGHTS_PATH, encoding="utf-8"))
        return isinstance(d, dict) and isinstance(d.get("w"), dict) and bool(d.get("meta"))
    except Exception:  # noqa: BLE001
        return False


def load_weights():
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and "w" in d:
            return {**DEFAULT_W, **{k: float(v) for k, v in d["w"].items()}}
    except Exception:  # noqa: BLE001
        pass
    return dict(DEFAULT_W)


def save_weights(w, meta=None):
    os.makedirs(os.path.dirname(WEIGHTS_PATH), exist_ok=True)
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as fh:
        json.dump({"w": {k: round(float(v), 4) for k, v in w.items()},
                   "meta": meta or {}}, fh, ensure_ascii=False, indent=2)


def train(samples, iters=800, lr=0.35, l2=0.02, drop=("pos",)):
    """samples = [(row, label)]；标签 1=人工留，0=人工丢。返回权重。

    最后一步做**先验校准**：把偏置调到"预测出的留的比例 ≈ 人工留的比例"。
    不做这一步，输出会整体偏低（实测阈值 0.5 时一句都不留），
    下游按阈值挑选会直接失效。

    drop：训练后置零的特征。默认去掉 pos —— 消融实验（留一验证）里它对
    F1 没有贡献，还会引入"后段必砍"的偏置。
    """
    import numpy as np
    X = np.array([s[0] for s in samples], dtype=np.float64)
    y = np.array([s[1] for s in samples], dtype=np.float64)
    w = np.array([DEFAULT_W[k] for k in FEATURES], dtype=np.float64)
    n = max(1, len(y))
    for _ in range(iters):
        z = X @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = X.T @ (p - y) / n + l2 * w
        w -= lr * g
    # 让权重有可读性：归一到 |w| 最大为 3
    mx = float(np.max(np.abs(w))) or 1.0
    if mx > 3.0:
        w *= 3.0 / mx
    for k in (drop or ()):
        if k in FEATURES:
            w[FEATURES.index(k)] = 0.0
    # ---- 先验校准 ----
    target = float(y.mean()) if n else 0.5
    ci = FEATURES.index("const")
    w0 = w.copy()
    w0[ci] = 0.0
    base = X @ w0                          # 不含偏置的线性输出
    lo, hi = -12.0, 12.0
    for _ in range(60):                   # 二分找偏置，使平均预测概率≈目标比例
        mid = (lo + hi) / 2.0
        p = 1.0 / (1.0 + np.exp(-np.clip(base + mid, -30, 30)))
        if p.mean() > target:
            hi = mid
        else:
            lo = mid
    w[ci] = (lo + hi) / 2.0
    return {k: float(v) for k, v in zip(FEATURES, w)}


def evaluate(rows, labels, weights):
    """句子级 precision/recall/F1（阈值 0.5）+ AUC 近似。"""
    import numpy as np
    p = np.array(predict(rows, weights))
    y = np.array(labels, dtype=float)
    pick = p >= 0.5
    tp = float(((pick == 1) & (y == 1)).sum())
    fp = float(((pick == 1) & (y == 0)).sum())
    fn = float(((pick == 0) & (y == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    # 时长加权版本（更贴近成片长度）
    dur = np.array([r[6] for r in rows])
    return {"prec": prec, "rec": rec, "f1": f1, "n": int(len(y)),
            "n_pos": int(y.sum()), "n_pick": int(pick.sum())}
