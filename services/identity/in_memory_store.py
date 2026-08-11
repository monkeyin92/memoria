"""In-memory adapter for the identity store seam.

Suitable for Control API development fixtures and unit tests.  Thread-safe
for a single event loop; version assignment is guarded by a reentrant lock
so per-device versions stay monotonic.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from typing import TypeVar

from services.identity.domain import (
    BindingStatus,
    BindingVersionConflictError,
    DeviceBinding,
    DeviceBindingRole,
    IdempotencyRecord,
    IdentityConflictError,
    IdentityNotFoundError,
    PersonSubject,
    Relationship,
    RelationshipStatus,
    TransferIntent,
)
from services.identity.repository import AuditEvent, OutboxEvent


class InMemoryIdentityStore:
    """In-memory implementation of the ``IdentityStore`` protocol."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._persons: dict[str, PersonSubject] = {}
        self._relationships: dict[str, Relationship] = {}
        self._bindings: dict[str, DeviceBinding] = {}
        self._transfers: dict[str, TransferIntent] = {}
        self._idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self._audit: list[AuditEvent] = []
        self._outbox: dict[str, OutboxEvent] = {}

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save_person(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        with self._lock:
            self._persons[person.person_id] = person
            if audit_event is not None:
                self._audit.append(audit_event)
            if outbox_event is not None:
                self._enqueue_outbox_locked(outbox_event)

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
        with self._lock:
            existing = self._persons.get(person.person_id)
            if existing is not None:
                return existing
            self._persons[person.person_id] = person
            if audit_event is not None:
                self._audit.append(audit_event)
            if outbox_event is not None:
                self._enqueue_outbox_locked(outbox_event)
            return person

    async def declare_age_evidence(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        with self._lock:
            self._persons[person.person_id] = person
            if audit_event is not None:
                self._audit.append(audit_event)

    async def verify_age_evidence(
        self,
        person: PersonSubject,
        *,
        evidence_id: str,
        verifier_person_id: str,
        actor_person_id: str | None = None,
        scope: str = "registration",
    ) -> None:
        with self._lock:
            self._persons[person.person_id] = person

    async def update_person_profile(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        with self._lock:
            self._persons[person.person_id] = person
            if audit_event is not None:
                self._audit.append(audit_event)

    async def get_person(
        self,
        person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject | None:
        with self._lock:
            return self._persons.get(person_id)

    async def person_exists(self, person_id: str) -> bool:
        with self._lock:
            return person_id in self._persons

    async def has_active_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: str,
        at: datetime,
    ) -> bool:
        with self._lock:
            return any(
                relationship.relation_type == relation_type
                and relationship.status == "active"
                and relationship.source_person_id == source_person_id
                and relationship.target_person_id == target_person_id
                and relationship.valid_from <= at
                and (
                    relationship.valid_until is None
                    or relationship.valid_until > at
                )
                for relationship in self._relationships.values()
            )

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
    ) -> None:
        with self._lock:
            existing = self._relationships.get(relationship.relationship_id)
            if (
                expected_updated_at is not None
                and existing is not None
                and existing.updated_at != expected_updated_at
            ):
                raise IdentityConflictError(
                    f"relationship {relationship.relationship_id} changed "
                    "concurrently"
                )
            self._relationships[relationship.relationship_id] = relationship
            for extra in extra_relationships:
                self._relationships[extra.relationship_id] = extra
            if audit_event is not None:
                self._audit.append(audit_event)
            self._audit.extend(extra_audit_events)
            if outbox_event is not None:
                self._enqueue_outbox_locked(outbox_event)

    async def get_relationship(
        self,
        relationship_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> Relationship | None:
        with self._lock:
            return self._relationships.get(relationship_id)

    async def list_relationships(
        self,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]:
        with self._lock:
            selected = [
                relationship
                for relationship in self._relationships.values()
                if person_id in (relationship.source_person_id, relationship.target_person_id)
                and (statuses is None or relationship.status in statuses)
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def scan_relationships(
        self,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]:
        with self._lock:
            selected = [
                relationship
                for relationship in self._relationships.values()
                if statuses is None or relationship.status in statuses
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def persist_binding(
        self,
        binding: DeviceBinding,
        *,
        previous_binding_id: str | None = None,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding:
        with self._lock:
            versions = [
                item.binding_version
                for item in self._bindings.values()
                if item.device_id == binding.device_id
            ]
            if previous_binding_id is not None:
                previous = self._bindings.get(previous_binding_id)
                if previous is None:
                    raise IdentityNotFoundError(
                        f"binding {previous_binding_id} to supersede does not exist"
                    )
                if previous.device_id != binding.device_id:
                    raise BindingVersionConflictError(
                        "superseding binding must target the same device"
                    )
                if previous.status != "active":
                    raise BindingVersionConflictError(
                        "only an active binding can be superseded"
                    )
                version = previous.binding_version + 1
                valid_until = binding.valid_from
                if previous.valid_until is not None and previous.valid_until < valid_until:
                    valid_until = previous.valid_until
                self._bindings[previous_binding_id] = replace(
                    previous,
                    status="superseded",
                    valid_until=valid_until,
                    roles=_end_roles(previous, "superseded", binding.valid_from),
                )
            else:
                version = (max(versions) + 1) if versions else 1
            if version in versions:
                raise BindingVersionConflictError(
                    f"binding version {version} already exists for device {binding.device_id}"
                )
            persisted = replace(binding, binding_version=version)
            self._bindings[binding.binding_id] = persisted
            if audit_event is not None:
                self._audit.append(_with_binding_version(audit_event, version))
            if outbox_event is not None:
                self._enqueue_outbox_locked(_with_binding_version(outbox_event, version))
            return persisted

    async def get_binding(
        self,
        binding_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None:
        with self._lock:
            return self._bindings.get(binding_id)

    async def get_active_binding(
        self,
        device_id: str,
        now: datetime,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None:
        with self._lock:
            for binding in sorted(
                self._bindings.values(),
                key=lambda item: item.binding_version,
                reverse=True,
            ):
                if binding.device_id != device_id or binding.status != "active":
                    continue
                if binding.valid_from > now:
                    continue
                if binding.valid_until is not None and binding.valid_until <= now:
                    continue
                return binding
            return None

    async def list_binding_versions(
        self,
        device_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        binding
                        for binding in self._bindings.values()
                        if binding.device_id == device_id
                    ),
                    key=lambda item: item.binding_version,
                )
            )

    async def scan_bindings(
        self,
        statuses: tuple[BindingStatus, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        binding
                        for binding in self._bindings.values()
                        if statuses is None or binding.status in statuses
                    ),
                    key=lambda item: item.created_at,
                )
            )

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
    ) -> DeviceBinding:
        with self._lock:
            binding = self._bindings.get(binding_id)
            if binding is None:
                raise IdentityNotFoundError(f"binding {binding_id} does not exist")
            transitioned = replace(
                binding,
                status=status,
                valid_until=valid_until,
                roles=_end_roles(binding, status, at),
            )
            self._bindings[binding_id] = transitioned
            if audit_event is not None:
                self._audit.append(
                    _with_transition_state(audit_event, status, valid_until)
                )
            if outbox_event is not None:
                self._enqueue_outbox_locked(
                    _with_transition_state(outbox_event, status, valid_until)
                )
            return transitioned

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
    ) -> None:
        with self._lock:
            if idempotency_record is not None:
                self._write_idempotency_locked(idempotency_record)
            existing = self._transfers.get(intent.transfer_id)
            if (
                expected_updated_at is not None
                and existing is not None
                and existing.updated_at != expected_updated_at
            ):
                raise IdentityConflictError(
                    f"transfer {intent.transfer_id} changed concurrently"
                )
            self._transfers[intent.transfer_id] = intent
            if audit_event is not None:
                self._audit.append(audit_event)
            if outbox_event is not None:
                self._enqueue_outbox_locked(outbox_event)

    async def get_transfer_intent(
        self,
        transfer_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> TransferIntent | None:
        with self._lock:
            return self._transfers.get(transfer_id)

    async def list_transfer_intents(
        self,
        device_id: str,
        statuses: tuple[str, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]:
        with self._lock:
            selected = [
                intent
                for intent in self._transfers.values()
                if intent.device_id == device_id
                and (statuses is None or intent.status in statuses)
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def scan_transfer_intents(
        self,
        statuses: tuple[str, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]:
        with self._lock:
            selected = [
                intent
                for intent in self._transfers.values()
                if statuses is None or intent.status in statuses
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

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
    ) -> DeviceBinding:
        with self._lock:
            if idempotency_record is not None:
                self._write_idempotency_locked(idempotency_record)
            stored = self._transfers.get(intent.transfer_id)
            if stored is None:
                raise IdentityNotFoundError(
                    f"transfer {intent.transfer_id} does not exist"
                )
            if stored.status != "pending":
                raise RuntimeError(
                    f"transfer {intent.transfer_id} is not pending (status={stored.status})"
                )
            previous = self._bindings.get(previous_binding_id)
            if previous is None:
                raise IdentityNotFoundError(
                    f"binding {previous_binding_id} to supersede does not exist"
                )
            if previous.device_id != binding.device_id:
                raise BindingVersionConflictError(
                    "superseding binding must target the same device"
                )
            if previous.status != "active":
                raise BindingVersionConflictError(
                    "only an active binding can be superseded"
                )
            version = previous.binding_version + 1
            if any(
                item.binding_version == version
                for item in self._bindings.values()
                if item.device_id == binding.device_id
            ):
                raise BindingVersionConflictError(
                    f"binding version {version} already exists for device {binding.device_id}"
                )
            valid_until = binding.valid_from
            if previous.valid_until is not None and previous.valid_until < valid_until:
                valid_until = previous.valid_until
            self._bindings[previous_binding_id] = replace(
                previous,
                status="superseded",
                valid_until=valid_until,
                roles=_end_roles(previous, "superseded", binding.valid_from),
            )
            persisted = replace(binding, binding_version=version)
            self._bindings[binding.binding_id] = persisted
            self._transfers[intent.transfer_id] = intent
            self._audit.extend(
                _with_binding_version(event, version) for event in audit_events
            )
            for event in outbox_events:
                self._enqueue_outbox_locked(_with_binding_version(event, version))
            if idempotency_record is not None and "binding_version" in (
                idempotency_record.result_payload
            ):
                result_payload = dict(idempotency_record.result_payload)
                result_payload["binding_version"] = version
                self._idempotency[(idempotency_record.scope_key, idempotency_record.idempotency_key)] = replace(
                    idempotency_record, result_payload=result_payload
                )
            return persisted

    def _enqueue_outbox_locked(self, event: OutboxEvent) -> None:
        if event.event_id in self._outbox:
            raise RuntimeError(f"outbox event {event.event_id} already enqueued")
        self._outbox[event.event_id] = event

    def _write_idempotency_locked(self, record: IdempotencyRecord) -> None:
        key = (record.scope_key, record.idempotency_key)
        if key in self._idempotency:
            raise IdentityConflictError(
                f"idempotency key {record.idempotency_key!r} already used "
                f"for scope {record.scope_key!r}"
            )
        self._idempotency[key] = record

    async def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._audit.append(event)

    async def enqueue_outbox(self, event: OutboxEvent) -> None:
        with self._lock:
            self._enqueue_outbox_locked(event)

    def transfer_intents(self) -> tuple[TransferIntent, ...]:
        with self._lock:
            return tuple(
                sorted(self._transfers.values(), key=lambda item: item.created_at)
            )

    async def get_idempotency_record(
        self,
        scope_key: str,
        idempotency_key: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> IdempotencyRecord | None:
        with self._lock:
            return self._idempotency.get((scope_key, idempotency_key))

    async def save_idempotency_record(
        self,
        record: IdempotencyRecord,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> bool:
        with self._lock:
            key = (record.scope_key, record.idempotency_key)
            if key in self._idempotency:
                return False
            self._idempotency[key] = record
            return True

    def audit_events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._audit)

    def outbox_events(self) -> tuple[OutboxEvent, ...]:
        with self._lock:
            return tuple(sorted(self._outbox.values(), key=lambda item: item.created_at))


def _end_roles(
    binding: DeviceBinding, status: str, at: datetime
) -> tuple[DeviceBindingRole, ...]:
    return tuple(
        replace(grant, status=status, ended_at=at)  # type: ignore[arg-type]
        for grant in binding.roles
        if grant.status == "active"
    ) + tuple(
        grant for grant in binding.roles if grant.status != "active"
    )


_IdentityEvent = TypeVar("_IdentityEvent", AuditEvent, OutboxEvent)


def _with_binding_version(event: _IdentityEvent, version: int) -> _IdentityEvent:  # noqa: UP047
    """Rewrite the advisory ``binding_version`` to the store-assigned value."""
    payload = dict(event.payload)
    if "binding_version" in payload:
        payload["binding_version"] = version
    return replace(event, payload=payload)


def _with_transition_state(  # noqa: UP047

    event: _IdentityEvent, status: str, valid_until: datetime | None
) -> _IdentityEvent:
    """Rewrite the advisory status/valid_until to the transition values."""
    payload = dict(event.payload)
    payload["status"] = status
    payload["valid_until"] = valid_until.isoformat() if valid_until else None
    return replace(event, payload=payload)
