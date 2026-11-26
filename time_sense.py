# -*- coding: utf-8 -*-
"""时间轴 × 能量轴：把已有的能量测量摊到时间上。

子枫 2026-11-25：
    「有声音的位置分数高，没声音的分数低。不要给他内容信息，
      要告诉他这个位置有信息密度而且高。」
    「刚好三轴中间有能量轴，我们已经有了，只是没叠加时间轴。」

所以这里**不新造任何测量**：
    · 能量轴：`brains/dsp.envelope()` 的 20ms dB 包络（三轴之一，早就有）
    · 时间轴：把包络按 0.5 秒一格压成 0~9 的分数
    · 阈值用 `coords.SILENCE_DB`（-45dB），和判据同一个口径

输出不含任何字的内容——只有时间、能量、空/实。
"""
from __future__ import annotations

import os
import sys

import numpy as np

BASE = os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)          # 主项目是扁平结构：coords/brains 都在根目录

import coords                       # noqa: E402
from brains import dsp              # noqa: E402

FPS = 2.0            # 每 0.5 秒一格
LOUD_DB = coords.ENERGY_STRONG_DB   # 满格参考：用判据里的"强"(-20dB)


def curve(audio, sr=16000, fps=FPS):
    """能量轴 → 时间轴：每 1/fps 秒一个 0~9。

    取这一格内包络的**中位数**（不是最大值）——最大值会被一声咳嗽带飞，
    中位数才代表「这半秒整体有多响」。
    """
    env = dsp.envelope(audio, sr)
    if env.size == 0:
        return []
    frame_s = dsp.FRAME_MS / 1000.0
    per = max(1, int(round((1.0 / fps) / frame_s)))
    values = []
    for start in range(0, env.size, per):
        chunk = env[start:start + per]
        if chunk.size == 0:
            continue
        level = float(np.median(chunk))
        if level <= coords.SILENCE_DB:
            values.append(0)
            continue
        ratio = (level - coords.SILENCE_DB) / (LOUD_DB - coords.SILENCE_DB)
        values.append(int(np.clip(round(ratio * 9), 1, 9)))
    return values


def art(values, fps=FPS, per_line=60):
    lines = []
    for start in range(0, len(values), per_line):
        chunk = values[start:start + per_line]
        head = "%5.1fs " % (start / fps)
        tail = " %5.1fs" % ((start + len(chunk)) / fps)
        lines.append(head + "".join(str(v) for v in chunk) + tail)
    return "\n".join(lines)


def ruler(total, fps=FPS, per_line=60):
    step = max(1, int(5 * fps))
    out = []
    for start in range(0, int(total * fps) + 1, per_line):
        marks = [" "] * per_line
        for offset in range(0, per_line, step):
            label = str(int((start + offset) / fps))
            for index, char in enumerate(label):
                if offset + index < per_line:
                    marks[offset + index] = char
        out.append("      " + "".join(marks))
    return "\n".join(out)


def describe(values, fps=FPS):
    if not values:
        return ""
    total = len(values) / fps
    zero = sum(1 for v in values if v == 0) / fps
    loud = sum(1 for v in values if v >= 7) / fps
    return ("总长 %.1f 秒：安静（0 分）%.1f 秒（%.0f%%），"
            "响（7~9 分）%.1f 秒（%.0f%%），中间 %.1f 秒。"
            % (total, zero, zero / total * 100, loud, loud / total * 100,
               total - zero - loud))


def analyse(audio, sr=16000, fps=FPS):
    values = curve(audio, sr, fps)
    return {"fps": fps, "values": values, "art": art(values, fps),
            "ruler": ruler(len(values) / fps, fps),
            "note": describe(values, fps)}


def main():
    import brain
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\Media\demo_input.mp4"
    calc = brain.Calculator(path, track=0)
    try:
        audio = calc.audio
    finally:
        pass
    result = analyse(audio)
    print("时间轴 × 能量轴（每格 %.1f 秒，数字=能量 0~9，0=%d dB 以下）"
          % (1 / result["fps"], coords.SILENCE_DB))
    print(result["ruler"])
    print(result["art"])
    print("\n%s" % result["note"])
    calc.close()


if __name__ == "__main__":
    main()
