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
    "长故事",
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


def _small_count(value: str) -> int:
    digits = dict(zip("一二三四五六七八九", range(1, 10), strict=True)) | {"两": 2}
    if value.isdecimal():
        return int(value)
    if "十" in value:
        tens, units = value.split("十", 1)
        return digits.get(tens, 1) * 10 + digits.get(units, 0)
    return digits.get(value, 0)


def spoken_reply_budget_instruction(max_chars: int, max_sentences: int, *, query: str = "") -> str:
    scope = ""
    story = re.search(r"(?:给我讲|讲一个|讲个|编一个|编个|自创|想听|要听).{0,32}(?:故事|童话)", query)
    no_story = re.search(r"(?:不要|不用|别|不想)(?:给我)?(?:讲|编|听)[^。！？!?，,；;]{0,32}(?:故事|童话)", query)
    if story and not no_story and max_sentences <= 10:
        story_sentences = min(6, max(1, max_sentences - 1))
        scope += (
            f"【这轮故事必须讲完】这次先讲一个完整短故事，正文最多{story_sentences}句短话，对话也算句数。"
            "第一句交代主角和地点，只安排一个小困难，最后一句必须解决困难、回到安全处。"
            "不要长篇承诺，不在结尾增加新危险，不留下主角遇险的悬念。"
        )
    count = r"(?P<count>[一二三四五六七八九十两\d]{1,3})"
    scope_query = re.sub(r"\s+", "", query)
    per_group = re.search(
        r"每一?(?P<unit>个|颗|项|种|位|本|条|类)(?P<noun>(?:(?!每)[一-龥a-zA-Z]){0,8}?)"
        r"(?:都|只|要|各|分别|至少)?(?:讲|说|写|用|介绍|解释|回答|描述)?" + count + r"句", scope_query,
    )
    # Bind to the corresponding topic, not a recipient or an intervening group.
    groups = list(re.finditer(
        count + re.escape(per_group["unit"] + per_group["noun"]), scope_query[:per_group.start()],
    )) if per_group else []
    if groups and per_group:
        requested, each = _small_count(groups[-1]["count"]), _small_count(per_group["count"])
        if requested > 0 and requested * each > max_sentences:
            selected = min(requested, max(1, max_sentences - 1), max(1, (max_chars - 12) // 12))
            coverage = f"全部{selected}项" if selected == requested else f"前{selected}项"
            scope += (
                f"【必须先缩减承诺】用户要求{requested}项各{each}句，超出本轮范围。"
                f"开头明确这次先讲{coverage}、每项一句；随后逐项讲完{coverage}，每句不超过20字。"
                f"不要再承诺每项{each}句，不加长铺垫或结尾反问。"
            )
    return (
        f"【本轮口语范围】正文最多{max_chars}字、{max_sentences}句；在这个范围内讲完整。"
        "多部分问题先覆盖每个要点，再补细节，不要只讲前几项就结束。"
        "要求的篇幅或句数放不下时，开头简短说明这次缩减的范围，不承诺未讲出的内容。"
        "使用短句和熟悉的词，不堆术语和长铺垫；结尾自然收住，不朗读这些限制。"
        "知识解释优先用具体例子讲特点，不按专业分类名罗列；新名词要马上用日常话解释。"
        "要求原创时明确是新编的故事，不冒充某部作品的原版剧情。"
        + scope
    )


__all__ = [
    "SHORT_REPLY_AUDIENCES",
    "ResponseDepth",
    "ResponseDepthPolicy",
    "response_depth_for",
    "spoken_reply_budget_instruction",
]
