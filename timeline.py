# -*- coding: utf-8 -*-
"""统一时间轴：所有小脑的结果都落在**同一条源片时间轴**上。

子枫 2026-11-24：
    「所有小脑要在时间轴上严格同步。」
    「小脑处理出的结果不需要大模型主动请求，它是被动接受。」

所以：
    · 每条结果都带 `t`（源片秒）、`timeline="source"`、`brain`、`kind`；
    · `snapshot(t)` 把同一时刻各小脑的结果合成一条，容差默认 ±20ms
      （一帧的量级，超过就说明两个脑没对齐）；
    · 时间轴可落盘（`coords_timeline.json`），事后能复查"当时各脑看到了什么"。
"""
from __future__ import annotations

import json
import os

TOLERANCE = 0.020      # 20ms：比一帧还小，算"同一时刻"
SCHEMA_VERSION = 1


def entry_of(brain, kind, payload):
    """把一次探针结果规范成时间轴条目。"""
    t = payload.get("at")
    if t is None:
        t = payload.get("start")
    if t is None:
        raise ValueError("%s/%s 的结果没有时间" % (brain, kind))
    return {
        "t": round(float(t), 3),
        "brain": brain,
        "kind": kind,
        "timeline": "source",
        "payload": payload,
    }


class Timeline:
    """按源片时间排序的结果集合。"""

    def __init__(self, tolerance=TOLERANCE):
        self.tolerance = float(tolerance)
        self.entries = []

    def __len__(self):
        return len(self.entries)

    def push(self, brain, kind, payload):
        item = entry_of(brain, kind, payload)
        self.entries.append(item)
        self.entries.sort(key=lambda row: (row["t"], row["brain"]))
        return item

    def push_many(self, rows):
        for brain, kind, payload in rows:
            self.push(brain, kind, payload)
        return self.entries

    def at(self, t):
        """该时刻所有小脑的结果（±容差）。"""
        t = float(t)
        return [row for row in self.entries
                if abs(row["t"] - t) <= self.tolerance]

    def span(self, start, end):
        start, end = float(start), float(end)
        return [row for row in self.entries if start <= row["t"] <= end]

    def snapshot(self, t):
        """同一时刻的"合成一帧"：{脑名: 结果}。"""
        rows = self.at(t)
        return {
            "t": round(float(t), 3),
            "timeline": "source",
            "brains": {row["brain"]: row["payload"] for row in rows},
            "count": len(rows),
            "aligned": self.is_aligned(t),
        }

    def is_aligned(self, t):
        """该时刻的多个脑是否落在容差内（严格同步的检验）。"""
        rows = self.at(t)
        if len(rows) < 2:
            return True
        stamps = [row["t"] for row in rows]
        return (max(stamps) - min(stamps)) <= self.tolerance

    def to_json(self):
        return {"schema": "smartcut.coords-timeline",
                "schema_version": SCHEMA_VERSION,
                "timeline": "source",
                "tolerance": self.tolerance,
                "count": len(self.entries),
                "entries": self.entries}

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_json(), handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path
