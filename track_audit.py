# -*- coding: utf-8 -*-
"""逐轨审核：在合成之前，对每条音轨单独审一遍。

为什么必须在合成前（子枫 2026-11-23）：
  「审核要能发现问题是什么音轨上的问题、什么问题，这样才能单独改一个音轨。」

  合成之后声音混在一起，只能靠频谱猜"这是谁的错"——猜错就误伤
  （实测在成片上检测音效，误压了 8.4% 的人声）。

  合成前每条轨都是干净的，所以：
    · 说话轨 → 审“半句 / 切字 / 能量断裂”
    · 其他轨 → 审“音效残留 / 突兀尖峰”
  两个问题各自归到自己那条轨，互不牵连。

输出格式（带轨道归属，能直接拿去改）：
  {"track": 2, "track_name": "第3条", "kind": "speech",
   "problem": "半句", "at": 595.48, "duration": 0.32,
   "severity": "critical", "suggest": "把出点往后挪 0.12 秒",
   "confidence": 0.9}
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

SR = 16000
# 判定阈值（三段式：合格 / 边缘 / 超限）
GAP_DB = -50.0             # 低于这个算“几乎无声”
CLIFF_DB = 35.0            # 20ms 内掉这么多 = 断崖（25dB 人声起伏仍会触发）
CLIFF_DB_EDGE = 45.0       # 边缘区上限
SPIKE_DB = 20.0            # 非说话轨的突兀尖峰（合格线；12dB 误报太多）
SPIKE_DB_EDGE = 28.0


def _grab_track(src, ffmpeg, track, ss=None, dur=None, sr=SR):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-i", src, "-map", "0:a:%d" % track, "-ac", "1",
            "-ar", str(sr), "-f", "s16le", "-"]
    done = subprocess.run(cmd, capture_output=True)
    return (np.frombuffer(done.stdout, dtype="<i2").astype(np.float32)
            / 32768.0)


def _env(audio, sr=SR, win=0.02):
    step = max(1, int(sr * win))
    n = audio.size // step
    if not n:
        return np.zeros(0), win
    frames = audio[:n * step].reshape(n, step)
    return (20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)),
                                     1e-9)), win)


def _grade(value, ok, edge):
    """三段式：合格 / 边缘 / 超限。"""
    v = abs(float(value))
    if v <= ok:
        return "ok"
    if v <= edge:
        return "edge"
    return "over"


def audit_speech_track(src, ffmpeg, track, words=None, max_seconds=None,
                       keep=None, audio=None, sample_rate=SR):
    """审说话轨：找“能量断崖”（话说到一半断掉）。"""
    sample_rate = int(sample_rate or SR)
    if audio is None:
        audio = _grab_track(src, ffmpeg, track, dur=max_seconds,
                            sr=sample_rate)
    else:
        audio = np.asarray(audio, dtype=np.float32)
    if audio.size < sample_rate // 2:
        return []
    env, win = _env(audio, sr=sample_rate)
    if env.size < 3:
        return []
    d = np.diff(env)
    findings = []
    for k in range(1, len(d)):
        drop = -float(d[k])
        if drop <= CLIFF_DB:
            continue
        g = _grade(drop, CLIFF_DB, CLIFF_DB_EDGE)
        at = round(k * win, 2)
        # 断崖之后如果是长时间静音，那是“真的没声了”（正常句末）；
        # 只有“断崖后马上又有声”才是剪辑造成的（被切过）。
        after = env[k + 1:k + 30]
        if not after.size:
            continue
        # 真断崖的特征：掉下去之后**马上又回到接近原来的水平**。
        # 只是“掉下去不再起来”是正常句末，不该报。
        peak_back = float(after.max())
        if peak_back < env[k - 1] - 10.0:
            continue
        # 只保留落在“要留的段”里的断崖；被删掉的部分掉了是正常的
        if keep:
            _in = any(lo - 0.05 <= at <= hi + 0.05 for lo, hi in keep)
            if not _in:
                continue
        findings.append({
            "track": track, "track_name": "第%d条" % (track + 1),
            "kind": "speech", "problem": "能量断崖",
            "at": at, "duration": round(win * 2, 2),
            "severity": "critical" if g == "over" else "major",
            "grade": g,
            "drop_db": round(drop, 1),
            "suggest": "这处掉 %.0fdB 后马上又有声，是切过的痕迹；"
                       "检查出点是否落在字中间" % drop,
            "confidence": 0.85,
        })
    return findings


def audit_other_track(src, ffmpeg, track, max_seconds=None, audio=None,
                      sample_rate=SR):
    """审非说话轨：找突兀尖峰（音效残留 / 爆音）。"""
    sample_rate = int(sample_rate or SR)
    if audio is None:
        audio = _grab_track(src, ffmpeg, track, dur=max_seconds,
                            sr=sample_rate)
    else:
        audio = np.asarray(audio, dtype=np.float32)
    if audio.size < sample_rate // 2:
        return []
    env, win = _env(audio, sr=sample_rate)
    if env.size < 3:
        return []
    base = float(np.median(env))
    findings = []
    k = 0
    while k < len(env):
        rise = float(env[k]) - base
        if rise <= SPIKE_DB:
            k += 1
            continue
        start = k
        while k < len(env) and env[k] - base > SPIKE_DB * 0.6:
            k += 1
        dur = (k - start) * win
        if dur < 0.02:
            continue
        g = _grade(rise, SPIKE_DB, SPIKE_DB_EDGE)
        findings.append({
            "track": track, "track_name": "第%d条" % (track + 1),
            "kind": "other", "problem": "突兀尖峰",
            "at": round(start * win, 2), "duration": round(dur, 2),
            "severity": "major" if g == "over" else "minor",
            "grade": g, "rise_db": round(rise, 1),
            "suggest": "比这条轨的中位数高 %.0fdB，确认是不是多余音效"
                       % rise,
            "confidence": 0.7,
        })
    return findings


def audit(src, ffmpeg, track_kinds, words=None, max_seconds=None, spans=None,
          audio_cache=None):
    """对每条轨各审一遍，返回带轨道归属的问题清单。

    ``audio_cache`` 应是本次时间轴任务共享的 ``AudioEvidenceCache``。
    每条轨仍独立读取、独立判定；这里只把原来的重复整轨 ffmpeg 解码改成
    有界的 PCM 索引读取，不会把多轨混成一条。
    """
    out = []
    # track_kinds 的键是字符串（JSON 里存的 '0'/'1'…），这里统一转成 int，
    # 否则后面 "track + 1" 会报 can only concatenate str (not "int") to str。
    _items = []
    for _k, _v in (track_kinds or {}).items():
        try:
            _items.append((int(_k), _v))
        except (TypeError, ValueError):
            continue
    # 只审“成片里会保留的部分”（子枫流程第 10 步：按时间轴顺序同步进行）。
    # 审整条源片会把“正常说话的自然起伏”也算成断崖（实测 132 个误报）。
    _keep = []
    if spans:
        for _sp in spans:
            try:
                _keep.append((float(_sp[0]), float(_sp[1])))
            except (IndexError, TypeError, ValueError):
                continue
    for track, info in sorted(_items):
        kind = (info or {}).get("kind", "unknown")
        try:
            _audio = None
            _sample_rate = SR
            if audio_cache is not None:
                _entry = audio_cache.entry(track)
                _end = float(_entry.get("duration") or 0.0)
                if max_seconds is not None:
                    _end = min(_end, max(0.0, float(max_seconds)))
                _audio = audio_cache.review_samples(track, 0.0, _end)
                _sample_rate = int(getattr(audio_cache, "sample_rate", SR)
                                   or SR)
            if kind == "speech":
                _rows = audit_speech_track(
                    src, ffmpeg, track, words=words,
                    max_seconds=max_seconds, keep=_keep, audio=_audio,
                    sample_rate=_sample_rate)
                # 只报掉得最深的 10 个：人声本来就有起伏，
                # 全列出来反而看不出重点（子枫会自己听）。
                _rows.sort(key=lambda x: -float(x.get("drop_db", 0)))
                out.extend(_rows[:10])
            elif kind in ("other", "dense"):
                _rows = audit_other_track(
                    src, ffmpeg, track, max_seconds=max_seconds,
                    audio=_audio, sample_rate=_sample_rate)
                # 只报最突出的 8 个：尖峰逐个列出来没人看得完，
                # 而且大部分是正常的游戏音效（子枫会自己调音量）。
                _rows.sort(key=lambda x: -float(x.get("rank", 0)))
                out.extend(_rows[:8])
        except Exception as exc:  # noqa: BLE001
            # 取消不是一条“审核失败”问题，必须让工作流立即停止，不能吞掉后
            # 继续渲染。避免硬依赖缓存模块，兼容嵌入方的同名取消异常。
            if exc.__class__.__name__ == "AudioEvidenceCancelled":
                raise
            out.append({
                "track": track, "track_name": "第%d条" % (track + 1),
                "kind": kind, "problem": "审核失败",
                "at": 0, "duration": 0, "severity": "minor",
                "suggest": str(exc)[:80], "confidence": 0,
            })
    return out


def summarize(findings):
    out = {"count": len(findings), "by_track": {}, "by_kind": {},
           "critical": 0}
    for f in findings or []:
        out["by_track"][f.get("track_name")] = (
            out["by_track"].get(f.get("track_name"), 0) + 1)
        out["by_kind"][f.get("problem")] = (
            out["by_kind"].get(f.get("problem"), 0) + 1)
        if f.get("severity") == "critical":
            out["critical"] += 1
    return out


def to_text(findings, limit=30):
    lines = []
    for f in (findings or [])[:limit]:
        lines.append("%s（%s）%.2f 秒 +%.2fs：%s [%s]  建议：%s"
                     % (f.get("track_name"), f.get("kind"), f.get("at", 0),
                        f.get("duration", 0), f.get("problem"),
                        f.get("severity"), f.get("suggest", "")[:40]))
    return "\n".join(lines)
