# -*- coding: utf-8 -*-
"""实况结构分段：开场 → 目标 → 过程 → 结果 → 预告（规则优先，模型只做辅助）。

为什么不用模型直接打标签：实测它把"哇""看吧"标成开场、把"这两个不行"标成结果、
把"我们下一个火箭"标成本集目标——位置和语义都没抓住。
而这些阶段其实有很强的**位置 + 关键词**特征：

  hook    ：片头前 40 秒里的招呼句（哈喽/大家好/这里是/兄弟们）
  goal    ：前 2 分钟里带"今天/这集/要/准备/目标/上第 N 章"的句子
  result  ：带"做出来/完成/成功/搞定/拿到了/装上/启动/可以用"的句子
  tease   ：最后 2 分钟里带"下期/下一期/后面/明天/接下来我们"的句子
  progress：其余
"""
from __future__ import annotations

import re

GREET = ("哈喽", "大家好", "兄弟们", "这里是", "欢迎", "各位")
GOAL_WORDS = ("今天", "这一集", "这集", "本期", "目标", "打算", "准备", "要去做",
              "我们要", "上第", "第三章", "第四章", "接下来要做")
RESULT_WORDS = ("做出来", "做完了", "完成", "成功", "搞定", "拿到了", "装上了",
                "启动了", "可以用", "就行了", "好了", "成了", "通了", "出来了",
                "能做了", "搞定了", "凑齐")
TEASE_WORDS = ("下期", "下一期", "明天", "后面", "接下来我们", "下次", "再说",
               "先这样", "拜拜", "再见")
EXCLAM = ("哇", "我靠", "卧槽", "牛", "帅", "nb", "草")


def _norm(t):
    return re.sub(r"\s+", "", t or "")


def label_sentences(data):
    """给每句打阶段标签，返回 list[str]（长度=句子数）。"""
    segs = data.get("segments") or []
    n = len(segs)
    if not n:
        return []
    dur = float(data.get("duration") or 0)
    labels = ["progress"] * n
    for i, s in enumerate(segs):
        t = _norm(str(s[2] or ""))
        if not t:
            labels[i] = "other"
            continue
        a = float(s[0])
        d = float(s[1]) - a
        # 1) 开场招呼：前 45 秒 + 招呼词
        if a <= 45 and any(w in t for w in GREET):
            labels[i] = "hook"
            continue
        # 2) 本集目标：前 150 秒 + 目标词
        if a <= 150 and any(w in t for w in GOAL_WORDS):
            labels[i] = "goal"
            continue
        # 3) 结尾预告：最后 150 秒 + 预告词
        if dur and a >= dur - 150 and any(w in t for w in TEASE_WORDS):
            labels[i] = "tease"
            continue
        # 4) 结果/成果
        if any(w in t for w in RESULT_WORDS):
            labels[i] = "result"
            continue
        # 5) 纯感叹（哇/我靠/草）不算推进
        if len(t) <= 3 and any(w in t.lower() for w in EXCLAM):
            labels[i] = "other"
            continue
        # 6) 太短的残句
        if len(t) <= 2:
            labels[i] = "other"
            continue
    return labels


def spine_mask(labels, data, keep_kinds=("hook", "goal", "result", "tease")):
    """哪些句属于"叙事骨架"（必须保留），用于配额分配。"""
    return [x in keep_kinds for x in labels]


def summary(labels):
    cnt = {}
    for x in labels:
        cnt[x] = cnt.get(x, 0) + 1
    return cnt
