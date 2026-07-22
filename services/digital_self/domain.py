"""Public records and failures for immutable Digital Self versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

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


type ManifestEntry = MemoryClaimManifestEntry | PersonaTraitManifestEntry


@dataclass(frozen=True, slots=True)
class DigitalSelfSourceSummary:
    memory_claim_count: int
    persona_trait_count: int
    persona_version_id: str | None
    source_summary_sha256: str


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
