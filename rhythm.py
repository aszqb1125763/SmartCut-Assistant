# -*- coding: utf-8 -*-
"""章内节奏：把过长的块按 8~15 秒切开，切在停顿上（子枫中位 11.6 秒）。

实测子枫：单段中位 11.6 秒 / 最长 53.8 秒 / 58 段。
我们体裁模式：中位 15.4~19 秒 / 44 段 —— 块状，看着"一段一段"，不像他那样"碎着走"。

做法（不改内容，只改切点分布）：
  1. 对每个连续块，如果长度 > span_split（默认 16 秒），在里面找停顿：
     优先切在"句间停顿 ≥0.45 秒 + 句子说完"的位置
  2. 按目标 11 秒左右均分，切口尽量贴停顿，避免切在字中间
  3. 切完的块长度落在 6~16 秒为主，太短的（<4 秒）与相邻块并回去
"""
from __future__ import annotations


def split_blocks(data, spans, target=11.0, max_len=16.0, min_len=4.0):
    """把长块切碎到 target 附近。返回新 spans（时间顺序不变）。"""
    segs = data.get("segments") or []
    if not segs:
        return spans
    # 句尾停顿
    ends = []
    for i in range(len(segs) - 1):
        ends.append((float(segs[i][1]), float(segs[i + 1][0]) - float(segs[i][1])))
    ends.sort()

    def pause_at(t):
        """t 附近最像停顿的位置（±1.2 秒内，停顿越长越好）。"""
        best = None
        for e, gap in ends:
            d = abs(e - t)
            if d > 1.2:
                continue
            score = gap - d * 0.35
            if best is None or score > best[0]:
                best = (score, e)
        return best[1] if best else None

    out = []
    for a, b in spans:
        length = b - a
        if length <= max_len:
            out.append((a, b))
            continue
        n = max(2, int(round(length / target)))
        cuts = []
        for k in range(1, n):
            want = a + length * k / n
            p = pause_at(want)
            cuts.append(p if p and a + min_len < p < b - min_len else want)
        cuts = sorted(set(cuts))
        prev = a
        pieces = []
        for c in cuts:
            if c - prev >= min_len:
                # 切口两侧各留 50ms 空隙：否则两段首尾相接，
                # 后面的合并/吸附逻辑会把它们并回一大块（实测中位 11.7 秒被打回 20 秒）
                pieces.append((prev, c - 0.05))
                prev = c + 0.05
        pieces.append((prev, b))
        # 太短的并回前一块
        merged = []
        for x, y in pieces:
            if merged and (y - x) < min_len:
                merged[-1] = (merged[-1][0], y)
            else:
                merged.append((x, y))
        out.extend(merged)
    return out


def stats(spans):
    import numpy as np
    lens = np.array([b - a for a, b in spans]) if spans else np.array([0.0])
    return {"n": len(spans), "median": float(np.median(lens)),
            "mean": float(lens.mean()), "max": float(lens.max()),
            "lt5": int((lens < 5).sum())}
