# -*- coding: utf-8 -*-
"""关键节点画面审稿（已接入智能剪辑生产链）。

它不逐帧扫描整条视频，而是在剪切边界、章节转折、叙事骨架句和高分句附近
各取前/中/后三帧。默认只用 OpenCV/NumPy 做可复现的黑屏、静止、重复和
突变代理检测；机器上存在 SmolVLM2 + mmproj 时，可显式启用视觉模型复核
少量高风险节点。视觉模型不可用或超时时，规则报告仍然完整可用。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

import llm_gateway

BASE = Path(__file__).resolve().parent
VLM_DIR = BASE / "models" / "vlm"
VLM_CLI = BASE / "bin" / "llama" / "llama-mtmd-cli.exe"

ROLE_PRIORITY = {"hook": 96, "goal": 94, "result": 98, "tease": 92, "closing": 92}


def _no_window():
    return 0x08000000 if os.name == "nt" else 0


def _ffmpeg():
    try:
        import index as index_module
        return str(index_module.find_ffmpeg())
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _duration(data):
    segs = data.get("segments") or []
    return float(data.get("duration") or (segs[-1][1] if segs else 0.0))


def _sample_times(time_value, duration, delta=1.0):
    center = max(0.0, min(float(duration), float(time_value)))
    return [max(0.0, center - delta), center, min(float(duration), center + delta)]


def select_nodes(data, spans=None, sentence_scores=None, max_nodes=18, delta=1.0):
    """选择少量最值得看画面的节点，返回 JSON 可序列化字典列表。"""
    segs = data.get("segments") or []
    duration = _duration(data)
    if duration <= 0:
        return []
    candidates = []

    def add(priority, kind, time_value, reason, segment_index=None, sample_times=None,
            gap_seconds=None):
        time_value = max(0.0, min(duration, float(time_value)))
        candidates.append({
            "priority": int(priority), "kind": str(kind), "time": round(time_value, 3),
            "reason": str(reason), "segment_index": segment_index,
            "sample_times": [round(float(v), 3) for v in
                             (sample_times or _sample_times(time_value, duration, delta))],
            "gap_seconds": None if gap_seconds is None else round(float(gap_seconds), 3),
        })

    clean_spans = sorted((float(a), float(b)) for a, b in (spans or []) if float(b) > float(a))
    for index, (left, right) in enumerate(zip(clean_spans, clean_spans[1:])):
        left_time = max(left[0], left[1] - 0.18)
        right_time = min(right[1], right[0] + 0.18)
        add(110, "edit_boundary", right[0], "成片相邻片段的视觉接缝",
            sample_times=[max(0.0, left_time - delta), left_time, right_time],
            gap_seconds=max(0.0, right[0] - left[1]))

    for section in ((data.get("script") or {}).get("sections") or []):
        start = float(section.get("start", 0.0))
        title = str(section.get("title") or "未命名章节")
        add(100, "chapter_transition", start, "章节转折：%s" % title)

    try:
        import stage
        roles = stage.label_sentences(data)
    except Exception:  # noqa: BLE001
        roles = []
    for index, segment in enumerate(segs):
        role = roles[index] if index < len(roles) else ""
        if role in ROLE_PRIORITY:
            center = (float(segment[0]) + float(segment[1])) / 2.0
            add(ROLE_PRIORITY[role], "story_%s" % role, center,
                "叙事骨架：%s" % role, segment_index=index)

    scores = (None if llm_gateway.api_enabled() else
              sentence_scores if sentence_scores is not None else data.get("scores"))
    if scores is not None and len(scores) == len(segs):
        ranked = sorted(range(len(segs)), key=lambda i: float(scores[i]), reverse=True)
        picked = 0
        for index in ranked:
            center = (float(segs[index][0]) + float(segs[index][1])) / 2.0
            if any(abs(center - item["time"]) < 8.0 and item["kind"] == "score_peak"
                   for item in candidates):
                continue
            add(80, "score_peak", center, "逐句价值高分 %.3f" % float(scores[index]),
                segment_index=index)
            picked += 1
            if picked >= 5:
                break

    # 没有剧本/分数的退化素材也覆盖开头、中段、结尾，避免视觉层直接失效。
    if len(candidates) < 3:
        for ratio, name in ((0.08, "开头"), (0.5, "中段"), (0.92, "结尾")):
            add(40, "timeline_probe", duration * ratio, "退化时间线探针：%s" % name)

    candidates.sort(key=lambda item: (-item["priority"], item["time"], item["kind"]))
    limit = max(1, int(max_nodes))

    def even_sample(rows, count):
        rows = sorted(rows, key=lambda item: (item["time"], -item["priority"], item["kind"]))
        if count <= 0:
            return []
        if len(rows) <= count:
            return rows
        if count == 1:
            return [rows[len(rows) // 2]]
        picked = []
        for index in range(count):
            pos = round(index * (len(rows) - 1) / float(count - 1))
            row = rows[int(pos)]
            if row not in picked:
                picked.append(row)
        return picked

    boundaries = [row for row in candidates if row["kind"] == "edit_boundary"]
    story = [row for row in candidates if row["kind"].startswith("story_")]
    chapters = [row for row in candidates if row["kind"] == "chapter_transition"]
    peaks = [row for row in candidates if row["kind"] == "score_peak"]
    selected = []
    # 接缝最多占一半名额，剩余名额保留给剧情节点、章节和高价值句；每组
    # 都按时间均匀取样，确保长片的后半段不会永远看不到。
    selected.extend(even_sample(boundaries, min(len(boundaries), max(1, limit // 2))))
    remaining = limit - len(selected)
    selected.extend(even_sample(story, min(len(story), max(0, remaining // 2))))
    remaining = limit - len(selected)
    selected.extend(even_sample(chapters, min(len(chapters), min(2, remaining))))
    remaining = limit - len(selected)
    selected.extend(even_sample(peaks, min(len(peaks), remaining)))
    chosen = {(row["kind"], row["time"]) for row in selected}
    for item in candidates:
        if len(selected) >= limit:
            break
        key = (item["kind"], item["time"])
        if key in chosen:
            continue
        if item["kind"] != "edit_boundary" and any(
                other["kind"] != "edit_boundary" and abs(item["time"] - other["time"]) < 2.5
                for other in selected):
            continue
        selected.append(item)
        chosen.add(key)
    selected.sort(key=lambda item: item["time"])
    for index, item in enumerate(selected):
        item["id"] = "visual_%03d" % (index + 1)
    return selected


def _extract_one(video, time_value, output, width=384):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", "%.3f" % max(0.0, float(time_value)), "-i", str(video),
        "-frames:v", "1", "-vf", "scale=%d:-2" % int(width), "-q:v", "4",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, timeout=45,
                            creationflags=_no_window())
    return result.returncode == 0 and output.is_file() and output.stat().st_size > 500


def extract_node_frames(video, node, cache_dir, width=384):
    """按节点抽三帧，并把缓存身份/精度写进节点供审核链追溯。

    精确接缝、成片头尾必须走源时间精确帧；章节、剧情与 VLM 上下文走
    2fps 共享代理。代理只用于理解场景，不能拿它覆盖音频切点或自动否决。
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    frame_cache = node.pop("_frame_cache", None)
    if frame_cache is None:
        try:
            import video_frame_cache
            frame_cache = video_frame_cache.VideoFrameCache(str(video))
        except Exception:  # noqa: BLE001
            frame_cache = None
    exact = str(node.get("kind") or "") in {
        "edit_boundary", "output_start", "output_end",
    }
    paths = []
    evidence = []
    for time_value in node["sample_times"]:
        path = None
        meta = None
        if frame_cache is not None:
            try:
                if exact:
                    path, meta = frame_cache.exact_frame_path_at(
                        time_value, width=width)
                else:
                    path, meta = frame_cache.frame_path_at(time_value)
            except Exception as exc:  # noqa: BLE001
                try:
                    import video_frame_cache
                    if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                        raise
                except ImportError:
                    pass
                meta = {"precision": "legacy_exact_fallback", "reused": False,
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        if not path:
            token = hashlib.sha1((str(Path(video)) + "|%.6f|%d" %
                                  (float(time_value), int(width))).encode("utf-8")).hexdigest()[:14]
            fallback = cache_dir / ("frame_%s.jpg" % token)
            reused = fallback.is_file() and fallback.stat().st_size > 500
            if reused or _extract_one(video, time_value, fallback, width=width):
                path = str(fallback)
            if meta is None:
                meta = {"precision": "legacy_exact_fallback", "reused": reused}
        paths.append(str(path) if path else None)
        evidence.append(meta or {"precision": "unavailable", "reused": False})
    node["frame_cache_evidence"] = evidence
    node["frame_precision"] = ("source_seek_nearest_frame" if exact else
                               "review_proxy_nearest_0.5s")
    node["visual_authority"] = ("seam_evidence" if exact else
                                "context_only_no_veto")
    return paths


def frame_features(path):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("无法读取帧：%s" % path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    hist = cv2.calcHist([image], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3).ravel()
    hist = hist / (hist.sum() + 1e-9)
    values = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    values /= values.sum() + 1e-12
    nz = values[values > 0]
    entropy = float(-(nz * np.log2(nz)).sum())
    edges = cv2.Canny(gray, 80, 160)
    return {
        "brightness": float(gray.mean()),
        "dark_ratio": float(np.mean(gray < 22)),
        "bright_ratio": float(np.mean(gray > 235)),
        "edge_density": float(np.mean(edges > 0)),
        "entropy": entropy,
        "contrast": float(gray.std()),
        "hist": hist,
        "small": small,
    }


def frame_distance(left, right):
    """0 表示近似相同，1 表示画面完全不同。"""
    hist = 0.5 * float(np.abs(left["hist"] - right["hist"]).sum())
    pixel = float(np.mean(np.abs(left["small"] - right["small"])))
    return max(0.0, min(1.0, 0.62 * hist + 0.38 * min(1.0, pixel * 2.2)))


def _public_features(item):
    return {key: round(float(item[key]), 5) for key in
            ("brightness", "dark_ratio", "bright_ratio", "edge_density",
             "entropy", "contrast")}


def analyze_node(node, frame_paths):
    loaded = []
    for path in frame_paths:
        if not path:
            loaded.append(None)
            continue
        try:
            loaded.append(frame_features(path))
        except Exception:  # noqa: BLE001
            loaded.append(None)
    issues = []
    score = 100.0
    if any(item is None for item in loaded):
        issues.append({"type": "frame_missing", "severity": "critical",
                       "action": "inspect", "message": "关键帧提取或解码失败"})
        score -= 50.0
    valid = [item for item in loaded if item is not None]
    distances = [None, None, None]
    if all(item is not None for item in loaded):
        distances = [frame_distance(loaded[0], loaded[1]),
                     frame_distance(loaded[1], loaded[2]),
                     frame_distance(loaded[0], loaded[2])]
        center = loaded[1]
        # 暗色游戏场景不能仅凭平均亮度被当成黑屏。确定性否决要求画面同时
        # 极暗、低信息量且几乎没有边缘；纯色空白则使用更严格的低熵条件。
        black_like = (center["brightness"] < 14.0 and center["dark_ratio"] > 0.94
                      and center["entropy"] < 3.0 and center["edge_density"] < 0.025)
        flat_like = (center["entropy"] < 1.5 and center["contrast"] < 12.0
                     and center["edge_density"] < 0.015)
        if black_like or flat_like:
            issues.append({"type": "black_or_blank", "severity": "major",
                           "action": "inspect_or_drop",
                           "message": "中心帧疑似黑屏、空白或加载过渡"})
            score -= 28.0
        static = max(distances[:2]) < 0.035
        if static and center["edge_density"] >= 0.035:
            issues.append({"type": "static_ui_like", "severity": "minor",
                           "action": "inspect",
                           "message": "前中后三帧几乎不变，可能是菜单、背包或停滞画面"})
            score -= 7.0
        # 高分句可能只是解释、吐槽或菜单操作；静止并不等于剧情结果缺失。
        # 只有明确标成 result 的叙事节点，规则层才能把“无画面变化”升级为
        # 结果画面缺失。其他高分节点保持为次级视觉提示。
        if node["kind"] == "story_result" and static:
            issues.append({"type": "claimed_progress_without_visual_change", "severity": "major",
                           "action": "inspect_or_replace",
                           "message": "高价值/结果节点没有可见画面变化"})
            score -= 12.0
        if node["kind"] == "edit_boundary" and distances[1] > 0.62:
            issues.append({"type": "hard_visual_jump", "severity": "major",
                           "action": "inspect_transition",
                           "message": "相邻成片段画面差异很大，需确认是否为合理跳切"})
            score -= 13.0
        if node["kind"] == "edit_boundary" and (node.get("gap_seconds") or 0) >= 8.0 \
                and distances[1] < 0.045:
            issues.append({"type": "invisible_time_skip", "severity": "minor",
                           "action": "inspect_pacing",
                           "message": "跨过较长原片时间但画面几乎不变，可能仍在同一菜单/等待状态"})
            score -= 5.0
    scene_proxy = "unavailable"
    if valid:
        center = loaded[1] if loaded[1] is not None else valid[len(valid) // 2]
        if center["brightness"] < 16 or center["dark_ratio"] > 0.94:
            scene_proxy = "black_or_loading_like"
        elif len(valid) == 3 and max(value for value in distances[:2] if value is not None) < 0.035 \
                and center["edge_density"] >= 0.035:
            scene_proxy = "static_ui_like"
        else:
            scene_proxy = "active_or_changing"
    return {
        **node,
        "frame_paths": frame_paths,
        "frame_features": [_public_features(item) if item is not None else None for item in loaded],
        "distances": {
            "before_to_center": None if distances[0] is None else round(distances[0], 5),
            "center_to_after": None if distances[1] is None else round(distances[1], 5),
            "before_to_after": None if distances[2] is None else round(distances[2], 5),
        },
        "scene_proxy": scene_proxy,
        "score": round(max(0.0, score), 3),
        "issues": issues,
    }


def _add_cross_node_duplicates(rows):
    features = []
    for row in rows:
        path = row.get("frame_paths", [None, None, None])[1]
        try:
            features.append(frame_features(path) if path else None)
        except Exception:  # noqa: BLE001
            features.append(None)
    for index, current in enumerate(features):
        if current is None:
            continue
        for earlier in range(index):
            if features[earlier] is None or abs(rows[index]["time"] - rows[earlier]["time"]) < 15.0:
                continue
            distance = frame_distance(features[earlier], current)
            if distance < 0.022:
                rows[index]["issues"].append({
                    "type": "repeated_visual", "severity": "minor", "action": "inspect_duplicate",
                    "message": "与 %.1f 秒节点画面近乎重复" % rows[earlier]["time"],
                })
                rows[index]["score"] = round(max(0.0, rows[index]["score"] - 6.0), 3)
                break


def make_contact_sheet(frame_paths, output):
    images = []
    for label, path in zip(("BEFORE", "CENTER", "AFTER"), frame_paths):
        if path:
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        else:
            image = None
        if image is None:
            image = np.zeros((216, 384, 3), dtype=np.uint8)
        image = cv2.resize(image, (384, 216), interpolation=cv2.INTER_AREA)
        cv2.rectangle(image, (0, 0), (132, 30), (0, 0, 0), -1)
        cv2.putText(image, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                    (255, 255, 255), 1, cv2.LINE_AA)
        images.append(image)
    sheet = np.hstack(images)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        raise RuntimeError("联系表编码失败")
    encoded.tofile(output)
    return str(output)


def _vlm_files(tier="fast"):
    if tier == "accurate":
        model = VLM_DIR / "SmolVLM2-2.2B-Instruct-Q8_0.gguf"
        projector = VLM_DIR / "mmproj-SmolVLM2-2.2B-Instruct-f16.gguf"
    else:
        model = VLM_DIR / "SmolVLM2-500M-Video-Instruct-Q8_0.gguf"
        projector = VLM_DIR / "mmproj-SmolVLM2-500M-Video-Instruct-f16.gguf"
    return model, projector


def vlm_available(tier="fast"):
    model, projector = _vlm_files(tier)
    return VLM_CLI.is_file() and model.is_file() and projector.is_file()


def describe_contact_sheet(contact_sheet, tier="fast", timeout=360):
    """显式调用本地视觉模型。失败返回结构化错误，不影响规则审稿。"""
    model, projector = _vlm_files(tier)
    if not vlm_available(tier):
        return {"status": "unavailable", "raw": "", "parsed": None}
    prompt = (
        "The image contains BEFORE, CENTER and AFTER frames from a gameplay edit. "
        "Return JSON only with keys scene_type, temporal_change, continuity, risk, summary. "
        "scene_type must be gameplay, menu_ui, loading_black, facecam, or unclear. "
        "temporal_change must be meaningful, idle, or abrupt. continuity must be coherent, "
        "uncertain, or broken. risk must be none, review, or drop. Keep summary under 20 words."
    )
    command = [
        str(VLM_CLI), "-m", str(model), "--mmproj", str(projector),
        "--image", str(contact_sheet), "-p", prompt, "-n", "150", "-c", "2048",
        "-ngl", "all", "--temp", "0", "--log-colors", "off",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                timeout=max(30, int(timeout)), creationflags=_no_window())
        # mtmd-cli 的生成文本在 stdout，加载日志在 stderr；--log-disable 会连生成
        # 文本一起吞掉，因此保留日志并只解析 stdout。
        raw = (result.stdout or "").strip()
        if not raw:
            raw = (result.stderr or "").strip()
        match = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(match.group(0)) if match else None
        schema_valid = bool(parsed
                            and parsed.get("scene_type") in
                            {"gameplay", "menu_ui", "loading_black", "facecam", "unclear"}
                            and parsed.get("temporal_change") in
                            {"meaningful", "idle", "abrupt"}
                            and parsed.get("continuity") in
                            {"coherent", "uncertain", "broken"}
                            and parsed.get("risk") in {"none", "review", "drop"})
        status = "ok" if result.returncode == 0 and schema_valid else (
            "partial" if result.returncode == 0 and parsed else "error")
        return {"status": status,
                "raw": raw[-2000:], "parsed": parsed,
                "schema_valid": schema_valid,
                "returncode": int(result.returncode)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "raw": str(exc)[:500], "parsed": None}


def review_video(video, data, spans=None, sentence_scores=None, output_dir=None,
                 max_nodes=18, use_vlm=False, vlm_limit=2, vlm_tier="fast",
                 on_log=None, should_cancel=None):
    """执行关键节点视觉审稿并返回报告；默认不启动大模型。"""
    import time
    started = time.time()
    output_dir = Path(output_dir or (BASE / "cache_visual_critic"))
    frames_dir = output_dir / "frames"
    sheets_dir = output_dir / "contact_sheets"
    nodes = select_nodes(data, spans=spans, sentence_scores=sentence_scores,
                         max_nodes=max_nodes)
    frame_cache = None
    try:
        import video_frame_cache
        frame_cache = video_frame_cache.VideoFrameCache(
            str(video), should_cancel=should_cancel)
    except Exception:  # noqa: BLE001
        pass
    rows = []
    for index, node in enumerate(nodes, 1):
        if should_cancel is not None and should_cancel():
            try:
                import video_frame_cache
                raise video_frame_cache.VideoFrameCacheCancelled(
                    "visual review cancelled")
            except ImportError as exc:
                raise RuntimeError("visual review cancelled") from exc
        if on_log:
            on_log("视觉审稿：抽取节点 %d/%d（%.1f 秒，%s）" %
                   (index, len(nodes), node["time"], node["kind"]))
        if frame_cache is not None:
            node["_frame_cache"] = frame_cache
        paths = extract_node_frames(video, node, frames_dir)
        rows.append(analyze_node(node, paths))
    _add_cross_node_duplicates(rows)

    vlm_runs = 0
    if use_vlm and vlm_available(vlm_tier):
        risky = sorted(rows, key=lambda row: (row["score"], -row["priority"]))
        for row in risky[:max(0, int(vlm_limit))]:
            sheet = make_contact_sheet(row["frame_paths"], sheets_dir / (row["id"] + ".jpg"))
            row["contact_sheet"] = sheet
            row["vlm"] = describe_contact_sheet(sheet, tier=vlm_tier)
            vlm_runs += 1

    all_issues = [issue for row in rows for issue in row["issues"]]
    critical = sum(issue["severity"] == "critical" for issue in all_issues)
    major = sum(issue["severity"] == "major" for issue in all_issues)
    score = float(np.mean([row["score"] for row in rows])) if rows else 0.0
    report = {
        "schema": "visual_critic_v1", "experimental": True,
        "source": str(video), "duration": round(_duration(data), 3),
        "node_count": len(rows), "score": round(score, 3),
        "passed": bool(rows and critical == 0 and major <= max(2, math.ceil(len(rows) * 0.25))
                       and score >= 78.0),
        "critical_issues": critical, "major_issues": major,
        "vlm_requested": bool(use_vlm), "vlm_available": vlm_available(vlm_tier),
        "vlm_tier": str(vlm_tier), "vlm_runs": vlm_runs,
        "elapsed_seconds": round(time.time() - started, 3),
        "nodes": rows,
        "limitations": [
            "规则层只能识别画面代理特征，不能判断游戏任务本身是否完成。",
            "菜单/背包可能是有效讲解画面，因此 static_ui_like 默认只要求复核。",
            "视觉模型只复核少量高风险节点，输出不得直接覆盖确定性规则或人工标签。",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "visual_critic_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report

