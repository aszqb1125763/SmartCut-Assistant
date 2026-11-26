# -*- coding: utf-8 -*-
"""画面识别：抽帧 + 镜头切换点检测（1~2 秒一帧，压到 192 宽，够看清就行）。

用途：
  1. 找原片自带的"镜头切换点"（画面骤然变化的地方）
  2. 让剪辑的刀口尽量落在这些点上 —— 人剪片子就是这么切的，
     落在天然切换点上，观众不会觉得"这里硬拗了一下"
  3. 提供每个时间点的画面特征（亮度/色彩/边缘密度），给以后的判定用

性能：单次 ffmpeg 扫描，2fps 抽帧 + 缩到 192 宽，33 分钟素材约 40~90 秒；
      只保留"切换点时间"和少量统计，不落盘图片（临时帧用完即删）。
"""

from __future__ import annotations

try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass


import os
import shutil
import subprocess
import tempfile

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
_LAST_CACHE_AUDIT = None
import sys
if BASE not in sys.path:
    sys.path.insert(0, BASE)


def _ffmpeg():
    import index as ix
    return ix.find_ffmpeg()


def _no_win():
    return 0x08000000 if os.name == "nt" else 0


def sample_features(video: str, fps: float = 2.0, width: int = 192,
                    max_seconds: float | None = None,
                    on_log=None, should_cancel=None):
    """抽帧并算逐帧特征。返回 (时间数组, 直方图数组, 亮度数组, 边缘密度数组)。

    帧存成临时 JPEG，读完即删；不占内存、不占磁盘。
    """
    global _LAST_CACHE_AUDIT
    import cv2
    try:
        import video_frame_cache
        cache = video_frame_cache.VideoFrameCache(
            video, ffmpeg=_ffmpeg(), should_cancel=should_cancel)
        hists, bright, edge, proxy_times = [], [], [], []
        for time_value, img in cache.iter_frames(
                0.0, max_seconds, fps=fps, width=width):
            h = cv2.calcHist([img], [0, 1, 2], None, [8, 8, 8],
                             [0, 256] * 3)
            h = h / (h.sum() + 1e-9)
            hists.append(h.ravel())
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            bright.append(float(gray.mean()))
            edge.append(float(np.mean(cv2.Canny(gray, 80, 160) > 0)))
            proxy_times.append(float(time_value))
        if hists:
            _LAST_CACHE_AUDIT = dict(cache.audit)
            _LAST_CACHE_AUDIT.update({
                "consumer": "shot_boundary_features",
                "precision": "review_proxy_nearest_0.5s",
            })
            return (np.asarray(proxy_times, dtype=float), np.array(hists),
                    np.array(bright), np.array(edge))
    except Exception as exc:  # noqa: BLE001
        try:
            import video_frame_cache
            if isinstance(exc, video_frame_cache.VideoFrameCacheCancelled):
                raise
        except ImportError:
            pass
        _LAST_CACHE_AUDIT = {
            "reused": False, "precision": "legacy_dense_decode_fallback",
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:160]),
        }
    ff = _ffmpeg()
    tmp = tempfile.mkdtemp(prefix="frames_")
    try:
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y"]
        try:
            import index as _ix
            cmd += _ix.hwaccel_args()
        except Exception:  # noqa: BLE001
            pass
        if max_seconds:
            cmd += ["-t", "%.2f" % max_seconds]
        cmd += ["-i", video, "-vf", "fps=%g,scale=%d:-2" % (fps, width),
                "-q:v", "4", os.path.join(tmp, "f_%06d.jpg")]
        r = subprocess.run(cmd, capture_output=True, creationflags=_no_win())
        files = sorted(f for f in os.listdir(tmp) if f.startswith("f_"))
        if not files:
            if on_log:
                on_log("抽帧失败：%s" % (r.stderr or b"")[-200:])
            return np.zeros(0), np.zeros((0, 512)), np.zeros(0), np.zeros(0)
        hists, bright, edge = [], [], []
        for name in files:
            p = os.path.join(tmp, name)
            buf = np.fromfile(p, dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)      # 中文路径安全
            os.remove(p)
            if img is None:
                continue
            h = cv2.calcHist([img], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
            h = h / (h.sum() + 1e-9)
            hists.append(h.ravel())
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            bright.append(float(gray.mean()))
            edge.append(float(np.mean(cv2.Canny(gray, 80, 160) > 0)))
        n = len(hists)
        times = np.arange(n, dtype=float) / float(fps)
        return times, np.array(hists), np.array(bright), np.array(edge)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def shot_boundaries(times, hists, fps: float = 2.0, thr: float = 0.45,
                    min_gap: float = 0.8):
    """从逐帧直方图找镜头切换点（相邻帧差很大、且是局部最大）。"""
    if len(hists) < 3:
        return np.zeros(0)
    d = 0.5 * np.abs(hists[1:] - hists[:-1]).sum(axis=1)
    cand = []
    for i in range(1, len(d) - 1):
        if d[i] >= thr and d[i] >= d[i - 1] and d[i] >= d[i + 1]:
            cand.append(times[i + 1])
    out = []
    for t in cand:
        if not out or t - out[-1] >= min_gap:
            out.append(float(t))
    return np.array(out)


def analyze(video: str, fps: float = 2.0, width: int = 192, thr: float = 0.45,
            on_log=None, should_cancel=None):
    """一步到位：返回 {"times","hists","bright","edge","cuts"}。"""
    import time as _t
    t0 = _t.time()
    times, hists, bright, edge = sample_features(video, fps=fps, width=width,
                                                 on_log=on_log,
                                                 should_cancel=should_cancel)
    cuts = shot_boundaries(times, hists, fps=fps, thr=thr)
    if on_log:
        on_log("画面识别：抽 %d 帧（%gfps，宽 %d），找到 %d 个镜头切换点，用时 %.1f 秒"
               % (len(times), fps, width, len(cuts), _t.time() - t0))
    return {"times": times, "hists": hists, "bright": bright, "edge": edge,
            "cuts": cuts, "fps": fps, "elapsed": _t.time() - t0,
            "visual_cache": dict(_LAST_CACHE_AUDIT or {})}


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("用法：python frames.py <视频> [fps]")
        raise SystemExit(2)
    v = _sys.argv[1]
    fps = float(_sys.argv[2]) if len(_sys.argv) > 2 else 2.0
    res = analyze(v, fps=fps, on_log=print)
    print("切换点前 10 个：", [round(x, 1) for x in res["cuts"][:10]])
