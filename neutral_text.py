# -*- coding: utf-8 -*-
"""中立转写：正文干净，只标少数不确定处。

子枫 2026-11-24：
    「大量的词是绝对能转写成功的，失败的反而少数。
      你不能把整个句子所有可能性让他选择，这也没用。」

设计：
    正文：原样输出（实测 368 字里 97.8% 都在这里）
    附注：只列把握 < 0.45 的位置，每处 3 个同音候选

为什么阈值取 0.45：
    turbo 实测低把握字占比 — 0.70: 14.8% / 0.50: 5.7% / 0.45: 约 4% / 0.30: 2.0%
    0.45 落在"明显不确定"的区间，不会把正常字也标上。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.environ.get("SMARTCUT_HOME") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)         # 主项目是扁平结构：coords/brains 都在根目录

from pypinyin import Style, pinyin      # noqa: E402

ASR_DIR = os.path.join(BASE, "models", "asr")
INDEX_PATH = os.path.join(ASR_DIR, "homophones.json")

THRESHOLD = 0.45
TOP = 3
_index = None


def _load_index():
    global _index
    if _index is None:
        try:
            with open(INDEX_PATH, encoding="utf-8") as handle:
                _index = json.load(handle)
        except (OSError, ValueError):
            _index = {}
    return _index


def _candidates(char, limit=TOP):
    index = _load_index()
    try:
        py = pinyin(char, style=Style.TONE3)[0][0]
    except Exception:  # noqa: BLE001
        return [char]
    others = [c for c in index.get(py, []) if c != char]
    return [char] + others[:limit - 1]


def neutral(rows, threshold=THRESHOLD):
    """词级转写 → (干净正文, 不确定清单)。"""
    text = ""
    concerns = []
    for row in rows:
        word = str(row.get("word") or "")
        prob = float(row.get("prob") or 1.0)
        text += word
        if prob < threshold and len(word) == 1:
            concerns.append({
                "pos": len(text) - 1,
                "char": word,
                "prob": round(prob, 2),
                "t": round(float(row.get("start") or 0.0), 2),
                "candidates": _candidates(word),
            })
    return text, concerns


def render(rows, threshold=THRESHOLD):
    """给模型看的完整块：正文 + 不确定清单。"""
    text, concerns = neutral(rows, threshold)
    if not concerns:
        return text, concerns
    lines = [text, "", "不确定 %d 处（把握 < %.2f）：" % (len(concerns), threshold)]
    for item in concerns:
        lines.append("  %.2fs 第 %d 字「%s」%.2f → 可能：%s"
                     % (item["t"], item["pos"] + 1, item["char"],
                        item["prob"], " / ".join(item["candidates"])))
    return "\n".join(lines), concerns


def main():
    import asr
    wav = os.path.join(BASE, "data", "_long.wav")
    rows = asr.transcribe(wav, name="large-v3-turbo", beam_size=5,
                          with_probability=True)
    block, concerns = render(rows)
    total = sum(len(str(r.get("word") or "")) for r in rows)
    print("共 %d 字，标注 %d 处（%.1f%%）\n"
          % (total, len(concerns), len(concerns) / total * 100))
    print(block[:900])


if __name__ == "__main__":
    main()
