"""Deterministic follow-up claims so model variance cannot lose the owner's words.

Two rules run after the delegate extractor, both measured against the DEMO-02
fixed set (HANDOFF 2026-09-21):

* mood follow-up — an explicit feeling plus a care-worthy reason always becomes
  a claim.  The shipped prompt left that decision to the model and qwen-flash
  dropped "我今天被老师批评了，好难过。" in 3 of 6 invocations.
* plain self-statement net — a short first-person declarative ("我今天去公园散
  步了。", "我在纺织厂工作了30年。") always becomes a claim with the owner's own
  sentence as the value.  The prompt contract already says plain daily facts are
  extracted ("日常事实照常提取"), but qwen-flash splits them into atomic values
  ("公园" / "散步" as separate claims, or drops "纺织厂" entirely), which broke
  the park storyboard in every measured run and the factory story in 1 of 3.

Both rules fail closed: unknown feelings and non-statements produce nothing,
trivial one-off moods stay unclaimed, and every appended claim is a candidate
until confirmed like any other extraction.  A rule is skipped only when a
delegate claim already carries the whole statement (normalized superset), so
the owner's full sentence — the surface the recall path matches against — is
always on file even when the model returned a bare value ("难过") or split the
utterance into atomic values.
"""

from __future__ import annotations

import re
from datetime import datetime

from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    ExtractedClaim,
    MemoryExtraction,
    MemoryExtractor,
)
from services.archive.memory_write_policy import explicit_remember_content

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

#: Feeling words that route a sentence to the mood rule *or* keep it away from
#: the plain self-statement net. Broader than _FEELINGS: a bare "有点烦"/"有点
#: 无聊" is not a mood-rule trigger (no care-worthy reason) but must also never
#: become a daily-fact claim.
_FEELING_GUARD_WORDS: tuple[str, ...] = (
    *_FEELINGS,
    "烦",
    "无聊",
    "难受",
    "累",
    "舒坦",
    "痛快",
)

#: A plain self-statement: optional time word, then a first-person declarative
#: without a question mark. Length is capped so only short own-words statements
#: are netted, not narrations or dictation.
_PLAIN_SELF_STATEMENT = re.compile(r"(?:今天|昨天|前天|前几天|上午|下午|晚上|中午)?我.{1,58}")
_MAX_NET_STATEMENT_CHARS = 60


def _normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


#: The net skips only when a delegate claim already carries the statement itself
#: (normalized superset or equal). A shorter value must never suppress the net:
#: qwen-flash answered "好久没来看我了" (no 儿子) for the son utterance, and the
#: sentence is what matches the recall surface.
def _statement_covered_by_claims(claims: tuple[ExtractedClaim, ...], statement: str) -> bool:
    statement_norm = _normalized(statement)
    if not statement_norm:
        return True
    return any(
        statement_norm in _normalized(claim.value) for claim in claims
    )


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


def daily_statement_claim(text: str, *, occurred_at: datetime) -> ExtractedClaim | None:
    """Return the deterministic claim for a short plain first-person statement.

    Feeling-bearing sentences belong to the mood rule and questions, long
    narrations and non-statements produce nothing.
    """

    stripped = str(text or "").strip()
    if not stripped or len(stripped) > _MAX_NET_STATEMENT_CHARS:
        return None
    if "？" in stripped or "?" in stripped:
        return None
    if any(word in stripped for word in _FEELING_GUARD_WORDS):
        return None
    if _PLAIN_SELF_STATEMENT.fullmatch(stripped) is None:
        return None
    return ExtractedClaim(
        domain_category="daily_life",
        subject_key="self",
        predicate="fact",
        value=stripped,
        confidence=_CLAIM_CONFIDENCE,
        valid_from=occurred_at,
    )


class MoodFollowupEnsuringExtractor:
    """Wrap the production extractor so model variance cannot drop the owner's words."""

    def __init__(self, delegate: MemoryExtractor) -> None:
        self._delegate = delegate
        self.version = f"{delegate.version}|mood-followup"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        extraction = await self._delegate.extract(event)
        text = str(event.payload.get("text") or "").strip()
        # The delegate extractors resolve a sentence-initial remember command
        # before extracting; the nets must judge the same resolved content.
        resolved = explicit_remember_content(text) or text
        claim = mood_followup_claim(resolved, occurred_at=event.occurred_at) or (
            daily_statement_claim(resolved, occurred_at=event.occurred_at)
        )
        if claim is None or _statement_covered_by_claims(extraction.claims, resolved):
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
