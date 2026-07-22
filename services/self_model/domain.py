"""Public contracts for the evidence-backed self model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from services.archive.domain import SpeakerClass

CognitiveClaimType = Literal[
    "belief",
    "preference",
    "value",
    "decision_rule",
    "red_line",
    "uncertainty",
    "conflict",
    "support",
]
ItemStatus = Literal["candidate", "confirmed", "disputed", "retracted", "superseded"]
DecisionKind = Literal["real", "hypothetical"]
RelationshipProfileStatus = Literal["candidate", "approved", "revoked", "superseded"]
SourceRelation = Literal["support", "counterexample"]
SelfModelItemKind = Literal["cognitive_claim", "decision_case", "relationship_profile"]


class SelfModelNotFoundError(LookupError):
    """The requested record does not exist in the caller's account scope."""


class InvalidSelfModelTransitionError(ValueError):
    """The requested lifecycle transition is invalid."""


class UntrustedSelfModelSourceError(ValueError):
    """The evidence is not eligible to shape the owner's self model."""


class SelfModelVersionConflictError(RuntimeError):
    """The optimistic item version no longer matches."""


class SelfModelIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different command."""


class RelationshipReferenceError(ValueError):
    """A relationship profile must reference existing account-scoped archive rows."""


@dataclass(frozen=True, slots=True)
class SourceInput:
    source_event_id: str
    relation: SourceRelation = "support"
    adopted: bool = True
    negative: bool = False


@dataclass(frozen=True, slots=True)
class SelfModelSource:
    source_event_id: str
    relation: SourceRelation
    adopted: bool
    negative: bool
    speaker_class: SpeakerClass
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CognitiveClaim:
    claim_id: str
    account_id: str
    claim_type: CognitiveClaimType
    statement: str
    context: str
    confidence: float
    sharing_scope: str
    status: ItemStatus
    unresolved_conflict: bool
    sources: tuple[SelfModelSource, ...]
    owner_reviewed_at: datetime | None
    step_up_verified: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionCase:
    case_id: str
    account_id: str
    kind: DecisionKind
    context: str
    options: tuple[str, ...]
    constraints: tuple[str, ...]
    chosen_option: str
    rejected_options: tuple[str, ...]
    outcome: str
    reflection: str
    still_endorsed: bool
    sharing_scope: str
    status: ItemStatus
    unresolved_conflict: bool
    sources: tuple[SelfModelSource, ...]
    owner_reviewed_at: datetime | None
    step_up_verified: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RelationshipProfile:
    profile_id: str
    account_id: str
    version_number: int
    person_id: str
    relationship_id: str
    salutation: str
    tone: str
    advice_style: str
    sharing_scope: str
    boundaries: tuple[str, ...]
    status: RelationshipProfileStatus
    unresolved_conflict: bool
    sources: tuple[SelfModelSource, ...]
    owner_reviewed_at: datetime | None
    step_up_verified: bool
    created_at: datetime


type SelfModelItem = CognitiveClaim | DecisionCase | RelationshipProfile


class SelfModelRegistryPort(Protocol):
    async def create_cognitive_claim(
        self,
        *,
        account_id: str,
        claim_type: CognitiveClaimType,
        statement: str,
        confidence: float,
        idempotency_key: str,
        context: str = "",
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> CognitiveClaim: ...

    async def create_decision_case(
        self,
        *,
        account_id: str,
        kind: DecisionKind,
        context: str,
        options: tuple[str, ...],
        constraints: tuple[str, ...],
        chosen_option: str,
        rejected_options: tuple[str, ...],
        outcome: str,
        reflection: str,
        still_endorsed: bool,
        idempotency_key: str,
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> DecisionCase: ...

    async def create_relationship_profile(
        self,
        *,
        account_id: str,
        person_id: str,
        relationship_id: str,
        salutation: str,
        tone: str,
        advice_style: str,
        boundaries: tuple[str, ...],
        idempotency_key: str,
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> RelationshipProfile: ...

    async def add_source(
        self,
        *,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        source_event_id: str,
        relation: SourceRelation,
        adopted: bool,
        negative: bool,
        expected_version: int,
        idempotency_key: str,
    ) -> SelfModelItem: ...

    async def review_cognitive_claim(
        self,
        *,
        account_id: str,
        claim_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> CognitiveClaim: ...

    async def review_decision_case(
        self,
        *,
        account_id: str,
        case_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> DecisionCase: ...

    async def review_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        version_number: int,
        status: Literal["approved", "revoked"],
        expected_status: Literal["candidate", "approved"],
        step_up_verified: bool,
        idempotency_key: str,
    ) -> RelationshipProfile: ...

    async def revise_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        expected_version: int,
        salutation: str,
        tone: str,
        advice_style: str,
        boundaries: tuple[str, ...],
        idempotency_key: str,
        sharing_scope: str,
        unresolved_conflict: bool = False,
    ) -> RelationshipProfile: ...

    async def get_cognitive_claim(
        self, *, account_id: str, claim_id: str
    ) -> CognitiveClaim: ...

    async def get_decision_case(
        self, *, account_id: str, case_id: str
    ) -> DecisionCase: ...

    async def get_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        version_number: int | None = None,
    ) -> RelationshipProfile: ...

    async def cognitive_claims(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[CognitiveClaim, ...]: ...

    async def decision_cases(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[DecisionCase, ...]: ...

    async def relationship_profiles(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[RelationshipProfile, ...]: ...

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, object]]]: ...

    async def delete_account(self, account_id: str) -> dict[str, int]: ...
