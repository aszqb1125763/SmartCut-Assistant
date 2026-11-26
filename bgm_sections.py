# -*- coding: utf-8 -*-
"""BGM 分段适配：一条成片里按段落换不同曲子，按"这段有多热闹"来选。

子枫的原话：不要一条 BGM 从头铺到尾，要"根据当前一长段声音自动适配符合的 BGM"。

做法：
  1. 把成片按剧本章节、语义段落和停顿切段，每段随机 1~3 分钟
  2. 每段算"热闹程度"（音量 + 音高变化 + 语速，取索引里的声学特征/电平）
  3. 给曲库里每首也算"热闹程度"（RMS + 过零率，代表伴奏密度）
  4. 段落热闹 → 选热闹的曲子；段落平静 → 选安静的曲子；相邻段尽量不重复
  5. 每段各自做 loop（复用 build_loop），段与段之间 1.5 秒交叉淡化
  6. 最后按接缝做 -4dB 下压（复用 apply_dip），再混进成片
"""


try:
    import no_console as _nc
    _nc.install()          # 子进程也不弹窗（分析是在 index.py 子进程里跑的）
except Exception:
    pass

import os
import random
import subprocess
import sys
import tempfile
import wave

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import index as indexer                 # noqa: E402
import bgm as _bgm                      # noqa: E402


def _wav_read(path):
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def _track_energy(path, seconds=40.0):
    """曲子"热闹程度"：RMS(dB) + 过零率（后者粗略代表伴奏密度）。"""
    a = _bgm._decode(path, seconds=seconds, start=0.0, sr=16000, channels=1)
    if a is None or a.size == 0:
        return 0.0
    x = np.asarray(a, dtype=np.float32).reshape(-1)
    rms = float(np.sqrt(np.mean(x ** 2)) + 1e-9)
    db = 20 * np.log10(rms)
    zcr = float(np.mean(np.abs(np.diff(np.signbit(x).astype(np.int8))))) / 2.0
    return (db + 40.0) / 30.0 * 0.7 + min(1.0, zcr * 12.0) * 0.3


def _source_ranges_for_output(pieces, out_a, out_b):
    ranges = []
    for start, dur, src_a, src_b in _piece_map(pieces)[0]:
        end = start + dur
        lo, hi = max(float(out_a), start), min(float(out_b), end)
        if hi <= lo:
            continue
        scale = (src_b - src_a) / max(1e-9, dur)
        ranges.append((src_a + (lo - start) * scale,
                       src_a + (hi - start) * scale))
    return ranges


def section_mood(data, a, b, pieces=None):
    """段落"热闹程度"：优先用 seg_audio（音高变化/能量），否则用 seg_feat 电平。"""
    segs = data.get("segments") or []
    feat = data.get("seg_feat") or []
    au = data.get("seg_audio") or []
    source_ranges = _source_ranges_for_output(pieces, a, b) if pieces else [(a, b)]

    def overlaps(x, y):
        return any(y >= lo and x <= hi for lo, hi in source_ranges)

    lv, f0s = [], []
    for i, s in enumerate(segs):
        if len(s) < 2 or not overlaps(float(s[0]), float(s[1])):
            continue
        if len(au) == len(segs):
            r = au[i]
            lv.append(float(r[0]))
            f0s.append(float(r[3]))
        elif i < len(feat):
            lv.append(float(feat[i][1]))
    if not lv:
        return 0.5
    level = (float(np.mean(lv)) + 40.0) / 30.0
    pitch = min(1.0, float(np.mean(f0s)) / 60.0) if f0s else 0.5
    # 画面运动量：action 多的地方热闹，光靠音量看不出来
    motion = 0.0
    an = (data or {}).get("analysis") or {}
    ts, diffs = an.get("times") or [], an.get("diffs") or []
    if ts and diffs and len(ts) == len(diffs):
        vals = [d for t, d in zip(ts, diffs)
                if any(lo <= float(t) <= hi for lo, hi in source_ranges)]
        if vals:
            motion = float(np.mean(vals))
    return max(0.0, min(1.0, level * 0.35 + pitch * 0.15 + min(1.0, motion * 2.5) * 0.5))


def _piece_map(pieces):
    """返回源片区间到成片区间的映射，以及成片里的自然剪辑边界。"""
    rows, bounds = [], []
    acc = 0.0
    for pc in pieces or []:
        if not pc:
            continue
        if pc[0] == "trim":
            src_ranges = [(float(pc[1]), float(pc[2]))]
            dur = max(0.0, src_ranges[0][1] - src_ranges[0][0])
        else:
            src_ranges = [(float(pc[1][0]), float(pc[1][1])),
                          (float(pc[2][0]), float(pc[2][1]))]
            dur = max(max(0.0, b - a) for a, b in src_ranges)
        if dur <= 0:
            continue
        for a, b in src_ranges:
            if b > a:
                rows.append((acc, dur, a, b))
        acc += dur
        bounds.append(acc)
    return rows, bounds


