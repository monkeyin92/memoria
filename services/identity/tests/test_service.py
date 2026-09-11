"""IdentityService behavior across the in-memory and SQLite adapters."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.identity.domain import (
    ROLE_DEFAULT_PERMISSIONS,
    AgeEvidenceError,
    BindingManifest,
    CustomPersonaLimitError,
    DeviceBinding,
    DeviceBindingRole,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    ModeConstraintError,
    RelationshipLifecycleError,
    RoleConstraintError,
    StructuredPersonaFields,
    TransferLifecycleError,
    effective_permissions,
    manifest_from_dict,
)
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import IdentityService
from services.identity.sqlite_store import SqliteIdentityStore
from services.identity.testing_authorities import (
    TestTransferAuthority,
    make_test_service,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> InMemoryIdentityStore | SqliteIdentityStore:
    if request.param == "memory":
        return InMemoryIdentityStore()
    return SqliteIdentityStore(tmp_path / "identity.sqlite3")


@pytest.fixture
def service(store: InMemoryIdentityStore | SqliteIdentityStore) -> IdentityService:
    from services.identity.authority import DeterministicConsentSnapshotResolver

    return IdentityService(
        store,
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )


def _now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_registration_reconciliation_is_audited_and_idempotent(
    service: IdentityService, store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    original = await service.register_person(
        person_id="old-owner", display_name="主人", timezone="Asia/Shanghai", now=_now(),
    )
    evidence = "control-wechat-phone-v1:" + "a" * 64
    updated = await service.reconcile_account_registration(
        person_id=original.person_id, evidence_id=evidence, source_revision=1, now=_now(),
    )
    assert updated.created_at == original.created_at
    assert updated.updated_at > original.updated_at
    assert (updated.subject_category, updated.age_band, updated.age_evidence_status) == (
        "adult", "adult", "verified"
    )
    assert await service.reconcile_account_registration(
        person_id=original.person_id, evidence_id=evidence, source_revision=1,
        now=_now() + timedelta(days=1),
    ) == updated
    if isinstance(store, InMemoryIdentityStore):
        audits = [event for event in store._audit if event.action == "person.registration_reconciled"]
        assert len(audits) == 1
        assert len([event for event in store._outbox.values() if event.event_id == audits[0].event_id]) == 1
        payload = audits[0].payload
    else:
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM identity_audit_events WHERE action = 'person.registration_reconciled'",
            ).fetchall()
            assert len(rows) == 1
            assert connection.execute(
                "SELECT count(*) FROM identity_outbox WHERE event_id = ?", (rows[0]["event_id"],),
            ).fetchone()[0] == 1
            payload = json.loads(rows[0]["payload_json"])
    assert payload["source"] == "control.wechat_phone_registration.v1"
    assert payload["evidence_id"] == evidence
    assert payload["source_profile_revision"] == 1
    assert payload["previous_subject_category"] == "unknown"
    assert "verifier_person_id" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["minor", "disputed", "disabled"])
async def test_registration_reconciliation_does_not_override_protected_subjects(
    service: IdentityService, store: InMemoryIdentityStore | SqliteIdentityStore,
    protected: str,
) -> None:
    original = await service.register_person(
        person_id="protected", display_name="不可提升", timezone="Asia/Shanghai", now=_now(),
    )
    person = (
        replace(original, subject_category="minor", age_band="under_14") if protected == "minor"
        else replace(original, age_evidence_status="disputed") if protected == "disputed"
        else replace(original, status="disabled")
    )
    await store.save_person(person)
    with pytest.raises(IdentityConflictError):
        await service.reconcile_account_registration(
            person_id=person.person_id, evidence_id="control-wechat-phone-v1:" + "b" * 64,
            source_revision=1, now=_now(),
        )
    assert await service.get_person(person.person_id, actor_person_id=person.person_id) == person


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence,revision", [("", 1), ("self-asserted-adult", 1), ("control-wechat-phone-v1:" + "a" * 64, 0)])
async def test_registration_reconciliation_requires_policy_receipt(
    service: IdentityService, evidence: str, revision: int,
) -> None:
    with pytest.raises(AgeEvidenceError):
        await service.reconcile_account_registration(
            person_id="owner", evidence_id=evidence, source_revision=revision, now=_now(),
        )


_TEST_AUTHORITY = TestTransferAuthority(b"test-secret-that-is-at-least-32-bytes")


@pytest.mark.asyncio
async def test_register_exact_replay_returns_persisted_object(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    now = _now()
    first = await service.register_person(
        display_name="幂等",
        timezone="Asia/Shanghai",
        subject_category="unknown",
        age_band="unknown",
        age_evidence_status="unverified",
        now=now,
    )
    replayed = await service.register_person(
        display_name="幂等",
        timezone="Asia/Shanghai",
        subject_category="unknown",
        age_band="unknown",
        age_evidence_status="unverified",
        person_id=first.person_id,
        now=now + timedelta(days=1),
    )
    # The replay returns the persisted object, not the request snapshot.
    assert replayed.person_id == first.person_id
    if isinstance(store, InMemoryIdentityStore):
        persisted = store._persons[first.person_id]
        assert replayed is persisted
    else:
        stored = await service.get_person(
            first.person_id, actor_person_id=first.person_id
        )
        assert replayed.display_name == stored.display_name
        assert replayed.created_at == stored.created_at
    assert replayed.created_at == first.created_at
    assert replayed.display_name == first.display_name


@pytest.mark.asyncio
async def test_register_content_drift_conflicts(
    service: IdentityService,
) -> None:
    now = _now()
    person_id = (
        await service.register_person(
            display_name="原名",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            now=now,
        )
    ).person_id
    with pytest.raises(IdentityConflictError):
        await service.register_person(
            display_name="改名",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=person_id,
            now=now,
        )
    with pytest.raises(IdentityConflictError):
        await service.register_person(
            display_name="原名",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="evidence-drift",
            person_id=person_id,
            now=now,
        )


@pytest.mark.asyncio
async def test_register_concurrent_exact_replay_and_drift(
    service: IdentityService,
) -> None:
    import asyncio

    now = _now()
    same_id = f"conc-{uuid.uuid4().hex[:8]}"
    first, second = await asyncio.gather(
        service.register_person(
            display_name="并发",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=same_id,
            now=now,
        ),
        service.register_person(
            display_name="并发",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=same_id,
            now=now,
        ),
    )
    assert first.person_id == second.person_id == same_id
    assert first.display_name == second.display_name == "并发"
    drift_id = f"drift-{uuid.uuid4().hex[:8]}"
    results = await asyncio.gather(
        service.register_person(
            display_name="甲",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=drift_id,
            now=now,
        ),
        service.register_person(
            display_name="乙",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=drift_id,
            now=now,
        ),
        return_exceptions=True,
    )
    assert sorted(type(result).__name__ for result in results) == [
        "IdentityConflictError",
        "PersonSubject",
    ]


def _receipt(
    *,
    actor: str,
    subject: str,
    device: str,
    binding_id: str,
    binding_version: int,
    expires_at: datetime,
    nonce: str = "step-up-1",
) -> tuple[str, str]:
    return _TEST_AUTHORITY.mint(
        actor_person_id=actor,
        subject_person_id=subject,
        device_id=device,
        binding_id=binding_id,
        binding_version=binding_version,
        expires_at=expires_at,
        nonce=nonce,
    )


async def _register(
    service: IdentityService,
    *,
    name: str,
    category: str = "unknown",
    age_band: str = "unknown",
    evidence: str = "unverified",
    evidence_id: str | None = None,
    now: datetime,
) -> str:
    person = await service.register_person(
        display_name=name,
        timezone="Asia/Shanghai",
        subject_category=category,
        age_band=age_band,
        age_evidence_status=evidence,
        age_evidence_id=(
            evidence_id
            if evidence_id is not None
            else f"evidence-{name}" if category == "adult" else None
        ),
        now=now,
    )
    return person.person_id


async def _establish_guardian(
    service: IdentityService,
    *,
    guardian: str,
    ward: str,
    now: datetime,
) -> str:
    """Two-party confirmed guardian_of relationship (binding prerequisite)."""
    proposed = await service.propose_relationship(
        source_person_id=guardian,
        target_person_id=ward,
        relation_type="guardian_of",
        established_evidence_id="evidence-guardian",
        actor_person_id=guardian,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=guardian,
        now=now,
    )
    active = await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=ward,
        now=now,
    )
    return active.relationship_id


async def _establish_relationship(
    service: IdentityService,
    *,
    relation_type: str,
    source: str,
    target: str,
    now: datetime,
) -> str:
    proposed = await service.propose_relationship(
        source_person_id=source,
        target_person_id=target,
        relation_type=relation_type,  # type: ignore[arg-type]
        established_evidence_id=f"evidence-{relation_type}",
        actor_person_id=source,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=source,
        now=now,
    )
    active = await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=target,
        now=now,
    )
    return active.relationship_id


async def _adult(service: IdentityService, name: str, now: datetime) -> str:
    return await _register(
        service,
        name=name,
        category="adult",
        age_band="adult",
        evidence="verified",
        now=now,
    )


async def _minor(service: IdentityService, name: str, now: datetime) -> str:
    return await _register(
        service,
        name=name,
        category="minor",
        age_band="under_14",
        evidence="verified",
        now=now,
    )


# ---------------------------------------------------------------------------
# 成年 / 未知
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_registration_never_defaults_to_adult(service: IdentityService) -> None:
    now = _now()
    person = await service.register_person(
        display_name="朋友", timezone="Asia/Shanghai", now=now
    )
    assert person.subject_category == "unknown"
    assert person.age_band == "unknown"
    assert person.age_evidence_status == "unverified"


@pytest.mark.asyncio
async def test_adult_claim_requires_verified_evidence(service: IdentityService) -> None:
    now = _now()
    with pytest.raises(AgeEvidenceError):
        await _register(
            service,
            name="无证据成人",
            category="adult",
            age_band="adult",
            evidence="unverified",
            now=now,
        )


@pytest.mark.asyncio
async def test_age_declaration_cannot_claim_adult_or_verified(
    service: IdentityService,
) -> None:
    now = _now()
    person_id = await _register(
        service,
        name="声明者",
        category="unknown",
        age_band="unknown",
        evidence="unverified",
        now=now,
    )
    # Ordinary declarations can never claim adult.
    with pytest.raises(AgeEvidenceError):
        await service.declare_age_evidence(
            person_id=person_id,
            age_band="adult",
            actor_person_id=person_id,
            now=now + timedelta(days=1),
        )
    # A minor declaration succeeds with at most unverified evidence.
    declared = await service.declare_age_evidence(
        person_id=person_id,
        age_band="14_17",
        actor_person_id=person_id,
        now=now + timedelta(days=1),
    )
    assert declared.subject_category == "minor"
    assert declared.age_band == "14_17"
    assert declared.age_evidence_status == "unverified"


@pytest.mark.asyncio
async def test_age_verification_requires_verified_adult_verifier_and_evidence(
    service: IdentityService,
) -> None:
    now = _now()
    verifier = await _register(
        service,
        name="权威核验人",
        category="adult",
        age_band="adult",
        evidence="verified",
        now=now,
    )
    target = await _register(
        service,
        name="待核验",
        category="unknown",
        age_band="unknown",
        evidence="unverified",
        now=now,
    )
    # A non-verified adult verifier is rejected.
    plain = await _register(
        service,
        name="普通人",
        category="unknown",
        age_band="unknown",
        evidence="unverified",
        now=now,
    )
    with pytest.raises(AgeEvidenceError):
        await service.verify_age_evidence(
            person_id=target,
            evidence_id="evidence-verification-1",
            verifier_person_id=plain,
            now=now + timedelta(days=1),
        )
    # Self-verification is impossible.
    with pytest.raises(AgeEvidenceError):
        await service.verify_age_evidence(
            person_id=verifier,
            evidence_id="evidence-self",
            verifier_person_id=verifier,
            now=now + timedelta(days=1),
        )
    # Missing evidence is rejected.
    with pytest.raises(AgeEvidenceError):
        await service.verify_age_evidence(
            person_id=target,
            evidence_id="",
            verifier_person_id=verifier,
            now=now + timedelta(days=1),
        )
    await _establish_guardian(
        service, guardian=verifier, ward=target, now=now + timedelta(days=1)
    )
    verified = await service.verify_age_evidence(
        person_id=target,
        evidence_id="evidence-verification-1",
        verifier_person_id=verifier,
        now=now + timedelta(days=2),
    )
    assert verified.subject_category == "adult"
    assert verified.age_band == "adult"
    assert verified.age_evidence_status == "verified"
    # A guardian (even verified adult) declaring for the ward can only
    # maintain a minor band; the ward stays verified adult after a
    # contradictory declaration downgrades into disputed.
    downgraded = await service.declare_age_evidence(
        person_id=target,
        age_band="under_14",
        actor_person_id=verifier,
        now=now + timedelta(days=3),
    )
    assert downgraded.subject_category == "unknown"
    assert downgraded.age_band == "unknown"
    assert downgraded.age_evidence_status == "disputed"


@pytest.mark.asyncio
async def test_verified_adult_registration_requires_evidence(
    service: IdentityService,
) -> None:
    now = _now()
    with pytest.raises(AgeEvidenceError):
        await service.register_person(
            display_name="无证据成人",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            now=now,
        )
    with pytest.raises(AgeEvidenceError):
        await service.register_person(
            display_name="超长证据",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="x" * 129,
            now=now,
        )
    adult = await service.register_person(
        display_name="有证据成人",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="evidence-adult-1",
        now=now,
    )
    assert adult.subject_category == "adult"


@pytest.mark.asyncio
async def test_unknown_subject_can_bind_self_but_remains_fail_closed(
    service: IdentityService,
) -> None:
    now = _now()
    unknown = await _register(service, name="未知", now=now)
    manifest = await service.create_binding(
        device_id="dev-self-unknown",
        declared_mode="self_use",
        account_owner_person_id=unknown,
        primary_subject_ids=(unknown,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )

    assert manifest.primary_subject_ids == (unknown,)
    assert manifest.account_owner_id == unknown


# ---------------------------------------------------------------------------
# 四种 declared mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_for_child_mode_requires_minor_and_guardian(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    manifest = await service.create_binding(
        device_id="dev-child",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    assert manifest.declared_mode == "parent_for_child"
    assert manifest.primary_subject_ids == (child,)
    assert manifest.guardian_ids == (parent,)
    assert manifest.account_owner_id == parent


@pytest.mark.asyncio
async def test_parent_for_child_rejects_missing_guardian_and_non_binding_role(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    with pytest.raises(ModeConstraintError, match="guardian"):
        await service.create_binding(
            device_id="dev-child-bad",
            declared_mode="parent_for_child",
            account_owner_person_id=parent,
            primary_subject_ids=(child,),
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )
    with pytest.raises(RoleConstraintError, match="unknown binding role"):
        await service.create_binding(
            device_id="dev-child-bad2",
            declared_mode="parent_for_child",
            account_owner_person_id=parent,
            primary_subject_ids=(child,),
            roles=((parent, "guardian"), (parent, "beneficiary")),  # type: ignore[arg-type]
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )


@pytest.mark.asyncio
async def test_self_use_mode_binds_owner_as_subject(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    relative = await _adult(service, "亲友", now)
    manifest = await service.create_binding(
        device_id="dev-self",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    assert manifest.primary_subject_ids == (owner,)
    with pytest.raises(ModeConstraintError):
        await service.create_binding(
            device_id="dev-self-bad",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            roles=((relative, "guardian"),),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )


@pytest.mark.asyncio
async def test_child_for_parent_mode_separates_payer_from_subject(
    service: IdentityService,
) -> None:
    now = _now()
    child_buyer = await _adult(service, "子女", now)
    father = await _adult(service, "父亲", now)
    await _establish_relationship(
        service,
        relation_type="child_of",
        source=child_buyer,
        target=father,
        now=now,
    )
    await _establish_relationship(
        service,
        relation_type="emergency_contact_for",
        source=child_buyer,
        target=father,
        now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-senior",
        declared_mode="child_for_parent",
        account_owner_person_id=child_buyer,
        primary_subject_ids=(father,),
        roles=((child_buyer, "device_admin"), (child_buyer, "emergency_contact")),
        service_profile_version="senior-v1",
        policy_bundle_version="policy-senior-v1",
        now=now,
    )
    assert manifest.primary_subject_ids == (father,)
    assert manifest.account_owner_id == child_buyer
    assert child_buyer in manifest.device_admin_ids
    assert child_buyer in manifest.emergency_contact_ids


@pytest.mark.asyncio
async def test_child_for_parent_requires_admin_and_emergency_contact(
    service: IdentityService,
) -> None:
    now = _now()
    buyer = await _adult(service, "子女", now)
    father = await _adult(service, "父亲", now)
    with pytest.raises(ModeConstraintError, match="device_admin"):
        await service.create_binding(
            device_id="dev-senior-bad",
            declared_mode="child_for_parent",
            account_owner_person_id=buyer,
            primary_subject_ids=(father,),
            roles=((buyer, "emergency_contact"),),
            service_profile_version="senior-v1",
            policy_bundle_version="policy-senior-v1",
            now=now,
        )
    with pytest.raises(ModeConstraintError, match="emergency contact"):
        await service.create_binding(
            device_id="dev-senior-bad2",
            declared_mode="child_for_parent",
            account_owner_person_id=buyer,
            primary_subject_ids=(father,),
            roles=((buyer, "device_admin"),),
            service_profile_version="senior-v1",
            policy_bundle_version="policy-senior-v1",
            now=now,
        )


@pytest.mark.asyncio
async def test_family_shared_mode_requires_family_space_and_members(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child_a = await _minor(service, "孩子A", now)
    child_b = await _minor(service, "孩子B", now)
    await _establish_guardian(service, guardian=parent, ward=child_a, now=now)
    await _establish_guardian(service, guardian=parent, ward=child_b, now=now)
    manifest = await service.create_binding(
        device_id="dev-family",
        declared_mode="family_shared",
        account_owner_person_id=parent,
        primary_subject_ids=(child_a, child_b),
        roles=((parent, "guardian"), (parent, "member")),
        family_space_id="family-1",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )
    assert manifest.family_space_id == "family-1"
    assert set(manifest.primary_subject_ids) == {child_a, child_b}
    assert manifest.guardian_ids == (parent,)
    assert manifest.member_ids == (parent,)
    with pytest.raises(ModeConstraintError, match="family_space_id"):
        await service.create_binding(
            device_id="dev-family-bad",
            declared_mode="family_shared",
            account_owner_person_id=parent,
            primary_subject_ids=(child_a,),
            roles=((parent, "guardian"),),
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            now=now,
        )


# ---------------------------------------------------------------------------
# 角色边界
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payer_and_admin_have_no_content_read_rights(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    aunt = await _adult(service, "姑姑", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    await _establish_guardian(service, guardian=aunt, ward=child, now=now)
    manifest = await service.create_binding(
        device_id="dev-boundary",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"), (aunt, "device_admin")),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    assert "content.read" not in effective_permissions(manifest, parent)
    assert "memory.private.read" not in effective_permissions(manifest, parent)
    assert "content.read" not in effective_permissions(manifest, aunt)
    assert "memory.guardian.summary" in effective_permissions(manifest, parent)
    assert "content.read" in effective_permissions(manifest, child)


@pytest.mark.asyncio
async def test_device_admin_cannot_revoke_or_transfer_binding(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    aunt = await _adult(service, "姑姑", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    await _establish_guardian(service, guardian=aunt, ward=child, now=now)
    await service.create_binding(
        device_id="dev-boundary2",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"), (aunt, "device_admin")),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    with pytest.raises(IdentityAccessDeniedError):
        await service.revoke_binding(
            device_id="dev-boundary2", actor_person_id=aunt, now=now
        )
    with pytest.raises(IdentityAccessDeniedError):
        await service.create_transfer_intent(
            device_id="dev-boundary2",
            to_account_owner_person_id=aunt,
            step_up_evidence_id="step-up-1",
            policy_receipt_id="receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=aunt,
            now=now,
        )


@pytest.mark.asyncio
async def test_guardian_cannot_self_declare_and_unknown_permissions_rejected(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    with pytest.raises(RoleConstraintError, match="guardian"):
        await service.create_binding(
            device_id="dev-bad-role",
            declared_mode="parent_for_child",
            account_owner_person_id=child,
            primary_subject_ids=(child,),
            roles=((child, "guardian"),),
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )
    parent = await _adult(service, "家长", now)
    with pytest.raises(RoleConstraintError, match="permissions"):
        await service.create_binding(
            device_id="dev-bad-perms",
            declared_mode="parent_for_child",
            account_owner_person_id=parent,
            primary_subject_ids=(child,),
            roles=((parent, "guardian"),),
            role_permissions={((parent, "guardian")): frozenset({"read.everything"})},
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )


# ---------------------------------------------------------------------------
# 版本切换与审计
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_keeps_monotonic_versions_and_audit_trail(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    v1 = await service.create_binding(
        device_id="dev-versions",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    v2 = await service.supersede_binding(
        device_id="dev-versions",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v4",
        policy_bundle_version="policy-cn-minor-v6",
        actor_person_id=parent,
        now=now + timedelta(minutes=1),
    )
    assert v2.binding_version == 2
    assert v2.supersedes_binding_id == v1.binding_id
    versions = await service.list_binding_versions("dev-versions")
    assert [version.binding_version for version in versions] == [1, 2]
    assert versions[0].status == "superseded"
    assert versions[1].status == "active"
    active = await service.get_active_manifest(
        "dev-versions", now=now + timedelta(minutes=2)
    )
    assert active is not None and active.binding_version == 2
    assert active.service_profile_version == "student-cn-v4"
    v3 = await service.supersede_binding(
        device_id="dev-versions",
        declared_mode="self_use",
        account_owner_person_id=parent,
        primary_subject_ids=(parent,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        actor_person_id=parent,
        now=now + timedelta(minutes=3),
    )
    assert v3.binding_version == 3
    assert v3.declared_mode == "self_use"
    assert len(await service.list_binding_versions("dev-versions")) == 3


@pytest.mark.asyncio
async def test_revoke_unbind_preserves_history(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    manifest = await service.create_binding(
        device_id="dev-revoke",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    revoked = await service.revoke_binding(
        device_id="dev-revoke", actor_person_id=owner, reason="device_lost", now=now
    )
    assert revoked.status == "revoked"
    assert await service.get_active_manifest("dev-revoke", now=now) is None
    versions = await service.list_binding_versions("dev-revoke")
    assert len(versions) == 1
    assert versions[0].status == "revoked"
    assert versions[0].binding_id == manifest.binding_id
    with pytest.raises(IdentityNotFoundError):
        await service.revoke_binding(
            device_id="dev-revoke", actor_person_id=owner, now=now
        )


@pytest.mark.asyncio
async def test_expire_timeboxed_binding(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    await service.create_binding(
        device_id="dev-expiry",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        valid_until=now + timedelta(hours=1),
        now=now,
    )
    assert await service.get_active_manifest(
        "dev-expiry", now=now + timedelta(hours=2)
    ) is None
    assert await service.expire_bindings(now=now + timedelta(hours=2)) == 1
    versions = await service.list_binding_versions("dev-expiry")
    assert versions[0].status == "expired"


@pytest.mark.asyncio
async def test_list_active_manifests_for_person_is_scoped_to_owner_and_active_roles(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "列表主人", now)
    member = await _adult(service, "列表成员", now)
    child = await _minor(service, "列表孩子", now)
    stranger = await _adult(service, "无关用户", now)
    await _establish_guardian(service, guardian=owner, ward=child, now=now)
    await _establish_relationship(
        service,
        relation_type="family_member_of",
        source=member,
        target=child,
        now=now,
    )

    self_manifest = await service.create_binding(
        device_id="dev-list-self",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    family_manifest = await service.create_binding(
        device_id="dev-list-family",
        declared_mode="family_shared",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((member, "member"),),
        family_space_id="family-list",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )

    owner_manifests = await service.list_active_manifests_for_person(
        owner, now=now, actor_person_id=owner
    )
    assert [manifest.binding_id for manifest in owner_manifests] == [
        family_manifest.binding_id,
        self_manifest.binding_id,
    ]
    member_manifests = await service.list_active_manifests_for_person(
        member, now=now, actor_person_id=member
    )
    assert [manifest.binding_id for manifest in member_manifests] == [
        family_manifest.binding_id
    ]
    child_manifests = await service.list_active_manifests_for_person(
        child, now=now, actor_person_id=child
    )
    assert [manifest.binding_id for manifest in child_manifests] == [
        family_manifest.binding_id
    ]
    assert await service.list_active_manifests_for_person(
        stranger, now=now, actor_person_id=stranger
    ) == ()

    await service.create_binding(
        device_id="dev-list-revoked",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    await service.revoke_binding(
        device_id="dev-list-revoked",
        actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    await service.create_binding(
        device_id="dev-list-superseded",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=2),
    )
    await service.create_binding(
        device_id="dev-list-expired",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        valid_until=now + timedelta(hours=2),
        now=now + timedelta(minutes=3),
    )
    await service.supersede_binding(
        device_id="dev-list-superseded",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v2",
        policy_bundle_version="policy-self-v2",
        actor_person_id=owner,
        now=now + timedelta(minutes=4),
    )
    await service.expire_bindings(now=now + timedelta(hours=3))
    future_id = "binding-list-future"
    await store.persist_binding(
        DeviceBinding(
            binding_id=future_id,
            device_id="dev-list-future",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            binding_version=1,
            valid_from=now + timedelta(hours=4),
            created_at=now + timedelta(hours=4),
            roles=(
                DeviceBindingRole(
                    binding_id=future_id,
                    person_id=owner,
                    role="account_owner",
                    permissions=frozenset({"binding.manage"}),
                    granted_at=now + timedelta(hours=4),
                ),
            ),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
        )
    )

    active = await service.list_active_manifests_for_person(
        owner,
        now=now + timedelta(hours=3, minutes=1),
        actor_person_id=owner,
    )
    versions = {
        (manifest.device_id, manifest.binding_version) for manifest in active
    }
    assert ("dev-list-self", self_manifest.binding_version) in versions
    assert ("dev-list-family", family_manifest.binding_version) in versions
    assert ("dev-list-superseded", 2) in versions
    assert ("dev-list-revoked", 1) not in versions
    assert ("dev-list-superseded", 1) not in versions
    assert ("dev-list-expired", 1) not in versions
    assert ("dev-list-future", 1) not in versions


@pytest.mark.asyncio
async def test_list_active_bindings_for_person_actor_contract(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    """Store list mirrors the Postgres api-role double visibility."""
    now = _now()
    owner = await _adult(service, "契约主人", now)
    member = await _adult(service, "契约成员", now)
    child = await _minor(service, "契约孩子", now)
    stranger = await _adult(service, "契约外人", now)
    await _establish_guardian(service, guardian=owner, ward=child, now=now)
    await _establish_relationship(
        service,
        relation_type="family_member_of",
        source=member,
        target=child,
        now=now,
    )
    self_manifest = await service.create_binding(
        device_id="dev-actor-self",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    family_manifest = await service.create_binding(
        device_id="dev-actor-family",
        declared_mode="family_shared",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((member, "member"),),
        family_space_id="family-actor",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )

    # 共享可见：actor 是参与者时按 person/actor 双重资格可见，
    # 而不是 actor != person 一律拒绝。
    owner_actors_for_member = await store.list_active_bindings_for_person(
        member, now=now, actor_person_id=owner
    )
    assert [binding.binding_id for binding in owner_actors_for_member] == [
        family_manifest.binding_id
    ]
    member_actors_for_owner = await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=member
    )
    assert [binding.binding_id for binding in member_actors_for_owner] == [
        family_manifest.binding_id
    ]

    # 无关 actor：person 参与但 actor 与绑定无关 -> 空。
    assert await store.list_active_bindings_for_person(
        member, now=now, actor_person_id=stranger
    ) == ()
    assert await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=stranger
    ) == ()

    # 已结束/撤销 role 的 actor 失去 actor 资格。
    revoked_id = "binding-actor-revoked"
    await store.persist_binding(
        DeviceBinding(
            binding_id=revoked_id,
            device_id="dev-actor-revoked",
            declared_mode="family_shared",
            account_owner_person_id=owner,
            primary_subject_ids=(child,),
            family_space_id="family-actor",
            binding_version=1,
            valid_from=now,
            created_at=now,
            roles=(
                DeviceBindingRole(
                    binding_id=revoked_id,
                    person_id=owner,
                    role="account_owner",
                    permissions=ROLE_DEFAULT_PERMISSIONS["account_owner"],
                    granted_at=now,
                ),
                DeviceBindingRole(
                    binding_id=revoked_id,
                    person_id=child,
                    role="primary_subject",
                    permissions=ROLE_DEFAULT_PERMISSIONS["primary_subject"],
                    granted_at=now,
                ),
                DeviceBindingRole(
                    binding_id=revoked_id,
                    person_id=member,
                    role="member",
                    permissions=ROLE_DEFAULT_PERMISSIONS["member"],
                    granted_at=now,
                    status="revoked",
                    ended_at=now + timedelta(minutes=1),
                ),
            ),
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
        )
    )
    with_revoked_actor = await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=member
    )
    assert [binding.binding_id for binding in with_revoked_actor] == [
        family_manifest.binding_id
    ]

    # 无 actor fail closed；scope 值不解锁（PG api 池策略不看 scope GUC）。
    assert await store.list_active_bindings_for_person(owner, now=now) == ()
    assert await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=None, scope="migration"
    ) == ()
    migration_scoped = await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=owner, scope="migration"
    )
    assert {binding.binding_id for binding in migration_scoped} == {
        self_manifest.binding_id,
        family_manifest.binding_id,
        revoked_id,
    }


# ---------------------------------------------------------------------------
# 设备转赠不继承旧主体数据
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_intent_requires_two_party_accept_and_inherits_nothing(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    new_owner = await _adult(service, "新主人", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    v1 = await service.create_binding(
        device_id="dev-gift",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    policy_id, step_up = _receipt(
        actor=parent,
        subject=new_owner,
        device="dev-gift",
        binding_id=v1.binding_id,
        binding_version=v1.binding_version,
        expires_at=now + timedelta(days=8),
    )
    intent = await service.create_transfer_intent(
        device_id="dev-gift",
        to_account_owner_person_id=new_owner,
        step_up_evidence_id=step_up,
        policy_receipt_id=policy_id,
        idempotency_key=str(uuid.uuid4()),
        actor_person_id=parent,
        now=now + timedelta(minutes=1),
    )
    assert intent.status == "pending"
    with pytest.raises(IdentityAccessDeniedError):
        await service.accept_transfer_intent(
            transfer_id=intent.transfer_id,
            actor_person_id=parent,
            declared_mode="self_use",
            primary_subject_ids=(parent,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(minutes=2),
            idempotency_key=str(uuid.uuid4()),
        )
    accept_key = str(uuid.uuid4())
    transferred = await service.accept_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=new_owner,
        primary_subject_ids=(new_owner,),
        declared_mode="self_use",
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=3),
        idempotency_key=accept_key,
    )
    assert transferred.binding_version == 2
    assert transferred.reason == "transfer"
    assert transferred.account_owner_id == new_owner
    assert transferred.primary_subject_ids == (new_owner,)
    assert transferred.guardian_ids == ()
    assert transferred.device_admin_ids == ()
    assert all(role.person_id != child for role in transferred.roles)
    assert all(role.person_id != parent for role in transferred.roles)
    replayed = await service.accept_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=new_owner,
        primary_subject_ids=(new_owner,),
        declared_mode="self_use",
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=4),
        idempotency_key=accept_key,
    )
    assert replayed.binding_id == transferred.binding_id
    versions = await service.list_binding_versions("dev-gift")
    assert len(versions) == 2
    assert versions[0].binding_id == v1.binding_id
    assert versions[0].status == "superseded"
    assert versions[0].primary_subject_ids == (child,)
    assert versions[1].status == "active"
    active = await service.get_active_manifest(
        "dev-gift", now=now + timedelta(minutes=6)
    )
    assert active is not None
    assert active.primary_subject_ids == (new_owner,)


@pytest.mark.asyncio
async def test_transfer_intent_requires_different_owner_and_evidence(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    other = await _adult(service, "其他人", now)
    await service.create_binding(
        device_id="dev-gift2",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    with pytest.raises(IdentityConflictError, match="different account owner"):
        await service.create_transfer_intent(
            device_id="dev-gift2",
            to_account_owner_person_id=owner,
            step_up_evidence_id="step-up-evidence-1",
            policy_receipt_id="policy-receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=owner,
            now=now,
        )
    with pytest.raises(IdentityAccessDeniedError):
        await service.create_transfer_intent(
            device_id="dev-gift2",
            to_account_owner_person_id=other,
            step_up_evidence_id="step-up-evidence-1",
            policy_receipt_id="policy-receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=other,
            now=now,
        )
    with pytest.raises(TransferLifecycleError, match="step-up"):
        await service.create_transfer_intent(
            device_id="dev-gift2",
            to_account_owner_person_id=other,
            step_up_evidence_id=" ",
            policy_receipt_id="policy-receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=owner,
            now=now,
        )


# ---------------------------------------------------------------------------
# 关系生命周期与转授权
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relationship_requires_both_party_confirmation(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    pending = await service.propose_relationship(
        source_person_id=parent,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="evidence-1",
        actor_person_id=parent,
        now=now,
    )
    assert pending.status == "pending"
    one_side = await service.confirm_relationship(
        relationship_id=pending.relationship_id, person_id=parent, now=now
    )
    assert one_side.status == "pending"
    active = await service.confirm_relationship(
        relationship_id=pending.relationship_id, person_id=child, now=now
    )
    assert active.status == "active"
    assert active.confirmed_by_source_at is not None
    assert active.confirmed_by_target_at is not None
    with pytest.raises(IdentityConflictError):
        await service.confirm_relationship(
            relationship_id=pending.relationship_id, person_id=parent, now=now
        )
    outsider = await _adult(service, "外人", now)
    with pytest.raises(IdentityAccessDeniedError):
        await service.confirm_relationship(
            relationship_id=pending.relationship_id, person_id=outsider, now=now
        )


@pytest.mark.asyncio
async def test_relationship_lifecycle_transitions(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    relationship = await service.propose_relationship(
        source_person_id=parent,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="evidence-1",
        actor_person_id=parent,
        now=now,
    )
    relationship = await service.confirm_relationship(
        relationship_id=relationship.relationship_id, person_id=parent, now=now
    )
    relationship = await service.confirm_relationship(
        relationship_id=relationship.relationship_id, person_id=child, now=now
    )
    assert relationship.status == "active"
    relationship = await service.suspend_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=parent,
        now=now,
    )
    assert relationship.status == "suspended"
    relationship = await service.resume_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=child,
        now=now,
    )
    assert relationship.status == "active"
    relationship = await service.dispute_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=parent,
        reason="监护范围争议",
        now=now,
    )
    assert relationship.status == "disputed"
    one_side = await service.acknowledge_dispute_resolution(
        relationship_id=relationship.relationship_id,
        person_id=child,
        now=now,
    )
    assert one_side.status == "disputed"
    relationship = await service.acknowledge_dispute_resolution(
        relationship_id=relationship.relationship_id,
        person_id=parent,
        now=now,
    )
    assert relationship.status == "active"
    revoked = await service.revoke_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=parent,
        evidence_id="evidence-revoke",
        now=now,
    )
    assert revoked.status == "revoked"
    with pytest.raises(RelationshipLifecycleError):
        await service.suspend_relationship(
            relationship_id=relationship.relationship_id,
            actor_person_id=parent,
            now=now,
        )


@pytest.mark.asyncio
async def test_non_self_relationship_cannot_skip_confirmation(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    with pytest.raises(RelationshipLifecycleError, match="confirmation"):
        await service.propose_relationship(
            source_person_id=parent,
            target_person_id=child,
            relation_type="emergency_contact_for",
            established_evidence_id="evidence-1",
            requires_confirmation=False,
            actor_person_id=parent,
            now=now,
        )
    with pytest.raises(IdentityAccessDeniedError, match="may not propose"):
        await service.propose_relationship(
            source_person_id=parent,
            target_person_id=child,
            relation_type="emergency_contact_for",
            established_evidence_id="evidence-1",
            actor_person_id=child,
            now=now,
        )


@pytest.mark.asyncio
async def test_delegation_chain_depth_is_capped(service: IdentityService) -> None:
    now = _now()
    people = [await _adult(service, f"人{i}", now) for i in range(6)]
    previous_id: str | None = None
    for index in range(1, 5):
        proposed = await service.propose_relationship(
            source_person_id=people[index - 1],
            target_person_id=people[index],
            relation_type="delegate_for",
            established_evidence_id=f"evidence-delegate-{index}",
            can_delegate=True,
            delegated_from_relationship_id=previous_id,
            actor_person_id=people[index - 1],
            now=now,
        )
        confirmed = await service.confirm_relationship(
            relationship_id=proposed.relationship_id,
            person_id=people[index - 1],
            now=now,
        )
        active = await service.confirm_relationship(
            relationship_id=confirmed.relationship_id,
            person_id=people[index],
            now=now,
        )
        assert active.status == "active"
        previous_id = active.relationship_id
    assert active.delegation_depth == 3
    with pytest.raises(RelationshipLifecycleError, match="depth"):
        await service.propose_relationship(
            source_person_id=people[4],
            target_person_id=people[5],
            relation_type="delegate_for",
            established_evidence_id="evidence-delegate-6",
            can_delegate=True,
            delegated_from_relationship_id=previous_id,
            actor_person_id=people[4],
            now=now,
        )


@pytest.mark.asyncio
async def test_expire_relationships_by_valid_until(service: IdentityService) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    await service.propose_relationship(
        source_person_id=parent,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="evidence-1",
        valid_until=now + timedelta(days=30),
        actor_person_id=parent,
        now=now,
    )
    assert await service.expire_relationships(now=now + timedelta(days=31)) == 1
    relationships = await service.list_relationships(
        person_id=child, statuses=("expired",)
    )
    assert len(relationships) == 1


# ---------------------------------------------------------------------------
# Manifest 序列化与可序列化回放
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_manifest_is_serializable_round_trip(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    await service.create_binding(
        device_id="dev-json",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    manifest = await service.get_active_manifest("dev-json", now=now)
    assert manifest is not None
    assert isinstance(manifest, BindingManifest)
    decoded = manifest_from_dict(json.loads(manifest.to_json()))
    assert decoded.to_dict() == manifest.to_dict()


@pytest.mark.asyncio
async def test_sqlite_store_persists_across_instances(tmp_path: Path) -> None:
    now = _now()
    path = tmp_path / "identity-persist.sqlite3"
    first = SqliteIdentityStore(path)
    service = make_test_service(first)[0]
    owner = await _adult(service, "本人", now)
    await service.create_binding(
        device_id="dev-persist",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    reopened = SqliteIdentityStore(path)
    service = make_test_service(reopened)[0]
    manifest = await service.get_active_manifest("dev-persist", now=now)
    assert manifest is not None
    assert manifest.binding_version == 1
    assert manifest.account_owner_id == owner
    person = await service.get_person(owner)
    assert person.subject_category == "adult"


# ---------------------------------------------------------------------------
# Persona assignments (person -> persona, subject 级)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_assignment_crud_is_idempotent_and_subject_scoped(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    child = await _adult(service, "孩子", now)
    manifest = await service.create_binding(
        device_id="dev-persona",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )
    binding_id = manifest.binding_id

    # No explicit assignment yet: the lazy fallback is the binding default.
    assert (
        await service.get_persona_assignment(
            binding_id=binding_id, subject_id=child
        )
        is None
    )
    assert await service.list_persona_assignments(binding_id=binding_id) == ()

    record = await service.set_persona_assignment(
        binding_id=binding_id,
        subject_id=child,
        persona_selection="taoxi",
        actor_person_id=owner,
        now=now,
    )
    assert record.binding_id == binding_id
    assert record.subject_id == child
    assert record.assignment_id == "taoxi:v1"
    assert record.persona_id == "taoxi"
    assert record.persona_version == 1

    # An exact replay is idempotent: the persisted row is returned unchanged.
    replay = await service.set_persona_assignment(
        binding_id=binding_id,
        subject_id=child,
        persona_selection="taoxi",
        actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    assert replay.created_at == record.created_at
    assert replay.updated_at == record.updated_at

    # A different persona overwrites the override but keeps created_at.
    replaced = await service.set_persona_assignment(
        binding_id=binding_id,
        subject_id=child,
        persona_selection="mianmian",
        actor_person_id=owner,
        now=now + timedelta(minutes=2),
    )
    assert replaced.persona_id == "mianmian"
    assert replaced.assignment_id == "mianmian:v1"
    assert replaced.created_at == record.created_at
    assert replaced.updated_at > record.updated_at

    listed = await service.list_persona_assignments(binding_id=binding_id)
    assert len(listed) == 1
    assert listed[0].subject_id == child
    assert listed[0].persona_id == "mianmian"

    # Deleting the override restores the lazy binding-default fallback.
    assert (
        await service.delete_persona_assignment(
            binding_id=binding_id, subject_id=child, now=now + timedelta(minutes=3)
        )
        is True
    )
    assert (
        await service.get_persona_assignment(
            binding_id=binding_id, subject_id=child
        )
        is None
    )
    assert (
        await service.delete_persona_assignment(
            binding_id=binding_id, subject_id=child, now=now + timedelta(minutes=4)
        )
        is False
    )


@pytest.mark.asyncio
async def test_persona_assignment_rejects_non_canonical_selection(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    manifest = await service.create_binding(
        device_id="dev-persona-invalid",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    with pytest.raises(ValueError):
        await service.set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=owner,
            persona_selection="",
            actor_person_id=owner,
            now=now,
        )
    with pytest.raises(ValueError):
        await service.set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=owner,
            persona_selection="x" * 40,
            actor_person_id=owner,
            now=now,
        )


# ---------------------------------------------------------------------------
# Custom personas (account-level, create-once)
# ---------------------------------------------------------------------------


def _structured(**overrides: object) -> StructuredPersonaFields:
    base: dict[str, object] = {
        "style_description": "爱追问，偶尔说点冷幽默",
        "warmth": "bright",
        "directness": "direct",
        "response_length": "brief",
        "question_frequency": "frequent",
        "interview_depth": "structured",
        "welcome_text": "嗨，我是小岸。",
        "conversation_instruction": "保持轻快、多追问，不装可爱。",
        "voice_instruction": "声音清亮、语速稍快。",
        "default_voice_emotion": "happy",
        "default_voice_rate": 1.02,
    }
    base.update(overrides)
    return StructuredPersonaFields(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_custom_persona_create_list_get_delete_is_account_scoped(
    service: IdentityService,
    store: InMemoryIdentityStore | SqliteIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    record = await service.create_custom_persona(
        owner_person_id=owner,
        display_name="小岸",
        structured=_structured(),
        actor_person_id=owner,
        now=now,
    )
    # ``cu_`` + uuid4 hex, canonical id namespace, always v1.
    assert record.persona_id.startswith("cu_")
    assert len(record.persona_id) <= 32
    assert record.persona_version == 1
    assert record.fallback_designed_voice == "starlight"
    assert record.source == "user_created"

    listed = await service.list_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    )
    assert [item.persona_id for item in listed] == [record.persona_id]

    fetched = await service.get_custom_persona(
        record.persona_id, owner_person_id=owner, actor_person_id=owner
    )
    assert fetched == record

    # Another account can neither see nor delete it.
    other = await _adult(service, "别人", now)
    assert (
        await service.list_custom_personas(
            owner_person_id=other, actor_person_id=other
        )
        == ()
    )
    with pytest.raises(IdentityNotFoundError):
        await service.get_custom_persona(
            record.persona_id, owner_person_id=other, actor_person_id=other
        )
    foreign = await service.delete_custom_persona(
        persona_id=record.persona_id, owner_person_id=other, actor_person_id=other
    )
    assert foreign.deleted is False
    assert (
        await service.get_custom_persona(
            record.persona_id, owner_person_id=owner, actor_person_id=owner
        )
        == record
    )

    outcome = await service.delete_custom_persona(
        persona_id=record.persona_id, owner_person_id=owner, actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    assert outcome.deleted is True
    assert outcome.drifted_subjects == 0
    assert (
        await service.list_custom_personas(
            owner_person_id=owner, actor_person_id=owner
        )
        == ()
    )
    # Deleting again is an explicit not-found (idempotent-ish, no error).
    assert (
        await service.delete_custom_persona(
            persona_id=record.persona_id, owner_person_id=owner, actor_person_id=owner
        )
    ).deleted is False
    # The audit trail recorded create + delete.
    if isinstance(store, InMemoryIdentityStore):
        actions = [event.action for event in store._audit]
        assert "persona.custom.create" in actions
        assert "persona.custom.delete" in actions


@pytest.mark.asyncio
async def test_custom_persona_delete_drops_referencing_assignments(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    child = await _adult(service, "孩子", now)
    record = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        actor_person_id=owner, now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-custom-persona",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    await service.set_persona_assignment(
        binding_id=manifest.binding_id,
        subject_id=child,
        persona_selection=record.persona_id,
        actor_person_id=owner,
        now=now,
    )
    assert (
        await service.count_custom_persona_references(
            record.persona_id, actor_person_id=owner
        )
        == 1
    )
    outcome = await service.delete_custom_persona(
        persona_id=record.persona_id, owner_person_id=owner, actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    assert outcome.deleted is True
    assert outcome.drifted_subjects == 1
    # No dangling override: the subject falls back to the binding default.
    assert (
        await service.get_persona_assignment(
            binding_id=manifest.binding_id, subject_id=child
        )
        is None
    )


@pytest.mark.asyncio
async def test_custom_persona_limit_is_enforced(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    for index in range(5):
        await service.create_custom_persona(
            owner_person_id=owner, display_name=f"人格{index}", structured=_structured(),
            actor_person_id=owner, now=now + timedelta(seconds=index),
        )
    assert await service.count_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    ) == 5
    with pytest.raises(CustomPersonaLimitError) as excinfo:
        await service.create_custom_persona(
            owner_person_id=owner, display_name="第六个", structured=_structured(),
            actor_person_id=owner, now=now + timedelta(minutes=1),
        )
    assert excinfo.value.limit == 5


@pytest.mark.asyncio
async def test_custom_persona_only_owner_may_create(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    member = await _adult(service, "成员", now)
    with pytest.raises(IdentityAccessDeniedError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="越权", structured=_structured(),
            actor_person_id=member, now=now,
        )


@pytest.mark.asyncio
async def test_custom_persona_idempotent_replay_returns_same_record(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    first = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        idempotency_key="create-1", actor_person_id=owner, now=now,
    )
    replay = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        idempotency_key="create-1", actor_person_id=owner,
        now=now + timedelta(minutes=5),
    )
    assert replay.persona_id == first.persona_id
    assert await service.count_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    ) == 1
    # Same key, different content is a conflict (never a silent second row).
    with pytest.raises(IdentityConflictError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="改名", structured=_structured(),
            idempotency_key="create-1", actor_person_id=owner, now=now,
        )


@pytest.mark.asyncio
async def test_custom_persona_rejects_out_of_domain_fields(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    # Out-of-domain enum values are refused (fail closed, never clamped).
    with pytest.raises(ValueError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="坏值",
            structured=_structured(warmth="甜"), actor_person_id=owner, now=now,
        )
    # Names and rate bounds mirror the database CHECK constraints.
    with pytest.raises(ValueError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="名字" * 9,
            structured=_structured(), actor_person_id=owner, now=now,
        )
    with pytest.raises(ValueError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="语速",
            structured=_structured(default_voice_rate=1.5),
            actor_person_id=owner, now=now,
        )
    assert await service.list_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    ) == ()


@pytest.mark.asyncio
async def test_persona_assignment_accepts_own_custom_and_rejects_foreign(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    other_owner = await _adult(service, "别人", now)
    mine = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        actor_person_id=owner, now=now,
    )
    theirs = await service.create_custom_persona(
        owner_person_id=other_owner, display_name="他人人格", structured=_structured(),
        actor_person_id=other_owner, now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-custom-assignment",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    record = await service.set_persona_assignment(
        binding_id=manifest.binding_id,
        subject_id=owner,
        persona_selection=mine.persona_id,
        actor_person_id=owner,
        now=now,
    )
    assert record.persona_id == mine.persona_id
    assert record.assignment_id == f"{mine.persona_id}:v1"
    # A custom persona owned by another account is refused (cross-account).
    with pytest.raises(ValueError):
        await service.set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=owner,
            persona_selection=theirs.persona_id,
            actor_person_id=owner,
            now=now,
        )


@pytest.mark.asyncio
async def test_sqlite_custom_persona_update_is_rejected_by_trigger(
    tmp_path: Path,
) -> None:
    """The storage layer refuses any update: create-once, not app-level only."""
    store = SqliteIdentityStore(tmp_path / "identity.sqlite3")
    from services.identity.authority import DeterministicConsentSnapshotResolver

    service = IdentityService(
        store,
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )
    now = _now()
    owner = await _adult(service, "主人", now)
    record = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        actor_person_id=owner, now=now,
    )
    with store._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE identity_custom_personas SET display_name = '改名' "
                "WHERE persona_id = ?",
                (record.persona_id,),
            )
        # DELETE is allowed (the ordinary delete path drops the row).
        connection.execute(
            "DELETE FROM identity_custom_personas WHERE persona_id = ?",
            (record.persona_id,),
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM identity_custom_personas"
            ).fetchone()[0]
            == 0
        )
