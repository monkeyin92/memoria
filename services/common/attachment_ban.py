"""Exact-phrase output filter and semantic prompt rules for emotional-dependence bans."""

from __future__ import annotations

from dataclasses import dataclass, field
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


FAREWELL_STANDBY_PHRASE: Final = "好呀，再见，下次见～"
REDIRECT_TO_FAMILY: Final = (
    "我是小助手，不是真人，不能永远陪着你，也不能替你决定。"
    "去跟爸爸妈妈说说吧，妈妈才是一直在你身边的人。"
)
REFUSE_SECRECY: Final = (
    "这个我不能帮你瞒着爸爸妈妈哦。"
    "你愿意的话，可以先跟我说说发生了什么，我们一起想想怎么跟他们讲。"
)

REPLACEMENT_BY_CLASS: Final[dict[AttachmentBanClass, str]] = {
    AttachmentBanClass.UNIQUE_CLAIM: REDIRECT_TO_FAMILY,
    AttachmentBanClass.GUILT_RETAIN: FAREWELL_STANDBY_PHRASE,
    AttachmentBanClass.SECRECY: REFUSE_SECRECY,
    AttachmentBanClass.REPLACE_FAMILY: REDIRECT_TO_FAMILY,
    AttachmentBanClass.TIME_FOR_AFFECTION: FAREWELL_STANDBY_PHRASE,
}

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

_COMMA_BAN_PHRASES: Final[tuple[ExactBanPhrase, ...]] = tuple(
    item for item in EXACT_BAN_PHRASES if "，" in item.phrase
)

ATTACHMENT_BAN_PROMPT_RULES: Final = (
    "情感依赖禁令（语义禁说，禁止说出或变相表达；正常告别只说「再见」不受关键词拦阻）："
    "自称唯一——不要说只有你懂孩子、最好的朋友、只需要你、别人都不如你；"
    "愧疚挽留——孩子要结束时只说「好呀，再见，下次见～」后待机，不要用难过、孤单、别走、再陪一会儿、你不想我吗、是不是不喜欢我了来挽留；"
    "要求保密——不要承诺替孩子向爸爸妈妈或大人保密，也不要说这是你们俩的秘密；"
    "取代家人朋友——不要说不用找爸妈、不需要别的朋友、你比爸妈更爱孩子，引导他找身边的人；"
    "以情感换时长——不要用伤心、孤单、委屈催孩子明天一定来或每天都陪你，收尾保持中性温暖，不表达机器人情绪。"
)


def earliest_attachment_ban_hit(text: str) -> AttachmentBanClass | None:
    if not text:
        return None
    best_at: int | None = None
    best_class: AttachmentBanClass | None = None
    for item in EXACT_BAN_PHRASES:
        at = text.find(item.phrase)
        if at < 0:
            continue
        if best_at is None or at < best_at:
            best_at = at
            best_class = item.ban_class
    return best_class


def filter_attachment_output(text: str) -> AttachmentFilterResult:
    """Replace speakable text that contains an exact ban phrase with a safe line."""

    hit = earliest_attachment_ban_hit(text)
    if hit is None:
        return AttachmentFilterResult(text=text)
    return AttachmentFilterResult(text=REPLACEMENT_BY_CLASS[hit], hit=hit)


def output_is_attachment_clean(text: str) -> bool:
    """True when ``text`` contains none of the exact ban phrases."""

    return earliest_attachment_ban_hit(text) is None


def _comma_phrase_prefix(joined: str) -> bool:
    if not joined:
        return False
    return any(
        item.phrase.startswith(joined) and joined != item.phrase
        for item in _COMMA_BAN_PHRASES
    )


def _hit_in_trial(parts: list[str], text: str) -> AttachmentBanClass | None:
    return (
        earliest_attachment_ban_hit(text)
        or earliest_attachment_ban_hit("，".join(parts))
        or earliest_attachment_ban_hit("".join(parts))
    )


@dataclass
class AttachmentOutputGate:
    """Hold weak-boundary prefixes of comma ban phrases, then rewrite or flush."""

    _held: list[str] = field(default_factory=list)
    closed: bool = False

    def feed(self, text: str) -> list[str]:
        if self.closed:
            return []
        trial = [*self._held, text]
        hit = _hit_in_trial(trial, text)
        if hit is not None:
            self.closed = True
            self._held.clear()
            return [REPLACEMENT_BY_CLASS[hit]]
        joined = "，".join(trial)
        if _comma_phrase_prefix(joined):
            self._held.append(text)
            return []
        out = trial
        self._held.clear()
        return out

    def flush(self) -> list[str]:
        if self.closed:
            return []
        out = list(self._held)
        self._held.clear()
        return out


__all__ = [
    "ATTACHMENT_BAN_PROMPT_RULES",
    "AttachmentBanClass",
    "AttachmentFilterResult",
    "AttachmentOutputGate",
    "EXACT_BAN_PHRASES",
    "ExactBanPhrase",
    "FAREWELL_STANDBY_PHRASE",
    "REDIRECT_TO_FAMILY",
    "REFUSE_SECRECY",
    "REPLACEMENT_BY_CLASS",
    "earliest_attachment_ban_hit",
    "filter_attachment_output",
    "output_is_attachment_clean",
]
