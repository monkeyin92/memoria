"""IdentityService person registration, reconciliation and age-evidence behavior."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from services.identity.domain import (
    AgeEvidenceError,
    IdentityConflictError,
)
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import IdentityService
from services.identity.tests.service_test_support import (
    _establish_guardian,
    _now,
    _register,
)

# Re-exported (not redefined) so pytest discovers the fixture by name.
from services.identity.tests.service_test_support import (
    service as service,
)
from services.identity.tests.service_test_support import (
    store as store,
)


@pytest.mark.asyncio
async def test_registration_reconciliation_is_audited_and_idempotent(
    service: IdentityService, store: InMemoryIdentityStore,
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
    service: IdentityService, store: InMemoryIdentityStore,
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


@pytest.mark.asyncio
async def test_register_exact_replay_returns_persisted_object(
    service: IdentityService,
    store: InMemoryIdentityStore,
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
