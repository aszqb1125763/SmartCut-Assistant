# -*- coding: utf-8 -*-
"""Shared AI model provider presets and settings field conventions.

This module deliberately contains no network client.  The settings UI and
future model backends import the same provider ids and base URL defaults so a
saved configuration has one stable meaning.
"""
from __future__ import annotations


PROVIDERS = (
    ("local", "本地模型", ""),
    ("openai_compatible", "OpenAI 兼容 · 自定义", ""),
    ("openai", "OpenAI", "https://api.openai.com/v1"),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1"),
    ("dashscope", "通义千问（DashScope）",
     "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1"),
    ("moonshot", "Moonshot / Kimi", "https://api.moonshot.cn/v1"),
    ("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4"),
    ("ollama", "Ollama / vLLM", "http://127.0.0.1:11434/v1"),
    ("anthropic", "Anthropic Claude", "https://api.anthropic.com/v1"),
    ("gemini", "Google Gemini",
     "https://generativelanguage.googleapis.com/v1beta/openai"),
)

PROVIDER_IDS = frozenset(item[0] for item in PROVIDERS)
PROVIDER_URLS = {item[0]: item[2] for item in PROVIDERS}

DEFAULT_PROVIDER = "local"
DEFAULT_TIMEOUT = 120
MIN_TIMEOUT = 5
MAX_TIMEOUT = 600


def normalize_provider(value) -> str:
    provider = str(value or DEFAULT_PROVIDER).strip().lower()
    return provider if provider in PROVIDER_IDS else DEFAULT_PROVIDER


def default_base_url(provider) -> str:
    return PROVIDER_URLS.get(normalize_provider(provider), "")


def normalize_timeout(value) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, number))


def is_remote(provider) -> bool:
    return normalize_provider(provider) != "local"
