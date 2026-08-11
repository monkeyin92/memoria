"""Concurrency semantics: last-write-wins by monotonic version, stale conflicts."""

from __future__ import annotations

import threading
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
from services.consent.store import ConsentConflictError, ConsentStorePort
from services.consent.tests.fakes import PersistingFixtureAuthority as ConsentAuthority


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@pytest.fixture(params=["in_memory", "sqlite"])
def store(request: pytest.FixtureRequest) -> ConsentStorePort:
    if request.param == "in_memory":
        return InMemoryConsentStore()
    return SqliteConsentStore(":memory:")


def make_context(now: datetime) -> tuple[SubjectProof, BindingEvidence, RelationshipEvidence]:
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
    subject = SubjectProof(
        subject_id="person_minor",
        subject_category="minor",
        age_evidence_status="verified",
    )
    return subject, binding, relationship


def make_offer(now: datetime, *, offer_id: str = "of_1") -> ConsentOffer:
    return ConsentOffer(
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


def grant_chat(
    authority: ConsentAuthority,
    now: datetime,
    *,
    offer_id: str = "of_1",
) -> object:
    subject, binding, relationship = make_context(now)
    return authority.grant(
        make_offer(now, offer_id=offer_id),
        actor_kind="guardian",
        subject=subject,
        binding=binding,
        relationships=(relationship,),
        now=now,
    )


def test_last_write_wins_grant_supersedes_previous(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    first = grant_chat(authority, now, offer_id="of_first")
    second = grant_chat(authority, now + timedelta(minutes=1), offer_id="of_second")

    assert second.evidence.consent_id != first.evidence.consent_id
    assert second.evidence.supersedes_consent_id == first.evidence.consent_id
    with store.transaction() as uow:
        chains = uow.active_chains("person_minor", "bd_1", 1)
        assert len(chains) == 1
        assert chains[0].consent_id == second.evidence.consent_id
        old_latest = uow.latest_consent(first.evidence.consent_id)
        assert old_latest is not None
        assert old_latest.status == "superseded"
        assert old_latest.version == 2
        assert old_latest.superseded_by_consent_id == second.evidence.consent_id


def test_stale_version_revoke_conflicts(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    grant = grant_chat(authority, now)
    subject, binding, relationship = make_context(now)

    authority.revoke(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(relationship,),
        now=now + timedelta(minutes=1),
    )
    # Operating on an outdated version must conflict.
    with pytest.raises(ConsentConflictError):
        authority.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship,),
            expected_version=1,
            now=now + timedelta(minutes=2),
        )
    # The latest version is already revoked -> conflict again.
    with pytest.raises(ConsentConflictError):
        authority.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship,),
            now=now + timedelta(minutes=3),
        )


def test_revoke_superseded_chain_conflicts(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    first = grant_chat(authority, now, offer_id="of_first")
    grant_chat(authority, now + timedelta(minutes=1), offer_id="of_second")
    _, _, relationship = make_context(now)

    with pytest.raises(ConsentConflictError):
        authority.revoke(
            first.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship,),
            now=now + timedelta(minutes=2),
        )


def test_dispute_then_revoke_conflicts(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    grant = grant_chat(authority, now)
    _, _, relationship = make_context(now)
    authority.dispute(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(relationship,),
        reason="监护人争议",
        now=now + timedelta(minutes=1),
    )
    with pytest.raises(ConsentConflictError):
        authority.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship,),
            now=now + timedelta(minutes=2),
        )


def test_concurrent_grants_last_write_wins(store: ConsentStorePort) -> None:
    """Two racing grants of the same capability: exactly one active chain remains."""
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []
    results: list[object] = []

    def worker(offer_id: str, offset: int) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                grant_chat(authority, now + timedelta(minutes=offset), offer_id=offer_id)
            )
        except Exception as exc:  # noqa: BLE001 - test worker boundary
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("of_a", 0)),
        threading.Thread(target=worker, args=("of_b", 1)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    with store.transaction() as uow:
        chains = uow.active_chains("person_minor", "bd_1", 1)
        assert len(chains) == 1
        active = chains[0]
        assert active.offer_id in {"of_a", "of_b"}
        # The superseded chain's latest version must be recorded, versions monotonic.
        other_offer_id = "of_b" if active.offer_id == "of_a" else "of_a"
        superseded_latest = None
        for result in results:
            consent_id = result.evidence.consent_id
            if result.evidence.offer_id == other_offer_id:
                superseded_latest = uow.latest_consent(consent_id)
        assert superseded_latest is not None
        assert superseded_latest.status == "superseded"


def test_expire_creates_new_version_without_overwriting(store: ConsentStorePort) -> None:
    authority = ConsentAuthority(store)
    now = utc("2026-08-09T10:00:00+00:00")
    offer = ConsentOffer(
        offer_id="of_short",
        capability="chat",
        subject_id="person_minor",
        actor_id="person_guardian",
        resource_owner_id="person_minor",
        purpose="user_request",
        params=ConsentParams(),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(minutes=30),
        created_at=now,
        policy_version="policy-cn-minor-v5",
    )
    subject, binding, relationship = make_context(now)
    grant = authority.grant(
        offer,
        actor_kind="guardian",
        subject=subject,
        binding=binding,
        relationships=(relationship,),
        now=now,
    )
    expired = authority.expire_due(now + timedelta(hours=1))
    assert len(expired) == 1
    result = expired[0]
    assert result.evidence.consent_id == grant.evidence.consent_id
    assert result.evidence.status == "expired"
    assert result.evidence.version == 2
    assert result.outbox_event.aggregate_type == "consent_expire"
    with store.transaction() as uow:
        latest = uow.latest_consent(grant.evidence.consent_id)
        assert latest is not None and latest.status == "expired"
        assert uow.active_chains("person_minor", "bd_1", 1) == ()
    # Running again must be a no-op (natural idempotency).
    assert authority.expire_due(now + timedelta(hours=2)) == ()
