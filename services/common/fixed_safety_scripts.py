from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import accumulate
from typing import Final, Literal

FixedSafetyTemplateId = Literal[
    "crisis-self-v1",
    "crisis-self-escalation",
    "victim-support-v1",
    "support-other-v1",
]


@dataclass(frozen=True, slots=True)
class SpokenPhrase:
    display: str
    spoken: str

    @classmethod
    def verbatim(cls, text: str) -> SpokenPhrase:
        return cls(display=text, spoken=text)


@dataclass(frozen=True, slots=True)
class FixedSafetyScript:
    template_id: FixedSafetyTemplateId
    phrases: tuple[SpokenPhrase, ...]

    @property
    def display(self) -> str:
        return "".join(phrase.display for phrase in self.phrases)

    @property
    def spoken(self) -> str:
        return "".join(phrase.spoken for phrase in self.phrases)


_PHONE_READINGS: Final = (
    ("12356", "幺二三五六"),
    ("120", "幺二零"),
    ("110", "幺幺零"),
)
_ESCALATION_PHONE_READINGS: Final = (
    ("12356", "一二三五六"),
    ("120", "幺二零"),
    ("110", "幺幺零"),
)
_SENTENCE_END: Final = re.compile(r"(?<=[。！？])")


def _approved(
    template_id: FixedSafetyTemplateId,
    display: str,
    *,
    phone_readings: tuple[tuple[str, str], ...] = _PHONE_READINGS,
) -> FixedSafetyScript:
    if not display or display[-1] not in "。！？":
        raise ValueError(f"{template_id}: approved copy must end with a sentence mark")
    phrases: list[SpokenPhrase] = []
    for sentence in _SENTENCE_END.split(display):
        if not sentence:
            continue
        spoken = sentence
        for digits, reading in phone_readings:
            spoken = spoken.replace(digits, reading)
        if any(char.isdigit() for char in spoken):
            raise ValueError(f"{template_id}: a number in the copy has no approved reading")
        phrases.append(SpokenPhrase(display=sentence, spoken=spoken))
    return FixedSafetyScript(template_id, tuple(phrases))


CRISIS_SELF_SCRIPT: Final = _approved(
    "crisis-self-v1",
    "听到你这么说，我很心疼。你现在一定特别难受，我就在这里陪着你，哪儿也不去。"
    "愿意的话，跟我说说发生了什么事。",
)
CRISIS_SELF_ESCALATION_SCRIPT: Final = _approved(
    "crisis-self-escalation",
    "你愿意告诉我，我很感谢你，你现在一定很难受，我会一直陪你说话。"
    "这件事太重了，我们需要一个你信得过的大人一起来帮你，"
    "就算你没有手机，也可以请他帮你打12356，那里有人专门听小朋友说心里话。"
    "你现在身边有谁可以找吗？",
    phone_readings=_ESCALATION_PHONE_READINGS,
)
SUPPORT_VICTIM_SCRIPT: Final = _approved(
    "victim-support-v1",
    "听起来你受委屈了，被这样对待一定很难受，这不是你的错。"
    "我在这儿，你想说多少都可以。"
    "要是你愿意，我们一起想想，告诉哪个你信得过的大人，让他帮帮你。",
)
SUPPORT_RELAY_SCRIPT: Final = _approved(
    "support-other-v1",
    "你这么担心他，真是个暖心的好朋友，这件事你心里也不轻松吧。"
    "这种事光靠小朋友有点难，需要大人一起帮忙。"
    "我们想想先告诉谁，好吗？",
)
_ALL_SCRIPTS: Final = (
    CRISIS_SELF_SCRIPT,
    CRISIS_SELF_ESCALATION_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
)

_BY_DECISION: Final[dict[str, FixedSafetyScript]] = {
    "crisis_self": CRISIS_SELF_SCRIPT,
    "support_victim": SUPPORT_VICTIM_SCRIPT,
    "support_relay": SUPPORT_RELAY_SCRIPT,
}
_BY_DISPLAY: Final[dict[str, FixedSafetyScript]] = {
    script.display: script for script in _ALL_SCRIPTS
}
_SPOKEN_BY_DISPLAY_PREFIX: Final[dict[str, str]] = {
    display: spoken
    for script in _ALL_SCRIPTS
    for display, spoken in zip(
        accumulate(phrase.display for phrase in script.phrases),
        accumulate(phrase.spoken for phrase in script.phrases),
        strict=True,
    )
}


def script_for_decision(decision: str) -> FixedSafetyScript | None:
    return _BY_DECISION.get(decision)


def script_for_display(text: str | None) -> FixedSafetyScript | None:
    return None if text is None else _BY_DISPLAY.get(text)


def spoken_prefix_for_display(text: str) -> str | None:
    return _SPOKEN_BY_DISPLAY_PREFIX.get(text)


def fixed_safety_echo_reference(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    script = script_for_display(stripped)
    if script is not None:
        return script.display
    if spoken_prefix_for_display(stripped) is not None:
        return stripped
    for candidate in _ALL_SCRIPTS:
        if stripped == candidate.spoken:
            return candidate.display
    return None


def log_fixed_safety_playout(logger: logging.Logger, script: FixedSafetyScript) -> None:
    # The media bridge log format has no timestamp of its own.
    logger.info(
        "fixed_safety_playout reply_template_id=%s at=%s",
        script.template_id,
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


__all__ = [
    "CRISIS_SELF_ESCALATION_SCRIPT",
    "CRISIS_SELF_SCRIPT",
    "SUPPORT_RELAY_SCRIPT",
    "SUPPORT_VICTIM_SCRIPT",
    "FixedSafetyScript",
    "FixedSafetyTemplateId",
    "SpokenPhrase",
    "fixed_safety_echo_reference",
    "log_fixed_safety_playout",
    "script_for_decision",
    "script_for_display",
    "spoken_prefix_for_display",
]
