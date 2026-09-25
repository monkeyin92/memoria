"""Public records and failures for immutable Digital Self versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from services.self_model.domain import CognitiveClaimType, DecisionKind

VersionStatus = Literal["draft", "testing", "approved", "frozen", "revoked"]


class VersionNotFoundError(LookupError):
    """The version does not exist in the caller's account scope."""


class EmptyDigitalSelfSourceError(ValueError):
    """No confirmed owner material is available for a Digital Self build."""


class InvalidVersionTransitionError(ValueError):
    """The requested lifecycle transition is not valid for the current state."""


class SourceSnapshotConflictError(RuntimeError):
    """An optimistic source or manifest digest did not match current state."""


class ManifestIntegrityError(RuntimeError):
    """Persisted manifest bytes, digest, or source summary disagree."""


@dataclass(frozen=True, slots=True)
class MemoryClaimManifestEntry:
    claim_id: str
    category: str
    subject_key: str
    predicate: str
    value: str
    confidence: float
    sensitive_domain: str
    extractor_version: str
    source_event_id: str
    valid_at: str
    entry_type: Literal["memory_claim"] = "memory_claim"


@dataclass(frozen=True, slots=True)
class PersonaTraitManifestEntry:
    trait_id: str
    persona_version_id: str
    category: str
    description: str
    context: str
    counterexample: str
    confidence: float
    source_event_ids: tuple[str, ...]
    entry_type: Literal["persona_trait"] = "persona_trait"


@dataclass(frozen=True, slots=True)
class CognitiveClaimManifestEntry:
    claim_id: str
    claim_type: CognitiveClaimType
    statement: str
    context: str
    confidence: float
    sharing_scope: str
    support_source_event_ids: tuple[str, ...]
    counterexample_source_event_ids: tuple[str, ...]
    entry_type: Literal["cognitive_claim"] = "cognitive_claim"


@dataclass(frozen=True, slots=True)
class DecisionCaseManifestEntry:
    case_id: str
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
    support_source_event_ids: tuple[str, ...]
    counterexample_source_event_ids: tuple[str, ...]
    entry_type: Literal["decision_case"] = "decision_case"


@dataclass(frozen=True, slots=True)
class RelationshipProfileManifestEntry:
    profile_id: str
    version_number: int
    person_id: str
    relationship_id: str
    salutation: str
    tone: str
    advice_style: str
    sharing_scope: str
    boundaries: tuple[str, ...]
    support_source_event_ids: tuple[str, ...]
    counterexample_source_event_ids: tuple[str, ...]
    entry_type: Literal["relationship_profile"] = "relationship_profile"


@dataclass(frozen=True, slots=True)
class VoiceProfileManifestRef:
    profile_id: str
    version_number: int
    provider: str
    target_model: str
    resource_id: str
    provider_expires_at: str
    speaker_sha256: str

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("voice profile ref requires profile_id")
        if self.version_number < 1:
            raise ValueError("voice profile ref version_number must be >= 1")
        if self.provider != "volcengine_doubao":
            raise ValueError("voice profile ref provider is unsupported")
        if self.target_model != "seed-icl-2.0":
            raise ValueError("voice profile ref target_model is unsupported")
        if self.resource_id != "seed-icl-2.0":
            raise ValueError("voice profile ref resource_id is unsupported")
        if len(self.speaker_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.speaker_sha256
        ):
            raise ValueError("voice profile ref speaker digest must be lowercase SHA-256")
        try:
            expires_at = datetime.fromisoformat(self.provider_expires_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("voice profile ref expiry must be ISO-8601") from exc
        offset = expires_at.utcoffset()
        if expires_at.tzinfo is None or offset is None:
            raise ValueError("voice profile ref expiry must be UTC aware")
        if offset.total_seconds() != 0:
            raise ValueError("voice profile ref expiry must be UTC")


type ManifestEntry = (
    MemoryClaimManifestEntry
    | PersonaTraitManifestEntry
    | CognitiveClaimManifestEntry
    | DecisionCaseManifestEntry
    | RelationshipProfileManifestEntry
)


@dataclass(frozen=True, slots=True)
class DigitalSelfSourceSummary:
    memory_claim_count: int
    persona_trait_count: int
    persona_version_id: str | None
    source_summary_sha256: str
    cognitive_claim_count: int = 0
    decision_case_count: int = 0
    relationship_profile_count: int = 0
    voice_profile: VoiceProfileManifestRef | None = None


@dataclass(frozen=True, slots=True)
class DigitalSelfManifest:
    schema_version: str
    compiler_version: str
    policy_version: str
    parent_version_id: str | None
    rollback_target_version_id: str | None
    entries: tuple[ManifestEntry, ...]
    source_summary: DigitalSelfSourceSummary


@dataclass(frozen=True, slots=True)
class DigitalSelfVersion:
    version_id: str
    account_id: str
    version_number: int
    status: VersionStatus
    manifest: DigitalSelfManifest
    manifest_sha256: str
    created_at: datetime


class RegistryPort(Protocol):
    async def build(
        self,
        *,
        account_id: str,
        parent_version_id: str | None = None,
        expected_source_summary_sha256: str | None = None,
    ) -> DigitalSelfVersion: ...

    async def get(self, *, account_id: str, version_id: str) -> DigitalSelfVersion: ...

    async def list(self, *, account_id: str) -> tuple[DigitalSelfVersion, ...]: ...

    async def begin_testing(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion: ...

    async def approve(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion: ...

    async def freeze(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion: ...

    async def revoke(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion: ...

    async def rollback(
        self,
        *,
        account_id: str,
        target_version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion: ...
