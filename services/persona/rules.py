"""Deterministic persona candidates shared by SQLite and PostgreSQL adapters."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from services.persona.domain import PersonaEvidence, PersonaTraitCategory

TICS = ("我觉得", "其实", "说实话", "怎么说呢", "坦白说", "总的来说")
AUTO_PROMOTE = frozenset(
    {"verbal_tic", "sentence_length", "speech_rate", "pause_style", "discourse_style"}
)
OWNER_AUTO_PROMOTE_WEIGHT = 2.7
UNCERTAIN_AUTO_PROMOTE_WEIGHT = 5.4
UNCERTAIN_AUTO_PROMOTE_SESSIONS = 3
EXCLUSIVE_STYLE_CATEGORIES = frozenset({"sentence_length", "speech_rate", "pause_style"})
EXCLUSIVE_BUCKET_DOMINANCE_RATIO = 2
_FIXED_STYLE_DESCRIPTIONS = frozenset(
    {
        *(f"表达观点时常用“{tic}”自然起句" for tic in TICS),
        "日常表达偏好短句，先给出核心意思",
        "日常表达偏好中等长度句子，信息与节奏较均衡",
        "日常表达偏好较完整的长句和上下文铺垫",
        "说话节奏偏从容，适合保留自然停顿",
        "说话节奏中等，吐字和信息密度较均衡",
        "说话节奏偏快，倾向连续表达完整想法",
        "表达时会留较多自然停顿",
        "表达时停顿密度适中",
        "表达较连贯，停顿相对少",
        "表达复杂问题时偏好按“先…再…”组织顺序",
    }
)
CATEGORY_ORDER = {
    "verbal_tic": 0,
    "sentence_length": 1,
    "speech_rate": 2,
    "pause_style": 3,
    "emphasis_style": 4,
    "emotional_expression": 5,
    "discourse_style": 6,
    "narrative_style": 7,
    "decision_habit": 8,
    "value_priority": 9,
}


@dataclass(frozen=True, slots=True)
class PersonaCandidate:
    category: PersonaTraitCategory
    normalized_key: str
    description: str
    context: str
    counterexample: str = ""


@dataclass(frozen=True, slots=True)
class ExclusiveBucketObservation:
    trait_id: str
    status: str
    review_event_id: str | None
    updated_at: str
    speaker_class: str | None
    session_id: str | None
    payload: Mapping[str, Any]
    weight: float


class PersonaExtractor(Protocol):
    version: str

    async def extract(
        self,
        text: str,
        evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]: ...


def semantic_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", text).strip("。！？!?，,")
    return hashlib.sha256(normalized.encode()).hexdigest()[:20]


def extract_candidates(text: str, evidence: PersonaEvidence) -> tuple[PersonaCandidate, ...]:
    candidates: list[PersonaCandidate] = []
    for tic in TICS:
        if tic in text:
            candidates.append(
                PersonaCandidate(
                    category="verbal_tic",
                    normalized_key=tic,
                    description=f"表达观点时常用“{tic}”自然起句",
                    context=evidence.scene,
                )
            )
    if "决定" in text and any(marker in text for marker in ("习惯", "通常", "会先", "先")):
        candidates.append(
            PersonaCandidate(
                category="decision_habit",
                normalized_key=semantic_key(text),
                description=text.rstrip("。！？!?"),
                context=evidence.scene,
            )
        )
    if any(marker in text for marker in ("最重要", "原则是", "我认为", "应该")):
        candidates.append(
            PersonaCandidate(
                category="value_priority",
                normalized_key=semantic_key(text),
                description=text.rstrip("。！？!?"),
                context=evidence.scene,
            )
        )
    if "先" in text and "再" in text:
        candidates.append(
            PersonaCandidate(
                category="discourse_style",
                normalized_key="sequence:first_then",
                description="表达复杂问题时偏好按“先…再…”组织顺序",
                context=evidence.scene,
            )
        )
    visible_chars = sum(1 for char in text if not char.isspace() and char not in "，。！？；,.!?;")
    length_bucket = (
        "concise" if visible_chars <= 28 else "detailed" if visible_chars >= 55 else "balanced"
    )
    length_description = {
        "concise": "日常表达偏好短句，先给出核心意思",
        "balanced": "日常表达偏好中等长度句子，信息与节奏较均衡",
        "detailed": "日常表达偏好较完整的长句和上下文铺垫",
    }[length_bucket]
    candidates.append(
        PersonaCandidate(
            category="sentence_length",
            normalized_key=length_bucket,
            description=length_description,
            context=evidence.scene,
        )
    )
    if evidence.speech_duration_ms:
        chars_per_second = visible_chars / (evidence.speech_duration_ms / 1000)
        rate_bucket = (
            "slow" if chars_per_second < 3 else "fast" if chars_per_second > 6 else "moderate"
        )
        rate_description = {
            "slow": "说话节奏偏从容，适合保留自然停顿",
            "moderate": "说话节奏中等，吐字和信息密度较均衡",
            "fast": "说话节奏偏快，倾向连续表达完整想法",
        }[rate_bucket]
        candidates.append(
            PersonaCandidate(
                category="speech_rate",
                normalized_key=rate_bucket,
                description=rate_description,
                context=evidence.scene,
            )
        )
    if evidence.pause_ratio is not None:
        pause_bucket = (
            "frequent"
            if evidence.pause_ratio > 0.4
            else "sparse"
            if evidence.pause_ratio < 0.15
            else "moderate"
        )
        candidates.append(
            PersonaCandidate(
                category="pause_style",
                normalized_key=pause_bucket,
                description={
                    "frequent": "表达时会留较多自然停顿",
                    "moderate": "表达时停顿密度适中",
                    "sparse": "表达较连贯，停顿相对少",
                }[pause_bucket],
                context=evidence.scene,
            )
        )
    return tuple(dict.fromkeys(candidates))


class RuleBasedPersonaExtractor:
    version = "persona-rules-zh-v1"

    async def extract(
        self,
        text: str,
        evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]:
        return extract_candidates(text, evidence)


def should_auto_promote(
    *,
    category: PersonaTraitCategory,
    status: str,
    owner_weight: float,
    uncertain_weight: float,
    uncertain_session_count: int,
    uncertain_profile_count: int,
) -> bool:
    if (
        status == "disabled"
        or category not in AUTO_PROMOTE
        or category in EXCLUSIVE_STYLE_CATEGORIES
    ):
        return False
    owner_ready = owner_weight >= OWNER_AUTO_PROMOTE_WEIGHT
    uncertain_ready = (
        owner_weight == 0
        and uncertain_weight + 1e-9 >= UNCERTAIN_AUTO_PROMOTE_WEIGHT
        and uncertain_session_count >= UNCERTAIN_AUTO_PROMOTE_SESSIONS
        and uncertain_profile_count == 1
    )
    return owner_ready or uncertain_ready


def _dominant_bucket(counts: Mapping[str, float], minimum: float) -> str | None:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] < minimum:
        return None
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if runner_up and ranked[0][1] < EXCLUSIVE_BUCKET_DOMINANCE_RATIO * runner_up:
        return None
    return ranked[0][0]


def exclusive_auto_promote_target(
    category: PersonaTraitCategory,
    observations: tuple[ExclusiveBucketObservation, ...],
    *,
    uncertain_profile_id: str | None = None,
) -> str | None:
    """Choose one exclusive style bucket from one trusted evidence lane."""

    if category not in EXCLUSIVE_STYLE_CATEGORIES:
        return None
    states: dict[str, tuple[str, str | None, str]] = {}
    owner_counts: dict[str, float] = defaultdict(float)
    uncertain_counts: dict[str, dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    uncertain_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    profiles: set[str] = set()
    for observation in observations:
        if observation.status == "disabled":
            continue
        states[observation.trait_id] = (
            observation.status,
            observation.review_event_id,
            observation.updated_at,
        )
        if observation.speaker_class == "owner":
            owner_counts[observation.trait_id] += observation.weight
            continue
        if observation.speaker_class != "uncertain":
            continue
        provenance = trusted_uncertain_profile(observation.payload)
        if provenance is None:
            continue
        profile_id, _quality = provenance
        profiles.add(profile_id)
        uncertain_counts[profile_id][observation.trait_id] += observation.weight
        if observation.session_id:
            uncertain_sessions[(profile_id, observation.trait_id)].add(observation.session_id)

    manually_confirmed = [
        (updated_at, trait_id)
        for trait_id, (status, review_event_id, updated_at) in states.items()
        if status == "confirmed" and review_event_id is not None
    ]
    if manually_confirmed:
        return max(manually_confirmed)[1]
    if owner_counts:
        return _dominant_bucket(owner_counts, OWNER_AUTO_PROMOTE_WEIGHT)
    if uncertain_profile_id is not None:
        profiles = {uncertain_profile_id} if uncertain_profile_id in profiles else set()
    if len(profiles) != 1:
        return None
    profile_id = next(iter(profiles))
    target = _dominant_bucket(
        uncertain_counts[profile_id],
        UNCERTAIN_AUTO_PROMOTE_WEIGHT,
    )
    if target is None:
        return None
    if len(uncertain_sessions[(profile_id, target)]) < UNCERTAIN_AUTO_PROMOTE_SESSIONS:
        return None
    return target


def trusted_uncertain_profile(payload: Mapping[str, Any]) -> tuple[str, float] | None:
    """Return the shadow-owner profile and quality for Persona-safe evidence."""

    profile_id = payload.get("speaker_profile_id")
    model_version = payload.get("speaker_model_version")
    template_version = payload.get("speaker_template_version")
    quality = payload.get("speaker_quality_score")
    if (
        payload.get("persona_eligible") is not True
        or payload.get("speaker_reason_code") != "shadow_owner_candidate"
        or not isinstance(profile_id, str)
        or not profile_id.strip()
        or not isinstance(model_version, str)
        or not model_version.strip()
        or isinstance(template_version, bool)
        or not isinstance(template_version, int)
        or template_version < 1
        or isinstance(quality, bool)
        or not isinstance(quality, (int, float))
        or not math.isfinite(float(quality))
        or not 0.5 <= float(quality) <= 1
    ):
        return None
    return profile_id.strip(), float(quality)


def safe_confirmed_style_description(description: str) -> str | None:
    """Return only fixed, non-personal style labels safe for uncertain speakers."""

    normalized = description.strip()
    return normalized if normalized in _FIXED_STYLE_DESCRIPTIONS else None
