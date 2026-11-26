# -*- coding: utf-8 -*-
"""三轴并排输出：时间 / 文字 / 能量，一列一个采样点，竖直对齐。

子枫 2026-11-25：
    「把三个数据给大脑，在每一秒中的位置，文字是什么，能量是怎样的一个横轴，
      竖直输出每次每组三个数据：

      时间轴- 1-2 3-4-5-6-7-8-9
      文字轴-我我我我我我我我我
      能量轴-10-9-8-7-6-5-4-3-2」

所以这里不做"融合"，只做**对齐**：
    每一列 = 一个字（它说的那一刻）
    第一行 = 这个字在几秒
    第二行 = 这个字（多音字给候选，让它自己选）
    第三行 = 这个字的能量档（0~9）

三行严格等宽，一列三数据。模型横着读是时间，竖着读是一个字的三项。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)         # 主项目是扁平结构：coords/brains 都在根目录

import coords                    # noqa: E402
from brains import dsp           # noqa: E402

SR = 16000
LOW_PROB = 0.45          # 低于它就给同音候选
PER_LINE = 24            # 一行放几个字


def _level(value_db):
    """能量 → 0~9（和 time_sense 同一套映射）。"""
    if value_db <= coords.SILENCE_DB:
        return 0
    ratio = (value_db - coords.SILENCE_DB) / (
        coords.ENERGY_STRONG_DB - coords.SILENCE_DB)
    return int(min(9, max(1, round(ratio * 9))))


def _candidates(char, index, limit=3):
    try:
        from pypinyin import Style, pinyin
        py = pinyin(char, style=Style.TONE3)[0][0]
    except Exception:  # noqa: BLE001
        return [char]
    others = [c for c in index.get(py, []) if c != char]
    return [char] + others[:limit - 1]


def load_index():
    path = os.path.join(BASE, "models", "asr", "homophones.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def columns(words, audio, sr=SR):
    """每个字一列：[(秒, 显示文字, 能量档)]。"""
    env = dsp.envelope(audio, sr)
    frame_s = dsp.FRAME_MS / 1000.0
    index = load_index()
    rows = []
    for word in words:
        text = str(word.get("word") or "")
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        prob = float(word.get("prob") or 1.0)
        chars = [c for c in text if c.strip()]
        if not chars:
            continue
        # 这个词的能量（只算有声帧）
        a = max(0, int(start / frame_s))
        b = min(env.size, max(a + 1, int(end / frame_s)))
        seg = env[a:b]
        voiced = seg[seg > coords.SILENCE_DB]
        level = _level(float(voiced.mean()) if voiced.size else -120.0)

        span = max(1, len(chars))
        for position, char in enumerate(chars):
            moment = start + (end - start) * position / span
            if prob < LOW_PROB and span == 1:
                shown = "[%s]" % "/".join(_candidates(char, index))
            else:
                shown = char
            rows.append((round(moment, 1), shown, level))
    return rows


def per_second(words, audio, sr=SR):
    """每秒一列：这一秒说了哪些字、有多响。"""
    env = dsp.envelope(audio, sr)
    frame_s = dsp.FRAME_MS / 1000.0
    total = float(words[-1]["end"]) if words else 0.0
    count = int(total) + 1
    texts = [[] for _ in range(count)]
    for word in words:
        text = str(word.get("word") or "")
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        index = min(count - 1, max(0, int(start)))
        chars = [c for c in text if c.strip()]
        if chars:
            texts[index].extend(chars)
    rows = []
    for second in range(count):
        a = max(0, int(second / frame_s))
        b = min(env.size, max(a + 1, int((second + 1) / frame_s)))
        seg = env[a:b]
        voiced = seg[seg > coords.SILENCE_DB] if seg.size else seg
        level = _level(float(voiced.mean()) if voiced.size else -120.0)
        chars = texts[second]
        shown = "".join(chars[:2]) + ("+" if len(chars) > 2 else "")
        rows.append((float(second), shown or "·", level))
    return rows


def render_seconds(rows, per_line=20):
    out = []
    for start in range(0, len(rows), per_line):
        chunk = rows[start:start + per_line]
        moments = " ".join("%4.0f" % row[0] for row in chunk)
        texts = " ".join("%-4s" % row[1] for row in chunk)
        levels = " ".join("%4d" % row[2] for row in chunk)
        out.append("时间轴- %s" % moments)
        out.append("文字轴- %s" % texts)
        out.append("能量轴- %s" % levels)
        out.append("")
    return "\n".join(out)


def render(rows, per_line=PER_LINE):
    """三行一组：时间 / 文字 / 能量。按 per_line 分段。"""
    out = []
    for start in range(0, len(rows), per_line):
        chunk = rows[start:start + per_line]
        moments = " ".join("%4.1f" % row[0] for row in chunk)
        texts = " ".join("%-4s" % row[1] for row in chunk)
        levels = " ".join("%4d" % row[2] for row in chunk)
        out.append("时间轴- %s" % moments)
        out.append("文字轴- %s" % texts)
        out.append("能量轴- %s" % levels)
        out.append("")
    return "\n".join(out)


def analyse(words, audio, per_line=PER_LINE, by_second=True):
    if by_second:
        rows = per_second(words, audio)
        return {"rows": rows, "text": render_seconds(rows), "by": "second"}
    rows = columns(words, audio)
    return {"rows": rows, "text": render(rows, per_line), "by": "char"}


def main():
    import asr
    import brain

    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\Media\demo_input.mp4"
    rows = asr.transcribe(path, name="large-v3-turbo", with_probability=True)
    words = [{"start": r["start"], "end": r["end"], "word": r["word"],
              "prob": r["prob"]} for r in rows]
    calc = brain.Calculator(path, track=0)
    try:
        audio = calc.audio
    finally:
        calc.close()
    result = analyse(words, audio)
    print("三轴并排（一列一个时刻；能量 0=静音 9=最响）\n")
    print(result["text"])


if __name__ == "__main__":
    main()
