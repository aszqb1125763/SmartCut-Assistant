# -*- coding: utf-8 -*-
"""画面小脑：只吃原始帧，只出画面特征与档位。

覆盖条款：C01 亮度 / C02 色彩 / C03 结构（边缘密度）/ C04 运动（帧差）。

**不许** import 其它小脑，**不许** import 本地模型（VLM 在 visual_critic，属于上层）。
"""
from __future__ import annotations

import numpy as np

import coords

from .base import BrainPlugin


def _load_cv2():
    try:
        import cv2
        return cv2
    except Exception:  # noqa: BLE001
        return None


def features(frame):
    """一帧的画面四特征（C01–C04）。输入 BGR uint8。"""
    cv2 = _load_cv2()
    if cv2 is None or frame is None:
        return None
    small = cv2.resize(frame, (320, 180))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 24], [0, 180, 0, 256])
    hist = cv2.normalize(hist, hist).flatten()
    return {
        "bright": float(gray.mean()),
        "sat": float(hsv[:, :, 1].mean()),
        "edge": float(np.mean(cv2.Canny(gray, 80, 160) > 0)),
        "hist": hist,
        "gray": gray,
    }


def _hist_corr(a, b):
    cv2 = _load_cv2()
    if cv2 is None:
        return 0.0
    return float(cv2.compareHist(a.astype(np.float32),
                                 b.astype(np.float32), cv2.HISTCMP_CORREL))


def _motion(a_gray, b_gray):
    if a_gray is None or b_gray is None:
        return 0.0
    if a_gray.shape != b_gray.shape:
        return 0.0
    return float(np.mean(np.abs(a_gray.astype(np.float32)
                                - b_gray.astype(np.float32))) / 255.0)


class PictureBrain(BrainPlugin):
    name = "picture"
    accepts = ("frames",)
    emits = ("bright_gap", "hist_corr", "edge_gap", "motion")

    # -- 切点前后的画面差（C01–C03）----------------------------------------
    def probe_jump(self, at, window=0.60):
        """切点两侧的画面差：亮度/直方图/边缘（C01–C03）。"""
        self.probes += 1
        at = float(at)
        times, frames = self.src["frames"].frames(at - window, at + window,
                                        fps=5.0, width=360)
        base = {"probe": "picture_jump", "brain": self.name,
                "at": round(at, 3), "clause": ["C01", "C02", "C03"]}
        base.update(self.meta())
        if len(frames) < 2:
            base.update({"grade": None, "reason": "no_frames"})
            return base
        before = None
        after = None
        for index, stamp in enumerate(times):
            if stamp <= at - 1e-6:
                before = index
            elif after is None:
                after = index
        # 抽帧窗口偶尔会全落在切点同侧：退到首尾各一帧，别直接判"看不清"
        if before is None:
            before = 0
        if after is None:
            after = len(frames) - 1
        if before == after:
            base.update({"grade": None, "reason": "no_pair"})
            return base
        fa = features(frames[before])
        fb = features(frames[after])
        if fa is None or fb is None:
            base.update({"grade": None, "reason": "no_cv2"})
            return base
        bright_gap = abs(fa["bright"] - fb["bright"])
        hist_corr = _hist_corr(fa["hist"], fb["hist"])
        edge_gap = abs(fa["edge"] - fb["edge"])
        over = (bright_gap > coords.PICTURE_BRIGHT_GAP
                or hist_corr < coords.PICTURE_HIST_CORR
                or edge_gap > coords.PICTURE_EDGE_GAP)
        base.update({
            "bright_gap": round(bright_gap, 1),
            "hist_corr": round(hist_corr, 3),
            "edge_gap": round(edge_gap, 3),
            "grade": "over" if over else "ok",
            "row": "M20" if over else "M21",
            "severity": "P2" if over else "none",
        })
        return base

    # -- 区间：逐帧特征 + 静止比例（C01–C04）--------------------------------
    def probe_span(self, start, end, fps=2.0):
        """一段时间的画面轨迹：亮度/色彩/结构逐帧 + 运动/静止比例。"""
        self.probes += 1
        times, frames = self.src["frames"].frames(start, end, fps=fps, width=360)
        base = {"probe": "picture_span", "brain": self.name,
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "frames": len(frames), "clause": ["C01", "C02", "C03", "C04"]}
        base.update(self.meta())
        if len(frames) < 2:
            base.update({"grade": None, "reason": "no_frames"})
            return base
        feats = [features(frame) for frame in frames]
        feats = [row for row in feats if row]
        if len(feats) < 2:
            base.update({"grade": None, "reason": "no_cv2"})
            return base
        motions = [_motion(feats[i]["gray"], feats[i + 1]["gray"])
                   for i in range(len(feats) - 1)]
        still = sum(1 for value in motions if value < coords.PICTURE_MOTION)
        base.update({
            "bright_mean": round(float(np.mean([f["bright"] for f in feats])), 1),
            "bright_std": round(float(np.std([f["bright"] for f in feats])), 1),
            "sat_mean": round(float(np.mean([f["sat"] for f in feats])), 1),
            "edge_mean": round(float(np.mean([f["edge"] for f in feats])), 3),
            "motion_mean": round(float(np.mean(motions)) if motions else 0.0, 3),
            "motion_max": round(float(np.max(motions)) if motions else 0.0, 3),
            "still_ratio": round(still / float(len(motions)), 3) if motions else 0.0,
            "grade": "ok",
            "row": "M21",
        })
        return base

    def probe(self, start, end):
        return self.probe_span(start, end)
