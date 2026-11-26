# -*- coding: utf-8 -*-
"""在第11期开发集和第12期最终确认集评估全片导演审稿。"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import statistics
import sys
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent
_EVAL_ROOT_TEXT = os.environ.get("SMARTCUT_EVAL_ROOT", "").strip()
EVAL_ROOT = Path(_EVAL_ROOT_TEXT).expanduser() if _EVAL_ROOT_TEXT else None
EXPERIMENT = EVAL_ROOT / "evaluation" / "general" if EVAL_ROOT else None
OUT = EVAL_ROOT / "evaluation" / "director" if EVAL_ROOT else None
HOLDOUT_INDEX = EVAL_ROOT / "cache" / "holdout_index.json" if EVAL_ROOT else None
HOLDOUT_SRT = EVAL_ROOT / "subtitles" / "development.srt" if EVAL_ROOT else None
EPISODE12_SRT = EVAL_ROOT / "subtitles" / "confirmation.srt" if EVAL_ROOT else None
EPISODE12_LABELS = (EVAL_ROOT / "evaluation" / "labels" /
                    "confirmation_labels.json") if EVAL_ROOT else None

sys.path.insert(0, str(PROJECT))
import autocut  # noqa: E402
import director_critic  # noqa: E402


def _require_eval_root():
    if EVAL_ROOT is None:
        raise RuntimeError(
            "请先设置 SMARTCUT_EVAL_ROOT，指向你自己的脱敏评估数据根目录。")
    return EVAL_ROOT


def _load_experiment_module():
    _require_eval_root()
    path = EXPERIMENT / "run_general_experiment.py"
    spec = importlib.util.spec_from_file_location("general_experiment_holdout", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载严格 holdout 评估脚本：%s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CACHE = module.cache_map()
    return module


def _sentence_scores(case, model):
    X = case[model.get("feature_key", "base")]
    mean = np.asarray(model["mean"], dtype=float)
    std = np.asarray(model["std"], dtype=float) + 1e-9
    weights = np.asarray(model["weights"], dtype=float)
    raw = np.clip((X - mean) / std, -8.0, 8.0) @ weights
    if str(model.get("kind") or "binary") == "pairwise":
        ratio = max(0.0, min(1.0, float(model.get("target_ratio", 0.0))))
        count = max(1, min(len(raw), int(round(ratio * len(raw)))))
        threshold = float(np.partition(raw, len(raw) - count)[len(raw) - count])
        raw = raw - threshold
    else:
        raw = raw + float(model.get("bias", 0.0))
    return 1.0 / (1.0 + np.exp(-np.clip(raw, -30, 30)))


def _pipeline_spans(data, scores, level="medium"):
    work = copy.deepcopy(data)
    work["scores"] = [round(float(value), 7) for value in scores]
    conf = copy.deepcopy(autocut.load_conf())
    conf.setdefault("select", {}).update({
        "genre": "gameplay", "auto_retake": "0", "retake_llm": "0",
    })
    conf.setdefault("transition", {})["shot_snap"] = "0"
    duration = float(work["duration"])
    semantic = autocut.detect_cuts_by_semantics(work, conf)
    base = autocut.keep_segments(
        semantic if semantic is not None else autocut.detect_cuts(work, conf)[0], duration)
    logs = []
    spans = autocut.apply_cut_level(work, base, conf, level, on_log=logs.append)
    return spans, logs


def _prediction(data, spans):
    return np.asarray(director_critic.mask_from_spans(data, spans), dtype=bool)


def _repeat_proxy(data, mask):
    picked = np.flatnonzero(mask)
    repeated = 0
    for i in picked:
        if any(director_critic.similarity(data["segments"][i][2], data["segments"][j][2]) >= 0.84
               for j in picked if max(0, i - 45) <= j < i):
            repeated += 1
    return repeated / max(1, len(picked))


def _row(exp, case, data, spans, audit):
    pred = _prediction(data, spans)
    row = exp.metrics(case, pred)
    row.update({
        "output_minutes": sum(float(b) - float(a) for a, b in spans) / 60.0,
        "span_count": len(spans),
        "median_span_seconds": statistics.median([float(b) - float(a) for a, b in spans])
        if spans else 0.0,
        "repeat_proxy": _repeat_proxy(data, pred),
        "director_score": audit["score"],
        "director_passed": audit["passed"],
        "director_issues": len(audit["issues"]),
        "director_critical": sum(item["severity"] == "critical" for item in audit["issues"]),
    })
    return row


def _exact_metrics(labels, predictions, stages):
    y = np.asarray(labels, dtype=bool)
    pred = np.asarray(predictions, dtype=bool)
    tp = int((pred & y).sum())
    fp = int((pred & ~y).sum())
    fn = int((~pred & y).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    spine = np.asarray([kind in ("hook", "goal", "result", "tease")
                        for kind in stages], dtype=bool) & y
    return {
        "precision": precision, "recall": recall, "f1": f1,
        "actual_keep_ratio": float(y.mean()),
        "predicted_keep_ratio": float(pred.mean()),
        "spine_recall": float((pred & spine).sum() / spine.sum()) if spine.sum() else 1.0,
        "usable": int(len(y)), "positive": int(y.sum()),
    }


def _episode12_exact_eval(exp, model):
    payload = json.loads(EPISODE12_LABELS.read_text(encoding="utf-8"))
    duration_by_source = {
        row["source"]: float(row["duration"])
        for row in payload["metrics"]["per_source"]
    }
    grouped = {}
    for row in payload["labels"]:
        grouped.setdefault(row["source"], []).append(row)

    before_y, before_pred, before_stages = [], [], []
    after_y, after_pred, after_stages = [], [], []
    source_rows = []
    for source, rows in grouped.items():
        rows.sort(key=lambda item: int(item["raw_index"]))
        segments = [[float(item["start"]), float(item["end"]), str(item["text"])]
                    for item in rows]
        data = {"segments": segments,
                "duration": duration_by_source.get(source, float(segments[-1][1]))}
        case = {"segments": segments, "duration": data["duration"]}
        exp.add_features(case)
        scores = _sentence_scores(case, model)
        initial_spans = director_critic.spans_from_mask(
            data, [float(value) >= 0.5 for value in scores])
        target = sum(b - a for a, b in initial_spans)
        initial_audit = director_critic.review(data, initial_spans, scores, target)
        rebuilt, rebuild = director_critic.rebuild(
            data, initial_spans, scores, target_seconds=target, max_rounds=2)
        initial_mask = director_critic.mask_from_spans(data, initial_spans)
        rebuilt_mask = director_critic.mask_from_spans(data, rebuilt)
        usable = [i for i, item in enumerate(rows) if item.get("label") is not None]
        labels = [bool(rows[i]["label"]) for i in usable]
        stages = [case["stage"][i] for i in usable]
        before_y.extend(labels)
        before_pred.extend(bool(initial_mask[i]) for i in usable)
        before_stages.extend(stages)
        after_y.extend(labels)
        after_pred.extend(bool(rebuilt_mask[i]) for i in usable)
        after_stages.extend(stages)
        source_rows.append({
            "source": source,
            "usable": len(usable),
            "before_director": initial_audit["score"],
            "after_director": rebuild["final"]["score"],
            "accepted_rounds": sum(bool(item["accepted"])
                                   for item in rebuild["history"][1:]),
        })
    return {
        "before": _exact_metrics(before_y, before_pred, before_stages),
        "after": _exact_metrics(after_y, after_pred, after_stages),
        "sources": source_rows,
        "label_source": payload["schema"],
        "leakage_guard": payload["metrics"]["leakage_guard"],
    }


def main():
    _require_eval_root()
    exp = _load_experiment_module()
    raw = json.loads(HOLDOUT_INDEX.read_text(encoding="utf-8"))
    finished = exp.parse_srt(HOLDOUT_SRT)
    case = exp.build_case("第11期_严格holdout", str(raw.get("source") or ""),
                          str(HOLDOUT_SRT), raw_data=raw, fin_segments=finished)
    candidate = json.loads((EXPERIMENT / "general_model_candidate.json").read_text(encoding="utf-8"))
    current = json.loads((PROJECT / "conf" / "editor_model.json").read_text(encoding="utf-8"))

    variants = {}
    specs = (
        ("current_score_candidate", current, "local"),
        ("general_score_candidate", candidate, "local"),
        ("current_full_pipeline", current, "pipeline"),
        ("general_full_pipeline", candidate, "pipeline"),
    )
    for name, model, source_mode in specs:
        scores = _sentence_scores(case, model)
        if source_mode == "pipeline":
            initial_spans, logs = _pipeline_spans(raw, scores, "medium")
        else:
            initial_spans = director_critic.spans_from_mask(
                raw, [float(value) >= 0.5 for value in scores])
            logs = ["逐句候选：score >= 0.5；尚未经过全局结构层"]
        target = sum(float(b) - float(a) for a, b in initial_spans)
        initial_audit = director_critic.review(raw, initial_spans, scores, target)
        rebuilt_spans, rebuild = director_critic.rebuild(
            raw, initial_spans, scores, target_seconds=target, max_rounds=2)
        variants[name] = {
            "before": _row(exp, case, raw, initial_spans, initial_audit),
            "after": _row(exp, case, raw, rebuilt_spans, rebuild["final"]),
            "rebuild": rebuild,
            "source_mode": source_mode,
            "pipeline_logs": logs[-16:],
        }

    episode12_exact = None
    if EPISODE12_LABELS.is_file():
        episode12_exact = {
            "current_score_candidate": _episode12_exact_eval(exp, current),
            "general_score_candidate": _episode12_exact_eval(exp, candidate),
            "note": "一次性严格确认；第12期不得用于训练、阈值或审稿规则调参。",
        }

    report = {
        "generated": "2026-11-22",
        "holdout": "第11期开发验证；第12期为一次性最终确认",
        "alignment_coverage": case["coverage"],
        "human_positive_ratio": float(case["y"].mean()),
        "variants": variants,
        "episode12_exact_holdout": episode12_exact,
        "decision": "candidate_only_not_integrated",
        "limitations": [
            "holdout 标签来自 ASR 台词对齐，不能评价纯视觉高光。",
            "导演分是结构一致性指标，不替代人工内容质量判断。",
            "只有 F1/召回、导演分、重复率和章节覆盖同时不退化，才建议接生产。",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "director_critic_holdout_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["第11期严格 holdout：导演审稿 + 反馈重建", "=" * 46, ""]
    for name, result in variants.items():
        lines.append(name)
        for phase in ("before", "after"):
            row = result[phase]
            lines.append("  %-6s P/R/F1 %.3f/%.3f/%.3f  骨架 %.3f  章节 %s  "
                         "导演 %.1f  重复 %.3f  %.2f 分钟" % (
                             phase, row["precision"], row["recall"], row["f1"],
                             row["spine_recall"],
                             "%.3f" % row["chapter_coverage"]
                             if row.get("chapter_coverage") is not None else "n/a",
                             row["director_score"], row["repeat_proxy"],
                             row["output_minutes"]))
        delta = result["after"]["f1"] - result["before"]["f1"]
        lines.append("  F1 变化：%+.3f；导演分变化：%+.1f；接受迭代：%d" % (
            delta,
            result["after"]["director_score"] - result["before"]["director_score"],
            sum(bool(item["accepted"]) for item in result["rebuild"]["history"][1:])))
        lines.append("")
    if episode12_exact:
        lines.append("第12期 Premiere 精确标签（1178 个可用标签，一次性确认）")
        for name in ("current_score_candidate", "general_score_candidate"):
            result = episode12_exact[name]
            before, after = result["before"], result["after"]
            lines.append("  %s" % name)
            lines.append("    before P/R/F1 %.3f/%.3f/%.3f  骨架 %.3f  保留 %.3f" % (
                before["precision"], before["recall"], before["f1"],
                before["spine_recall"], before["predicted_keep_ratio"]))
            lines.append("    after  P/R/F1 %.3f/%.3f/%.3f  骨架 %.3f  保留 %.3f" % (
                after["precision"], after["recall"], after["f1"],
                after["spine_recall"], after["predicted_keep_ratio"]))
        lines.append("  注：按 7 个原片分别执行导演重建后汇总；未用第12期调任何规则。")
        lines.append("")
    lines.append("结论：候选能力未接入生产；报告只写入验收目录。")
    (OUT / "director_critic_holdout_report.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

