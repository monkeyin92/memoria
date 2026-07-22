"""Public PersonaEngine records; storage and extraction stay behind the seam."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from services.archive.domain import SpeakerClass

PersonaTraitCategory = Literal[
    "verbal_tic",
    "sentence_length",
    "speech_rate",
    "pause_style",
    "emphasis_style",
    "emotional_expression",
    "discourse_style",
    "narrative_style",
    "decision_habit",
    "value_priority",
]
PersonaTraitStatus = Literal["candidate", "confirmed", "disabled"]
LEGACY_COGNITIVE_TRAIT_CATEGORIES: frozenset[PersonaTraitCategory] = frozenset(
    {"decision_habit", "value_priority"}
)


class PersonaCounterexampleRequiredError(ValueError):
    """A decision/value trait cannot become active without an explicit boundary."""


def require_persona_counterexample(
    *,
    category: PersonaTraitCategory,
    action: Literal["confirm", "correct", "disable"],
    counterexample: str,
) -> None:
    if (
        action != "disable"
        and category in LEGACY_COGNITIVE_TRAIT_CATEGORIES
        and not counterexample.strip()
    ):
        raise PersonaCounterexampleRequiredError(
            "decision and value traits require a counterexample before confirmation"
        )


@dataclass(frozen=True, slots=True)
class PersonaEvidence:
    account_id: str
    source_event_id: str
    learning_allowed: bool
    scene: str = "conversation"
    contamination_flags: tuple[str, ...] = ()
    speech_duration_ms: int | None = None
    pause_ratio: float | None = None
    quality_score: float | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.source_event_id.strip():
            raise ValueError("persona evidence requires account_id and source_event_id")
        if not self.scene.strip() or len(self.scene) > 128:
            raise ValueError("persona evidence scene must contain 1..128 characters")
        if any(not flag.strip() or len(flag) > 64 for flag in self.contamination_flags):
            raise ValueError("persona contamination flags must contain 1..64 characters")
        if self.speech_duration_ms is not None and self.speech_duration_ms <= 0:
            raise ValueError("speech_duration_ms must be positive")
        for value in (self.pause_ratio, self.quality_score):
            if value is not None and not 0 <= value <= 1:
                raise ValueError("persona ratios and quality must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ObservationResult:
    accepted: bool
    reason: str
    candidate_trait_ids: tuple[str, ...] = ()
    published_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaTrait:
    trait_id: str
    category: PersonaTraitCategory
    description: str
    context: str
    counterexample: str
    confidence: float
    status: PersonaTraitStatus
    observation_count: int
    source_event_ids: tuple[str, ...]
    updated_at: datetime
    version_id: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaReview:
    account_id: str
    trait_id: str
    action: Literal["confirm", "correct", "disable"]
    corrected_description: str | None = None
    counterexample: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.trait_id.strip():
            raise ValueError("persona review requires account_id and trait_id")
        if self.action == "correct" and not (self.corrected_description or "").strip():
            raise ValueError("corrected_description is required for correction")
        if self.action != "correct" and self.corrected_description is not None:
            raise ValueError("corrected_description is only valid for correction")
        if self.counterexample is not None and len(self.counterexample) > 2000:
            raise ValueError("persona counterexample must not exceed 2000 characters")


@dataclass(frozen=True, slots=True)
class PersonaRequest:
    account_id: str
    speaker_class: SpeakerClass
    topic: str = ""
    enabled: bool = True
    max_chars: int = 1200
    confirmed_style_only: bool = False

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not 160 <= self.max_chars <= 4000:
            raise ValueError("persona request requires account_id and max_chars 160..4000")
        if len(self.topic) > 1000:
            raise ValueError("persona topic must not exceed 1000 characters")
        if self.confirmed_style_only and self.speaker_class != "uncertain":
            raise ValueError("confirmed style access is only valid for uncertain speakers")


@dataclass(frozen=True, slots=True)
class PersonaCapsuleEntry:
    trait_id: str
    category: PersonaTraitCategory
    description: str
    context: str
    counterexample: str
    confidence: float
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonaCapsule:
    version_id: str | None = None
    version_number: int | None = None
    entries: tuple[PersonaCapsuleEntry, ...] = ()
    prompt_fragment: str = ""
    delivery_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class PersonaVersion:
    version_id: str
    version_number: int
    status: Literal["active", "superseded"]
    reason: str
    trait_ids: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersonaConsent:
    account_id: str
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None = None


class PersonaEnginePort(Protocol):
    async def observe(self, evidence: PersonaEvidence) -> ObservationResult: ...

    async def capsule(self, request: PersonaRequest) -> PersonaCapsule: ...

    async def review(self, command: PersonaReview) -> PersonaTrait: ...

    async def rollback(self, *, account_id: str, version_id: str) -> PersonaVersion: ...

    async def traits(self, *, account_id: str) -> tuple[PersonaTrait, ...]: ...

    async def versions(self, *, account_id: str) -> tuple[PersonaVersion, ...]: ...

    async def grant_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
    ) -> PersonaConsent: ...

    async def revoke_consent(self, *, account_id: str) -> PersonaConsent: ...

    async def learning_allowed(self, *, account_id: str) -> bool: ...
