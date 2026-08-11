"""Shared store behavior: every backend must behave identically.

The same scenarios run against InMemory and SQLite: same-transaction
grant/revoke/dispute/snapshot/audit/outbox, stable event ids on idempotent
retries, monotonic versions, and rollback on mid-transaction failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.consent.authority import SubjectProof
from services.consent.evidence import (
    BindingEvidence,
    ConsentOffer,
    ConsentParams,
    RelationshipEvidence,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.consent.sqlite_store import SqliteConsentStore
from services.consent.store import (
    ConsentConflictError,
    ConsentStorePort,
    ConsentUnitOfWork,
)
from services.consent.tests.fakes import PersistingFixtureAuthority as ConsentAuthority
from services.consent.tests.test_evidence import base_offer


@pytest.fixture(params=["in_memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> ConsentStorePort:
    if request.param == "in_memory":
        return InMemoryConsentStore()
    return SqliteConsentStore(":memory:")


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def guardian_context(now: datetime, *, offer_id: str = "of_1") -> tuple[SubjectProof, ConsentOffer]:
    offer = ConsentOffer(
        offer_id=offer_id,
        capability="chat",
        subject_id="person_minor",
        actor_id="person_guardian",
        resource_owner_id="person_minor",
        purpose="user_request",
        params=ConsentParams(max_session_seconds=3600, retention_ttl_seconds=90 * 86400),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        created_at=now,
        policy_version="policy-cn-minor-v5",
    )
    subject = SubjectProof(
        subject_id="person_minor",
        subject_category="minor",
        age_evidence_status="verified",
    )
    return subject, offer


def grant_chat(
    authority: ConsentAuthority,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    offer_id: str = "of_1",
) -> object:
    subject, offer = guardian_context(now, offer_id=offer_id)
    binding = BindingEvidence(
        binding_id="bd_1",
        version=1,
        device_id="dev_1",
        status="active",
        declared_mode="parent_for_child",
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    relationship = RelationshipEvidence(
        relationship_id="rel_1",
        snapshot_id="rs_1",
        revision=1,
        relation_type="guardian_of",
        status="active",
        source_person_id="person_guardian",
        target_person_id="person_minor",
        binding_id="bd_1",
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    return authority.grant(
        offer,
        actor_kind="guardian",
        subject=subject,
        binding=binding,
        relationships=(relationship,),
        idempotency_key=idempotency_key,
        now=now,
    )


def test_replacement_reserves_distinct_supersede_and_active_revisions(
    store: ConsentStorePort,
) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    first = grant_chat(
        authority,
        now=now,
        idempotency_key="replacement-first",
        offer_id="offer-first",
    )
    second = grant_chat(
        authority,
        now=now + timedelta(minutes=1),
        idempotency_key="replacement-second",
        offer_id="offer-second",
    )
    assert first.evidence.version == 1  # type: ignore[attr-defined]
    assert second.evidence.version == 3  # type: ignore[attr-defined]
    assert second.outbox_event.version == 3  # type: ignore[attr-defined]
    assert second.snapshot.version == 2  # type: ignore[attr-defined]
    assert second.snapshot.snapshot_id != first.snapshot.snapshot_id  # type: ignore[attr-defined]

    with store.transaction() as uow:
        superseded = uow.get_consent(first.evidence.consent_id, 2)  # type: ignore[attr-defined]
        record = uow.get_idempotency("replacement-second")
        uow.commit()
    assert superseded is not None and superseded.status == "superseded"
    assert record is not None and record.version == 3


def test_offer_append_only_roundtrip_and_latest_version(store: ConsentStorePort) -> None:
    first = base_offer(offer_id="offer-store", version=1)
    second = base_offer(
        offer_id="offer-store",
        version=2,
        purpose="runtime_profile_issue",
        supersedes_offer_id="offer-store",
    )
    with store.transaction() as uow:
        uow.append_offer(first)
        uow.append_offer(second)
        uow.commit()

    with store.transaction() as uow:
        assert uow.offer_by_id("offer-store", 1) == first
        assert uow.offer_by_id("offer-store", 2) == second
        assert uow.latest_offer("offer-store") == second


def test_offer_duplicate_version_conflicts(store: ConsentStorePort) -> None:
    offer = base_offer(offer_id="offer-duplicate")
    with store.transaction() as uow:
        uow.append_offer(offer)
        uow.commit()
    with store.transaction() as uow:
        with pytest.raises(ConsentConflictError):
            uow.append_offer(offer)


def test_offer_rollback_is_atomic(store: ConsentStorePort) -> None:
    offer = base_offer(offer_id="offer-rollback")
    uow = store.transaction()
    uow.append_offer(offer)
    uow.rollback()
    with store.transaction() as read:
        assert read.latest_offer("offer-rollback") is None


def test_grant_writes_consent_snapshot_audit_outbox_in_one_transaction(
    store: ConsentStorePort,
) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    result = grant_chat(authority, now=now)

    with store.transaction() as uow:
        latest = uow.latest_consent(result.evidence.consent_id)
        assert latest is not None
        assert latest.version == 1
        assert latest.status == "active"
        assert uow.snapshot_by_id(result.snapshot.snapshot_id) is not None
        assert uow.outbox_by_id(result.outbox_event.event_id) is not None


def test_idempotent_retry_returns_same_event_id_and_evidence(
    store: ConsentStorePort,
) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    first = grant_chat(authority, now=now, idempotency_key="grant-chat-1")
    second = grant_chat(authority, now=now, idempotency_key="grant-chat-1")

    assert second.outbox_event.event_id == first.outbox_event.event_id
    assert second.evidence.evidence_id == first.evidence.evidence_id
    assert second.evidence.consent_id == first.evidence.consent_id
    assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert second.evidence == first.evidence
    # Replay must not write duplicate rows.
    with store.transaction() as uow:
        assert uow.outbox_by_id(first.outbox_event.event_id) is not None
        chains = uow.active_chains("person_minor", "bd_1", 1)
        assert len(chains) == 1


def test_same_idempotency_key_with_different_content_conflicts(
    store: ConsentStorePort,
) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    grant_chat(authority, now=now, idempotency_key="grant-chat-1")
    subject, offer = guardian_context(now)
    binding = BindingEvidence(
        binding_id="bd_1",
        version=1,
        device_id="dev_1",
        status="active",
        declared_mode="parent_for_child",
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    relationship = RelationshipEvidence(
        relationship_id="rel_1",
        snapshot_id="rs_1",
        revision=1,
        relation_type="guardian_of",
        status="active",
        source_person_id="person_guardian",
        target_person_id="person_minor",
        binding_id="bd_1",
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    different = ConsentOffer(
        offer_id="of_2",
        capability="tutor",
        subject_id="person_minor",
        actor_id="person_guardian",
        resource_owner_id="person_minor",
        purpose="runtime_profile_issue",
        params=ConsentParams(max_session_seconds=1800),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        created_at=now,
        policy_version="policy-cn-minor-v5",
    )
    with pytest.raises(ConsentConflictError):
        authority.grant(
            different,
            actor_kind="guardian",
            subject=subject,
            binding=binding,
            relationships=(relationship,),
            idempotency_key="grant-chat-1",
            now=now,
        )


def test_revoke_dispute_snapshot_outbox_same_transaction(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    grant = grant_chat(authority, now=now)

    revoked = authority.revoke(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(grant.snapshot.relationships[0],),
        now=now + timedelta(minutes=5),
    )
    assert revoked.evidence.status == "revoked"
    assert revoked.evidence.version == 2
    assert revoked.evidence.supersedes_consent_id is None
    assert revoked.evidence.superseded_by_consent_id is None
    assert revoked.outbox_event.aggregate_type == "consent_revoke"
    assert revoked.audit_entry.action == "revoke"

    with store.transaction() as uow:
        latest = uow.latest_consent(grant.evidence.consent_id)
        assert latest is not None and latest.status == "revoked"
        assert uow.outbox_by_id(revoked.outbox_event.event_id) is not None
        assert uow.snapshot_by_id(revoked.snapshot.snapshot_id) is not None
        # Old snapshot must be preserved (append-only).
        assert uow.snapshot_by_id(grant.snapshot.snapshot_id) is not None
        assert uow.latest_snapshot("person_minor", "bd_1", 1) is not None


def test_revoke_idempotent_retry_stable_event_id(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    grant = grant_chat(authority, now=now)
    first = authority.revoke(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(grant.snapshot.relationships[0],),
        now=now + timedelta(minutes=5),
        idempotency_key="revoke-chat-1",
    )
    second = authority.revoke(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(grant.snapshot.relationships[0],),
        now=now + timedelta(minutes=6),
        idempotency_key="revoke-chat-1",
    )
    assert second.outbox_event.event_id == first.outbox_event.event_id
    assert second.evidence == first.evidence


def test_rollback_on_mid_transaction_failure(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(_FailOnAuditStore(store))
    now = utc("2026-08-09T10:00:00+00:00")
    with pytest.raises(RuntimeError, match="audit write failed"):
        grant_chat(authority, now=now, idempotency_key="grant-chat-1")

    # Nothing may persist: consent rows, snapshot, outbox, idempotency.
    with store.transaction() as uow:
        assert uow.all_active_chains() == ()
        assert uow.get_idempotency("grant-chat-1") is None


def test_offer_audit_failure_rolls_back_offer_and_outbox(
    store: ConsentStorePort,
) -> None:
    authority = ConsentAuthority(_FailOnAuditStore(store, fail_action="offer_create"))
    now = utc("2026-08-09T10:00:00+00:00")
    with pytest.raises(RuntimeError, match="audit write failed"):
        authority.create_offer(
            offer_id="of_atomic_offer",
            capability="chat",
            subject_id="person_minor",
            actor_id="person_guardian",
            resource_owner_id="person_minor",
            purpose="user_request",
            params=ConsentParams(max_session_seconds=60),
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(minutes=1),
            created_at=now,
            policy_version="policy-v2",
        )
    with store.transaction() as uow:
        assert uow.latest_offer("of_atomic_offer") is None


class _FailOnAuditStore:
    """Wraps a store and raises inside the transaction before commit."""

    def __init__(self, inner: ConsentStorePort, *, fail_action: str = "grant") -> None:
        self._inner = inner
        self._fail_action = fail_action

    def transaction(self) -> ConsentUnitOfWork:
        return _FailOnAuditUow(self._inner.transaction(), self._fail_action)

    def close(self) -> None:
        self._inner.close()


class _FailOnAuditUow:
    def __init__(self, inner: ConsentUnitOfWork, fail_action: str) -> None:
        self._inner = inner
        self._fail_action = fail_action

    def latest_offer(self, offer_id: str) -> object:
        return self._inner.latest_offer(offer_id)

    def offer_by_id(self, offer_id: str, version: int) -> object:
        return self._inner.offer_by_id(offer_id, version)

    def lock_offer_head(self, offer_id: str, actor_id: str, subject_id: str) -> object:
        return self._inner.lock_offer_head(offer_id, actor_id, subject_id)

    def latest_consent(self, consent_id: str) -> object:
        return self._inner.latest_consent(consent_id)

    def get_consent(self, consent_id: str, version: int) -> object:
        return self._inner.get_consent(consent_id, version)

    def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> object:
        return self._inner.lock_consent_head(
            request_actor_id,
            evidence_actor_id,
            subject_id,
            binding_id,
            binding_version,
            capability,
            purpose,
        )

    def active_chains(self, subject_id: str, binding_id: str, binding_version: int) -> object:
        return self._inner.active_chains(subject_id, binding_id, binding_version)

    def all_active_chains(self) -> object:
        return self._inner.all_active_chains()

    def latest_snapshot(self, subject_id: str, binding_id: str, binding_version: int) -> object:
        return self._inner.latest_snapshot(subject_id, binding_id, binding_version)

    def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> object:
        return self._inner.lock_snapshot_head(
            request_actor_id, subject_id, binding_id, binding_version
        )

    def snapshot_by_id(self, snapshot_id: str) -> object:
        return self._inner.snapshot_by_id(snapshot_id)

    def outbox_by_id(self, event_id: str) -> object:
        return self._inner.outbox_by_id(event_id)

    def get_idempotency(self, idempotency_key: str) -> object:
        return self._inner.get_idempotency(idempotency_key)

    def append_consent(self, evidence: object) -> None:
        self._inner.append_consent(evidence)  # type: ignore[arg-type]

    def append_offer(self, offer: object) -> None:
        self._inner.append_offer(offer)  # type: ignore[arg-type]

    def append_snapshot(self, snapshot: object) -> None:
        self._inner.append_snapshot(snapshot)  # type: ignore[arg-type]

    def append_outbox(self, event: object) -> None:
        self._inner.append_outbox(event)  # type: ignore[arg-type]

    def append_audit(self, entry: object) -> None:
        if getattr(entry, "action", None) == self._fail_action:
            raise RuntimeError("audit write failed")
        self._inner.append_audit(entry)  # type: ignore[arg-type]

    def save_idempotency(self, record: object) -> None:
        self._inner.save_idempotency(record)  # type: ignore[arg-type]

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()


def test_snapshot_version_monotonic_per_binding(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    first = grant_chat(authority, now=now)
    assert first.snapshot.version == 1

    revoked = authority.revoke(
        first.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(first.snapshot.relationships[0],),
        now=now + timedelta(minutes=5),
    )
    assert revoked.snapshot.version == 2
    assert revoked.snapshot.snapshot_id != first.snapshot.snapshot_id

    # A later binding version starts a fresh snapshot sequence; old snapshots remain.
    binding_v2 = BindingEvidence(
        binding_id="bd_1",
        version=2,
        device_id="dev_1",
        status="active",
        declared_mode="parent_for_child",
        valid_from=now + timedelta(minutes=10),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    subject, offer = guardian_context(now + timedelta(minutes=10), offer_id="of_binding_v2")
    relationship_v2 = RelationshipEvidence(
        relationship_id="rel_1",
        snapshot_id="rs_1",
        revision=2,
        relation_type="guardian_of",
        status="active",
        source_person_id="person_guardian",
        target_person_id="person_minor",
        binding_id="bd_1",
        valid_from=now + timedelta(minutes=10),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    granted_v2 = authority.grant(
        offer,
        actor_kind="guardian",
        subject=subject,
        binding=binding_v2,
        relationships=(relationship_v2,),
        now=now + timedelta(minutes=10),
    )
    assert granted_v2.snapshot.binding_version == 2
    assert granted_v2.snapshot.version == 1
    with store.transaction() as uow:
        assert uow.snapshot_by_id(first.snapshot.snapshot_id) is not None
        assert uow.snapshot_by_id(revoked.snapshot.snapshot_id) is not None


def test_append_duplicate_version_conflicts(store: ConsentStorePort) -> None:
    from services.consent.evidence import ConsentEvidence

    now = utc("2026-08-09T10:00:00+00:00")
    _, offer = guardian_context(now)
    evidence = ConsentEvidence(
        consent_id="c_dup",
        version=1,
        snapshot_id="snap_dup",
        status="active",
        subject_id="person_minor",
        resource_owner_id="person_minor",
        actor_id="person_guardian",
        actor_kind="guardian",
        device_id="dev_1",
        binding_id="bd_1",
        binding_version=1,
        capability="chat",
        purpose="user_request",
        policy_version="policy-cn-minor-v5",
        evidence_id="ev_dup",
        offer_id=offer.offer_id,
        idempotency_key=None,
        params=offer.params,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        supersedes_consent_id=None,
        superseded_by_consent_id=None,
        canonical_hash="",
    )
    with store.transaction() as uow:
        uow.append_consent(evidence)
        uow.commit()
    with store.transaction() as uow:
        with pytest.raises(ConsentConflictError):
            uow.append_consent(evidence)


def test_roundtrip_preserves_evidence_and_snapshot(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    result = grant_chat(authority, now=now)
    with store.transaction() as uow:
        stored_evidence = uow.get_consent(result.evidence.consent_id, result.evidence.version)
        stored_snapshot = uow.snapshot_by_id(result.snapshot.snapshot_id)
        stored_event = uow.outbox_by_id(result.outbox_event.event_id)
    assert stored_evidence == result.evidence
    assert stored_snapshot == result.snapshot
    assert stored_event == result.outbox_event
