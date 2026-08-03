"""Deterministic lexical KWS fallback for short Chinese control commands."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEPARATOR_RE = re.compile(r"[\s，。！？、,.!?;；：:‘’“”\"'（）()\[\]【】]+")
DEFAULT_HARD_STOP_KEYWORDS = (
    "停",
    "停一下",
    "等一下",
    "等等",
    "先别说",
    "别说了",
    "打住",
    "我来说",
)


def normalize_keyword(text: str) -> str:
    return _SEPARATOR_RE.sub("", text.casefold()).strip()


@dataclass(frozen=True, slots=True)
class KeywordHit:
    keyword: str
    confidence: float
    start_sample: int
    end_sample: int
    hard_stop: bool = True


class ControlKeywordSpotter:
    """Match only an allowlisted command phrase, never arbitrary ASR text."""

    def __init__(
        self,
        keywords: tuple[str, ...] | list[str] = DEFAULT_HARD_STOP_KEYWORDS,
        *,
        min_confidence: float = 0.78,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        normalized = {normalize_keyword(item): item.strip() for item in keywords if item.strip()}
        if not normalized:
            raise ValueError("at least one keyword is required")
        self._keywords = tuple(
            sorted(normalized.items(), key=lambda item: len(item[0]), reverse=True)
        )
        self.min_confidence = min_confidence

    def process(
        self,
        text: str,
        *,
        start_sample: int,
        end_sample: int,
        confidence: float = 1.0,
    ) -> tuple[KeywordHit, ...]:
        if start_sample < 0 or end_sample <= start_sample:
            raise ValueError("keyword sample range is invalid")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        normalized = normalize_keyword(text)
        if confidence < self.min_confidence or not normalized:
            return ()
        for needle, original in self._keywords:
            if normalized == needle:
                return (
                    KeywordHit(
                        keyword=original,
                        confidence=confidence,
                        start_sample=start_sample,
                        end_sample=end_sample,
                    ),
                )
        return ()


KeywordSpotter = ControlKeywordSpotter

__all__ = [
    "ControlKeywordSpotter",
    "DEFAULT_HARD_STOP_KEYWORDS",
    "KeywordHit",
    "KeywordSpotter",
    "normalize_keyword",
]
