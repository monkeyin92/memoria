"""Public domain contracts for Memoria's life archive."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol

from services.archive.object_store import ObjectRef

SpeakerClass = Literal["owner", "guest", "uncertain", "assistant", "system"]
MemoryStatus = Literal["confirmed", "disputed", "retracted", "corrected"]
RawVoiceRetentionPolicy = Literal["account_lifetime"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def canonical_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    event_id: str
    account_id: str
    event_type: str
    occurred_at: datetime
    speaker_class: SpeakerClass
    source: str
    payload: Mapping[str, Any]
    session_id: str | None = None
    turn_id: int | None = None
    generation_id: int | None = None
    speaker_identity_id: str | None = None
    consent_grant_id: str | None = None
    schema_version: int = 1
    supersedes_event_id: str | None = None
    content_sha256: str = field(init=False)
    idempotency_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("event_id", "account_id", "event_type", "source"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        occurred_at = _utc(self.occurred_at)
        payload_json = canonical_payload(self.payload)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        idempotency_content = {
            "account_id": self.account_id,
            "consent_grant_id": self.consent_grant_id,
            "event_type": self.event_type,
            "generation_id": self.generation_id,
            "payload": json.loads(payload_json),
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source": self.source,
            "speaker_class": self.speaker_class,
            "speaker_identity_id": self.speaker_identity_id,
            "supersedes_event_id": self.supersedes_event_id,
            "turn_id": self.turn_id,
        }
        fingerprint = canonical_payload(
            {"occurred_at": occurred_at.isoformat(), **idempotency_content}
        )
        object.__setattr__(
            self,
            "content_sha256",
            hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(),
        )
        # The deterministic event_id identifies a logical event. A retry can be
        # rebuilt after its source clock has advanced, but it must never change
        # the event's owner, session, or immutable payload.
        object.__setattr__(
            self,
            "idempotency_sha256",
            hashlib.sha256(canonical_payload(idempotency_content).encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class RecordResult:
    event_id: str
    outbox_id: str
    recorded_at: datetime
    duplicate: bool
    blob_duplicate: bool | None = None
    retained_object_key: str | None = None


@dataclass(frozen=True, slots=True)
class ContextQuery:
    account_id: str
    speaker_class: SpeakerClass
    text: str = ""
    session_id: str | None = None
    limit: int = 20
    include_sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("account_id must not be blank")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class ContextBundle:
    evidence: tuple[EvidenceEvent, ...] = ()
    memory_ids: tuple[str, ...] = ()
    persona_version: str | None = None
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class MemoryReview:
    review_event_id: str
    account_id: str
    target_id: str
    action: Literal["confirm", "dispute", "retract", "correct"]
    occurred_at: datetime
    corrected_text: str | None = None

    def __post_init__(self) -> None:
        for name in ("review_event_id", "account_id", "target_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))
        if self.action == "correct" and not (self.corrected_text or "").strip():
            raise ValueError("corrected_text is required for a correction")
        if self.action != "correct" and self.corrected_text is not None:
            raise ValueError("corrected_text is only valid for a correction")


@dataclass(frozen=True, slots=True)
class ReviewedMemory:
    target_id: str
    review_event_id: str
    status: MemoryStatus
    current_text: str | None


@dataclass(frozen=True, slots=True)
class RawVoiceConsent:
    consent_grant_id: str
    account_id: str
    policy_version: str
    retention_policy: RawVoiceRetentionPolicy
    granted_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RawVoiceRevocation:
    consent: RawVoiceConsent
    references: tuple[ObjectRef, ...]


class RawVoiceConsentRequiredError(PermissionError):
    """Raw owner audio arrived without a matching active consent grant."""


class IdempotencyConflictError(ValueError):
    """The same event id was reused for different immutable content."""


class EvidenceNotFoundError(LookupError):
    pass


class LifeArchivePort(Protocol):
    async def record(self, event: EvidenceEvent) -> RecordResult: ...

    async def event(
        self,
        *,
        account_id: str,
        event_id: str,
    ) -> EvidenceEvent | None: ...

    async def turn_event(
        self,
        *,
        account_id: str,
        session_id: str,
        turn_id: int,
        generation_id: int,
        event_type: str,
    ) -> EvidenceEvent | None: ...

    async def evidence_window(
        self,
        *,
        account_id: str,
        occurred_after: datetime,
        occurred_before: datetime,
        event_types: tuple[str, ...] = (),
        limit: int = 10_000,
    ) -> tuple[EvidenceEvent, ...]: ...

    async def context(self, query: ContextQuery) -> ContextBundle: ...

    async def review(self, command: MemoryReview) -> ReviewedMemory: ...

    async def grant_raw_voice_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
        retention_policy: RawVoiceRetentionPolicy,
        granted_at: datetime,
    ) -> RawVoiceConsent: ...

    async def active_raw_voice_consent(self, *, account_id: str) -> RawVoiceConsent | None: ...

    async def revoke_raw_voice_consent(
        self,
        *,
        account_id: str,
        revoked_at: datetime,
    ) -> RawVoiceRevocation: ...

    async def purge_raw_voice_blobs(
        self,
        *,
        account_id: str,
        object_keys: tuple[str, ...],
    ) -> None: ...

    async def record_with_blob(
        self,
        event: EvidenceEvent,
        reference: ObjectRef,
        *,
        retention_policy: RawVoiceRetentionPolicy,
    ) -> RecordResult: ...

    async def raw_voice_blobs(self, *, account_id: str) -> tuple[ObjectRef, ...]: ...
