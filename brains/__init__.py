# -*- coding: utf-8 -*-
"""小脑包：处理原始数据的插件集合。

分层（子枫 2026-11-24 定的三条）：
    原始数据（分类打包） → 【小脑插件】 → 结果 → 【本地模型】只做决策
    · 一个脑只拿自己声明的那几类原始数据（多一类 / 错一类直接报错）；
    · 小脑之间不交流（不 import、不注入彼此）；
    · 小脑不碰本地模型。

现有插件：
    audio_brain    音频（E03 / A04 / A07 / A13 / A20）
    speech_brain   词级（B04 / D03）
    picture_brain  画面（C01–C04）
"""
from .base import (AudioSource, BrainPlugin, FrameSource, RAW_KINDS,
                   SCHEMA_VERSION, SOURCE_TYPES, WordSource)
from .loader import available, list_brains, load, load_all

__all__ = ["AudioSource", "BrainPlugin", "FrameSource", "RAW_KINDS",
           "SCHEMA_VERSION", "SOURCE_TYPES", "WordSource",
           "available", "list_brains", "load", "load_all"]
