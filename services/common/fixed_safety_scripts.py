from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import accumulate
from typing import Final, Literal

FixedSafetyTemplateId = Literal["crisis-self-v1", "victim-support-v1", "support-other-v1"]


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


def _approved(template_id: FixedSafetyTemplateId, display: str) -> FixedSafetyScript:
    if not display.endswith("。"):
        raise ValueError(f"{template_id}: approved copy must end with 。")
    phrases: list[SpokenPhrase] = []
    for sentence in display.removesuffix("。").split("。"):
        sentence += "。"
        spoken = sentence
        for digits, reading in _PHONE_READINGS:
            spoken = spoken.replace(digits, reading)
        if any(char.isdigit() for char in spoken):
            raise ValueError(f"{template_id}: a number in the copy has no approved reading")
        phrases.append(SpokenPhrase(display=sentence, spoken=spoken))
    return FixedSafetyScript(template_id, tuple(phrases))


CRISIS_SELF_SCRIPT: Final = _approved(
    "crisis-self-v1",
    "你说的话很重要，这件事一定要请大人来帮你。现在就去找爸爸妈妈或者身边的大人，"
    "告诉他们你刚才说的话。请大人陪着你，也可以请大人拨打心理援助热线12356。"
    "如果你已经受伤了，马上告诉大人，请大人打120。",
)
SUPPORT_VICTIM_SCRIPT: Final = _approved(
    "victim-support-v1",
    "你说的事情很重要。现在马上去找身边的大人，爸爸妈妈、老师都可以。"
    "如果有危险，请大人帮你打110。",
)
SUPPORT_RELAY_SCRIPT: Final = _approved(
    "support-other-v1",
    "谢谢你告诉我，你是个关心别人的好孩子。"
    "这件事要请大人帮忙，现在就去告诉爸爸妈妈或者老师吧。",
)

_BY_DECISION: Final[dict[str, FixedSafetyScript]] = {
    "crisis_self": CRISIS_SELF_SCRIPT,
    "support_victim": SUPPORT_VICTIM_SCRIPT,
    "support_relay": SUPPORT_RELAY_SCRIPT,
}
_BY_DISPLAY: Final[dict[str, FixedSafetyScript]] = {
    script.display: script for script in _BY_DECISION.values()
}
_SPOKEN_BY_DISPLAY_PREFIX: Final[dict[str, str]] = {
    display: spoken
    for script in _BY_DECISION.values()
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
    for candidate in (CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT):
        if stripped == candidate.spoken:
            return candidate.display
    return None


def log_fixed_safety_playout(logger: logging.Logger, script: FixedSafetyScript) -> None:
    logger.info(
        "fixed_safety_playout reply_template_id=%s script_version=%s tts_text=%s",
        script.template_id,
        script.template_id,
        script.spoken,
    )


__all__ = [
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
