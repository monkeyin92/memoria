"""Storage seam for the identity domain.

``IdentityStore`` is the only persistence contract the service depends on.
Adapters: ``InMemoryIdentityStore`` (Control API dev fixtures),
``SqliteIdentityStore`` (local development) and ``PostgresIdentityStore``
(production authority with FORCE RLS, section 11.7 / PR-17).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from services.identity.domain import (
    BindingStatus,
    DeviceBinding,
    IdempotencyRecord,
    PersonaAssignmentRecord,
    PersonSubject,
    Relationship,
    RelationshipStatus,
    TransferIntent,
)


@dataclass(frozen=True)
class AuditEvent:
    """Append-only audit trail entry for identity mutations."""

    event_id: str
    action: str
    actor_person_id: str | None
    subject_person_id: str | None
    person_id: str | None
    device_id: str | None
    binding_id: str | None
    relationship_id: str | None
    payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class OutboxEvent:
    """Transactional outbox entry; ``event_id`` is the idempotency key."""

    outbox_id: str
    event_id: str
    topic: str
    payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class IdentityStore(Protocol):
    """Persistence contract used by ``IdentityService``.

    Versioned bindings are append-only: ``persist_binding`` atomically assigns
    the next per-device version (and, when ``previous_binding_id`` is given,
    marks that version superseded in the same transaction), so concurrent
    writers cannot produce duplicate versions.

    Mutations accept optional ``audit_event`` / ``outbox_event`` records that
    must be persisted in the SAME transaction as the domain change (section
    11.6): a binding/relationship/person mutation without its audit and outbox
    trail is not a committed mutation.

    ``actor_person_id`` and ``scope`` carry the request context.  The
    PostgreSQL adapter uses them for transaction-local RLS context
    (``app.identity_actor`` / ``app.identity_scope``). SQLite and in-memory
    adapters generally rely on query filters; account-level binding discovery
    also enforces actor visibility explicitly, matching PostgreSQL RLS.
    """

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def save_person(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None: ...

    async def register_person(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        evidence_id: str | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject:
        """Atomic compare-or-insert registration.

        Returns the actually persisted row: an exact canonical replay returns
        the stored object, a drift is an explicit conflict, and the caller
        never receives an unpersisted request snapshot.  On PostgreSQL this
        runs as the dedicated registration role through the SECURITY DEFINER
        compare-or-insert port (no standalone unrestricted read port exists).
        """
        ...

    async def declare_age_evidence(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None: ...

    async def verify_age_evidence(
        self,
        person: PersonSubject,
        *,
        evidence_id: str,
        verifier_person_id: str,
        actor_person_id: str | None = None,
        scope: str = "registration",
    ) -> None: ...

    async def update_person_profile(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None: ...

    async def reconcile_account_registration(
        self,
        person: PersonSubject,
        *,
        expected_updated_at: datetime,
        evidence_id: str,
        source_revision: int,
        audit_event: AuditEvent,
        outbox_event: OutboxEvent,
    ) -> None:
        """Registration authority only: reconcile an unspecified legacy person.

        Never overwrites minor/disputed/inactive records. The age fields, audit
        and outbox commit atomically; a verified-adult replay is a no-op.
        """
        ...

    async def get_person(
        self,
        person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject | None: ...

    async def person_exists(self, person_id: str) -> bool: ...

    async def has_active_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: str,
        at: datetime,
    ) -> bool: ...

    async def save_relationship(
        self,
        relationship: Relationship,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        extra_relationships: tuple[Relationship, ...] = (),
        extra_audit_events: tuple[AuditEvent, ...] = (),
        expected_updated_at: datetime | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None: ...

    async def get_relationship(
        self,
        relationship_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> Relationship | None: ...

    async def list_relationships(
        self,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]: ...

    async def scan_relationships(
        self,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]: ...

    async def persist_binding(
        self,
        binding: DeviceBinding,
        *,
        previous_binding_id: str | None = None,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding: ...

    async def get_binding(
        self,
        binding_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None: ...

    async def get_active_binding(
        self,
        device_id: str,
        now: datetime,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None: ...

    async def list_active_bindings_for_person(
        self,
        person_id: str,
        now: datetime,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]: ...

    async def list_binding_versions(
        self,
        device_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]: ...

    async def scan_bindings(
        self,
        statuses: tuple[BindingStatus, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]: ...

    async def transition_binding(
        self,
        binding_id: str,
        *,
        status: BindingStatus,
        valid_until: datetime | None,
        at: datetime,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding: ...

    async def save_transfer_intent(
        self,
        intent: TransferIntent,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        expected_updated_at: datetime | None = None,
        idempotency_record: IdempotencyRecord | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None: ...

    async def get_transfer_intent(
        self,
        transfer_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> TransferIntent | None: ...

    async def list_transfer_intents(
        self,
        device_id: str,
        statuses: tuple[str, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]: ...

    async def scan_transfer_intents(
        self,
        statuses: tuple[str, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]: ...

    async def complete_transfer(
        self,
        *,
        intent: TransferIntent,
        binding: DeviceBinding,
        previous_binding_id: str,
        audit_events: tuple[AuditEvent, ...],
        outbox_events: tuple[OutboxEvent, ...],
        idempotency_record: IdempotencyRecord | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding: ...

    async def get_idempotency_record(
        self,
        scope_key: str,
        idempotency_key: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> IdempotencyRecord | None: ...

    async def save_idempotency_record(
        self,
        record: IdempotencyRecord,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> bool: ...

    async def upsert_persona_assignment(
        self,
        record: PersonaAssignmentRecord,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonaAssignmentRecord:
        """Idempotent upsert keyed by ``(binding_id, subject_id)``.

        A replay whose persona is unchanged returns the persisted row
        verbatim (immutable ``created_at``/``updated_at``); a different
        persona overwrites the override while preserving ``created_at``.
        """
        ...

    async def get_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonaAssignmentRecord | None: ...

    async def list_persona_assignments(
        self,
        *,
        binding_id: str,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[PersonaAssignmentRecord, ...]: ...

    async def delete_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> bool:
        """Delete a subject override; ``False`` when no row existed."""
        ...

    async def append_audit(self, event: AuditEvent) -> None: ...

    async def enqueue_outbox(self, event: OutboxEvent) -> None: ...
