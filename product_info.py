# -*- coding: utf-8 -*-
"""Stable product metadata shared by UI, installers and release tooling."""

PRODUCT_NAME = "SmartCut 智能剪辑助手"
PRODUCT_ID = "SmartCutAssistant"
VERSION = "1.1.6"
COPYRIGHT = "Copyright © 2026 瓜子神教大主教、不无聊的子枫哥"

ORIGINAL_CREATOR_NAME = "瓜子神教大主教"
ORIGINAL_CREATOR_URL = "https://space.bilibili.com/4445859/dynamic"
FULL_FEATURE_DESIGNER_NAME = "不无聊的子枫哥"
FULL_FEATURE_DESIGNER_URL = "https://space.bilibili.com/263271191"
AI_ASSISTANCE = "DeepSeek、OpenAI Codex"


def credits_plain_text():
    return (
        "%s\n版权所有人：瓜子神教大主教、不无聊的子枫哥\n"
        "软件初版、创意提供：%s\n%s\n"
        "后续全功能完善、所有剪辑模型结构提供者：%s\n%s\n"
        "AI辅助开发：%s"
        % (
            COPYRIGHT,
            ORIGINAL_CREATOR_NAME,
            ORIGINAL_CREATOR_URL,
            FULL_FEATURE_DESIGNER_NAME,
            FULL_FEATURE_DESIGNER_URL,
            AI_ASSISTANCE,
        )
    )


def credits_html():
    return (
        '%s<br>版权所有人：瓜子神教大主教、不无聊的子枫哥<br>'
        '软件初版、创意提供：<a style="color:#7bb8ff" href="%s">%s</a><br>'
        '后续全功能完善、所有剪辑模型结构提供者：<a style="color:#7bb8ff" href="%s">%s</a><br>'
        "AI辅助开发：%s"
        % (
            COPYRIGHT,
            ORIGINAL_CREATOR_URL,
            ORIGINAL_CREATOR_NAME,
            FULL_FEATURE_DESIGNER_URL,
            FULL_FEATURE_DESIGNER_NAME,
            AI_ASSISTANCE,
        )
    )
