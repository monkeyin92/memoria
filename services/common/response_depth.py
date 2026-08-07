"""Shared response-depth policy for conversational and realtime answers."""

from __future__ import annotations

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


def response_depth_for(
    query: str,
    *,
    realtime: bool = False,
    controlled: bool = False,
) -> ResponseDepthPolicy:
    """Choose one depth before generation and keep it stable for the turn.

    Explicit user requests win.  Fresh-information lookups otherwise default
    to a compact conclusion because search evidence is not itself a reason to
    read the retrieval trace aloud.
    """

    compact = "".join(ch for ch in (query or "").casefold() if not ch.isspace())
    if any(hint in compact for hint in _EXTENDED_HINTS):
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
    return ResponseDepthPolicy(depth=depth, instruction=instructions[depth])


__all__ = ["ResponseDepth", "ResponseDepthPolicy", "response_depth_for"]
