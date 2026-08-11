"""Lifecycle extension contracts: delegation fail-closed cascades, two-party
dispute resolution, role-permission narrowing and the ownership transfer
intent flow (acceptance items 2/3/4/5/8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.identity.authority import DeterministicConsentSnapshotResolver
from services.identity.domain import (
    IdentityConflictError,
    IdentityNotFoundError,
    RelationshipLifecycleError,
    RoleConstraintError,
)
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import IdentityService
from services.identity.sqlite_store import SqliteIdentityStore
from services.identity.testing_authorities import TestTransferAuthority


@pytest.fixture(params=["memory", "sqlite"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> InMemoryIdentityStore | SqliteIdentityStore:
    if request.param == "memory":
        return InMemoryIdentityStore()
    return SqliteIdentityStore(tmp_path / "identity.sqlite3")


@pytest.fixture
def service(
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> IdentityService:
    return IdentityService(
        store,
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )


def _now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


_TEST_AUTHORITY = TestTransferAuthority(
    b"test-secret-that-is-at-least-32-bytes"
)


def _receipt(
    *,
    actor: str,
    subject: str,
    device: str,
    binding_id: str,
    binding_version: int,
    expires_at: datetime,
    nonce: str = "step-up-1",
) -> str:
    return _TEST_AUTHORITY.mint(
        actor_person_id=actor,
        subject_person_id=subject,
        device_id=device,
        binding_id=binding_id,
        binding_version=binding_version,
        expires_at=expires_at,
        nonce=nonce,
    )


async def _adult(service: IdentityService, name: str, now: datetime) -> str:
    return (
        await service.register_person(
            display_name=name,
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id=f"evidence-{name}",
            now=now,
        )
    ).person_id


async def _active_delegation(
    service: IdentityService,
    *,
    source: str,
    target: str,
    permissions: frozenset[str] = frozenset({"delegate.access"}),
    parent_id: str | None = None,
    can_delegate: bool = False,
    now: datetime,
) -> str:
    proposed = await service.propose_relationship(
        source_person_id=source,
        target_person_id=target,
        relation_type="delegate_for",
        established_evidence_id=f"evidence-{source}-{target}",
        permissions=permissions,
        can_delegate=can_delegate,
        delegated_from_relationship_id=parent_id,
        actor_person_id=source,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id, person_id=source, now=now
    )
    active = await service.confirm_relationship(
        relationship_id=proposed.relationship_id, person_id=target, now=now
    )
    assert active.status == "active"
    return active.relationship_id


@pytest.mark.asyncio
async def test_delegation_permissions_must_be_subset_of_parent(
    service: IdentityService,
) -> None:
    now = _now()
    a, b, c = await _adult(service, "甲", now), await _adult(service, "乙", now), await _adult(service, "丙", now)
    parent_id = await _active_delegation(
        service,
        source=a,
        target=b,
        permissions=frozenset({"delegate.access"}),
        can_delegate=True,
        now=now,
    )
    with pytest.raises(RelationshipLifecycleError, match="subset"):
        await service.propose_relationship(
            source_person_id=b,
            target_person_id=c,
            relation_type="delegate_for",
            established_evidence_id="evidence-b-c-wide",
            permissions=frozenset({"delegate.access", "content.read"}),
            can_delegate=False,
            delegated_from_relationship_id=parent_id,
            actor_person_id=b,
            now=now,
        )


@pytest.mark.asyncio
async def test_parent_revoke_cascades_children_fail_closed(
    service: IdentityService,
) -> None:
    now = _now()
    a, b, c = await _adult(service, "甲", now), await _adult(service, "乙", now), await _adult(service, "丙", now)
    parent_id = await _active_delegation(
        service, source=a, target=b, can_delegate=True, now=now
    )
    child_id = await _active_delegation(
        service, source=b, target=c, parent_id=parent_id, now=now
    )
    await service.revoke_relationship(
        relationship_id=parent_id,
        actor_person_id=a,
        evidence_id="evidence-revoke-parent",
        now=now + timedelta(minutes=1),
    )
    relationships = await service.list_relationships(person_id=c)
    child = next(item for item in relationships if item.relationship_id == child_id)
    assert child.status == "revoked"
    assert child.revocation_evidence_id == f"parent:{parent_id}:revoked"


@pytest.mark.asyncio
async def test_parent_dispute_suspends_and_both_party_resolution_restores(
    service: IdentityService,
) -> None:
    now = _now()
    a, b, c = await _adult(service, "甲", now), await _adult(service, "乙", now), await _adult(service, "丙", now)
    parent_id = await _active_delegation(
        service, source=a, target=b, can_delegate=True, now=now
    )
    child_id = await _active_delegation(
        service, source=b, target=c, parent_id=parent_id, now=now
    )
    await service.dispute_relationship(
        relationship_id=parent_id,
        actor_person_id=a,
        reason="争议",
        now=now + timedelta(minutes=1),
    )
    child = next(
        item
        for item in await service.list_relationships(person_id=c)
        if item.relationship_id == child_id
    )
    assert child.status == "suspended"
    assert child.auto_suspended is True
    # A single side cannot restore the parent.
    one_side = await service.acknowledge_dispute_resolution(
        relationship_id=parent_id, person_id=a, now=now + timedelta(minutes=2)
    )
    assert one_side.status == "disputed"
    restored = await service.acknowledge_dispute_resolution(
        relationship_id=parent_id, person_id=b, now=now + timedelta(minutes=3)
    )
    assert restored.status == "active"
    child = next(
        item
        for item in await service.list_relationships(person_id=c)
        if item.relationship_id == child_id
    )
    assert child.status == "active"
    assert child.auto_suspended is False
    with pytest.raises(RelationshipLifecycleError, match="disputed"):
        await service.acknowledge_dispute_resolution(
            relationship_id=parent_id, person_id=a, now=now + timedelta(minutes=4)
        )


@pytest.mark.asyncio
async def test_parent_expire_cascades_children_revoked(
    service: IdentityService,
) -> None:
    now = _now()
    a, b, c = await _adult(service, "甲", now), await _adult(service, "乙", now), await _adult(service, "丙", now)
    proposed = await service.propose_relationship(
        source_person_id=a,
        target_person_id=b,
        relation_type="delegate_for",
        established_evidence_id="evidence-expiring",
        permissions=frozenset({"delegate.access"}),
        can_delegate=True,
        valid_until=now + timedelta(days=30),
        actor_person_id=a,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id, person_id=a, now=now
    )
    parent = await service.confirm_relationship(
        relationship_id=proposed.relationship_id, person_id=b, now=now
    )
    parent_id = parent.relationship_id
    child_id = await _active_delegation(
        service, source=b, target=c, parent_id=parent_id, now=now
    )
    assert await service.expire_relationships(now=now + timedelta(days=31)) == 1
    child = next(
        item
        for item in await service.list_relationships(person_id=c)
        if item.relationship_id == child_id
    )
    assert child.status == "revoked"
    assert child.revocation_evidence_id == f"parent:{parent_id}:expired"


@pytest.mark.asyncio
async def test_role_permissions_cannot_widen_defaults(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    with pytest.raises(RoleConstraintError, match="subset"):
        await service.create_binding(
            device_id="dev-widen",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            role_permissions={
                (owner, "account_owner"): frozenset(
                    {"binding.manage", "content.read"}
                )
            },
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
    # Narrowing remains allowed.
    manifest = await service.create_binding(
        device_id="dev-narrow",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        role_permissions={
            (owner, "account_owner"): frozenset({"device.status.view"})
        },
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    assert manifest.roles[0].permissions == ("device.status.view",)


@pytest.mark.asyncio
async def test_transfer_cancel_and_expire(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    target = await _adult(service, "新主人", now)
    manifest = await service.create_binding(
        device_id="dev-tx",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    policy_id, step_up = _receipt(
        actor=owner,
        subject=target,
        device="dev-tx",
        binding_id=manifest.binding_id,
        binding_version=manifest.binding_version,
        expires_at=now + timedelta(days=8),
    )
    intent = await service.create_transfer_intent(
        device_id="dev-tx",
        to_account_owner_person_id=target,
        step_up_evidence_id=step_up,
        policy_receipt_id=policy_id,
        idempotency_key=str(uuid.uuid4()),
        valid_until=now + timedelta(days=7),
        actor_person_id=owner,
        now=now,
    )
    with pytest.raises(IdentityConflictError, match="already pending"):
        second_policy_id, second_step_up = _receipt(
            actor=owner,
            subject=target,
            device="dev-tx",
            binding_id=manifest.binding_id,
            binding_version=manifest.binding_version,
            expires_at=now + timedelta(days=8),
            nonce="step-up-2",
        )
        await service.create_transfer_intent(
            device_id="dev-tx",
            to_account_owner_person_id=target,
            step_up_evidence_id=second_step_up,
            policy_receipt_id=second_policy_id,
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=owner,
            now=now,
        )
    cancel_key = str(uuid.uuid4())
    cancelled = await service.cancel_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=owner,
        reason="changed my mind",
        now=now + timedelta(minutes=1),
        idempotency_key=cancel_key,
    )
    assert cancelled.status == "cancelled"
    # Terminal replay: a retried cancel returns the same cancelled intent.
    replayed_cancel = await service.cancel_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=owner,
        reason="changed my mind",
        now=now + timedelta(minutes=2),
        idempotency_key=cancel_key,
    )
    assert replayed_cancel.transfer_id == cancelled.transfer_id
    assert replayed_cancel.status == "cancelled"
    second_policy_id, second_step_up = _receipt(
        actor=owner,
        subject=target,
        device="dev-tx",
        binding_id=manifest.binding_id,
        binding_version=manifest.binding_version,
        expires_at=now + timedelta(days=2),
        nonce="step-up-3",
    )
    second = await service.create_transfer_intent(
        device_id="dev-tx",
        to_account_owner_person_id=target,
        step_up_evidence_id=second_step_up,
        policy_receipt_id=second_policy_id,
        idempotency_key=str(uuid.uuid4()),
        valid_until=now + timedelta(days=1),
        actor_person_id=owner,
        now=now + timedelta(minutes=3),
    )
    assert await service.expire_transfer_intents(
        now=now + timedelta(days=2)
    ) == 1
    with pytest.raises(IdentityConflictError, match="expired"):
        await service.accept_transfer_intent(
            transfer_id=second.transfer_id,
            actor_person_id=target,
            primary_subject_ids=(target,),
            declared_mode="self_use",
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(days=2, minutes=1),
            idempotency_key=str(uuid.uuid4()),
        )


@pytest.mark.asyncio
async def test_transfer_conflicts_when_binding_changed(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    target = await _adult(service, "新主人", now)
    manifest = await service.create_binding(
        device_id="dev-tx2",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    policy_id, step_up = _receipt(
        actor=owner,
        subject=target,
        device="dev-tx2",
        binding_id=manifest.binding_id,
        binding_version=manifest.binding_version,
        expires_at=now + timedelta(days=8),
    )
    intent = await service.create_transfer_intent(
        device_id="dev-tx2",
        to_account_owner_person_id=target,
        step_up_evidence_id=step_up,
        policy_receipt_id=policy_id,
        idempotency_key=str(uuid.uuid4()),
        actor_person_id=owner,
        now=now,
    )
    await service.supersede_binding(
        device_id="dev-tx2",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v2",
        policy_bundle_version="policy-self-v2",
        actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    with pytest.raises(IdentityConflictError, match="changed"):
        await service.accept_transfer_intent(
            transfer_id=intent.transfer_id,
            actor_person_id=target,
            primary_subject_ids=(target,),
            declared_mode="self_use",
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(minutes=2),
            idempotency_key=str(uuid.uuid4()),
        )
    stored = await service.get_transfer_intent(
        intent.transfer_id, actor_person_id=target
    )
    assert stored.status == "conflicted"


@pytest.mark.asyncio
async def test_transfer_outbox_events_and_idempotent_accept(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    target = await _adult(service, "新主人", now)
    manifest = await service.create_binding(
        device_id="dev-tx3",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    policy_id, step_up = _receipt(
        actor=owner,
        subject=target,
        device="dev-tx3",
        binding_id=manifest.binding_id,
        binding_version=manifest.binding_version,
        expires_at=now + timedelta(days=8),
    )
    intent = await service.create_transfer_intent(
        device_id="dev-tx3",
        to_account_owner_person_id=target,
        step_up_evidence_id=step_up,
        policy_receipt_id=policy_id,
        idempotency_key=str(uuid.uuid4()),
        actor_person_id=owner,
        now=now,
    )
    accept_key = str(uuid.uuid4())
    accepted = await service.accept_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=target,
        primary_subject_ids=(target,),
        declared_mode="self_use",
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=1),
        idempotency_key=accept_key,
    )
    replayed = await service.accept_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=target,
        primary_subject_ids=(target,),
        declared_mode="self_use",
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=2),
        idempotency_key=accept_key,
    )
    assert replayed.binding_id == accepted.binding_id
    if isinstance(store, InMemoryIdentityStore):
        topics = {event.topic for event in store.outbox_events()}
        assert "identity.transfer.created" in topics
        assert "identity.transfer.accepted" in topics
        assert "identity.binding.transferred" in topics
        payloads = {
            event.payload.get("binding_version") for event in store.outbox_events()
            if event.topic == "identity.binding.transferred"
        }
        assert payloads == {2}


@pytest.mark.asyncio
async def test_atomic_audit_outbox_on_failed_supersede(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    await service.create_binding(
        device_id="dev-atomic",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    # A revocation makes the next supersede fail; no audit/outbox may leak.
    await service.revoke_binding(
        device_id="dev-atomic",
        actor_person_id=owner,
        reason="unbind",
        now=now + timedelta(minutes=1),
    )
    with pytest.raises(IdentityNotFoundError, match="no active binding"):
        await service.supersede_binding(
            device_id="dev-atomic",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v2",
            policy_bundle_version="policy-self-v2",
            actor_person_id=owner,
            now=now + timedelta(minutes=2),
        )
    if isinstance(store, InMemoryIdentityStore):
        topics = [event.topic for event in store.outbox_events()]
        assert "identity.binding.superseded" not in topics


@pytest.mark.asyncio
async def test_concurrent_transfer_accepts_are_monotonic(
    tmp_path: Path,
) -> None:
    """Two concurrent accepts: both converge on the same monotonic version
    (idempotent replay), no duplicate binding version is ever issued."""
    store = SqliteIdentityStore(tmp_path / "identity-concurrent.sqlite3")
    service = IdentityService(
        store,
        consent_resolver=DeterministicConsentSnapshotResolver(),
        transfer_verifier=_TEST_AUTHORITY,
    )
    now = _now()
    owner = await _adult(service, "本人", now)
    target = await _adult(service, "新主人", now)
    manifest = await service.create_binding(
        device_id="dev-race",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    policy_id, step_up = _receipt(
        actor=owner,
        subject=target,
        device="dev-race",
        binding_id=manifest.binding_id,
        binding_version=manifest.binding_version,
        expires_at=now + timedelta(days=8),
    )
    intent = await service.create_transfer_intent(
        device_id="dev-race",
        to_account_owner_person_id=target,
        step_up_evidence_id=step_up,
        policy_receipt_id=policy_id,
        idempotency_key=str(uuid.uuid4()),
        actor_person_id=owner,
        now=now,
    )

    accept_key = str(uuid.uuid4())

    async def accept() -> int:
        manifest = await service.accept_transfer_intent(
            transfer_id=intent.transfer_id,
            actor_person_id=target,
            primary_subject_ids=(target,),
            declared_mode="self_use",
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(minutes=1),
            idempotency_key=accept_key,
        )
        return manifest.binding_version

    results = await asyncio.gather(accept(), accept(), return_exceptions=True)
    assert results == [2, 2]
    versions = await service.list_binding_versions("dev-race")
    assert [item.binding_version for item in versions] == [1, 2]
    stored = await service.get_transfer_intent(
        intent.transfer_id, actor_person_id=target
    )
    assert stored.status == "accepted"
