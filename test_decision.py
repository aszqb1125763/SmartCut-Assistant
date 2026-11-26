# -*- coding: utf-8 -*-
"""决策参数测试：权重属于本地模型，执行层只读参数。

子枫 2026-11-24：
    「所有要权重、均衡、思考的全部属于本地模型。」

守四条：
    1. 小脑源码里没有任何权重（也不许读 decision 配置）；
    2. 选点/闸门不许在代码里写死权重比例，只能 `decision.candidate_rank()`；
    3. 改 `conf/decision.json` 就能改决策（不用改代码）；
    4. 参数缺失时有可复现的默认值。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

import decision

BASE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(BASE, "brains")


class BrainsHaveNoWeightsTest(unittest.TestCase):
    def test_brains_never_import_decision(self):
        offenders = []
        for name in os.listdir(PKG):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                text = handle.read()
            if re.search(r"^\s*(import|from)\s+decision\b", text, re.M):
                offenders.append(name)
        self.assertEqual([], offenders,
                         "小脑不许读决策权重：%s" % offenders)

    def test_brains_have_no_weight_literals(self):
        """形如 `0.7 *` / `* 0.3` 的权重不许出现在小脑里。"""
        pattern = re.compile(r"\b\d\.\d+\s*\*|\*\s*0\.\d+\b")
        offenders = []
        for name in os.listdir(PKG):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                for line in handle:
                    if pattern.search(line) and "coords." not in line:
                        offenders.append("%s: %s" % (name, line.strip()[:60]))
        self.assertEqual([], offenders, "小脑里出现了权重：%s" % offenders)


class ExecutionReadsWeightsTest(unittest.TestCase):
    def test_seam_picker_has_no_hardcoded_ratio(self):
        with open(os.path.join(BASE, "seam_picker.py"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("0.7 * _rank", text)
        self.assertIn("decision.candidate_rank()", text)

    def test_report_uses_decision_gate(self):
        with open(os.path.join(BASE, "coords_report.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("decision.gate()", text)


class ConfigDrivesDecisionTest(unittest.TestCase):
    def test_candidate_rank_normalises(self):
        folder = tempfile.mkdtemp(prefix="dec_")
        path = os.path.join(folder, "decision.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"candidate_rank": {"semantic": 2, "axes": 8}}, handle)
        semantic, axes = decision.candidate_rank(path)
        self.assertAlmostEqual(0.2, semantic)
        self.assertAlmostEqual(0.8, axes)

    def test_defaults_are_reproducible(self):
        semantic, axes = decision.candidate_rank("/no/such/file.json")
        self.assertAlmostEqual(0.7, semantic)
        self.assertAlmostEqual(0.3, axes)

    def test_gate_override(self):
        folder = tempfile.mkdtemp(prefix="dec_")
        path = os.path.join(folder, "decision.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"gate": {"max_p0": 0}}, handle)
        row = decision.gate(path)
        self.assertEqual(0, row["max_p0"])
        self.assertIn("max_relative_over", row)   # 没写的沿用默认

    def test_real_config_present(self):
        row = decision.load()
        self.assertIn("candidate_rank", row)
        self.assertIn("gate", row)


if __name__ == "__main__":
    unittest.main()
