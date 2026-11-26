# -*- coding: utf-8 -*-
"""在“游戏音轨”上压音效，而不是在成片上猜。

为什么（子枫 2026-11-23）：
  合成前是多音轨的——人声在一条、游戏在另一条。在成片上做音效检测，
  人声和音效已经混在一起，只能靠频谱猜，结果把人声擦音也压了
  （实测 55.85 秒 -22.3dB 的正常说话被压了 -12dB，听起来就是"噪点"）。

  正确做法：在游戏轨上处理。实测这条轨的空档是 -96dB（纯静音）、
  有音效时 -29dB，差 67dB——比在混音里猜可靠得多。

判据（实测）：
  · 游戏轨能量 > -45dB → 有音效
  · 低于 -60dB → 纯静音，不用管
  · 前后各 RAMP 秒线性过渡（避免硬开关的"卡卡"）
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

SR = 16000
SILENT_DB = -60.0          # 低于这个算纯静音（实测空档 -96dB）
EFFECT_DB = -45.0          # 高于这个算有音效（实测音效 -29dB）
RAMP = 0.035               # 软开关渐变（秒）
CUT_DB = -18.0             # 压多少


def _track_audio(src, ffmpeg, track, ss=None, dur=None, sr=SR):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-i", src, "-map", "0:a:%d" % track, "-ac", "1", "-ar", str(sr),
            "-f", "s16le", "-"]
    done = subprocess.run(cmd, capture_output=True)
    return (np.frombuffer(done.stdout, dtype="<i2").astype(np.float32)
            / 32768.0)


def find_game_effects(audio, sr=SR, win=0.02):
    """在游戏轨上找“有声音”的区间。"""
    step = max(1, int(sr * win))
    n = audio.size // step
    if not n:
        return []
    frames = audio[:n * step].reshape(n, step)
    db = 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-9))
    loud = db > EFFECT_DB
    spans, start = [], None
    for k, flag in enumerate(loud):
        if flag and start is None:
            start = k
        elif not flag and start is not None:
            spans.append((start * win, k * win))
            start = None
    if start is not None:
        spans.append((start * win, n * win))
    # 太短的丢掉（<20ms），相邻 <100ms 的合并
    kept = [s for s in spans if s[1] - s[0] >= 0.02]
    merged = []
    for s in kept:
        if merged and s[0] - merged[-1][1] <= 0.10:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(s)
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def suppress_track(src, dst, ffmpeg, track, cut_db=CUT_DB, sr=SR):
    """把指定轨里的音效压掉，**其余轨原样保留**，输出到 dst。

    输出是一个和源片轨数一致的中间文件，可以直接喂给正常剪辑流程——
    这样人声轨一个字节都没动，只是游戏轨上的音效变轻了。
    """
    audio = _track_audio(src, ffmpeg, track, sr=sr)
    if audio.size < sr:
        return []
    spans = find_game_effects(audio, sr)
    if not spans:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", src, "-map", "0", "-c", "copy",
                        "-movflags", "+faststart", dst], check=True)
        return []
    gain = min(1.0, max(0.05, 10 ** (cut_db / 20.0)))
    parts = []
    for a, b in spans:
        parts.append(
            "volume='if(lt(t,%.3f),1,"
            "if(lt(t,%.3f),(1-%.4f)*(t-%.3f)/%.3f+%.4f,"
            "if(lt(t,%.3f),%.4f,"
            "if(lt(t,%.3f),%.4f+(1-%.4f)*(t-%.3f)/%.3f,1))))':eval=frame"
            % (a - RAMP, a, gain, a - RAMP, RAMP, gain,
               b, gain,
               b + RAMP, gain, gain, b, RAMP))
    graph = ",".join(parts)
    import tempfile
    script = os.path.join(tempfile.gettempdir(), "game_track_suppress.txt")
    # 把所有音轨都映射出来：目标轨过滤镜，其余轨直接复制
    ntr = _track_count(src, ffmpeg)
    lines, maps = [], []
    for i in range(ntr):
        if i == track:
            lines.append("[0:a:%d]%s[ga%d]" % (i, graph, i))
            maps += ["-map", "[ga%d]" % i]
        else:
            maps += ["-map", "0:a:%d" % i]
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(lines) if lines else "anull")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, "-filter_complex_script", script] + maps + [
           "-map", "0:v", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", dst]
    done = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    try:
        os.remove(script)
    except OSError:
        pass
    if done.returncode != 0:
        raise RuntimeError("游戏轨抑制失败：%s" % (done.stderr or "")[-300:])
    return spans


def _track_count(src, ffmpeg):
    """数一下源片有几条音轨。"""
    done = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", src],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    n = 0
    for line in (done.stderr or "").splitlines():
        if "Audio:" in line:
            n += 1
    return max(1, n)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("index_dir")
    ap.add_argument("--track", type=int, default=1,
                    help="游戏轨是 0-based 的第几条（默认 1，即第 2 条）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    import json
    import index as _index
    data = json.load(open(os.path.join(args.index_dir, "index.json"),
                          encoding="utf-8"))
    src = data.get("source") or data.get("path") or ""
    ff = _index.find_ffmpeg()
    audio = _track_audio(src, ff, args.track)
    spans = find_game_effects(audio)
    total = sum(b - a for a, b in spans)
    print("游戏轨（第 %d 条）有音效 %d 段，共 %.1f 秒（占 %.1f%%）"
          % (args.track + 1, len(spans), total,
             100.0 * total / max(1.0, audio.size / SR)))
    for a, b in spans[:10]:
        print("  %.2f ~ %.2f 秒（%.2f 秒）" % (a, b, b - a))
    if args.dry_run or not spans:
        return 0


if __name__ == "__main__":
    main()
