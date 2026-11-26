# -*- coding: utf-8 -*-
"""按视频分组、使用高置信标签训练通用剪辑价值候选模型。"""
from __future__ import annotations

import argparse
import json
import os
import time

import numpy as np

import general_model
import learn
import stage


def _metrics(y, score, threshold):
    y = np.asarray(y, dtype=np.float64)
    pred = np.asarray(score) >= float(threshold)
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int(((~pred) & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"prec": prec, "rec": rec, "f1": f1,
            "n": int(len(y)), "positive": int(y.sum()), "picked": int(pred.sum())}


def _average_precision(y, score):
    order = np.argsort(-np.asarray(score))
    labels = np.asarray(y)[order]
    total = int(labels.sum())
    if not total:
        return 0.0
    hit = 0
    acc = 0.0
    for rank, value in enumerate(labels, 1):
        if value:
            hit += 1
            acc += hit / rank
    return acc / total


def _best_threshold(groups, model):
    candidates = np.linspace(0.15, 0.85, 29)
    best = (0.0, 0.5)
    for threshold in candidates:
        vals = []
        for sample in groups:
            score = general_model.predict(model, sample["X"])
            vals.append(_metrics(sample["y"], score, threshold)["f1"])
        macro = float(np.mean(vals)) if vals else 0.0
        if macro > best[0] + 1e-12:
            best = (macro, float(threshold))
    return best[1]


def build_pair(raw_path, final_path, min_similarity=0.52):
    raw = learn.transcribe_cached(raw_path, "原片")
    final = learn.transcribe_cached(final_path, "成品")
    pairs = learn.align(final.get("segments") or [], raw.get("segments") or [])
    if len(pairs) < 10:
        return None
    cover = len(pairs) / max(1, len(final.get("segments") or []))
    if cover < 0.45:
        return None

    raw_segments = raw["segments"]
    direct = {}
    for fi, ri in pairs:
        similarity = learn.dice(learn.bigrams(final["segments"][fi][2]),
                                learn.bigrams(raw_segments[ri][2]))
        direct[ri] = max(direct.get(ri, 0.0), similarity)

    labels = np.full(len(raw_segments), -1.0, dtype=np.float64)
    confidence = np.zeros(len(raw_segments), dtype=np.float64)
    strong = sorted(i for i, similarity in direct.items() if similarity >= min_similarity)
    for i in strong:
        labels[i] = 1.0
        confidence[i] = 0.5 + 0.5 * direct[i]
    # 对齐句相邻一句经常是一对多 ASR 边界，保留为未知；远离正样本才作为负样本。
    positive_set = set(strong)
    for i in range(len(raw_segments)):
        if labels[i] >= 0:
            continue
        if any(abs(i - p) <= 1 for p in positive_set):
            continue
        labels[i] = 0.0
        confidence[i] = 1.0

    mask = labels >= 0
    if int((labels[mask] == 1).sum()) < 8 or int((labels[mask] == 0).sum()) < 8:
        return None
    X = general_model.feature_matrix(raw_segments, raw.get("duration"))
    stages = stage.label_sentences({"segments": raw_segments,
                                    "duration": raw.get("duration")})
    keep_ratio = float((labels[mask] == 1).sum() / max(1, mask.sum()))
    return {
        "name": os.path.basename(final_path), "raw": raw_path, "final": final_path,
        "X": X[mask], "y": labels[mask], "confidence": confidence[mask],
        "stages": np.asarray(stages, dtype=object)[mask],
        "cover": cover, "keep_ratio": keep_ratio,
        "n_ignored": int((~mask).sum()),
    }


def evaluate_loo(samples):
    rows = []
    for index, test in enumerate(samples):
        train = [sample for i, sample in enumerate(samples) if i != index]
        X = np.vstack([sample["X"] for sample in train])
        y = np.concatenate([sample["y"] for sample in train])
        groups = np.concatenate([[sample["name"]] * len(sample["y"]) for sample in train])
        model = general_model.fit(X, y, groups)
        threshold = _best_threshold(train, model)
        score = general_model.predict(model, test["X"])
        row = _metrics(test["y"], score, threshold)
        row.update({"name": test["name"], "threshold": threshold,
                    "average_precision": _average_precision(test["y"], score),
                    "cover": test["cover"], "keep_ratio": test["keep_ratio"],
                    "ignored": test["n_ignored"]})
        k = int(test["y"].sum())
        order = np.argsort(-score)[:k]
        row["topk_recall"] = float(test["y"][order].sum() / max(1, k))
        spine = np.isin(test["stages"], ["hook", "goal", "result", "tease"]) & (test["y"] == 1)
        pred = score >= threshold
        row["spine_coverage"] = float((pred & spine).sum() / max(1, spine.sum()))
        rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", nargs=2, action="append", default=[],
                        metavar=("原片", "成品"))
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    os.makedirs(args.out_dir, exist_ok=True)

    samples = []
    for raw, final in args.pair:
        sample = build_pair(raw, final)
        if sample:
            samples.append(sample)
    if len(samples) < 3:
        print("可用视频不足 3 组，停止训练。", flush=True)
        return 1

    loo = evaluate_loo(samples)
    macro_keys = ("prec", "rec", "f1", "average_precision", "topk_recall",
                  "spine_coverage")
    macro = {key: float(np.mean([row[key] for row in loo])) for key in macro_keys}

    X = np.vstack([sample["X"] for sample in samples])
    y = np.concatenate([sample["y"] for sample in samples])
    groups = np.concatenate([[sample["name"]] * len(sample["y"]) for sample in samples])
    fitted = general_model.fit(X, y, groups)
    threshold = _best_threshold(samples, fitted)
    model = {
        "model_type": "general_group_balanced_v1",
        "feature_names": general_model.FEATURE_NAMES,
        "mean": fitted["mean"].tolist(), "std": fitted["std"].tolist(),
        "weights": fitted["weights"].tolist(), "bias": fitted["bias"],
        "threshold": threshold, "videos": len(samples),
        "trained": time.strftime("%Y-%m-%d %H:%M:%S"),
        "macro_loo": macro, "loo": loo,
    }
    model_path = os.path.join(args.out_dir, "general_model_candidate.json")
    report_path = os.path.join(args.out_dir, "general_model_report.json")
    with open(model_path, "w", encoding="utf-8") as file:
        json.dump(model, file, ensure_ascii=False, indent=2)
    with open(report_path, "w", encoding="utf-8") as file:
        json.dump({"macro": macro, "videos": loo,
                   "decision": "candidate_only_not_promoted"},
                  file, ensure_ascii=False, indent=2)
    print(json.dumps({"model": model_path, "report": report_path,
                      "macro": macro}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
