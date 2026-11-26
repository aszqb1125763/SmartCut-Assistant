# -*- coding: utf-8 -*-
"""模型网关：**本地大模型想"看"什么的唯一入口**。

子枫 2026-11-24：
    「本地大模型只接受对应数据，不允许接收任何原始数据，所有数据都要经过小脑计算输出。
      大脑允许输出他想看的位置信息——想看画面，画面小脑输出画面结果。」

所以模型只能做两件事：
    1. 发一个 **请求**（我看哪儿、看哪一类）；
    2. 收一个 **结果**（小脑算好的数值/颜色/条款），**永远拿不到原始数据**。

请求长这样（模型自己拼，不需要任何数值计算）：
    {"look": "seam",  "at": 41.7}
    {"look": "color", "at": 41.7}
    {"look": "picture", "at": 41.7}
    {"look": "tail",  "at": 41.7}
    {"look": "edges", "start": 58.9, "end": 73.3, "wanted": ["S", "A"]}
    {"look": "candidates", "gap": [58.9, 73.3], "top": 5}
    {"look": "text", "start": 55.0, "end": 75.0}

被拒绝的请求（白名单之外，尤其是要原始数据）：
    {"look": "audio"}、{"look": "frames"}、{"look": "words"} → PermissionError
"""
from __future__ import annotations

import numpy as np

import coords

SCHEMA_VERSION = 1

#: 明确禁止：模型不许直接要原始数据
RAW_LOOKS = frozenset(("audio", "frames", "words", "raw", "pcm", "video"))

#: 允许的"看"（全部走小脑）
LOOKS = {
    "seam": "音频脑：一处接缝的三轴 + 尾音（E03/A07）",
    "color": "编排：一处接缝的颜色（绿/黄/红/灰）",
    "pair": "音频脑：一对切点的三轴（E03）",
    "tail": "音频脑：尾音衰减（A07）",
    "region": "音频脑：区间静音/人声（A20/M03）",
    "band": "音频脑：三频段（A04）",
    "breath": "音频脑：换气点（A13）",
    "picture": "画面脑：切点两侧画面差（C01–C03）",
    "picture_span": "画面脑：一段时间的画面轨迹（C01–C04）",
    "text": "文字脑：句子 + S/A 级切点（B11）",
    "axis": "三轴快照：文字(是什么+多久) + 能量(形态)，时间轴统一",
    "brief": "给模型的三个数据（定性）：文字是什么 / 多久 / 能量怎么样",
    "energy": "音频脑：一段的能量形态（上升/平台/下降/静音）",
    "context": "文字脑：给模型的文字包（句子 + 标记 + 概念）",
    "edges": "文字脑：区间内的 S/A 级切点（B11）",
    "word": "语音脑：切没切字（B04）",
    "voiced": "语音脑：有人声无词（D03）",
    "candidates": "编排：局部搜索出的可切位置（已排序）",
}


class LookDenied(PermissionError):
    """模型要了不给看的东西（原始数据，或白名单外的类别）。"""


def _number(request, key, default=None):
    value = request.get(key, default)
    if value is None:
        raise ValueError("请求缺 %s" % key)
    return float(value)


def _range(request):
    if "gap" in request:
        pair = request.get("gap") or []
        if len(pair) != 2:
            raise ValueError("gap 必须是 [start, end]")
        return float(pair[0]), float(pair[1])
    return _number(request, "start"), _number(request, "end")


def see(calc, request):
    """模型"看"一次：返回只有小脑结果，不含任何原始数据。"""
    if not isinstance(request, dict):
        raise LookDenied("请求必须是 dict")
    look = str(request.get("look") or "").strip().lower()
    if look in RAW_LOOKS:
        raise LookDenied("模型不许直接读原始数据：%s" % look)
    if look not in LOOKS:
        raise LookDenied("不认识的看：%s（可用：%s）"
                         % (look, ", ".join(sorted(LOOKS))))

    if look == "seam":
        result = calc.probe_seam(_number(request, "at"))
    elif look == "color":
        result = calc.look(_number(request, "at"))
    elif look == "pair":
        result = calc.probe_pair(_number(request, "left"),
                                 _number(request, "right"))
    elif look == "tail":
        result = calc.probe_tail(_number(request, "at"))
    elif look == "region":
        start, end = _range(request)
        result = calc.probe_region(start, end)
    elif look == "band":
        start, end = _range(request)
        result = calc.probe_band(start, end)
    elif look == "breath":
        start, end = _range(request)
        result = calc.probe_breath(start, end)
    elif look == "picture":
        result = calc.probe_picture_jump(_number(request, "at"))
    elif look == "picture_span":
        start, end = _range(request)
        result = calc.probe_picture_span(start, end)
    elif look == "text":
        start, end = _range(request)
        result = calc.probe_text(start, end)
    elif look == "axis":
        start, end = _range(request)
        result = calc.axis_snapshot(start, end)
    elif look == "brief":
        start, end = _range(request)
        result = calc.brief(start, end)
    elif look == "energy":
        start, end = _range(request)
        result = calc.energy_shape(start, end)
    elif look == "context":
        start, end = _range(request)
        result = calc.text_context(start, end,
                                   limit=int(request.get("limit", 12)))
    elif look == "edges":
        start, end = _range(request)
        wanted = tuple(request.get("wanted") or ("S", "A"))
        result = {"edges": calc.safe_edges(start, end, wanted=wanted)}
    elif look == "word":
        result = calc.check_cut_safety(_number(request, "at"))
    elif look == "voiced":
        start, end = _range(request)
        result = calc.probe_voiced(start, end)
    else:  # candidates
        start, end = _range(request)
        result = {"candidates": calc.candidates(start, end,
                                                top=int(request.get("top", 5)))}
    return {
        "ok": True, "look": look, "result": result,
        "spec_version": coords.SPEC_VERSION,
        "schema_version": SCHEMA_VERSION,
    }


def assert_no_raw(payload, _path="root"):
    """断言返回里没有原始数据（数组/字节/音频），模型只能拿到结果。"""
    if isinstance(payload, np.ndarray):
        raise AssertionError("模型拿到了原始数组：%s" % _path)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        raise AssertionError("模型拿到了原始字节：%s" % _path)
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_no_raw(value, "%s.%s" % (_path, key))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload[:50]):
            assert_no_raw(value, "%s[%d]" % (_path, index))
    return True
