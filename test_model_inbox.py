# -*- coding: utf-8 -*-
"""模型收件箱测试：小脑推、模型被动收。

守三条：
    1. 模型**不用请求**也能拿到（push 完 pending 就有）；
    2. 读过的不会再给（游标只前进）；
    3. 收件箱里只有结果，没有原始数据。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from model_inbox import Inbox
from timeline import Timeline


def fill():
    tl = Timeline()
    tl.push("audio", "seam", {"at": 1.0, "grade": "ok"})
    tl.push("text", "edge", {"at": 1.0, "grade": "S"})
    tl.push("picture", "jump", {"at": 2.0, "grade": "ok"})
    box = Inbox()
    box.push_many(tl.entries)
    return box


class PassiveReceiveTest(unittest.TestCase):
    def test_pending_without_request(self):
        box = fill()
        self.assertEqual(3, len(box.pending()))

    def test_take_advances_cursor(self):
        box = fill()
        first = box.take(limit=2)
        self.assertEqual(2, len(first))
        self.assertEqual(1, len(box.pending()))
        box.take()
        self.assertEqual(0, len(box.pending()))

    def test_snapshot_merges_same_moment(self):
        box = fill()
        snap = box.snapshot(1.0)
        self.assertEqual(2, snap["count"])
        self.assertEqual({"audio", "text"}, set(snap["brains"]))

    def test_latest_by_brain(self):
        box = fill()
        row = box.latest("audio")
        self.assertEqual("audio", row["brain"])


class PayloadTest(unittest.TestCase):
    def test_no_raw_data_in_inbox(self):
        import model_gateway
        box = fill()
        model_gateway.assert_no_raw(box.to_json())


class SaveTest(unittest.TestCase):
    def test_roundtrip(self):
        box = fill()
        folder = tempfile.mkdtemp(prefix="inbox_")
        path = os.path.join(folder, "model_inbox.json")
        box.save(path)
        again = Inbox.load(path)
        self.assertEqual(len(box.pending()), len(again.pending()))
        self.assertEqual(0, again.cursor)


if __name__ == "__main__":
    unittest.main()
