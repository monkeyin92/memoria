"""Deterministic persona candidates shared by SQLite and PostgreSQL adapters."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from services.persona.domain import PersonaEvidence, PersonaTraitCategory

TICS = ("我觉得", "其实", "说实话", "怎么说呢", "坦白说", "总的来说")
AUTO_PROMOTE = frozenset(
    {"verbal_tic", "sentence_length", "speech_rate", "pause_style"}
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
