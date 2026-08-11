"""Consent persistence seams: sync and async store ports plus unit-of-work.

Every mutation (grant / revoke / dispute / expire / snapshot) writes the
consent version rows, snapshot, audit entry and outbox event in a single
transaction.  Callers obtain a :class:`ConsentUnitOfWork` from the store,
perform reads and appends on it, then ``commit()``; an uncommitted work unit
must be ``rollback()``-ed (the authority does this automatically on failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.consent.events import AuditEntry, ConsentOutboxEvent
from services.consent.evidence import ConsentEvidence, ConsentOffer, ConsentSnapshot


class ConsentConflictError(RuntimeError):
    """A version/idempotency conflict: the operation targets stale state."""


class ConsentNotFoundError(LookupError):
    """The requested consent chain or snapshot does not exist."""


class ConsentConsistencyError(RuntimeError):
    """Persisted state violates an invariant (fail closed, never auto-heal)."""


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Stable mapping from idempotency key to the completed operation result."""

    idempotency_key: str
    content_hash: str
    consent_id: str | None
    version: int | None
    snapshot_id: str | None
    event_id: str
    created_at: datetime
    audit_id: str | None = None
    subject_id: str | None = None
    actor_id: str | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if len(self.idempotency_key) > 128:
            raise ValueError("idempotency_key too long")
        if not self.content_hash or len(self.content_hash) != 64:
            raise ValueError("content_hash must be a sha256 hex digest")
        if not self.event_id.strip():
            raise ValueError("event_id must be non-empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


class ConsentUnitOfWork(Protocol):
    """Transactional read/write handle for one consent operation."""

    def latest_offer(self, offer_id: str) -> ConsentOffer | None: ...

    def offer_by_id(self, offer_id: str, version: int) -> ConsentOffer | None: ...

    def lock_offer_head(
        self, offer_id: str, actor_id: str, subject_id: str
    ) -> ConsentOffer | None: ...

    def latest_consent(self, consent_id: str) -> ConsentEvidence | None: ...

    def get_consent(self, consent_id: str, version: int) -> ConsentEvidence | None: ...

    def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> ConsentEvidence | None: ...

    def active_chains(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> tuple[ConsentEvidence, ...]: ...

    def all_active_chains(self) -> tuple[ConsentEvidence, ...]: ...

    def latest_snapshot(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None: ...

    def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None: ...

    def snapshot_by_id(self, snapshot_id: str) -> ConsentSnapshot | None: ...

    def outbox_by_id(self, event_id: str) -> ConsentOutboxEvent | None: ...

    def audit_by_id(self, audit_id: str) -> AuditEntry | None: ...

    def get_idempotency(self, idempotency_key: str) -> IdempotencyRecord | None: ...

    def append_consent(self, evidence: ConsentEvidence) -> None: ...

    def append_offer(self, offer: ConsentOffer) -> None: ...

    def append_snapshot(self, snapshot: ConsentSnapshot) -> None: ...

    def append_outbox(self, event: ConsentOutboxEvent) -> None: ...

    def append_audit(self, entry: AuditEntry) -> None: ...

    def save_idempotency(self, record: IdempotencyRecord) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class ConsentStorePort(Protocol):
    """Synchronous consent store (InMemory / SQLite)."""

    def transaction(self) -> ConsentUnitOfWork: ...

    def close(self) -> None: ...


class AsyncConsentUnitOfWork(Protocol):
    async def latest_offer(self, offer_id: str) -> ConsentOffer | None: ...

    async def offer_by_id(self, offer_id: str, version: int) -> ConsentOffer | None: ...

    async def lock_offer_head(
        self, offer_id: str, actor_id: str, subject_id: str
    ) -> ConsentOffer | None: ...

    async def latest_consent(self, consent_id: str) -> ConsentEvidence | None: ...

    async def get_consent(self, consent_id: str, version: int) -> ConsentEvidence | None: ...

    async def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> ConsentEvidence | None: ...

    async def active_chains(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> tuple[ConsentEvidence, ...]: ...

    async def all_active_chains(self) -> tuple[ConsentEvidence, ...]: ...

    async def latest_snapshot(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None: ...

    async def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None: ...

    async def snapshot_by_id(self, snapshot_id: str) -> ConsentSnapshot | None: ...

    async def outbox_by_id(self, event_id: str) -> ConsentOutboxEvent | None: ...

    async def audit_by_id(self, audit_id: str) -> AuditEntry | None: ...

    async def get_idempotency(self, idempotency_key: str) -> IdempotencyRecord | None: ...

    async def append_consent(self, evidence: ConsentEvidence) -> None: ...

    async def append_offer(self, offer: ConsentOffer) -> None: ...

    async def append_snapshot(self, snapshot: ConsentSnapshot) -> None: ...

    async def append_outbox(self, event: ConsentOutboxEvent) -> None: ...

    async def append_audit(self, entry: AuditEntry) -> None: ...

    async def save_idempotency(self, record: IdempotencyRecord) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class AsyncConsentStorePort(Protocol):
    """Asynchronous consent store (PostgreSQL / asyncpg)."""

    async def transaction(self) -> AsyncConsentUnitOfWork: ...

    async def close(self) -> None: ...


__all__ = [
    "AsyncConsentStorePort",
    "AsyncConsentUnitOfWork",
    "ConsentConflictError",
    "ConsentConsistencyError",
    "ConsentNotFoundError",
    "ConsentStorePort",
    "ConsentUnitOfWork",
    "IdempotencyRecord",
]
