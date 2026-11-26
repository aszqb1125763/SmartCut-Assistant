# -*- coding: utf-8 -*-
"""ASR 不替人做选择：低置信字给同音候选。

（从独立同音候选实验脚本整理迁入，只改了路径；
逻辑、阈值、候选数一个字没动。）

子枫 2026-11-24：
    「语音转文字输出的是绝对结果，这违反了绝对中立的设定。
      语音文本如果碰到了同音字，不能自己做选择。」

所以这里把「一个字」换成「一组同音候选」：
    ASR 说：山[焰|艳|燕|验]火  ← 「焰」只有 0.58 把握
    模型/人看到括号里的候选，自己决定是哪个字。

依据：faster-whisper 的 word_timestamps 会给每个词一个 probability。
低于阈值的字，用同音字表列出候选（含原字，排第一）。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from pypinyin import Style, pinyin      # noqa: E402

CACHE = os.path.join(BASE, "models", "asr", "homophones.json")
LOW_PROB = 0.70          # 低于这个概率才算"没把握"
MAX_CANDIDATES = 16      # 每个字最多给几个候选（yan4 有 15 个同音字）


def build_index(path=CACHE):
    """GB2312 一级汉字 → 拼音 → 同音字表（只建一次，落盘缓存）。"""
    chars = []
    for high in range(0xB0, 0xD8):
        for low in range(0xA1, 0xFF):
            try:
                chars.append(bytes([high, low]).decode("gb2312"))
            except Exception:  # noqa: BLE001
                continue
    index = {}
    for char in chars:
        try:
            py = pinyin(char, style=Style.TONE3)[0][0]
        except Exception:  # noqa: BLE001
            continue
        index.setdefault(py, []).append(char)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False)
    return index


def load_index(path=CACHE):
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
    return build_index(path)


def candidates(char, index, limit=MAX_CANDIDATES):
    """某个字的同音候选（原字排第一）。"""
    try:
        py = pinyin(char, style=Style.TONE3)[0][0]
    except Exception:  # noqa: BLE001
        return [char]
    same = [c for c in index.get(py, []) if c != char]
    return [char] + same[:limit - 1]


def format_with_alternatives(words, index, low_prob=LOW_PROB):
    """把低置信字写成 山[焰|艳|燕]火 的形式；高置信字原样。"""
    out = ""
    concerns = []
    for word in words:
        text = str(word.get("word") or "")
        prob = float(word.get("prob") or 1.0)
        if len(text) == 1 and prob < low_prob:
            cands = candidates(text, index)
            out += "[%s]" % "|".join(cands)
            concerns.append({"char": text, "prob": round(prob, 2),
                             "candidates": cands})
        else:
            out += text
    return out, concerns


def main():
    import asr

    src = r"D:\Media\demo_input.mp4"
    wav = os.path.join(BASE, "data", "_homophone.wav")
    import subprocess
    subprocess.run([os.path.join(BASE, "bin", "ffmpeg.exe"),
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-i", src, "-map", "0:a:1", "-ac", "1", "-ar", "16000",
                    wav], capture_output=True)

    index = load_index()
    print("同音字表：%d 个读音" % len(index))

    rows = asr.transcribe(wav, with_probability=True)
    text, concerns = format_with_alternatives(rows, index)
    print("\n原样转写：", "".join(str(r.get("word")) for r in rows))
    print("中立写法：", text)
    if concerns:
        print("\n不确定的字：")
        for item in concerns:
            print("  「%s」把握 %.2f ｜ 同音候选：%s"
                  % (item["char"], item["prob"], "、".join(item["candidates"])))


if __name__ == "__main__":
    main()
