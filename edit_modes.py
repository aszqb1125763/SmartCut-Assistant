# -*- coding: utf-8 -*-
"""剪辑专项模式注册表。

模式负责决定“按什么内容逻辑剪”，力度只决定“留下多少”。新增体裁时在这里
注册，界面和自动任务不再各自硬编码 genre 参数。
"""
from __future__ import annotations

import copy


DEFAULT_MODE = "gameplay"

MODES = {
    "gameplay": {
        "label": "游戏实况",
        "description": "按目标、推进、结果和收尾组织章节；时长随素材与剧情复杂度浮动",
        "genre": "gameplay",
        "auto_retake": "1",
        "retake_llm": "1",
        "story_structure": True,
        "default_level": "medium",
    },
    "general": {
        "label": "通用视频",
        "description": "兼容模式：主要按空段、重复和逐句价值剪，不套游戏剧情结构",
        "genre": "off",
        "auto_retake": "0",
        "retake_llm": "0",
        "story_structure": False,
        "default_level": "medium",
    },
}

ALIASES = {
    "game": "gameplay", "游戏": "gameplay", "游戏实况": "gameplay",
    "off": "general", "generic": "general", "通用": "general", "通用视频": "general",
}


def normalize(name):
    key = str(name or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in MODES else DEFAULT_MODE


def profile(name):
    return MODES[normalize(name)]


def choices():
    return [(key, row["label"], row["description"]) for key, row in MODES.items()]


def label(name):
    return profile(name)["label"]


def apply_to_conf(conf, name, clone=False):
    target = copy.deepcopy(conf) if clone else conf
    key = normalize(name)
    row = MODES[key]
    target.setdefault("select", {}).update({
        "edit_mode": key,
        "genre": row["genre"],
        "auto_retake": row["auto_retake"],
        "retake_llm": row["retake_llm"],
    })
    return target
