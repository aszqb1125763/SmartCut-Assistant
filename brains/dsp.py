# -*- coding: utf-8 -*-
"""纯信号处理（不是小脑，是它们共用的工具箱）。

为什么单独一个文件：
    小脑之间不许互相 import，但"怎么算 RMS / 怎么取单侧窗"这种最基本的事
    不该在每个小脑里各写一遍。这里只放**纯函数**：输入数组，输出数值。
    它不做判据、出不了档位、不碰业务。
"""
from __future__ import annotations

import numpy as np

import coords

SR = 16000
FRAME_MS = 20


def envelope(audio, sr=SR, win_ms=FRAME_MS):
    """20ms 帧的 dB 包络（A03：窗必须 ≤20ms）。"""
    step = max(1, int(sr * win_ms / 1000.0))
    count = audio.size // step
    if not count:
        return np.zeros(0)
    frames = audio[:count * step].reshape(count, step)
    return 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-9))


def side_audio(audio, second, side, sr=SR, win=0.10):
    """单侧窗：[t-win, t] 或 [t, t+win]。

    接缝两侧听众能听到的是"左段出点之前"和"右段入点之后"，
    中间被删掉的音频不算——用中心窗会把将删的音频算进去（11-24 的目标函数 bug）。
    """
    half = max(1, int(win * sr))
    centre = int(round(second * sr))
    if side == "before":
        i0, i1 = max(0, centre - half), min(audio.size, centre)
    else:
        i0, i1 = max(0, centre), min(audio.size, centre + half)
    seg = audio[i0:i1]
    return seg if seg.size >= 64 else np.zeros(0)


def side_db(audio, second, side, sr=SR, win=0.10):
    seg = side_audio(audio, second, side, sr, win)
    if seg.size < 64:
        return -120.0
    return 20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9))


def side_tone(audio, second, side, sr=SR, win=0.10):
    seg = side_audio(audio, second, side, sr, win)
    if seg.size < 128:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    freq = np.fft.rfftfreq(seg.size, 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    return float((spec * freq).sum() / total)


def side_shape(audio, second, side, sr=SR, span=0.20, step=0.02):
    """单侧能量曲线（去均值）：左切点看前面、右切点看后面。"""
    n = max(1, int(round(span / step)))
    vals = []
    for k in range(n):
        offset = (k - n + 1) * step if side == "before" else (k + 1) * step
        vals.append(db_at(audio, second + offset, sr, win=step))
    arr = np.array(vals, dtype=np.float32)
    return arr - arr.mean()


def db_at(audio, second, sr=SR, win=0.10):
    i0 = max(0, int((second - win / 2) * sr))
    i1 = min(audio.size, int((second + win / 2) * sr))
    seg = audio[i0:i1]
    if seg.size < 64:
        return -120.0
    return 20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9))


def shape_distance(s1, s2):
    if s1 is None or s2 is None:
        return 999.0
    if s1.size != s2.size or s1.size == 0:
        return 999.0
    return float(np.sqrt(((s1 - s2) ** 2).mean()))


def band_rms(seg, sr, low, high):
    """频段能量（dB）。A04 三频段用。"""
    if seg.size < 256:
        return -120.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    freqs = np.fft.rfftfreq(seg.size, 1.0 / sr)
    mask = (freqs >= float(low)) & (freqs <= float(high))
    if not mask.any():
        return -120.0
    return float(20 * np.log10(max(float(spec[mask].mean()), 1e-9)))


def seam_axes(audio, left_t, right_t, sr=SR):
    """一处接缝的三轴真值（与听众听到的一致）。"""
    e = abs(side_db(audio, left_t, "before", sr)
            - side_db(audio, right_t, "after", sr))
    t = abs(side_tone(audio, left_t, "before", sr)
            - side_tone(audio, right_t, "after", sr))
    s = shape_distance(side_shape(audio, left_t, "before", sr),
                       side_shape(audio, right_t, "after", sr))
    return {"energy_db": round(float(e), 1),
            "tone_hz": round(float(t), 0),
            "shape_db": round(float(s), 1),
            "grade": coords.combine(coords.energy_grade(e),
                                    coords.tone_grade(t),
                                    coords.shape_grade(s))}
