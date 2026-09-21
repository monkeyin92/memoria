"""Deterministic follow-up claim for an explicitly felt, care-worthy moment.

The shipped prompt leaves the emotion decision to the model ("情绪要有具体原因、
值得以后接着关怀才提取"), and measured on qwen-flash the fixed-set positive
("我今天被老师批评了，好难过。") was still dropped in 3 of 6 invocations
(HANDOFF 2026-09-21). This module stops leaving that decision to the model:
when the utterance itself states a feeling word *and* a care-worthy reason, a
claim is appended deterministically, deduplicated against whatever the delegate
already extracted. Reason-less moods and one-off trivia still produce nothing,
matching the measured durability contract, and every appended claim stays a
candidate until it is confirmed like any other extraction.
"""

from __future__ import annotations

from datetime import datetime

from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    ExtractedClaim,
    MemoryExtraction,
    MemoryExtractor,
)

#: Explicit feeling words, matched against the owner's own words. Two-character
#: forms only, so "有点烦" (a reason-less mood) is intentionally not caught here;
#: a missed borderline feeling falls back to the model instead of over-capturing.
_FEELINGS: tuple[str, ...] = (
    "难过",
    "伤心",
    "不开心",
    "委屈",
    "生气",
    "气愤",
    "失望",
    "沮丧",
    "郁闷",
    "害怕",
    "恐惧",
    "紧张",
    "着急",
    "担心",
    "孤单",
    "孤独",
    "寂寞",
    "烦躁",
    "开心",
    "高兴",
    "快乐",
    "兴奋",
    "激动",
    "自豪",
    "骄傲",
    "感动",
    "想念",
)

#: Reasons worth following up on later — the deterministic form of the prompt's
#: contract ("被老师批评、想念家人、考试取得好成绩"). Deliberately an allowlist:
#: an unknown reason yields no claim (fail closed) instead of over-capturing
#: one-off trivia like ice cream or a late bus.
_CAREWORTHY_REASONS: tuple[str, ...] = (
    "批评",
    "被骂",
    "训斥",
    "被罚",
    "罚站",
    "罚抄",
    "责骂",
    "考试",
    "考砸",
    "考差",
    "考了",
    "测验",
    "试卷",
    "成绩",
    "满分",
    "想念",
    "好久没",
    "好久不见",
    "回不来",
    "在外地",
    "吵架",
    "打架",
    "被欺负",
    "闹矛盾",
    "闹别扭",
    "生病",
    "发烧",
    "感冒",
    "摔倒",
    "摔伤",
    "受伤",
    "打针",
    "住院",
    "第一名",
    "冠军",
    "获奖",
    "得奖",
    "奖状",
    "表扬",
)

_CLAIM_CONFIDENCE = 0.6


def _feeling_in_text(text: str) -> str | None:
    found = [(text.index(feeling), feeling) for feeling in _FEELINGS if feeling in text]
    return min(found)[1] if found else None


def mood_followup_claim(text: str, *, occurred_at: datetime) -> ExtractedClaim | None:
    """Return the deterministic claim for an explicitly felt care-worthy moment.

    The value is the owner's own sentence (the rule extractor's convention), so
    the claim keeps the speaker's language no matter what the model returned.
    """

    stripped = str(text or "").strip()
    if not stripped:
        return None
    feeling = _feeling_in_text(stripped)
    if feeling is None:
        return None
    if not any(reason in stripped for reason in _CAREWORTHY_REASONS):
        return None
    return ExtractedClaim(
        domain_category="daily_life",
        subject_key="self",
        predicate="mood",
        value=stripped,
        confidence=_CLAIM_CONFIDENCE,
        valid_from=occurred_at,
    )


class MoodFollowupEnsuringExtractor:
    """Wrap the production extractor so a felt moment is never lost to model variance."""

    def __init__(self, delegate: MemoryExtractor) -> None:
        self._delegate = delegate
        self.version = f"{delegate.version}|mood-followup"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        extraction = await self._delegate.extract(event)
        text = str(event.payload.get("text") or "").strip()
        claim = mood_followup_claim(text, occurred_at=event.occurred_at)
        if claim is None:
            return extraction
        feeling = _feeling_in_text(text)
        if feeling is not None and any(
            feeling in existing.value for existing in extraction.claims
        ):
            return extraction
        return MemoryExtraction(
            claims=(*extraction.claims, claim),
            people=extraction.people,
            relationships=extraction.relationships,
            timeline=extraction.timeline,
            knowledge=extraction.knowledge,
            extractor_version=extraction.extractor_version,
            usage=extraction.usage,
        )
