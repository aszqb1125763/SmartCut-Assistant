# -*- coding: utf-8 -*-
"""三段式范围判据：不判"绝对合格/不合格"，判"落在哪一段"。

**2026-11-24 改造：这里不再定义任何阈值，全部来自 `coords.py`。**
保留这个模块只是为了不改动历史调用点（`seam_picker`、`autopilot`、
测试都按 `tolerance.energy_grade(...)` 这种写法调用）。

原来的问题：`tolerance` 和 `seam_picker` 各自写了一份 5.0 / 500.0 / 5.0，
改一处忘一处，于是"选择器按 2 倍收、审计按 1 倍算"，
同一处接缝两个模块给不同结论——这是 2026-11-24 审计里
"35/42 超标 vs 找到了匹配切点"的真因。

以后：
    · 阈值 → `coords.ENERGY_OK_DB` 等
    · 分级 → `coords.grade()`（全项目唯一实现）
    · 这里只做名字兼容
"""
from __future__ import annotations

import coords

# 兼容旧名字（值来自 coords，不是这里定的）
ENERGY_CENTER = coords.ENERGY_OK_DB
TONE_CENTER = coords.TONE_OK_HZ
SHAPE_CENTER = coords.SHAPE_OK_DB
JUMP_CENTER = coords.JUMP_P90_DB
EDGE_FACTOR = coords.EDGE_FACTOR

grade = coords.grade
energy_grade = coords.energy_grade
tone_grade = coords.tone_grade
shape_grade = coords.shape_grade
jump_grade = coords.jump_grade
combine = coords.combine
label = coords.label
score_of = coords.score_of

__all__ = [
    "ENERGY_CENTER", "TONE_CENTER", "SHAPE_CENTER", "JUMP_CENTER",
    "EDGE_FACTOR", "grade", "energy_grade", "tone_grade", "shape_grade",
    "jump_grade", "combine", "label", "score_of",
]
