# -*- coding: utf-8 -*-
"""给剪辑加"耳朵"：从原声里算每句的声学特征（音高、能量起伏、停顿、噪声）。

为什么要有这个：现在模型只读台词文本，语气兴奋还是敷衍它看不出来。
但这些"听感"恰恰决定一段该留还是该剪：
    · 音高变化大 + 说话快 + 能量起伏大 = 情绪高（多半是精彩段落）
    · 音高平 + 语速慢 + 停顿多 = 敷衍铺垫（多半可以剪）
特征写进索引的 seg_audio，字段名见 FEATURE_NAMES。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SR = 16000
FEATURE_NAMES = ["rms_db", "peak_db", "f0_mean", "f0_std", "f0_range",
                 "pause_ratio", "energy_var", "zcr", "voiced_ratio"]


def _frames(x: np.ndarray, hop: int = 160, win: int = 400):
    """切成 25ms/10ms 的帧，返回 (帧数, win) 的视图。"""
    if len(x) < win:
        return np.zeros((0, win), dtype=np.float32)
    n = 1 + (len(x) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def _f0_autocorr(frames: np.ndarray, sr: int = SR,
                 fmin: float = 70.0, fmax: float = 400.0):
    """自相关估基频（够用、够快；不需要 librosa）。返回每帧 f0，0 表示无声/清音。"""
    out = np.zeros(len(frames), dtype=np.float32)
    lo = int(sr / fmax)
    hi = min(int(sr / fmin), frames.shape[1] - 1)
    for i, fr in enumerate(frames):
        fr = fr - fr.mean()
        e = float(np.dot(fr, fr))
        if e < 1e-6:
            continue
        ac = np.correlate(fr, fr, mode="full")[len(fr) - 1:]
        if hi <= lo:
            continue
        seg = ac[lo:hi]
        k = int(np.argmax(seg)) + lo
        if ac[k] <= 0.3 * ac[0]:
            continue
        out[i] = sr / float(k)
    return out


def analyze_audio(audio: np.ndarray, segments, pad: float = 0.15):
    """对每句说话区间算声学特征。audio 是 16k 单声道 float32。"""
    x = np.asarray(audio, dtype=np.float32)
    rows = []
    for a, b, _t in segments:
        s = max(0, int((float(a) - pad) * SR))
        e = min(len(x), int((float(b) + pad) * SR))
        clip = x[s:e]
        if clip.size < 400:
            rows.append([0.0] * len(FEATURE_NAMES))
            continue
        fr = _frames(clip)
        rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
        db = 20 * np.log10(np.maximum(rms, 1e-6))
        peak = 20 * np.log10(max(float(np.abs(clip).max()), 1e-6))
        # 有声帧门限：比本句的峰值低 35 dB 以上算静音
        thr = max(float(db.max()) - 35.0, -55.0)
        voiced = db > thr
        f0 = _f0_autocorr(fr)
        f0v = f0[(f0 > 0) & voiced]
        zcr = float(np.mean(np.abs(np.diff(np.signbit(clip).astype(np.int8))))) / 2.0
        rows.append([
            round(float(np.mean(db[voiced])) if voiced.any() else 0.0, 2),   # rms_db
            round(float(peak), 2),                                           # peak_db
            round(float(np.median(f0v)) if f0v.size else 0.0, 1),            # f0_mean
            round(float(np.std(f0v)) if f0v.size > 1 else 0.0, 1),           # f0_std
            round(float(np.percentile(f0v, 90) - np.percentile(f0v, 10))
                  if f0v.size > 3 else 0.0, 1),                              # f0_range
            round(1.0 - float(voiced.mean()), 3),                            # pause_ratio
            round(float(np.std(db[voiced])) if voiced.any() else 0.0, 2),    # energy_var
            round(zcr, 4),                                                   # zcr
            round(float(np.mean(f0 > 0)), 3),                                # voiced_ratio
        ])
    return rows


def analyze_file(video: str, segments, on_status=None):
    """抽音轨（复用索引那套）→ 算特征。"""
    import tempfile
    import index as ix
    def st(m):
        if on_status:
            on_status(m)
    st("抽音轨（算声学特征用）…")
    fd, wav = tempfile.mkstemp(suffix=".wav", dir=ix.temp_root())
    os.close(fd)
    try:
        ix.extract_audio_wav(video, wav, None)
        audio = ix.normalize_for_asr(ix.read_wav_mono(wav))
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    st("算每句的声学特征（音高/能量/停顿）…")
    return analyze_audio(audio, segments)


def cmd(idx_dir: str):
    """给已有索引用：算 seg_audio 并写回 index.json。"""
    import index as ix
    p = os.path.join(idx_dir, "index.json") if os.path.isdir(idx_dir) else idx_dir
    data = json.load(open(p, encoding="utf-8"))
    segs = data.get("segments") or []
    if not segs:
        print("索引里没有台词，先建索引")
        return 1
    if data.get("seg_audio"):
        print("已经有声学特征了（%d 句），要重算就删掉 seg_audio" % len(data["seg_audio"]))
        return 0
    rows = analyze_file(data["source"], segs, on_status=print)
    data["seg_audio"] = rows
    data["seg_audio_version"] = 1
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    print("已写入 %d 句的声学特征：%s" % (len(rows), p))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python audiofeat.py <索引目录>")
        raise SystemExit(2)
    raise SystemExit(cmd(sys.argv[1]))
