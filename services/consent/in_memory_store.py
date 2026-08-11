"""In-memory consent store (tests / local fixtures only, never production authority).

The unit of work buffers every write and applies it atomically on ``commit()``;
``rollback()`` discards the buffer.  Reads inside the transaction see the
buffered overlay first (read-your-writes), and a store-level lock serializes
transactions so version computation is race-free.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from services.consent.events import AuditEntry, ConsentOutboxEvent
from services.consent.evidence import ConsentEvidence, ConsentOffer, ConsentSnapshot
from services.consent.store import (
    ConsentConflictError,
    ConsentUnitOfWork,
    IdempotencyRecord,
)


@dataclass
class _InMemoryState:
    offers: dict[tuple[str, int], ConsentOffer] = field(default_factory=dict)
    consents: dict[tuple[str, int], ConsentEvidence] = field(default_factory=dict)
    snapshots: dict[str, ConsentSnapshot] = field(default_factory=dict)
    outbox: dict[str, ConsentOutboxEvent] = field(default_factory=dict)
    audit: dict[str, AuditEntry] = field(default_factory=dict)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)


class InMemoryConsentStore:
    """Process-local consent store with transactional unit-of-work semantics."""

    def __init__(self) -> None:
        self._state = _InMemoryState()
        self._lock = threading.Lock()

    def transaction(self) -> ConsentUnitOfWork:
        self._lock.acquire()
        return _InMemoryUow(self._state, self._lock)

    @property
    def outbox_events(self) -> tuple[ConsentOutboxEvent, ...]:
        return tuple(self._state.outbox.values())

    @property
    def audit_entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._state.audit.values())

    def close(self) -> None:
        return None


class _InMemoryUow:
    def __init__(self, state: _InMemoryState, lock: threading.Lock) -> None:
        self._state = state
        self._lock = lock
        self._pending_consents: dict[tuple[str, int], ConsentEvidence] = {}
        self._pending_offers: dict[tuple[str, int], ConsentOffer] = {}
        self._pending_snapshots: dict[str, ConsentSnapshot] = {}
        self._pending_outbox: dict[str, ConsentOutboxEvent] = {}
        self._pending_audit: dict[str, AuditEntry] = {}
        self._pending_idempotency: dict[str, IdempotencyRecord] = {}
        self._closed = False

    # -- reads (pending overlay first) ----------------------------------------
    def _merged_offers(self) -> dict[tuple[str, int], ConsentOffer]:
        merged = dict(self._state.offers)
        merged.update(self._pending_offers)
        return merged

    def latest_offer(self, offer_id: str) -> ConsentOffer | None:
        versions = [
            (version, offer)
            for (stored_id, version), offer in self._merged_offers().items()
            if stored_id == offer_id
        ]
        if not versions:
            return None
        return max(versions, key=lambda item: item[0])[1]

    def offer_by_id(self, offer_id: str, version: int) -> ConsentOffer | None:
        return self._merged_offers().get((offer_id, version))

    def lock_offer_head(
        self, offer_id: str, actor_id: str, subject_id: str
    ) -> ConsentOffer | None:
        del actor_id, subject_id
        return self.latest_offer(offer_id)

    def _merged_consents(self) -> dict[tuple[str, int], ConsentEvidence]:
        merged = dict(self._state.consents)
        merged.update(self._pending_consents)
        return merged

    def latest_consent(self, consent_id: str) -> ConsentEvidence | None:
        merged = self._merged_consents()
        versions = [
            (version, evidence)
            for (chain_id, version), evidence in merged.items()
            if chain_id == consent_id
        ]
        if not versions:
            return None
        return max(versions, key=lambda item: item[0])[1]

    def get_consent(self, consent_id: str, version: int) -> ConsentEvidence | None:
        return self._merged_consents().get((consent_id, version))

    def _latest_chains(self) -> dict[str, ConsentEvidence]:
        merged = self._merged_consents()
        latest: dict[str, ConsentEvidence] = {}
        for (consent_id, _version), evidence in merged.items():
            current = latest.get(consent_id)
            if current is None or evidence.version > current.version:
                latest[consent_id] = evidence
        return latest

    def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> ConsentEvidence | None:
        candidates = [
            evidence
            for evidence in self._latest_chains().values()
            if evidence.actor_id == evidence_actor_id
            and evidence.subject_id == subject_id
            and evidence.binding_id == binding_id
            and evidence.binding_version == binding_version
            and evidence.capability == capability
            and evidence.purpose == purpose
        ]
        del request_actor_id
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.version, item.status == "active"))

    def active_chains(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> tuple[ConsentEvidence, ...]:
        return tuple(
            evidence
            for evidence in self._latest_chains().values()
            if evidence.status == "active"
            and evidence.subject_id == subject_id
            and evidence.binding_id == binding_id
            and evidence.binding_version == binding_version
        )

    def all_active_chains(self) -> tuple[ConsentEvidence, ...]:
        return tuple(
            evidence for evidence in self._latest_chains().values() if evidence.status == "active"
        )

    def latest_snapshot(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None:
        merged = dict(self._state.snapshots)
        merged.update(self._pending_snapshots)
        candidates = [
            snapshot
            for snapshot in merged.values()
            if snapshot.subject_id == subject_id
            and snapshot.binding_id == binding_id
            and snapshot.binding_version == binding_version
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda snapshot: snapshot.version)

    def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None:
        del request_actor_id
        return self.latest_snapshot(subject_id, binding_id, binding_version)

    def snapshot_by_id(self, snapshot_id: str) -> ConsentSnapshot | None:
        merged = dict(self._state.snapshots)
        merged.update(self._pending_snapshots)
        return merged.get(snapshot_id)

    def outbox_by_id(self, event_id: str) -> ConsentOutboxEvent | None:
        merged = dict(self._state.outbox)
        merged.update(self._pending_outbox)
        return merged.get(event_id)

    def audit_by_id(self, audit_id: str) -> AuditEntry | None:
        merged = dict(self._state.audit)
        merged.update(self._pending_audit)
        return merged.get(audit_id)

    def get_idempotency(self, idempotency_key: str) -> IdempotencyRecord | None:
        merged = dict(self._state.idempotency)
        merged.update(self._pending_idempotency)
        return merged.get(idempotency_key)

    # -- writes (buffered) ----------------------------------------------------
    def append_offer(self, offer: ConsentOffer) -> None:
        key = (offer.offer_id, offer.version)
        if key in self._merged_offers():
            raise ConsentConflictError(
                f"offer version ({offer.offer_id}, {offer.version}) already exists"
            )
        self._pending_offers[key] = offer

    def append_consent(self, evidence: ConsentEvidence) -> None:
        key = (evidence.consent_id, evidence.version)
        if key in self._merged_consents():
            raise ConsentConflictError(
                f"consent version ({evidence.consent_id}, {evidence.version}) already exists"
            )
        self._pending_consents[key] = evidence

    def append_snapshot(self, snapshot: ConsentSnapshot) -> None:
        merged = dict(self._state.snapshots)
        merged.update(self._pending_snapshots)
        for existing in merged.values():
            if (
                existing.subject_id == snapshot.subject_id
                and existing.binding_id == snapshot.binding_id
                and existing.binding_version == snapshot.binding_version
                and existing.version == snapshot.version
            ):
                raise ConsentConflictError("snapshot version already exists for this binding")
        if snapshot.snapshot_id in merged:
            raise ConsentConflictError(f"snapshot {snapshot.snapshot_id!r} already exists")
        self._pending_snapshots[snapshot.snapshot_id] = snapshot

    def append_outbox(self, event: ConsentOutboxEvent) -> None:
        merged = dict(self._state.outbox)
        merged.update(self._pending_outbox)
        if event.event_id in merged:
            raise ConsentConflictError(f"outbox event {event.event_id!r} already exists")
        self._pending_outbox[event.event_id] = event

    def append_audit(self, entry: AuditEntry) -> None:
        merged = dict(self._state.audit)
        merged.update(self._pending_audit)
        if entry.audit_id in merged:
            raise ConsentConflictError(f"audit entry {entry.audit_id!r} already exists")
        self._pending_audit[entry.audit_id] = entry

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        merged = dict(self._state.idempotency)
        merged.update(self._pending_idempotency)
        if record.idempotency_key in merged:
            raise ConsentConflictError(f"idempotency key {record.idempotency_key!r} already exists")
        self._pending_idempotency[record.idempotency_key] = record

    # -- lifecycle ------------------------------------------------------------
    def commit(self) -> None:
        if self._closed:
            return
        self._state.offers.update(self._pending_offers)
        self._state.consents.update(self._pending_consents)
        self._state.snapshots.update(self._pending_snapshots)
        self._state.outbox.update(self._pending_outbox)
        self._state.audit.update(self._pending_audit)
        self._state.idempotency.update(self._pending_idempotency)
        self._closed = True
        self._lock.release()

    def rollback(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lock.release()

    def __enter__(self) -> ConsentUnitOfWork:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


__all__ = ["InMemoryConsentStore"]
