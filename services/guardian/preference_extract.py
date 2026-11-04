"""Clause-scoped preference extract for session-end child archive facts.

Binds subject, polarity, and temporal relation per clause, then reduces within
one sentence so mom-topic bleed, negated topics, and past→now withdraws do not
become positive archive labels. Auto-extract births use the A01 decision
contract (never confirmed, never confidence >= 1.0).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from services.common.memory_decision_contract import (
    MemoryDecision,
    MemoryLifecycle,
    Polarity,
    decide_auto_extract,
)
from services.guardian.child_memory import ArchiveFact, forbidden_topic, quotes_speech

RULE_VERSION: Final = "preference-extract-v2"
DEFAULT_AUTO_CONFIDENCE: Final = 0.75

SubjectRole = Literal["child", "other", "robot", "unknown"]
TemporalAnchor = Literal["past", "present", "unspecified"]
EpistemicMode = Literal["direct", "reported", "hypothetical", "correction"]

# Keep in sync with session_recap.INTEREST_TOPICS (single behavioral list).
TOPIC_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("霸王龙", "喜欢霸王龙"),
    ("恐龙", "喜欢恐龙"),
    ("太空", "喜欢太空"),
    ("火箭", "喜欢火箭"),
    ("画画", "喜欢画画"),
    ("积木", "喜欢积木"),
    ("赛车", "喜欢赛车"),
    ("火车", "喜欢火车"),
    ("小狗", "喜欢小狗"),
    ("小猫", "喜欢小猫"),
    ("公园", "喜欢去公园"),
)
_TOPICS_LONGEST: Final[tuple[tuple[str, str], ...]] = tuple(
    sorted(TOPIC_ROWS, key=lambda item: len(item[0]), reverse=True)
)

_SELF_PREFIX = re.compile(r"^(?:嗯|那个|就是|其实|还有)+")
_OTHER_PERSON = re.compile(
    r"妈妈|爸爸|爷爷|奶奶|外公|外婆|姥姥|姥爷|姐姐|哥哥|弟弟|妹妹|老师|同学"
    r"|朋友|阿姨|叔叔|舅舅|姑姑"
)
_PREFERENCE_FRAME = re.compile(
    r"(?:我|本人)?(?:最|特别|超|很|非常|挺|比较|好|更|也|还|一直|就|可|太)?"
    r"(?:喜欢|中意|钟意)"
)
_CHILD_FRAME = re.compile(
    r"(?:我|本人)(?:最|特别|超|很|非常|挺|比较|好|更|也|还|一直|就|可|太)?"
    r"(?:喜欢|中意|钟意)"
)
_ELIDED_FRAME = re.compile(
    r"^(?:也|还|又)?(?:最|特别|超|很|非常|挺|比较|好|更|太)?"
    r"(?:喜欢|中意|钟意)"
)
_NEGATION_BEFORE = re.compile(r"(?:不|没|别|不太)[再太怎么会]*$")
_NEG_DISLIKE = re.compile(r"(?:不|没|别|不太)(?:再|太|怎么|会)?(?:喜欢|中意|钟意)")
_NEG_BARE = re.compile(r"(?:不喜欢|不中意|不钟意|讨厌|害怕|怕)(?:了|啦|啊|呢)?$")
_PAST = re.compile(r"以前|之前|曾经|原来|早前|从前")
_PRESENT = re.compile(r"现在|如今|目前")
_REPORTED = re.compile(r"听说|据说|好像|有人说|.+说")
_HYPOTHETICAL = re.compile(r"如果|要是|假如|也许|可能")
_CORRECTION = re.compile(r"说错了|不对|其实不是|更正")
_CLAUSE_SPLIT = re.compile(r"[，,]|但|可是|不过|然而")
_SENTENCE_SPLIT = re.compile(r"[。.!！?？;；\n]")


@dataclass(frozen=True, slots=True)
class _ClauseSpan:
    turn_index: int
    sentence_index: int
    clause_index: int
    text: str


@dataclass(frozen=True, slots=True)
class _BoundClaim:
    subject_key: str
    label: str
    topic_needle: str
    polarity: Polarity
    temporal: TemporalAnchor
    epistemic: EpistemicMode
    is_child: bool
    turn_index: int
    sentence_index: int
    clause_index: int


@dataclass(frozen=True, slots=True)
class _ReducedClaim:
    subject_key: str
    label: str
    polarity: Polarity
    is_current: bool
    epistemic: EpistemicMode


def _normalize(text: str) -> str:
    return _SELF_PREFIX.sub("", " ".join(text.split())).strip()


def _iter_clauses(turns: Sequence[object]) -> list[_ClauseSpan]:
    spans: list[_ClauseSpan] = []
    for turn_index, turn in enumerate(turns):
        role = getattr(turn, "role", None)
        text = getattr(turn, "text", "")
        if role != "user" or not str(text).strip():
            continue
        for sentence_index, sentence in enumerate(_SENTENCE_SPLIT.split(str(text))):
            body = _normalize(sentence)
            if not body:
                continue
            # Also cut past→present fused clauses without comma.
            fused = re.sub(r"(以前|从前|之前|曾经|原来)([^现]{0,24}?)(现在|如今)", r"\1\2，\3", body)
            parts = [part.strip() for part in _CLAUSE_SPLIT.split(fused) if part.strip()]
            for clause_index, part in enumerate(parts):
                spans.append(
                    _ClauseSpan(
                        turn_index=turn_index,
                        sentence_index=sentence_index,
                        clause_index=clause_index,
                        text=part,
                    )
                )
    return spans


def _topics_in(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    remaining = text
    for needle, label in _TOPICS_LONGEST:
        if needle in remaining:
            found.append((needle, label))
            remaining = remaining.replace(needle, " " * len(needle), 1)
    return found


def _annotate_clause(
    text: str,
    *,
    prior_child_in_sentence: bool,
) -> tuple[SubjectRole, bool, Polarity, TemporalAnchor, EpistemicMode]:
    epistemic: EpistemicMode = "direct"
    if _CORRECTION.search(text):
        epistemic = "correction"
    elif _HYPOTHETICAL.search(text):
        epistemic = "hypothetical"
    elif _REPORTED.search(text):
        epistemic = "reported"

    temporal: TemporalAnchor = "unspecified"
    if _PAST.search(text) and _PRESENT.search(text):
        temporal = "present"
    elif _PRESENT.search(text):
        temporal = "present"
    elif _PAST.search(text):
        temporal = "past"

    child_match = _CHILD_FRAME.search(text)
    elided_match = _ELIDED_FRAME.search(text)
    any_frame = _PREFERENCE_FRAME.search(text)
    neg_dislike = _NEG_DISLIKE.search(text) is not None
    bare_neg = _NEG_BARE.search(text) is not None

    subject: SubjectRole = "unknown"
    has_frame = False
    polarity: Polarity = "unknown"

    if "你" in text and (any_frame is not None or "吗" in text):
        # 「你喜欢恐龙吗」 is about the robot, not the child.
        if child_match is None:
            return "robot", False, "unknown", temporal, epistemic

    if child_match is not None:
        prefix = text[: child_match.start()]
        if _OTHER_PERSON.search(prefix) is not None:
            subject = "other"
            has_frame = False
        else:
            subject = "child"
            has_frame = True
            if _NEGATION_BEFORE.search(prefix) or neg_dislike:
                polarity = "negative"
            else:
                polarity = "positive"
    elif elided_match is not None and prior_child_in_sentence:
        # 「也喜欢小猫」 continues the child's subject from an earlier clause.
        subject = "child"
        has_frame = True
        prefix = text[: elided_match.start()]
        if _NEGATION_BEFORE.search(prefix) or neg_dislike:
            polarity = "negative"
        else:
            polarity = "positive"
    elif any_frame is not None and _OTHER_PERSON.search(text) is not None:
        subject = "other"
        has_frame = False
    elif bare_neg or neg_dislike:
        # 「现在不喜欢了」 may omit the topic; still a negative preference frame.
        subject = "child" if prior_child_in_sentence or "我" in text else "unknown"
        has_frame = subject == "child"
        polarity = "negative"

    if epistemic in {"reported", "hypothetical"} and subject == "child":
        # Do not auto-promote reported/hypothetical as durable positives.
        has_frame = has_frame and polarity == "negative"

    return subject, has_frame, polarity, temporal, epistemic


def _bind_clause(
    *,
    subject_key: str,
    span: _ClauseSpan,
    prior_child_in_sentence: bool,
    prior_topics: list[str],
) -> tuple[list[_BoundClaim], bool]:
    subject, has_frame, polarity, temporal, epistemic = _annotate_clause(
        span.text, prior_child_in_sentence=prior_child_in_sentence
    )
    child_here = subject == "child" and has_frame
    if not child_here:
        return [], prior_child_in_sentence

    topics = _topics_in(span.text)
    claims: list[_BoundClaim] = []
    if not topics and polarity == "negative" and prior_topics:
        # 「现在不喜欢了」 withdraws the last positive topic in the sentence.
        for needle in prior_topics:
            label = next(lbl for n, lbl in TOPIC_ROWS if n == needle)
            claims.append(
                _BoundClaim(
                    subject_key=subject_key,
                    label=label,
                    topic_needle=needle,
                    polarity="negative",
                    temporal=temporal if temporal != "unspecified" else "present",
                    epistemic=epistemic,
                    is_child=True,
                    turn_index=span.turn_index,
                    sentence_index=span.sentence_index,
                    clause_index=span.clause_index,
                )
            )
        return claims, True

    for needle, label in topics:
        if forbidden_topic(label) or quotes_speech(label):
            continue
        claims.append(
            _BoundClaim(
                subject_key=subject_key,
                label=label,
                topic_needle=needle,
                polarity=polarity if polarity != "unknown" else "positive",
                temporal=temporal,
                epistemic=epistemic,
                is_child=True,
                turn_index=span.turn_index,
                sentence_index=span.sentence_index,
                clause_index=span.clause_index,
            )
        )
    return claims, True


def _reduce_sentence(claims: Sequence[_BoundClaim]) -> list[_ReducedClaim]:
    by_topic: dict[str, list[_BoundClaim]] = {}
    for claim in claims:
        by_topic.setdefault(claim.topic_needle, []).append(claim)

    reduced: list[_ReducedClaim] = []
    for needle, group in by_topic.items():
        ordered = sorted(group, key=lambda c: (c.turn_index, c.sentence_index, c.clause_index))
        is_current = False
        polarity: Polarity = "unknown"
        epistemic: EpistemicMode = "direct"
        label = ordered[0].label
        subject_key = ordered[0].subject_key
        for claim in ordered:
            epistemic = claim.epistemic
            label = claim.label
            subject_key = claim.subject_key
            if claim.polarity == "negative":
                is_current = False
                polarity = "substituted" if polarity == "positive" else "negative"
                continue
            if claim.polarity == "positive" and claim.temporal == "past":
                is_current = False
                polarity = "positive"
                continue
            if claim.polarity == "positive":
                is_current = True
                polarity = "positive"
        reduced.append(
            _ReducedClaim(
                subject_key=subject_key,
                label=label,
                polarity=polarity,
                is_current=is_current and polarity == "positive",
                epistemic=epistemic,
            )
        )
    return reduced


def _lifecycle_for(claim: _ReducedClaim) -> MemoryLifecycle | None:
    if claim.epistemic in {"reported", "hypothetical"}:
        return None
    if not claim.is_current or claim.polarity != "positive":
        return None
    return "pending_confirm"


def extract_preference_decisions(
    *,
    subject_key: str,
    turns: Sequence[object],
    source_session_id: str | None = None,
    rule_version: str = RULE_VERSION,
    record_id: str | None = None,
) -> tuple[MemoryDecision, ...]:
    """Return auto-extract decisions for child preference clauses."""

    spans = _iter_clauses(turns)
    sentence_claims: dict[tuple[int, int], list[_BoundClaim]] = {}
    prior_child = False
    prior_topics: list[str] = []
    last_key: tuple[int, int] | None = None

    for span in spans:
        key = (span.turn_index, span.sentence_index)
        if last_key != key:
            prior_child = False
            prior_topics = []
            last_key = key
        claims, prior_child = _bind_clause(
            subject_key=subject_key,
            span=span,
            prior_child_in_sentence=prior_child,
            prior_topics=list(prior_topics),
        )
        if claims:
            sentence_claims.setdefault(key, []).extend(claims)
            for claim in claims:
                if claim.polarity == "positive" and claim.topic_needle not in prior_topics:
                    prior_topics.append(claim.topic_needle)

    decisions: list[MemoryDecision] = []
    seen: set[str] = set()
    for key in sorted(sentence_claims):
        for reduced in _reduce_sentence(sentence_claims[key]):
            lifecycle = _lifecycle_for(reduced)
            if lifecycle is None or reduced.label in seen:
                continue
            # pending_confirm requires record_id only when durable; at draft time
            # use eligible_unwritten, then ingest upgrades with record_id.
            birth_lifecycle: MemoryLifecycle = (
                "pending_confirm" if record_id else "eligible_unwritten"
            )
            decision = decide_auto_extract(
                subject_key=reduced.subject_key,
                predicate="preference",
                object_text=reduced.label,
                polarity="positive",
                rule_version=rule_version,
                lifecycle=birth_lifecycle,
                source_session_id=source_session_id,
                confidence=DEFAULT_AUTO_CONFIDENCE,
                record_id=record_id,
            )
            decisions.append(decision)
            seen.add(reduced.label)
    return tuple(decisions)


def archive_facts_from_decisions(
    decisions: Sequence[MemoryDecision],
) -> tuple[ArchiveFact, ...]:
    facts: list[ArchiveFact] = []
    for decision in decisions:
        if decision.fact.polarity != "positive":
            continue
        if decision.lifecycle not in {"pending_confirm", "eligible_unwritten"}:
            continue
        try:
            facts.append(
                ArchiveFact(
                    subject_id=decision.fact.subject_key,
                    field="preference",
                    text=decision.fact.object_text,
                )
            )
        except ValueError:
            continue
    return tuple(facts)


def extract_interest_facts(
    subject_id: str, turns: Sequence[object]
) -> tuple[ArchiveFact, ...]:
    """Whitelist positive-current self preferences for the session-recap sink."""

    return archive_facts_from_decisions(
        extract_preference_decisions(subject_key=subject_id, turns=turns)
    )


__all__ = [
    "DEFAULT_AUTO_CONFIDENCE",
    "RULE_VERSION",
    "TOPIC_ROWS",
    "archive_facts_from_decisions",
    "extract_interest_facts",
    "extract_preference_decisions",
]
