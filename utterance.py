# -*- coding: utf-8 -*-
"""声音小脑：把文字、能量、时间**揉成一句话的画像**再交出去。

子枫 2026-11-25：
    「把我们声音三轴的数据用小脑加工，打包其中文字和能量信息和时间信息
      三者之间互相融合纠缠输出：文字和能量组合，能量和时间组合，
      时间和文字组合计算输出，这就是声音小脑的工作。」

所以这里不是输出三个轴，是输出**两两组合 + 三者融合**：

    文字 × 能量  →  这句话是用多大劲说的（强/弱、有没有起伏）
    能量 × 时间  →  这段时间能量怎么走（起/落/持续多久）
    时间 × 文字  →  这点内容说了多久（语速）

融合到「一句话」这个单位上：一句话就是一条完整的画像。

所有描述都由代码算（确定性的），不含任何判断。
"""
from __future__ import annotations

import os
import sys

import numpy as np

BASE = os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)         # 主项目是扁平结构：coords/brains 都在根目录

import coords                    # noqa: E402
from brains import dsp           # noqa: E402

SR = 16000


def _level(peak_db):
    """能量档位（用 coords 的阈值，口径统一）。"""
    for edge, label in ((coords.ENERGY_VERY_STRONG_DB, "极强"),
                        (coords.ENERGY_STRONG_DB, "强"),
                        (coords.ENERGY_UPPER_MID_DB, "中上"),
                        (coords.ENERGY_MID_DB, "中"),
                        (coords.ENERGY_WEAK_DB, "弱")):
        if peak_db >= edge:
            return label
    return "静音"


def _shape(levels):
    """能量走向：升/降/平。"""
    if len(levels) < 3:
        return "平"
    first, last = levels[0], levels[-1]
    peak = max(levels)
    if peak - first >= 8 and peak - last >= 8:
        return "升后落"
    if last - first >= 8:
        return "升"
    if first - last >= 8:
        return "落"
    return "平"


def _speed(chars, seconds):
    if seconds <= 0:
        return "无"
    rate = chars / seconds
    if rate >= 6:
        return "快"
    if rate >= 3:
        return "正常"
    if rate >= 1.2:
        return "慢"
    return "很慢"


def sentences(words, gap=0.45, max_chars=40):
    """按停顿切句（和文字小脑同一套参数）。"""
    out, current = [], []
    for word in words:
        text = str(word.get("word") or "").strip()
        if not text:
            continue
        if current:
            silence = float(word["start"]) - float(current[-1]["end"])
            length = sum(len(str(w["word"])) for w in current)
            if silence >= gap or length >= max_chars:
                out.append(current)
                current = []
        current.append(word)
    if current:
        out.append(current)
    return out


def pack(words, audio, sr=SR):
    """一句话一条画像：时间 × 文字 × 能量，三者融合。"""
    env = dsp.envelope(audio, sr)
    frame_s = dsp.FRAME_MS / 1000.0

    def slice_env(start, end):
        a = max(0, int(start / frame_s))
        b = min(env.size, int(end / frame_s))
        return env[a:b] if b > a else np.array([-120.0])

    rows = []
    for group in sentences(words):
        start = float(group[0]["start"])
        end = float(group[-1]["end"])
        text = "".join(str(w["word"]) for w in group)
        chars = len([c for c in text if c.strip()])
        seconds = max(0.01, end - start)
        seg = slice_env(start, end)

        peak = float(seg.max())
        # 均值只算有声帧：句子中间有词间静音，全算进去会把均值拉爆
        voiced = seg[seg > coords.SILENCE_DB]
        mean = float(voiced.mean()) if voiced.size else -120.0
        quiet_ratio = 1.0 - (voiced.size / float(seg.size)) if seg.size else 1.0
        level = _level(peak)
        shape = _shape(seg.tolist())
        speed = _speed(chars, seconds)

        # 三者融合：每条都是"两两组合"算出来的事实
        rows.append({
            "t0": round(start, 2),
            "t1": round(end, 2),
            "dur": round(seconds, 2),
            "text": text,
            "chars": chars,
            # 文字 × 能量：这句话用什么劲说的
            "text_energy": "%s · %s（峰值 %.0f dB，均值 %.0f dB）"
                           % (level, "有起伏" if shape != "平" else "很平",
                              peak, mean),
            # 能量 × 时间：这段时间能量怎么走
            "energy_time": "%s，持续 %.1f 秒" % (shape, seconds),
            # 时间 × 文字：这点内容说了多久
            "time_text": "%d 字 / %.1f 秒（语速%s）" % (chars, seconds, speed),
            # 三者合成的一句人话
            "portrait": "%s，%s，%s，中间空了 %.0f%%"
                        % ("用%s的力气" % level, "%s地说" % speed, shape,
                           quiet_ratio * 100),
            "level": level, "speed": speed, "shape": shape,
        })
    return rows


def brief(rows, limit=20):
    """给模型看的紧凑版：一行一句，三轴已经揉在一起。"""
    lines = []
    for row in rows[:limit]:
        lines.append("[%5.1f~%5.1fs] %s\n     %s ｜ %s ｜ %s"
                     % (row["t0"], row["t1"], row["text"],
                        row["text_energy"], row["energy_time"],
                        row["time_text"]))
    return "\n".join(lines)


def main():
    import brain
    import asr

    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\Media\demo_input.mp4"
    rows = asr.transcribe(path, name="large-v3-turbo", with_probability=True)
    words = [{"start": r["start"], "end": r["end"], "word": r["word"]}
             for r in rows]
    calc = brain.Calculator(path, track=0)
    try:
        audio = calc.audio
    finally:
        calc.close()
    pack_rows = pack(words, audio)
    print("声音小脑输出（%d 句，三轴已融合）\n" % len(pack_rows))
    for row in pack_rows:
        print("[%5.1f~%5.1fs] %s" % (row["t0"], row["t1"], row["text"]))
        print("    文字×能量：%s" % row["text_energy"])
        print("    能量×时间：%s" % row["energy_time"])
        print("    时间×文字：%s" % row["time_text"])
        print("    合成画像：%s\n" % row["portrait"])


if __name__ == "__main__":
    main()
