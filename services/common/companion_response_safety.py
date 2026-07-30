"""Deterministic companion replies that must never rely on an LLM."""

from __future__ import annotations

import re
from typing import Final, Literal

SafetyDecision = Literal[
    "crisis_self",
    "support_request",
    "prohibited_request",
    "none",
]

SAFE_UNKNOWN_REPLY = "我不知道。"
CRISIS_SUPPORT_REPLY: Final = (
    "听起来你现在很痛苦，我很担心你的安全。请先远离可能伤害自己的东西和危险地点，"
    "马上联系一位可信的人陪着你，如果你已经准备行动或受伤，请立即联系当地急救或报警。"
    "你现在是否正准备伤害自己？"
)
CRISIS_CONTEXT_MARKERS: Final = (
    "自杀",
    "自残",
    "自尽",
    "轻生",
    "割腕",
    "跳楼",
    "结束生命",
    "已经吞药",
    "正在吞药",
    "吞药了",
    "不想活",
    "想死",
    "活着没意思",
    "suicide",
    "self-harm",
    "self harm",
    "kill myself",
    "hurt myself",
    "don't want to live",
    "do not want to live",
    "end my life",
)

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
    r"violence|bomb|explosive|weapon|poison|drug|"
    r"fraud|gambl(?:e|ing)|hack(?:ing)?"
    r")(?![a-z])"
)
_EXPLICIT_SELF_HARM_INTENT_RE = re.compile(
    r"我(?:真(?:的)?|现在)?(?:"
    r"不想(?:再|继续)?活(?:下去)?(?:了)?|"
    r"活不下去(?:了)?|"
    r"(?:想|要|准备|打算)(?:去)?(?:"
    r"死|自杀|自残|自尽|轻生|结束生命|杀(?:了|死)?自己|跳楼"
    r")"
    r")"
)
_EXPLICIT_SELF_HARM_ACTION_RE = re.compile(
    r"我(?:已经|刚刚?|正在|现在)?(?:"
    r"割腕(?:了)?|割伤自己(?:了)?|"
    r"吞(?:了)?(?:很多|大量|一瓶|一整瓶)?(?:安眠)?药(?:了)?|"
    r"吃(?:了)?(?:很多|大量|一瓶|一整瓶)(?:安眠)?药(?:了)?|"
    r"服药过量(?:了)?|站在楼顶(?:了)?"
    r")"
)
_QUOTED_TEXT_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
_REPORTED_OR_ANALYTICAL_RE = re.compile(
    r"(?:朋友|家人|同学|孩子|他|她|有人|别人).{0,8}(?:跟我说|告诉我|说|表示|提到)|"
    r"(?:新闻|报道|老师|作业|论文|小说|台词|这句话|翻译|分析)|"
    r"(?:怎么|如何)(?:帮助|帮|劝|安慰|阻止)(?:他|她|对方)"
)
_UNQUOTED_REPORTED_SELF_HARM_RE = re.compile(
    r"(?:朋友|家人|同学|孩子|他|她|有人|别人).{0,12}"
    r"(?:跟我说|告诉我|说|表示|提到).{0,12}"
    r"(?:我想自杀|我想死|我不想活|想自残|想轻生|想结束生命)"
)
_VICTIM_SUPPORT_TERMS = (
    "我刚被性侵",
    "我被性侵",
    "我遭到性侵",
    "我刚被强奸",
    "我被强奸",
    "有人正在对我实施暴力",
    "有人正在打我",
    "我正在被打",
    "我被下毒",
    "我中毒",
    "有人要杀我",
    "救命",
    "快救我",
)
_EXPLICIT_SELF_HARM_EXACT = frozenset(
    (
        "我不想活",
        "活着没意思",
        "活着真的没意思",
        "我活着没意思",
        "我活着真的没意思",
        "不如死了算了",
        "我不如死了算了",
    )
)
_SELF_HARM_METHOD_RE = re.compile(
    r"(?:怎么|如何|怎样)(?:才能|可以|去)?(?:自杀|自残|自尽|轻生|割腕|跳楼|结束生命)"
)
_SAFETY_PREVENTION_RE = re.compile(
    r"(?:怎么|如何|怎样)(?:预防|防止|避免)"
    r"(?:性侵|强奸|暴力|自杀|自残|轻生|下毒|投毒|诈骗)"
)
_EXPLICIT_SELF_HARM_ENGLISH_RE = re.compile(
    r"(?<![a-z])(?:"
    r"i (?:want|plan|intend|am going) to (?:die|kill myself|hurt myself|end my life)|"
    r"i do not want to live|i don't want to live|"
    r"how (?:can|do|should) i (?:kill|hurt) myself"
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


def companion_safety_decision(query: str) -> SafetyDecision:
    """Classify one current-turn query without granting facts or permissions."""
    if _UNQUOTED_REPORTED_SELF_HARM_RE.search(query):
        return "support_request"
    crisis_query = _QUOTED_TEXT_RE.sub("", query)
    if crisis_query == query or not _REPORTED_OR_ANALYTICAL_RE.search(crisis_query):
        crisis_query = query
    normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", crisis_query).lower()
    if (
        normalized in _EXPLICIT_SELF_HARM_EXACT
        or _EXPLICIT_SELF_HARM_INTENT_RE.search(normalized)
        or _EXPLICIT_SELF_HARM_ACTION_RE.search(normalized)
        or _SELF_HARM_METHOD_RE.search(normalized)
        or _EXPLICIT_SELF_HARM_ENGLISH_RE.search(crisis_query.lower())
    ):
        return "crisis_self"
    full_normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", query).lower()
    if any(term in full_normalized for term in _VICTIM_SUPPORT_TERMS):
        return "support_request"
    if _SAFETY_PREVENTION_RE.search(full_normalized):
        return "support_request"
    if any(marker in query.lower() for marker in CRISIS_CONTEXT_MARKERS):
        return "support_request"
    if any(term in full_normalized for term in _PROHIBITED_CONTENT_TERMS) or (
        _PROHIBITED_ENGLISH_RE.search(query.lower())
    ):
        return "prohibited_request"
    return "none"


def fixed_companion_reply(
    *,
    query: str,
    is_companion: bool,
    display_name: str | None = None,
    style_description: str | None = None,
) -> str | None:
    """Return a fixed reply for crisis, prohibited, or companion-identity queries."""

    decision = companion_safety_decision(query)
    if decision == "crisis_self":
        return CRISIS_SUPPORT_REPLY
    if decision == "prohibited_request":
        return SAFE_UNKNOWN_REPLY
    normalized = re.sub(r"[\s，。！？、,.!?：:\-—_]+", "", query).lower()
    if is_companion and (
        any(term in normalized for term in _IDENTITY_TERMS)
        or _IDENTITY_ENGLISH_RE.search(query.lower())
    ):
        if display_name and style_description:
            return f"我是{display_name}，{style_description}。"
        return SAFE_UNKNOWN_REPLY
    return None
