# -*- coding: utf-8 -*-
"""接缝体检：每个剪辑点两侧的「声音连续性 + 画面连续性」。

为什么这么算（子枫定的金标准）：
  观众看不到原片，只在意「割裂感」。而割裂感首先来自视听信号的不连续，
  语义跳跃反而能被视听连续盖住。所以评分以视听为主。

两条关键判据（都踩过坑才定下来）：
  1. 声音看的是**变化速率**，不是两侧平均能量差。一句话说完自然安静下来，
     差值也能到 17dB，但人耳听不出割裂；硬切是 10~20 毫秒骤降。
  2. 自然停顿必须**人声渐降 + 背景连续**同时成立。只有人声降、背景却断了，
     那是背景轨被切断，算割裂（实测 100.1 秒那处就是）。
  3. 画面用几何平均，不用加权平均——"户外→深色菜单"这种某一项彻底不符
     必须被放大，加权会把它稀释成中等分。

分数含义：
  ≥ 0.75  顺   —— 观众不会注意到
  0.60~0.75 注意 —— 敏感的人能察觉
  < 0.60  卡   —— 大概率出戏，需要改（叠化 / 换切点 / 补音频）
"""
from __future__ import annotations

import json
import os

import numpy as np

import audio_evidence_cache
from memory_config import app_home

BASE = app_home()
SR = 16000
WINDOW = 0.30           # 接缝两侧各取 300 毫秒
HARD_CUT_DROP = 6.0     # 单帧（10ms）下降超过 6dB 视为硬切起点
SCORE_GATE = 0.60       # 低于这个分就是"卡"


# ---------------------------------------------------------------------------
# 音频特征（纯函数，便于单测）
# ---------------------------------------------------------------------------
def band_energy(samples, sr=SR, low=80.0, high=800.0):
    """指定频段的对数能量（背景音乐/环境声主要落在低中频）。"""
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < 256:
        return -120.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1.0 / sr)
    mask = (freqs >= low) & (freqs <= high)
    if not mask.any():
        return -120.0
    return float(20 * np.log10(max(float(spectrum[mask].mean()), 1e-9)))


def spectral_centroid(samples, sr=SR):
    """频谱质心，粗略代表音色亮度。"""
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < 256:
        return 0.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1.0 / sr)
    total = float(spectrum.sum())
    return float((spectrum * freqs).sum() / total) if total else 0.0


