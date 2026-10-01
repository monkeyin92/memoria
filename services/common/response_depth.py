"""Shared response-depth policy for conversational and realtime answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ResponseDepth(StrEnum):
    """How much content the user asked the assistant to deliver."""

    BRIEF = "brief"
    STANDARD = "standard"
    EXTENDED = "extended"


@dataclass(frozen=True, slots=True)
class ResponseDepthPolicy:
    depth: ResponseDepth
    instruction: str


# These are explicit requests for more detail.  A generic word such as
# "规划" is intentionally absent: realtime planning should stay concise unless
# the user explicitly asks for a detailed plan.
_EXTENDED_HINTS = (
    "详细",
    "展开讲",
    "完整说明",
    "深入",
    "逐步",
    "一步一步",
    "长一点",
    "多讲一点",
    "解释原因",
    "为什么",
    "怎么做",
    "讲个故事",
    "写一篇",
    "写一段",
    "创作",
    "朗读",
    "对比分析",
    "继续",
)
# "讲个故事" was the only wording that counted as asking for a story, so a child's "给我讲一个故事吧" (2026-10-01
# soak) was an ordinary answer and the story was cut off after four sentences. A request for a story names the
# story, with a verb that asks for it; "我今天听了一个故事" is not one.
_STORY_REQUEST = re.compile(
    r"(讲|说|来|听|读|编|想听|要听|看)"
    r"(一|个|一个|则|一则|段|一段|些|一些|点|一点|讲)?"
    r"(很短的|短短的|长长的|好听的|好玩的|有趣的|新的|别的|另外的|另一个|其他的|睡前)?"
    r"(童话|故事)"
)
# Asking "why" or "how" is not a request for a lecture. For the audiences below it used to be: every
# child's "天空为什么是蓝色的？" got the EXTENDED instruction and a minute of "瑞利散射" with a numbered list
# (2026-10-01 soak), which the child and elder speaking rules ("一到两句短话") are written against.
_SOFT_EXTENDED_HINTS = ("解释原因", "为什么", "怎么做")
# Service modes whose listener hears a reply once, aloud, and cannot skim: children and elders.
SHORT_REPLY_AUDIENCES = frozenset({"student_minor", "senior_companion"})

_BRIEF_HINTS = (
    "简短",
    "简洁",
    "简单说",
    "概括一下",
    "一句话",
    "直接说",
    "只说结论",
    "别展开",
    "不用详细",
)


_SHORT_AUDIENCE_INSTRUCTIONS: dict[str, dict[ResponseDepth, str]] = {
    "student_minor": {
        ResponseDepth.BRIEF: (
            "【回复长度】像和孩子聊天一样，用一两句短话说完，一次只讲一个要点；他想听更多会再问。"
        ),
        ResponseDepth.STANDARD: (
            "【回复长度】像和孩子聊天一样，用两三句短话讲清楚，一次只讲一个要点，不要列条目、"
            "不要用序号和符号；他想听更多会再问。"
        ),
        ResponseDepth.EXTENDED: (
            "【回复长度】他明确想听完整的内容：用短句讲得有头有尾，最多约一分钟，不要列条目，"
            "也不要在中间提前收尾。"
        ),
    },
    "senior_companion": {
        ResponseDepth.BRIEF: "【回复长度】用一两句短话说完，一次只讲一个要点，说慢一点。",
        ResponseDepth.STANDARD: (
            "【回复长度】用两三句短话讲清楚，一次只讲一个要点，不要列条目、不要用序号和符号，"
            "别一口气说太多。"
        ),
        ResponseDepth.EXTENDED: (
            "【回复长度】他明确想听更多：用短句慢慢讲完整，分成小段，最多约一分钟，不要列条目。"
        ),
    },
}


def response_depth_for(
    query: str,
    *,
    realtime: bool = False,
    controlled: bool = False,
    audience: str | None = None,
) -> ResponseDepthPolicy:
    """Choose one depth before generation and keep it stable for the turn.

    Explicit user requests win.  Fresh-information lookups otherwise default
    to a compact conclusion because search evidence is not itself a reason to
    read the retrieval trace aloud.
    """

    compact = "".join(ch for ch in (query or "").casefold() if not ch.isspace())
    short_audience = audience in SHORT_REPLY_AUDIENCES
    extended_hints = (
        tuple(hint for hint in _EXTENDED_HINTS if hint not in _SOFT_EXTENDED_HINTS)
        if short_audience
        else _EXTENDED_HINTS
    )
    if any(hint in compact for hint in extended_hints) or _STORY_REQUEST.search(compact):
        depth = ResponseDepth.EXTENDED
    elif any(hint in compact for hint in _BRIEF_HINTS):
        depth = ResponseDepth.BRIEF
    elif realtime:
        depth = ResponseDepth.BRIEF
    else:
        depth = ResponseDepth.STANDARD

    if controlled and depth is ResponseDepth.STANDARD:
        depth = ResponseDepth.BRIEF

    instructions = {
        ResponseDepth.BRIEF: (
            "【回复长度】先给最重要的结论，控制在几句短话内；不要复述查询过程、"
            "检索过程或无关背景。"
        ),
        ResponseDepth.STANDARD: (
            "【回复长度】先给结论，再补充必要解释；保持自然适中，不要为了完整而"
            "罗列无关细节。"
        ),
        ResponseDepth.EXTENDED: (
            "【回复长度】用户明确要求展开，请给完整、连贯且有层次的说明；不要在"
            "关键步骤或结论尚未说完时提前收尾。"
        ),
    }
    if short_audience:
        instructions.update(_SHORT_AUDIENCE_INSTRUCTIONS[audience or ""])
    return ResponseDepthPolicy(depth=depth, instruction=instructions[depth])


__all__ = [
    "SHORT_REPLY_AUDIENCES",
    "ResponseDepth",
    "ResponseDepthPolicy",
    "response_depth_for",
]
