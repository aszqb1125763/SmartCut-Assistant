# -*- coding: utf-8 -*-
"""判据单一真源测试（2026-11-24 建档）。

守四条：
  1. 阈值常量**只许在 coords.py 定义**（别处再定义一次就红，存量走白名单）。
  2. `tolerance.*` 与 `coords.*` 必须同源同结果（消灭"两套标准"）。
  3. `coords.py` 里每个标量阈值，都必须在 `_规范/04_阈值与量纲.md` 里有出处。
  4. 三段式的边界行为（ok / edge / over）。
"""
from __future__ import annotations

import os
import re
import unittest

import coords
import tolerance

BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(os.path.dirname(BASE), "_规范")
DOC_04 = os.path.join(ARCHIVE, "04_阈值与量纲.md")

#: 存量欠账：这些文件里还有同名阈值常量，P4 收口后从这里删掉。
#: 格式 (文件名, 常量名) —— 只许减、不许增。
KNOWN_DUPLICATES = {
    ("silence_trim.py", "SILENCE_DB"),   # P4 改 from coords import SILENCE_DB
}


def _py_files():
    for name in sorted(os.listdir(BASE)):
        path = os.path.join(BASE, name)
        if not name.endswith(".py") or not os.path.isfile(path):
            continue
        if name == "coords.py" or name.startswith("test_"):
            continue
        yield name, path


def _definitions(path):
    """返回 [(常量名, 行号)] —— 形如 `NAME = 12.3` 的定义。"""
    out = []
    pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*[-+]?\d+(?:\.\d+)?\s*(?:#.*)?$")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return out
    for index, line in enumerate(lines, 1):
        match = pattern.match(line)
        if match:
            out.append((match.group(1), index))
    return out


class ThresholdSingleSourceTest(unittest.TestCase):
    def test_thresholds_only_in_coords(self):
        """同名阈值不许在第二个文件里再定义一次。"""
        names = set(coords.thresholds())
        offenders = []
        for filename, path in _py_files():
            for name, line in _definitions(path):
                if name in names and (filename, name) not in KNOWN_DUPLICATES:
                    offenders.append("%s:%d %s" % (filename, line, name))
        self.assertEqual([], offenders,
                         "阈值只能在 coords.py 定义；存量欠账请登记 "
                         "KNOWN_DUPLICATES，不许新增：\n" + "\n".join(offenders))

    def test_known_duplicates_are_real(self):
        """白名单不许虚挂：登记了就必须真存在，清理完要删掉登记。"""
        stale = []
        for filename, name in KNOWN_DUPLICATES:
            path = os.path.join(BASE, filename)
            if not any(n == name for n, _ in _definitions(path)):
                stale.append("%s %s" % (filename, name))
        self.assertEqual([], stale,
                         "白名单已过期（该常量已不在该文件定义），请删除登记：\n"
                         + "\n".join(stale))

    def test_tolerance_matches_coords(self):
        """两处判同一个值必须同结论（这是"35/42 超标"那场争吵的根）。"""
        values = [0.0, 1.0, 4.999, 5.0, 5.001, 7.5, 9.999, 10.0, 10.001, 42.0]
        for value in values:
            self.assertEqual(coords.energy_grade(value), tolerance.energy_grade(value))
            self.assertEqual(coords.shape_grade(value), tolerance.shape_grade(value))
        for value in [0.0, 100.0, 499.9, 500.0, 500.1, 800.0, 1000.0, 1000.1]:
            self.assertEqual(coords.tone_grade(value), tolerance.tone_grade(value))
        self.assertEqual(coords.ENERGY_OK_DB, tolerance.ENERGY_CENTER)
        self.assertEqual(coords.EDGE_FACTOR, tolerance.EDGE_FACTOR)

    def test_grade_boundaries(self):
        self.assertEqual("ok", coords.grade(5.0, 5.0))
        self.assertEqual("edge", coords.grade(5.1, 5.0))
        self.assertEqual("edge", coords.grade(10.0, 5.0))
        self.assertEqual("over", coords.grade(10.001, 5.0))
        self.assertEqual("ok", coords.grade(-5.0, 5.0))       # 取绝对值
        self.assertEqual("over", coords.grade("bad", 5.0))    # 坏输入不许放行
        self.assertEqual("over", coords.combine("ok", "over", "ok"))
        self.assertEqual("edge", coords.combine("ok", "edge"))
        self.assertEqual("ok", coords.combine())


@unittest.skipUnless(os.path.isfile(DOC_04), "规范档案不在工作区（安装环境）")
class ThresholdDocumentedTest(unittest.TestCase):
    def test_coords_keys_documented(self):
        """coords.py 里每个标量阈值，都要在 04_阈值与量纲.md 有出处。"""
        with open(DOC_04, encoding="utf-8") as handle:
            text = handle.read()
        missing = [name for name in coords.thresholds()
                   if ("`%s`" % name) not in text]
        self.assertEqual([], missing,
                         "这些阈值键没有在 04_阈值与量纲.md 登记，"
                         "先补文档再写代码：\n" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