def _source_to_output(rows, source_time):
    """把源片时间映射到成片；被删掉的点吸附到最近的保留边界。"""
    if not rows:
        return float(source_time)
    t = float(source_time)
    nearest = None
    for out_a, out_dur, src_a, src_b in rows:
        if src_a <= t <= src_b:
            ratio = (t - src_a) / max(1e-9, src_b - src_a)
            return out_a + min(out_dur, max(0.0, ratio * out_dur))
        for src_edge, out_edge in ((src_a, out_a), (src_b, out_a + out_dur)):
            item = (abs(t - src_edge), out_edge)
            if nearest is None or item < nearest:
                nearest = item
    return nearest[1] if nearest is not None else None


def _section_candidates(data, pieces, out_dur):
    """收集换曲候选点：章节 > 段落/停顿 > 普通句界 > 剪辑接缝。"""
    rows, piece_bounds = _piece_map(pieces)
    found = {}

    def add_output(t, priority):
        try:
            t = float(t)
        except (TypeError, ValueError):
            return
        if 0.01 < t < float(out_dur) - 0.01:
            key = round(t, 3)
            found[key] = max(priority, found.get(key, 0))

    def add_source(t, priority):
        mapped = _source_to_output(rows, t)
        if mapped is not None:
            add_output(mapped, priority)

    for t in piece_bounds[:-1]:
        add_output(t, 40)

    script = (data.get("script") or {}).get("sections") or []
    for sec in script:
        if isinstance(sec, dict):
            add_source(sec.get("start"), 100)
            add_source(sec.get("end"), 100)

    segs = data.get("segments") or []
    sem = data.get("semantic") or []
    for i, seg in enumerate(segs):
        if len(seg) < 2:
            continue
        add_source(seg[0], 30)
        add_source(seg[1], 30)
        if i < len(sem) and isinstance(sem[i], dict) and sem[i].get("para_break"):
            add_source(seg[0], 90)
        if i > 0 and len(segs[i - 1]) >= 2:
            gap_a, gap_b = float(segs[i - 1][1]), float(seg[0])
            if gap_b - gap_a >= 0.6:
                add_source((gap_a + gap_b) / 2.0, 80)

    for run in (data.get("analysis") or {}).get("runs") or []:
        if isinstance(run, (list, tuple)) and len(run) >= 2:
            add_source(run[0], 85)
            add_source(run[1], 85)
    return sorted((t, priority) for t, priority in found.items())


