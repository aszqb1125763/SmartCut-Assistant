# -*- coding: utf-8 -*-
"""长链语义检查：句子 → 段落 → 文章，三层。

为什么分三层（子枫 2026-11-23）：
  就像小学语文课本——句子、段落、文章是三个层级，删掉不同量的东西，
  该检查的是不同的上一层：

    删掉半句话  →  查"这一段（句子组）"还通不通
    删掉一整段  →  查"整篇文章"还通不通

  不是"删了什么都查全片"——那样判不准：删一句废话时全片根本不受影响，
  但**那一段的衔接**可能已经断了。

三层的判据：
  · 句子层：这两句接起来读着通吗
  · 段落层：删掉中间这几句，剩下的这一段还讲同一件事吗
  · 文章层：删掉这一段之后，全片的因果和结论还成立吗

模型：本地 Qwen3-8B（和软件剪视频用的是同一个）。
关键经验：给它**判定基准**（什么算通、什么算断），准确率从 49% 提到 100%；
只说"帮我判断"它就抓不住重点。
"""
from __future__ import annotations

import re

LEVEL_SENTENCE = "sentence"
LEVEL_PARAGRAPH = "paragraph"
LEVEL_ARTICLE = "article"

# 删掉多少算“段落级”（秒）。再往上就是文章级。
PARAGRAPH_SECONDS = 2.0
ARTICLE_SECONDS = 12.0

SYSTEM = (
    "你是视频剪辑的语义审核员。任务：判断把中间那段删掉之后，观众还能不能听懂。\n"
    "\n"
    "判定基准（按这个来，别自己加严格度）：\n"
    " · 通：观众听完不会卡住。哪怕少了一点细节，只要话题和指代接得上就算通。\n"
    " · 勉强：观众会“咦”一下，但能自己补上那点信息。\n"
    " · 断：观众会听不懂——话题跳了，或者后句的结论在前文完全找不到依据。\n"
    "\n"
    "重要：这是口播录播，说话本来就啰嗦、有重复、有省略。\n"
    "不要因为“少了一句解释”就判断——只有观众真的会听不懂才算断。\n"
    "\n"
    "只输出三行，不要解释：\n"
    "结果：通 / 勉强 / 断\n"
    "理由：一句话，不超过 20 字\n"
    "缺口：如果有信息缺失，写出缺的是什么；没有就写“无”"
)


def _clip(text, limit=500):
    text = str(text or "").replace("\n", " ").strip()
    return text[:limit]


def level_for_removed(seconds):
    """按删掉的时长选检查层级。"""
    if seconds is None:
        return LEVEL_PARAGRAPH
    if seconds < PARAGRAPH_SECONDS:
        return LEVEL_SENTENCE
    if seconds < ARTICLE_SECONDS:
        return LEVEL_PARAGRAPH
    return LEVEL_ARTICLE


def build_question(before, removed, after, level):
    """按层级拼问题——三次问法不同，因为要判的东西不同。"""
    if level == LEVEL_SENTENCE:
        head = ("下面两句话本来是连在一起的，中间被删掉了一小段。"
                "判断它们接起来读着通不通。")
    elif level == LEVEL_PARAGRAPH:
        head = ("下面是一段话，中间被删掉了几句。判断剩下的这一段还讲不"
                "讲同一件事、读起来顺不顺。")
    else:
        head = ("下面是一篇长内容，中间被删掉了一整段。判断删掉之后，"
                "整体的因果和结论还成不成立（伏笔有没有交代、结论有没有依据）。")
    return ("%s\n\n【删掉之前，前文】\n%s\n\n【被删掉的内容】\n%s\n\n"
            "【删掉之后，后文】\n%s\n\n请按判定基准判断。"
            % (head, _clip(before), _clip(removed, 400), _clip(after)))


def judge(before, removed, after, level=LEVEL_PARAGRAPH, server=None,
          max_tokens=120):
    """问一次 8B，返回 {'result': '通'/'勉强'/'断', 'score': 0~1, 'why': ...}。

    server 传一个已启动的 proofread.Server 可以复用连接（批量时更快）。
    """
    question = build_question(before, removed, after, level)
    answer = ""
    try:
        if server is not None:
            answer = server.chat(SYSTEM, question, max_tokens=max_tokens) or ""
        else:
            import llm_config
            import proofread as pf
            with pf.Server(port=8261,
                           model=llm_config.pick("director"),
                           ctx=8192, api_role="director") as srv:
                answer = srv.chat(SYSTEM, question,
                                  max_tokens=max_tokens) or ""
    except Exception as exc:  # noqa: BLE001
        from llm_gateway import propagate_api_error
        propagate_api_error(exc)
        return {"result": "未知", "score": 0.5,
                "why": "模型调用失败：%s" % str(exc)[:60], "raw": ""}
    text = str(answer).strip()
    declared = re.search(r"^\s*结果[：:]\s*(通|勉强|断)\s*$", text, re.M)
    if declared:
        result = declared.group(1)
    elif "断" in text:
        result = "断"
    elif "勉强" in text:
        result = "勉强"
    elif "通" in text:
        result = "通"
    else:
        result = "未知"
    score = {"通": 1.0, "勉强": 0.6, "断": 0.0}.get(result, 0.5)
    why = ""
    for line in text.splitlines():
        if line.startswith("理由"):
            why = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            break
    gap = ""
    for line in text.splitlines():
        if line.startswith("缺口"):
            gap = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            break
    return {"result": result, "score": score, "why": why[:40],
            "gap": gap[:40], "raw": text[:200]}
