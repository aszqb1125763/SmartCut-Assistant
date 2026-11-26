# -*- coding: utf-8 -*-
"""BGM 审核：只审 BGM 和人声的和谐程度，不重审片段。

为什么单独审（子枫 2026-11-23）：
  BGM 是在「审核通过之后」才加进去的一条新轨。加完之后如果再把成片丢回
  原来那套「半句 / 切字 / 能量断崖」的判据，BGM 的声音会被算进人声里，
  于是误报一堆——而那些问题在加 BGM 之前已经审过了，不该重审。

  所以这里只看三件事：
    ① BGM 有没有盖过人声（能量比）
    ② BGM 和人声的频段有没有打架
    ③ BGM 片段切得碎不碎（子枫：不能 1 秒一个片段，也不用放完整）

判据用「范围值」，不用绝对值（机器精度远超人耳）。
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

SR = 16000
# 能量比：BGM 应该比人声低 8~20dB（低太多听不见，高太多盖人声）
BGM_ABOVE_VOICE_MAX = -6.0     # BGM 最多只能到“人声 -6dB”
BGM_BELOW_VOICE_MIN = -26.0    # 低于人声 26dB 就听不见了
# 频段打架：BGM 在 300~3000Hz（人声主频段）的能量占比
VOICE_BAND_LO, VOICE_BAND_HI = 300.0, 3000.0
BGM_VOICE_BAND_MAX = 0.55      # 超过 55% 说明 BGM 挤在人声频段里
# 片段长度
MIN_PIECE_SEC = 1.0            # 不能 1 秒一个片段
MAX_PIECES_RATIO = 1 / 30.0    # 平均每段至少 30 秒


def _grab(path, ffmpeg, track, ss=None, dur=None, sr=SR):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-i", path, "-map", "0:a:%d" % track, "-ac", "1",
            "-ar", str(sr), "-f", "s16le", "-"]
    done = subprocess.run(cmd, capture_output=True)
    return (np.frombuffer(done.stdout, dtype="<i2").astype(np.float32)
            / 32768.0)


def _env(audio, sr=SR, win=0.05):
    step = max(1, int(sr * win))
    n = audio.size // step
    if not n:
        return np.zeros(0), win
    frames = audio[:n * step].reshape(n, step)
    return (20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)),
                                     1e-9)), win)


def _band_ratio(audio, sr=SR, lo=VOICE_BAND_LO, hi=VOICE_BAND_HI):
    if audio.size < 1024:
        return 0.0
    spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    freq = np.fft.rfftfreq(audio.size, 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    return float(spec[(freq >= lo) & (freq <= hi)].sum() / total)


def _segments(audio, sr=SR, gap_db=-50.0, win=0.05):
    """把 BGM 轨按“有声音”切成段，返回 [(start, end), ...]。"""
    env, step = _env(audio, sr, win)
    if env.size == 0:
        return []
    loud = env > gap_db
    spans, start = [], None
    for k, flag in enumerate(loud):
        if flag and start is None:
            start = k
        elif not flag and start is not None:
            spans.append((start * step, k * step))
            start = None
    if start is not None:
        spans.append((start * step, env.size * step))
    # 合并间隔 <0.3 秒的（那不是“两段”，只是 BGM 里的一段轻）
    merged = []
    for s in spans:
        if merged and s[0] - merged[-1][1] < 0.3:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(s)
    return [(round(a, 2), round(b, 2)) for a, b in merged]


def audit(path, ffmpeg, bgm_track, voice_track, log=None):
    """审 BGM 和谐度。返回 findings 列表（带轨道归属）。"""
    findings = []
    total = 0.0
    try:
        import index as _ix
        total = float((_ix.probe_streams(path) or {}).get("duration") or 0.0)
    except Exception:
        total = 0.0

    bgm = _grab(path, ffmpeg, bgm_track)
    voice = _grab(path, ffmpeg, voice_track)
    if bgm.size < SR or voice.size < SR:
        return [{
            "track": bgm_track, "track_name": "第%d条" % (bgm_track + 1),
            "kind": "bgm", "problem": "读取失败", "at": 0,
            "severity": "minor", "suggest": "BGM 轨或人声轨读不到",
        }]

    n = min(bgm.size, voice.size)
    bgm, voice = bgm[:n], voice[:n]
    # 只比“人声在说话”的部分（人声静音时 BGM 比它响是正常的）
    venv, vstep = _env(voice)
    benv, _ = _env(bgm)
    m = min(venv.size, benv.size)
    talk = venv[:m] > -45.0
    if talk.sum() >= 3:
        v_db = float(venv[:m][talk].mean())
        b_db = float(benv[:m][talk].mean())
        diff = b_db - v_db
        if diff > BGM_ABOVE_VOICE_MAX:
            findings.append({
                "track": bgm_track, "track_name": "第%d条" % (bgm_track + 1),
                "kind": "bgm", "problem": "BGM 盖过人声",
                "at": 0, "duration": round(total, 1),
                "severity": "major", "diff_db": round(diff, 1),
                "suggest": "说话时 BGM 比人声高 %.0fdB，调到比人声低 8~20dB"
                           % diff,
                "confidence": 0.85,
            })
        elif diff < BGM_BELOW_VOICE_MIN:
            findings.append({
                "track": bgm_track, "track_name": "第%d条" % (bgm_track + 1),
                "kind": "bgm", "problem": "BGM 太轻",
                "at": 0, "duration": round(total, 1),
                "severity": "minor", "diff_db": round(diff, 1),
                "suggest": "BGM 比人声低 %.0fdB，基本听不见" % abs(diff),
                "confidence": 0.7,
            })

    # 频段打架
    ratio = _band_ratio(bgm)
    if ratio > BGM_VOICE_BAND_MAX:
        findings.append({
            "track": bgm_track, "track_name": "第%d条" % (bgm_track + 1),
            "kind": "bgm", "problem": "BGM 挤在人声频段",
            "at": 0, "duration": round(total, 1),
            "severity": "minor", "ratio": round(ratio, 2),
            "suggest": "BGM 有 %.0f%% 的能量在 300~3000Hz（人声区），"
                       "考虑换一首或降一点" % (ratio * 100),
            "confidence": 0.6,
        })

    # 片段碎不碎
    segs = _segments(bgm)
    if segs:
        short = [s for s in segs if s[1] - s[0] < MIN_PIECE_SEC]
        if short:
            findings.append({
                "track": bgm_track,
                "track_name": "第%d条" % (bgm_track + 1),
                "kind": "bgm", "problem": "BGM 切得太碎",
                "at": short[0][0], "duration": round(short[0][1] - short[0][0], 2),
                "severity": "major", "count": len(short),
                "suggest": "%d 段短于 %.0f 秒，最少也要 1 秒一段" % (
                    len(short), MIN_PIECE_SEC),
                "confidence": 0.8,
            })
        if total > 60 and len(segs) > max(2, total * MAX_PIECES_RATIO):
            findings.append({
                "track": bgm_track,
                "track_name": "第%d条" % (bgm_track + 1),
                "kind": "bgm", "problem": "BGM 片段太多",
                "at": 0, "duration": round(total, 1),
                "severity": "minor", "count": len(segs),
                "suggest": "整片切成 %d 段（平均 %.0f 秒一段），可以少切一点"
                           % (len(segs), total / max(1, len(segs))),
                "confidence": 0.55,
            })
    if log:
        for row in findings:
            log("     %s：%s（%s）" % (row["track_name"], row["problem"],
                                      row["suggest"][:40]))
    return findings
