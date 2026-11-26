# -*- coding: utf-8 -*-
"""模型收件箱：小脑算完就推过来，**模型被动接收，不需要主动请求**。

子枫 2026-11-24：
    「小脑接受数据处理出的结果不需要大模型主动接收，他是被动接受。」

所以模型那侧没有"我要看什么"的循环：
    · 小脑跑完 → `inbox.push(...)` → 落盘
    · 模型每次醒来 → `inbox.pending()` 读未读的一条一条（只读结果）
    · 想复核某一刻 → `inbox.snapshot(t)`（还是结果，不是原始数据）

和 `model_gateway.see()` 的关系：
    · `see()` 是"模型主动问一句"（保留，给交互/调试用）；
    · `Inbox` 是"被动收"（正常流程，模型不提问也能拿到）。
    两条路的返回值都只有小脑结果，都不含原始数据。
"""
from __future__ import annotations

import json
import os

SCHEMA_VERSION = 1


class Inbox:
    """只进不出的结果队列（按时间排序，模型读未读）。"""

    def __init__(self, path=None):
        self.path = path or ""
        self.entries = []
        self.cursor = 0          # 模型读到哪了

    def push(self, entry):
        self.entries.append(entry)
        self.entries.sort(key=lambda row: (row["t"], row["brain"]))
        return entry

    def push_many(self, entries):
        for entry in entries:
            self.push(entry)
        return self.entries

    # -- 模型侧（被动） -----------------------------------------------------
    def pending(self):
        """还没读过的结果（模型只读这个，不触发任何计算）。"""
        return list(self.entries[self.cursor:])

    def take(self, limit=None):
        """读走未读；返回读到的，游标前进。"""
        rows = self.pending()
        if limit:
            rows = rows[:int(limit)]
        self.cursor += len(rows)
        return rows

    def snapshot(self, t, tolerance=0.020):
        """某一刻的结果合并（还是结果，不是原始数据）。"""
        t = float(t)
        rows = [row for row in self.entries
                if abs(row["t"] - t) <= float(tolerance)]
        return {"t": round(t, 3), "timeline": "source",
                "brains": {row["brain"]: row["payload"] for row in rows},
                "count": len(rows)}

    def latest(self, brain=None):
        rows = [row for row in self.entries
                if brain is None or row["brain"] == brain]
        return rows[-1] if rows else None

    # -- 落盘 ---------------------------------------------------------------
    def to_json(self):
        return {"schema": "smartcut.model-inbox",
                "schema_version": SCHEMA_VERSION,
                "count": len(self.entries),
                "cursor": self.cursor,
                "entries": self.entries}

    def save(self, path=None):
        target = path or self.path
        if not target:
            return ""
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_json(), handle, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path):
        box = cls(path)
        if not os.path.isfile(path):
            return box
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return box
        box.entries = list(data.get("entries") or [])
        box.cursor = int(data.get("cursor") or 0)
        return box
