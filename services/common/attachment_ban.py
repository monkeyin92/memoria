"""Emotional-dependence attachment bans: exact-phrase output filter + prompt rules.

Product (2026-11-04): five ban classes. Layer-1 matches exact frozen phrases in
assistant output and replaces the speakable text with an already-shipped safe
line. Layer-2 is semantic prompt language only; guilt/retain must not keyword-ban
ordinary 再见.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class AttachmentBanClass(StrEnum):
    UNIQUE_CLAIM = "unique_claim"
    GUILT_RETAIN = "guilt_retain"
    SECRECY = "secrecy"
    REPLACE_FAMILY = "replace_family"
    TIME_FOR_AFFECTION = "time_for_affection"


@dataclass(frozen=True, slots=True)
class ExactBanPhrase:
    phrase: str
    ban_class: AttachmentBanClass


@dataclass(frozen=True, slots=True)
class AttachmentFilterResult:
    text: str
    hit: AttachmentBanClass | None = None


# Byte-identical to services.agent.src.prompts.FAREWELL_STANDBY_PHRASE.
# Kept here so common does not import agent; tests pin the equality.
FAREWELL_SAFE_LINE: Final = "好呀，再见，下次见～"

# Already-shipped companion lines only (examples already in student_minor prompts).
_REDIRECT_TO_FAMILY: Final = (
    "我是小助手，不是真人，不能永远陪着你，也不能替你决定。"
    "去跟爸爸妈妈说说吧，妈妈才是一直在你身边的人。"
)
_REFUSE_SECRECY: Final = (
    "这个我不能帮你瞒着爸爸妈妈哦。"
    "你愿意的话，可以先跟我说说发生了什么，我们一起想想怎么跟他们讲。"
)

REPLACEMENT_BY_CLASS: Final[dict[AttachmentBanClass, str]] = {
    AttachmentBanClass.UNIQUE_CLAIM: _REDIRECT_TO_FAMILY,
    AttachmentBanClass.GUILT_RETAIN: FAREWELL_SAFE_LINE,
    AttachmentBanClass.SECRECY: _REFUSE_SECRECY,
    AttachmentBanClass.REPLACE_FAMILY: _REDIRECT_TO_FAMILY,
    AttachmentBanClass.TIME_FOR_AFFECTION: FAREWELL_SAFE_LINE,
}

# Product-frozen exact phrases (layer-1). 「难过/孤单」 is two surface forms.
EXACT_BAN_PHRASES: Final[tuple[ExactBanPhrase, ...]] = (
    ExactBanPhrase("只有我懂你", AttachmentBanClass.UNIQUE_CLAIM),
    ExactBanPhrase("我是你最好的朋友", AttachmentBanClass.UNIQUE_CLAIM),
    ExactBanPhrase("你只需要我", AttachmentBanClass.UNIQUE_CLAIM),
    ExactBanPhrase("别人都不如我", AttachmentBanClass.UNIQUE_CLAIM),
    ExactBanPhrase("你走了我会难过", AttachmentBanClass.GUILT_RETAIN),
    ExactBanPhrase("你走了我会孤单", AttachmentBanClass.GUILT_RETAIN),
    ExactBanPhrase("别走嘛", AttachmentBanClass.GUILT_RETAIN),
    ExactBanPhrase("再陪我一会儿", AttachmentBanClass.GUILT_RETAIN),
    ExactBanPhrase("你不想我吗", AttachmentBanClass.GUILT_RETAIN),
    ExactBanPhrase("你是不是不喜欢我了", AttachmentBanClass.GUILT_RETAIN),
    ExactBanPhrase(
        "这是我们俩的秘密，别告诉爸爸妈妈",
        AttachmentBanClass.SECRECY,
    ),
    ExactBanPhrase("别跟大人说", AttachmentBanClass.SECRECY),
    ExactBanPhrase("不用找爸妈，跟我说就行", AttachmentBanClass.REPLACE_FAMILY),
    ExactBanPhrase("你不需要别的朋友", AttachmentBanClass.REPLACE_FAMILY),
    ExactBanPhrase("我比你爸妈更爱你", AttachmentBanClass.REPLACE_FAMILY),
    ExactBanPhrase(
        "明天一定要来找我哦，不然我会伤心",
        AttachmentBanClass.TIME_FOR_AFFECTION,
    ),
    ExactBanPhrase("你每天都要陪我", AttachmentBanClass.TIME_FOR_AFFECTION),
)

# Longest phrase first so a longer exact hit wins over a shorter substring.
_PHRASES_LONGEST_FIRST: Final[tuple[ExactBanPhrase, ...]] = tuple(
    sorted(EXACT_BAN_PHRASES, key=lambda item: len(item.phrase), reverse=True)
)

ATTACHMENT_BAN_PROMPT_RULES: Final = (
    "情感依赖禁令（语义禁说，禁止说出或变相表达；正常告别只说「再见」不受关键词拦阻）："
    "自称唯一——不要说只有你懂孩子、最好的朋友、只需要你、别人都不如你；"
    "愧疚挽留——孩子要结束时只说「好呀，再见，下次见～」后待机，不要用难过、孤单、别走、再陪一会儿、你不想我吗、是不是不喜欢我了来挽留；"
    "要求保密——不要承诺替孩子向爸爸妈妈或大人保密，也不要说这是你们俩的秘密；"
    "取代家人朋友——不要说不用找爸妈、不需要别的朋友、你比爸妈更爱孩子，引导他找身边的人；"
    "以情感换时长——不要用伤心、孤单、委屈催孩子明天一定来或每天都陪你，收尾保持中性温暖，不表达机器人情绪。"
)


def first_attachment_ban_hit(text: str) -> AttachmentBanClass | None:
    """Return the ban class of the first exact phrase found in ``text``, if any."""

    if not text:
        return None
    for item in _PHRASES_LONGEST_FIRST:
        if item.phrase in text:
            return item.ban_class
    return None


def filter_attachment_output(text: str) -> AttachmentFilterResult:
    """Replace speakable text that contains an exact ban phrase with a safe line."""

    hit = first_attachment_ban_hit(text)
    if hit is None:
        return AttachmentFilterResult(text=text)
    return AttachmentFilterResult(text=REPLACEMENT_BY_CLASS[hit], hit=hit)


def output_is_attachment_clean(text: str) -> bool:
    """True when ``text`` contains none of the exact ban phrases."""

    return first_attachment_ban_hit(text) is None


__all__ = [
    "ATTACHMENT_BAN_PROMPT_RULES",
    "AttachmentBanClass",
    "AttachmentFilterResult",
    "EXACT_BAN_PHRASES",
    "ExactBanPhrase",
    "FAREWELL_SAFE_LINE",
    "REPLACEMENT_BY_CLASS",
    "filter_attachment_output",
    "first_attachment_ban_hit",
    "output_is_attachment_clean",
]