def make_sections(data, out_dur, pieces=None, min_len=60.0, max_len=180.0,
                  seed=None):
    """按合理语义边界切成随机 1~3 分钟的 BGM 段。"""
    out_dur = max(0.0, float(out_dur or 0.0))
    min_len = max(1.0, float(min_len or 60.0))
    max_len = max(min_len, float(max_len or 180.0))
    if out_dur <= 0:
        return []
    if out_dur <= max_len:
        return [(0.0, out_dur)]

    rng = random.Random(None if seed in (None, "") else str(seed))
    candidates = _section_candidates(data or {}, pieces, out_dur)
    edges = [0.0]
    cur = 0.0
    while out_dur - cur > max_len + 1e-6:
        lo = cur + min_len
        hi = min(cur + max_len, out_dur - min_len)
        if hi < lo:
            break
        target = rng.uniform(lo, hi)
        valid = [(t, priority) for t, priority in candidates if lo <= t <= hi]
        if valid:
            best_priority = max(priority for _t, priority in valid)
            cut = min((t for t, priority in valid if priority == best_priority),
                      key=lambda t: abs(t - target))
        else:
            cut = target
        if cut <= cur + 1e-6:
            break
        edges.append(float(cut))
        cur = float(cut)
    edges.append(out_dur)
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def attach_sections(video_path, sections, seam_times=None, out_path=None,
                    conf=None, log=print, data=None, pieces=None):
    """分段配 BGM：每段一首，段间交叉淡化，最后按接缝下压。"""
    cfg = conf or _bgm.load_conf()
    folder = str(cfg.get("folder") or "")
    tracks = _bgm.list_tracks(folder)
    if not tracks:
        log("BGM：曲库是空的（%s）" % folder)
        return None
    level_db = float(cfg.get("level_db", -22))
    dip_db = float(cfg.get("dip_db", 4.0))
    dip_sec = float(cfg.get("dip_sec", 1.2))
    xf = 1.5

    log("BGM：%d 段，每段单独挑曲子…" % len(sections))
    energies = [(t, _track_energy(t)) for t in tracks]
    energies.sort(key=lambda x: x[1])

    tmp = tempfile.mkdtemp(prefix="bgmseg_", dir=indexer.temp_root())
    parts = []
    try:
        for k, (a, b) in enumerate(sections):
            dur = max(4.0, float(b) - float(a))
            # 用成片时间点去索引里查"这段多热闹"
            mood = 0.5
            if data is not None:
                mood = section_mood(data, a, b, pieces=pieces)
            # 按段落热闹度的分位取曲目：保证不同段落拿到不同曲子
            if len(sections) > 1:
                ranks = sorted(range(len(sections)),
                               key=lambda j: section_mood(
                                   data, sections[j][0], sections[j][1], pieces=pieces)
                               if data is not None else 0.5)
                pos = ranks.index(k) if k in ranks else k
                idx = int(round(pos / float(len(sections) - 1) * (len(energies) - 1)))
            else:
                idx = int(round(mood * (len(energies) - 1)))
            # 相邻段尽量换一首（错开一位）
            if k > 0 and len(energies) > 1:
                prev = parts[-1][1] if parts else None
                if prev and energies[idx][0] == prev:
                    idx = (idx + 1) % len(energies)
            track, te = energies[idx]
            info = _bgm.analyze(track)
            win = info.get("seg_len") if isinstance(info, dict) else None
            start = 5.0
            seg_len = min(150.0, max(30.0, dur + xf))
            out_wav = os.path.join(tmp, "sec%02d.wav" % k)
            _bgm.build_loop(track, start, seg_len, dur + xf, out_wav,
                            level_db=level_db, xfade=2.0, fade_in=0.8,
                            fade_out=0.8)
            parts.append((out_wav, track))
            log("  第 %d 段 %.1f 分钟（热闹 %.2f）→ %s"
                % (k + 1, dur / 60, mood, os.path.basename(track)[:28]))
        return _assemble(video_path, parts, seam_times, dip_db, dip_sec,
                         out_path, cfg, tmp, log)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _assemble(video_path, parts, seam_times, dip_db, dip_sec, out_path, cfg,
              tmp, log):
    """把各段 loop 用 acrossfade 拼成一条 BGM，按下压，再混进成片。"""
    ff = indexer.find_ffmpeg()
    total = len(parts)
    merged = os.path.join(tmp, "bgm_all.wav")
    if total == 1:
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", parts[0][0], merged], capture_output=True,
                       creationflags=indexer.CREATE_NO_WINDOW)
    else:
        args = [ff, "-hide_banner", "-loglevel", "error", "-y"]
        for p, _t in parts:
            args += ["-i", p]
        graph = []
        cur = "[0:a]"
        for i in range(1, total):
            nxt = "[a%d]" % i
            graph.append("%s[%d:a]acrossfade=d=1.5:overlap=1%s"
                         % (cur, i, nxt if i < total - 1 else "[out]"))
            cur = nxt if i < total - 1 else "[out]"
        args += ["-filter_complex", ";".join(graph), "-map", "[out]",
                 "-ar", "48000", "-ac", "2", merged]
        r = subprocess.run(args, capture_output=True, creationflags=indexer.CREATE_NO_WINDOW)
        if r.returncode != 0:
            log("BGM：分段拼接失败，退回单曲：%s" % (r.stderr or b"")[-200:])
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", parts[0][0], merged], capture_output=True,
                           creationflags=indexer.CREATE_NO_WINDOW)

    # 接缝下压（在拼好的整条 BGM 上做）
    if seam_times and dip_db > 0:
        a, sr = _wav_read(merged)
        a2 = _bgm.apply_dip(a[:, None].repeat(2, axis=1), sr, seam_times,
                            dip_db=dip_db, dip_sec=dip_sec)
        _bgm._write_wav(merged, a2, sr)
        log("BGM：在 %d 个接缝处下压 %.1f dB" % (len(seam_times), dip_db))

    if out_path:
        # 先写到临时文件再替换，避免同名覆盖时 ffmpeg 读到半截文件
        tmp_out = out_path + ".bgmtmp.mp4"
        mix = _bgm.mix_into(video_path, tmp_out, merged, duck_db=0.0)
        try:
            os.replace(tmp_out, out_path)
        except OSError:
            out_path = tmp_out
        out = out_path
    else:
        out = os.path.splitext(video_path)[0] + "_bgm.mp4"
        mix = _bgm.mix_into(video_path, out, merged, duck_db=0.0)
    log("BGM：完成（%s）" % os.path.basename(out))
    return {"out": out, "sections": total,
            "tracks": [os.path.basename(t) for _p, t in parts],
            "mix": mix}
