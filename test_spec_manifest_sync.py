# -*- coding: utf-8 -*-
"""规范条款清单（spec_manifest）一致性测试。

守三条：
  1. 运行副本 `conf/spec_manifest.json` 必须与工作区档案 `_规范/spec_manifest.json` 一致。
  2. 条款编号合法、不重复、分组对得上。
  3. **标了"已实现"的条款，必须有同名单测**（存量 7 条走白名单，只许减不许增）。

第 3 条是"完成的定义"落地：规范说做了 → 测试必须能查到；
没有测试的"已实现"一律不认。
"""
from __future__ import annotations

import json
import os
import re
import unittest

import coords

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_MANIFEST = os.path.join(BASE, "conf", "spec_manifest.json")
ARCHIVE = os.path.join(os.path.dirname(BASE), "_规范")
ARCHIVE_MANIFEST = os.path.join(ARCHIVE, "spec_manifest.json")

#: 建档时就已经标 ✅、但还没有专属单测的条款（历史欠账）。
#: **只许减，不许增**：以后新标 ✅ 的条款必须有 test_<编号小写>_*。
DONE_WITHOUT_TEST = {
    "A01", "A02", "A03", "A08", "A09", "B01", "E04",
}


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@unittest.skipUnless(os.path.isfile(RUNTIME_MANIFEST), "缺少 conf/spec_manifest.json")
class ManifestShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _load(RUNTIME_MANIFEST)
        cls.clauses = cls.data.get("clauses") or []

    def test_has_clauses(self):
        self.assertGreaterEqual(len(self.clauses), 52, "条款数不应少于建档时的 52 条")

    def test_ids_valid_and_unique(self):
        ids = [row.get("id") for row in self.clauses]
        bad = [ident for ident in ids if not re.match(r"^[A-E]\d{2}$", str(ident))]
        self.assertEqual([], bad, "编号必须是 A-E + 两位数字：%s" % bad)
        self.assertEqual(len(ids), len(set(ids)), "编号重复")

    def test_groups_match_counts(self):
        groups = self.data.get("groups") or {}
        total = sum(int(row.get("total") or 0) for row in groups.values())
        self.assertEqual(len(self.clauses), total, "分组计数与条款数不一致")
        self.assertEqual(self.data.get("clauses_total"), len(self.clauses))

    def test_spec_version_matches_code(self):
        self.assertEqual(coords.SPEC_VERSION, self.data.get("spec_version"),
                         "coords.SPEC_VERSION 与规范清单版本不一致")


@unittest.skipUnless(os.path.isfile(ARCHIVE_MANIFEST), "规范档案不在工作区")
class ManifestSyncTest(unittest.TestCase):
    def test_runtime_copy_matches_archive(self):
        runtime = _load(RUNTIME_MANIFEST)
        archive = _load(ARCHIVE_MANIFEST)
        self.assertEqual(archive.get("spec_version"), runtime.get("spec_version"))
        self.assertEqual(
            [row.get("id") for row in archive.get("clauses") or []],
            [row.get("id") for row in runtime.get("clauses") or []],
            "conf 副本与档案条款不一致：先跑 _规范/_tools/gen_manifest.py 再复制到 conf/")
        self.assertEqual(archive.get("counts"), runtime.get("counts"))


class DoneClauseNeedsTest(unittest.TestCase):
    """标了 ✅ 的条款必须有 test_<编号小写>_*。"""

    def _test_names(self):
        names = set()
        for filename in os.listdir(BASE):
            if not (filename.startswith("test_") and filename.endswith(".py")):
                continue
            path = os.path.join(BASE, filename)
            with open(path, encoding="utf-8") as handle:
                for match in re.finditer(r"def (test_[A-Za-z0-9_]+)\s*\(", handle.read()):
                    names.add(match.group(1).lower())
        return names

    def test_done_clauses_have_named_tests(self):
        try:
            clauses = coords.clauses()
        except Exception:  # noqa: BLE001
            self.skipTest("读不到条款清单")
        names = self._test_names()
        missing = []
        for ident, row in clauses.items():
            if row.get("status") != "done":
                continue
            if ident in DONE_WITHOUT_TEST:
                continue
            prefix = "test_%s_" % ident.lower()
            if not any(name.startswith(prefix) for name in names):
                missing.append(ident)
        self.assertEqual([], missing,
                         "这些条款标了 ✅ 但没有 test_<编号>_* 单测：\n"
                         + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
