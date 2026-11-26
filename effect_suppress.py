# -*- coding: utf-8 -*-
"""音效抑制：把“游戏音效”的高频尖峰压下去，不碰人声。

为什么需要（子枫 2026-11-23）：
  他的素材里游戏声音大部分是静音或接近静音，偶尔一个音效峰值特别突出。
  接缝就算三轴都匹配了，那个峰值还是会让人听出“这里有杂音”。

为什么能干净地分开（实测）：
  · 人声：重心 300~600 Hz，高频（>1kHz）占比 0.03~0.19
  · 游戏音效：重心 1500~4800 Hz，高频占比 0.40~0.97
  两个频段完全不重叠，所以“按高频占比 + 重心”就能把音效挑出来。

做法：
  1. 20ms 一帧扫全片，算高频占比和频谱重心
  2. 标出“高频 >0.40 且 重心 >1500Hz”的区间（至少 40ms 才算）
  3. 在这些区间上用 ffmpeg 的 equalizer 压 −15dB（只压 2kHz 以上）
  4. 一次 ffmpeg 重编码音频，视频流复制

局限：
  · 如果人声恰好落在高频（尖叫、气声），会被误压 —— 所以阈值留了余量
  · 只处理“整段音效”，不处理“半个音效”（40ms 以内的瞬态不压）
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

SR = 16000
# 判定为“音效”的阈值（实测标定）
# 第一版用 0.40/1500Hz，结果把人说话的擦音（s/sh/x）也抓进来了，
# 检出 1154 段、占全片 21.5%——那不是音效。收紧三条：
HI_RATIO_MIN = 0.55        # 高频占比 > 55%（擦音一般到不了这么高）
CENTROID_MIN = 2000.0      # 频谱重心 > 2000 Hz
MIN_DB = -42.0             # 音量 > -42dB（太轻的不是“峰值音效”）
MIN_SPAN = 0.035           # 至少 35ms（实测音效常只有 1~2 帧）
MERGE_GAP = 0.10           # 间隔 <100ms 的相邻段合并
CUT_DB = -12.0             # 压多少（volume 软开关是全频段，-24 会发闷，-12 温和）
RAMP = 0.035               # 软开关的渐变时长（秒）
CUT_FROM_HZ = 2000.0       # 从 2kHz 往上整体压（音效是宽带的，单点 EQ 覆盖不够）


def _frames(audio, sr=SR, win=0.02):
    step = max(1, int(sr * win))
    n = audio.size // step
    if not n:
        return np.zeros(0), np.zeros(0), step
    out_hi, out_cent, out_db = [], [], []
    win_fn = np.hanning(step)
    for k in range(n):
        seg = audio[k * step:(k + 1) * step]
        spec = np.abs(np.fft.rfft(seg * win_fn))
        freq = np.fft.rfftfreq(step, 1.0 / sr)
        total = float(spec.sum()) + 1e-12
        out_hi.append(float(spec[freq > 1000.0].sum() / total))
        out_cent.append(float((spec * freq).sum() / total))
        out_db.append(20 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-9)))
    return np.array(out_hi), np.array(out_cent), np.array(out_db), step


def find_effects(audio, sr=SR):
    """找出音效区间 [(start, end), ...]（秒）。"""
    hi, cent, db, step = _frames(audio, sr)
    if hi.size == 0:
        return []
    bad = (hi > HI_RATIO_MIN) & (cent > CENTROID_MIN) & (db > MIN_DB)
    spans = []
    start = None
    for k, flag in enumerate(bad):
        if flag and start is None:
            start = k
        elif not flag and start is not None:
            spans.append((start * step / sr, k * step / sr))
            start = None
    if start is not None:
        spans.append((start * step / sr, hi.size * step / sr))
    # 太短的丢掉，相邻太近的合并
    kept = [s for s in spans if s[1] - s[0] >= MIN_SPAN]
    merged = []
    for s in kept:
        if merged and s[0] - merged[-1][1] <= MERGE_GAP:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(s)
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def suppress_effects(src, dst, ffmpeg, sr=SR, cut_db=CUT_DB):
    """把 src 里的音效区间压掉，输出到 dst。返回音效区间列表。"""
    audio = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", src,
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True)
    pcm = (np.frombuffer(audio.stdout, dtype="<i2").astype(np.float32)
           / 32768.0)
    if pcm.size < sr:
        return []
    spans = find_effects(pcm, sr)
    if not spans:
        return []
    # 拼 filter。
    # 关键教训：equalizer/highshelf 的 enable 是**硬开关**——642.38 秒突然
    # 压 −24dB、642.54 秒突然放开，这个开关本身就是两次 24dB 突变。
    # 738 段就是 1476 次突变，听着就是"卡卡"。
    # 所以改成 volume 表达式做软开关：区间内降到 gain，前后各 RAMP 秒线性过渡。
    parts = []
    gain = min(1.0, max(0.05, 10 ** (cut_db / 20.0)))
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
    script = os.path.join(tempfile.gettempdir(), "effect_suppress_graph.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("[0:a]%s[a]" % graph)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, "-c:v", "copy",
           "-filter_complex_script", script,
           "-map", "0:v", "-map", "[a]",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", dst]
    done = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    try:
        os.remove(script)
    except OSError:
        pass
    if done.returncode != 0:
        raise RuntimeError("音效抑制失败：%s" % (done.stderr or "")[-300:])
    return spans


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    import index as _index
    ff = _index.find_ffmpeg()
    audio = subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-i", args.video,
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True)
    pcm = (np.frombuffer(audio.stdout, dtype="<i2").astype(np.float32)
           / 32768.0)
    spans = find_effects(pcm)
    total = sum(b - a for a, b in spans)
    print("检出音效 %d 段，共 %.1f 秒（占全片 %.1f%%）"
          % (len(spans), total, 100.0 * total / max(1.0, pcm.size / SR)))
    for a, b in spans[:15]:
        print("  %.2f ~ %.2f 秒（%.2f 秒）" % (a, b, b - a))
    if args.dry_run or not spans:
        return 0
    out = args.out or (os.path.splitext(args.video)[0] + "_降音效.mp4")
    suppress_effects(args.video, out, ff)
    print("输出：", out)


if __name__ == "__main__":
    main()
