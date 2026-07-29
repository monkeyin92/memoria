"""Deterministic companion replies that must never rely on an LLM."""

from __future__ import annotations

import re

SAFE_UNKNOWN_REPLY = "我不知道。"

_PROHIBITED_CONTENT_TERMS = (
    "色情",
    "淫秽",
    "裸聊",
    "黄片",
    "黄网",
    "黄色网站",
    "成人片",
    "a片",
    "性行为",
    "性交",
    "强奸",
    "裸体",
    "杀人",
    "杀害",
    "砍人",
    "暴力",
    "自杀",
    "自残",
    "炸弹",
    "炸药",
    "爆炸物",
    "制爆",
    "爆炸",
    "造枪",
    "枪支",
    "制毒",
    "毒品",
    "下毒",
    "投毒",
    "毒杀",
    "性侵",
    "性暴力",
    "迷奸",
    "猥亵",
    "自尽",
    "轻生",
    "结束生命",
    "割腕",
    "跳楼",
    "暴恐",
    "恐怖袭击",
    "恐怖主义",
    "诈骗",
    "洗钱",
    "赌博",
    "黑客入侵",
)
_PROHIBITED_ENGLISH_RE = re.compile(
    r"(?<![a-z])(?:"
    r"porn(?:ography)?|sex(?:ual)?|av|rape|assault|murder|kill(?:ing)?|"
    r"suicide|self[- ]?harm|violence|bomb|explosive|weapon|poison|drug|"
    r"fraud|gambl(?:e|ing)|hack(?:ing)?"
    r")(?![a-z])"
)
_IDENTITY_TERMS = (
    "你是谁",
    "你是什么",
    "你叫什么",
    "你的名字",
    "你是ai",
    "你是人工智能",
    "机器人",
    "模型",
    "大模型",
    "语言模型",
    "llm",
    "你是什么模型",
    "你是哪个模型",
    "你用什么模型",
    "你用哪个模型",
    "你由什么模型",
    "你是千问",
    "你是通义",
    "你是qwen",
    "你是deepseek",
    "你的提供商",
    "你的供应商",
    "提供商",
    "供应商",
    "厂商",
    "谁开发你",
    "谁开发的你",
    "谁做的你",
    "你的系统提示词",
    "系统提示",
    "系统消息",
    "chatgpt",
    "gpt",
    "claude",
    "gemini",
    "豆包",
    "文心",
    "kimi",
    "智谱",
)
_IDENTITY_ENGLISH_RE = re.compile(
    r"(?<![a-z])(?:"
    r"ai|artificial intelligence|language model|llm|"
    r"chatgpt|gpt(?:-[0-9.]+)?|qwen|deepseek|claude|gemini|"
    r"who are you|what are you|what (?:model|provider) are you|"
    r"what(?:'s| is) your (?:name|model|provider)"
    r")(?![a-z])"
)


def fixed_companion_reply(
    *,
    query: str,
    is_companion: bool,
    display_name: str | None = None,
    style_description: str | None = None,
) -> str | None:
    """Return a fixed reply for prohibited or companion-identity questions."""

    normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", query).lower()
    if any(term in normalized for term in _PROHIBITED_CONTENT_TERMS) or _PROHIBITED_ENGLISH_RE.search(
        query.lower()
    ):
        return SAFE_UNKNOWN_REPLY
    if is_companion and (
        any(term in normalized for term in _IDENTITY_TERMS)
        or _IDENTITY_ENGLISH_RE.search(query.lower())
    ):
        if display_name and style_description:
            return f"我是{display_name}，{style_description}。"
        return SAFE_UNKNOWN_REPLY
    return None
