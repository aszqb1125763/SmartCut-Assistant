# -*- coding: utf-8 -*-
"""小脑加载器：按类别装配，**不注入兄弟插件**。

子枫 2026-11-24：
    「数据直接分类打包给对应的小脑就行。」
所以这里只做一件事：按插件声明的 `accepts`，把对应那几类原始数据递给它；
多给一类、给错类型，构造时就抛 TypeError。
"""
from __future__ import annotations

import importlib
import os

from .base import BrainPlugin

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULES = ("audio_brain", "speech_brain", "picture_brain",
            "text_brain")


def available():
    """{名字: 插件类}——只扫本包。"""
    found = {}
    for module_name in _MODULES:
        module = importlib.import_module("%s.%s" % (__package__, module_name))
        for attr in dir(module):
            obj = getattr(module, attr)
            if (isinstance(obj, type) and issubclass(obj, BrainPlugin)
                    and obj is not BrainPlugin and getattr(obj, "name", "")):
                found[obj.name] = obj
    return found


def list_brains():
    """给报告/界面看的清单：名字 + 需要哪几类原始数据 + 输出字段。"""
    return {name: {"accepts": list(cls.accepts), "emits": list(cls.emits)}
            for name, cls in sorted(available().items())}


def load(name, **sources):
    """装一个小脑：只递它声明的那几类原始数据。"""
    plugins = available()
    if name not in plugins:
        raise KeyError("没有这个小脑：%s（现有：%s）"
                       % (name, ", ".join(sorted(plugins))))
    return plugins[name](**sources)


def load_all(**sources):
    """一次装好所有小脑：各自只拿到自己声明的那几类。"""
    out = {}
    for name, cls in sorted(available().items()):
        picked = {kind: sources[kind] for kind in cls.accepts
                  if kind in sources}
        if len(picked) != len(cls.accepts):
            continue
        out[name] = cls(**picked)
    return out