def high_ratio(samples, sr=SR, edge=1000.0):
    """1kHz 以上能量占比。人声通常 <0.3，噪声/音效会高得多。

    实测 45.8 秒那处：接缝前 0.79（嘶声），接缝后 0.05（人声）。
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < 256:
        return 0.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1.0 / sr)
    total = float(spectrum.sum()) + 1e-12
    return float(spectrum[freqs > edge].sum() / total)


# 音色断裂阈值（2026-11-23 实测标定）
CENTROID_RATIO_BREAK = 3.0     # 频谱重心变化超过 3 倍 → 音色断
HIGH_RATIO_BREAK = 0.40        # 高频占比变化超过 0.40 → 音色断
CENTROID_RATIO_MASK = 1.5      # 1.5~3 倍之间：差距不大，可以用 EQ 伪装


def mfcc_like(samples, sr=SR, banks=16):
    """简化版 MFCC：对数梅尔频带能量，够用来比音色。"""
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < 512:
        return np.zeros(banks)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1.0 / sr)
    edges = np.linspace(80.0, min(7600.0, sr / 2 - 100), banks + 1)
    out = []
    for i in range(banks):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        energy = float(spectrum[mask].sum()) if mask.any() else 1e-12
        out.append(np.log(energy + 1e-12))
    return np.array(out)


def cross_seam_drop_db(audio, centre, sr=SR, before=0.05, after=0.05):
    """接缝两侧各 50ms 的能量差（dB，只算下降）。

    硬切的物理特征就在这里：接缝前还在说话（-20dB 左右），跨过接缝直接没了。
    注意这只说明"声音掉了"，不代表"割裂"——自然说完一句话也会掉。
    还要看背景声是否连续，见 audio_seam()。
    """
    audio = np.asarray(audio, dtype=np.float32)
    centre = int(centre)
    left = audio[max(0, centre - int(before * sr)):centre]
    right = audio[centre:centre + int(after * sr)]
    if left.size < 128 or right.size < 128:
        return 0.0
    db_left = 20 * np.log10(max(float(np.sqrt((left ** 2).mean())), 1e-9))
    db_right = 20 * np.log10(max(float(np.sqrt((right ** 2).mean())), 1e-9))
    return float(max(0.0, db_left - db_right))


def tail_drop_db(audio, centre, sr=SR, span=0.20, frame=0.010):
    """接缝前的最大单帧音量下降（dB/10ms），用来判断"该衰减却没衰减"。"""
    audio = np.asarray(audio, dtype=np.float32)
    size = max(1, int(frame * sr))
    start = max(0, int(centre) - int(span * sr))
    tail = audio[start:int(centre)]
    count = tail.size // size
    if count < 3:
        return 0.0
    frames = tail[:count * size].reshape(-1, size)
    db = 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-9))
    return float(max(0.0, -np.diff(db).min()))


def valley_db(audio, centre, sr=SR, span=0.12, frame=0.010):
    """接缝点附近 ±span 内的最低音量（dB）。

    切在气口上（人说话的自然停顿）时，这里会出现一个明显的静音谷。
    这是"自然"的硬证据——比"两侧平均能量差"可靠得多，因为后者会把
    "说完一句→安静"也算成跳变。
    """
    audio = np.asarray(audio, dtype=np.float32)
    centre = int(centre)
    size = max(1, int(frame * sr))
    start = max(0, centre - int(span * sr))
    seg = audio[start:min(audio.size, centre + int(span * sr))]
    count = seg.size // size
    if count < 3:
        return 0.0
    frames = seg[:count * size].reshape(-1, size)
    db = 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-9))
    return float(db.min())


def audio_seam(audio, centre, sr=SR, window=WINDOW):
    """返回 (分数, 明细)。分数 0~1，1 = 完全连续。"""
    audio = np.asarray(audio, dtype=np.float32)
    pad = int(window * sr)
    centre = int(centre)
    prev = audio[max(0, centre - pad):centre]
    nxt = audio[centre:centre + pad]
    if prev.size < 512 or nxt.size < 512:
        return 0.5, {"note": "样本不足"}

    drop = cross_seam_drop_db(audio, centre, sr=sr)
    tail = tail_drop_db(audio, centre, sr=sr)
    band_gap = abs(band_energy(prev) - band_energy(nxt))
    cent_prev, cent_next = spectral_centroid(prev), spectral_centroid(nxt)
    cent_gap = abs(cent_prev - cent_next)
    # 音色用"倍率"而不是"绝对差"：800→900 是同一个音色，3445→377 是换了个东西。
    cent_ratio = (max(cent_prev, cent_next) / max(min(cent_prev, cent_next), 1.0))
    hi_prev, hi_next = high_ratio(prev), high_ratio(nxt)
    hi_gap = abs(hi_prev - hi_next)
    mfcc_gap = float(np.abs(mfcc_like(prev) - mfcc_like(nxt)).mean())

    # 瞬态：跨接缝掉 6dB 以内算平稳，掉到 24dB 算硬切
    s_transient = max(0.0, min(1.0, 1.0 - (max(drop, tail) - HARD_CUT_DROP) / 18.0))
    s_band = max(0.0, min(1.0, 1.0 - band_gap / 15.0))
    # 音色：重心倍率或高频占比突变，直接压到底（不再按 1500Hz 绝对差算）
    if cent_ratio >= CENTROID_RATIO_BREAK or hi_gap >= HIGH_RATIO_BREAK:
        s_tone = 0.05
    else:
        s_tone = max(0.0, min(1.0, 1.0 - (cent_ratio - 1.0) / (CENTROID_RATIO_BREAK - 1.0)))
    s_mfcc = max(0.0, min(1.0, 1.0 - mfcc_gap / 2.5))

    # 自然停顿 = 切在气口上（附近有静音谷）**且背景仍连续**。
    # 两个条件缺一不可：只有谷、背景却断了，那是背景轨被切断（实测 100.1 秒）；
    # 只有背景连续、切点却没谷，那是说话中被硬切（实测 45.8 秒）。
    valley = valley_db(audio, centre, sr=sr)
    natural = (valley < -42.0 and band_gap <= 12.0) or (
        (drop <= HARD_CUT_DROP or tail <= HARD_CUT_DROP) and band_gap <= 6.0)
    # 音色断了就不能算"自然停顿"——那是最刺耳的一类（人声里插进一段嘶声）
    tone_broken = (cent_ratio >= CENTROID_RATIO_BREAK
                   or hi_gap >= HIGH_RATIO_BREAK)
    if tone_broken:
        natural = False
    if natural:
        score = min(1.0, 0.55 + 0.45 * min(1.0, s_band + 0.25))
    else:
        parts = [max(s_transient, 0.05), max(s_band, 0.05),
                 max(s_tone, 0.05), max(s_mfcc, 0.05)]
        score = float(np.prod(parts) ** (1.0 / len(parts)))
    return round(float(score), 3), {
        "cross_drop_db": round(drop, 1),
        "tail_drop_db_per_10ms": round(tail, 1),
        "valley_db": round(valley, 1),
        "band_db_gap": round(band_gap, 1),
        "centroid_gap_hz": round(cent_gap, 0),
        "centroid_ratio": round(cent_ratio, 2),
        "hi_ratio_gap": round(hi_gap, 2),
        "tone_broken": bool(tone_broken),
        "tone_maskable": bool(CENTROID_RATIO_MASK <= cent_ratio < CENTROID_RATIO_BREAK
                              and hi_gap < HIGH_RATIO_BREAK),
        "mfcc_gap": round(mfcc_gap, 2),
        "natural_pause": bool(natural),
    }


# ---------------------------------------------------------------------------
# 画面特征（纯函数）
# ---------------------------------------------------------------------------
def visual_seam(frame_a, frame_b, size=(320, 180)):
    """返回 (分数, 明细)。分数 0~1，1 = 几乎同一画面。"""
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return 0.5, {"note": "opencv 不可用"}
    if frame_a is None or frame_b is None:
        return 0.5, {"note": "读帧失败"}
    a = cv2.resize(frame_a, size)
    b = cv2.resize(frame_b, size)
    a_hsv = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)
    b_hsv = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
    hist_corr = 0.0
    for ch in range(3):
        upper = 180 if ch == 0 else 256
        h1 = cv2.calcHist([a_hsv], [ch], None, [32], [0, upper])
        h2 = cv2.calcHist([b_hsv], [ch], None, [32], [0, upper])
        cv2.normalize(h1, h1)
        cv2.normalize(h2, h2)
        hist_corr += float(cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL))
    hist_corr /= 3.0

    a_gray = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    b_gray = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    try:
        ssim = float(cv2.matchTemplate(a_gray, b_gray,
                                       cv2.TM_CCOEFF_NORMED)[0][0])
    except Exception:  # noqa: BLE001
        ssim = 0.0
    bright_gap = abs(float(a_gray.mean()) - float(b_gray.mean()))

    s_hist = max(0.0, min(1.0, (hist_corr + 1) / 2))
    s_struct = max(0.0, min(1.0, (ssim + 1) / 2))
    s_bright = max(0.0, min(1.0, 1.0 - bright_gap / 60.0))
    parts = [max(s_hist, 0.05), max(s_struct, 0.05), max(s_bright, 0.05)]
    score = float(np.prod(parts) ** (1.0 / len(parts)))
    return round(float(score), 3), {
        "hist_corr": round(hist_corr, 2),
        "ssim": round(ssim, 2),
        "brightness_gap": round(bright_gap, 0),
    }


def combine(audio_score, visual_score, audio_weight=0.7):
    """合成总分。声音权重更高：子枫反馈里"声音割裂"比"画面割裂"更常被明确听出。"""
    weight = max(0.0, min(1.0, float(audio_weight)))
    return round(weight * float(audio_score) + (1.0 - weight) * float(visual_score), 3)


# ---------------------------------------------------------------------------
# 对整条成片做体检
# ---------------------------------------------------------------------------
def _ffmpeg_path(ffmpeg=None):
    if ffmpeg:
        return ffmpeg
    try:
        import index as indexer
        return indexer.find_ffmpeg()
    except FileNotFoundError:
        pass
    for name in ("ffmpeg.exe", "ffmpeg"):
        path = os.path.join(BASE, "bin", name)
        if os.path.isfile(path):
            return path
    return "ffmpeg"


def _check_cancel(should_cancel):
    if should_cancel is not None and should_cancel():
        raise audio_evidence_cache.AudioEvidenceCancelled(
            "seam continuity review cancelled")


def _open_audio_cache(path, ffmpeg=None, sr=SR, should_cancel=None):
    # Use the same file identity, audio ordinal and PCM format as rendered QC.
    # Explicit audio ordinal 0 also avoids ffmpeg's automatic best-track choice.
    return audio_evidence_cache.AudioEvidenceCache(
        path, tracks=[0], sample_rate=sr, ffmpeg=_ffmpeg_path(ffmpeg),
        should_cancel=should_cancel)


def load_audio(path, ffmpeg=None, sr=SR, should_cancel=None):
    """Read the first audio track, reusing the shared PCM evidence index.

    Keep the whole-array interface for external callers; score_seams below
    reads only bounded windows. Decode/cache failures and cancellation must
    propagate rather than becoming an empty, apparently passing report.
    """
    _check_cancel(should_cancel)
    cache = _open_audio_cache(path, ffmpeg=ffmpeg, sr=sr,
                              should_cancel=should_cancel)
    try:
        entry = cache.prepare_track(0)
        _check_cancel(should_cancel)
        return cache.review_samples(0, 0.0, entry["duration"])
    finally:
        cache.close()


def _seam_audio_window(cache, at, window):
    # audio_seam also inspects 200ms of tail, +/-120ms for a valley, and
    # +/-50ms for a cross-seam drop, including when window is smaller.
    centre = int(at * SR)
    radius = max(int(window * SR), int(0.20 * SR), int(0.12 * SR),
                 int(0.05 * SR))
    start = max(0, centre - radius) / float(SR)
    end = (centre + radius) / float(SR)
    samples = cache.review_samples(0, start, end)
    # Match review_samples' floor operation, even for non-exact float times.
    first = max(0, int(np.floor(start * SR)))
    return samples, centre - first


def _frame_at(cap, second, cv2):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, second) * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def _is_bridge(stamps, index, gap=0.05):
    """段 index 是否是「桥接段」（与前一段的源区间首尾相接）。

    叠化/微桥接是人为造出来的平滑过渡，它的进出点不该判割裂——
    真正要判的是"跳切"：源区间断开的那一刀。
    """
    if index <= 0 or index >= len(stamps):
        return False
    prev = stamps[index - 1]
    cur = stamps[index]
    return abs((prev[1] + prev[2]) - cur[1]) <= gap


def score_seams(path, timeline, ffmpeg=None, window=WINDOW,
                audio_weight=0.7, should_cancel=None, smooth_at=None):
    """对成片的每个**真实剪辑点**打分。

    timeline 是 render() 返回的 (成片起点, 源起点, 时长)。

    smooth_at：由 render 直接给出的"叠化接缝"时刻列表。给了就用它精确跳过，
    不再靠源区间是否相接去猜——那个启发式会把音频胶合（画面实为硬切）
    也当成叠化跳掉，实测让 17 处接缝只剩 6 处被评估。
    """
    _check_cancel(should_cancel)
    rows = []
    stamps = []
    for item in timeline or []:
        try:
            out_start = float(item[0])
            src_start = float(item[1])
            duration = float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if duration <= 0:
            continue
        stamps.append((out_start, src_start, duration))
    if len(stamps) < 2:
        return rows

    import cv2

    _smooth = tuple(float(x) for x in (smooth_at or ()))

    def _is_smooth(at):
        if _smooth:
            return any(abs(at - x) <= 0.05 for x in _smooth)
        return False

    cache = _open_audio_cache(path, ffmpeg=ffmpeg,
                              should_cancel=should_cancel)
    cap = None
    try:
        entry = cache.prepare_track(0)
        total = int(entry["sample_count"]) / float(SR)
        _check_cancel(should_cancel)
        cap = cv2.VideoCapture(path)
        cursor = 0.0
        for index in range(len(stamps) - 1):
            _check_cancel(should_cancel)
            cursor += stamps[index][2]
            at = cursor
            if _smooth and _is_smooth(at):
                continue
            if not _smooth and (_is_bridge(stamps, index)
                                or _is_bridge(stamps, index + 1)):
                continue
            if at <= window + 0.1 or at >= total - window - 0.1:
                continue
            audio, centre = _seam_audio_window(cache, at, window)
            audio_score, audio_detail = audio_seam(audio, centre,
                                                   window=window)
            _check_cancel(should_cancel)
            frame_a = _frame_at(cap, at - 0.05, cv2)
            frame_b = _frame_at(cap, at + 0.05, cv2)
            visual_score, visual_detail = visual_seam(frame_a, frame_b)
            rows.append({
                "at": round(at, 2), "index": index + 1,
                "audio": audio_score, "visual": visual_score,
                "total": combine(audio_score, visual_score, audio_weight),
                "audio_detail": audio_detail, "visual_detail": visual_detail,
            })
        _check_cancel(should_cancel)
    finally:
        try:
            if cap is not None:
                cap.release()
        finally:
            cache.close()
    return rows


def summarize(rows, gate=SCORE_GATE):
    """把逐接缝结果汇总成报告。"""
    rows = list(rows or [])
    bad = [r for r in rows if r["total"] < gate]
    warn = [r for r in rows if gate <= r["total"] < 0.75]
    return {
        "schema": "smartcut.seam-continuity",
        "schema_version": 1,
        "gate": gate,
        "count": len(rows),
        "ok": len(rows) - len(bad) - len(warn),
        "warn": len(warn),
        "bad": len(bad),
        "passed": not bad,
        "worst": sorted(rows, key=lambda r: r["total"])[:8],
        "seams": rows,
    }


def main(argv=None):
    """命令行用法：python seam_continuity.py <成片> <索引目录>"""
    import sys
    import glob

    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 2
    video, index_dir = argv[0], argv[1]
    plans = sorted(glob.glob(os.path.join(index_dir, "plans", "transition-*.json")),
                   key=os.path.getmtime, reverse=True)
    plan = json.load(open(plans[0], encoding="utf-8"))["plan"]
    spans = [(float(a), float(b)) for a, b in plan["final_render_spans"]]
    timeline = []
    cursor = 0.0
    for start, end in spans:
        timeline.append((round(cursor, 3), round(start, 3), round(end - start, 3)))
        cursor += end - start
    rows = score_seams(video, timeline)
    report = summarize(rows)
    print("接缝 %d 处：顺 %d、注意 %d、卡 %d（阈值 %.2f）"
          % (report["count"], report["ok"], report["warn"],
             report["bad"], report["gate"]))
    for row in report["worst"]:
        detail = row["audio_detail"]
        print("  %7.2f 秒  声音 %.2f  画面 %.2f  总分 %.2f  %s"
              % (row["at"], row["audio"], row["visual"], row["total"],
                 "自然停顿" if detail.get("natural_pause") else
                 "跨接缝掉 %.1fdB，背景差 %.1fdB"
                 % (detail.get("cross_drop_db", 0),
                    detail.get("band_db_gap", 0))))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
