"""Policy-decision and serialized-profile tests for the multi-subject API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.multi_subject_runtime import (
    PolicyActorMismatchError,
)
from services.control_api.app.routes import multi_subject as multi_subject_routes
from services.control_api.tests.multi_subject_test_support import (
    _bind_family,
    _bind_self,
    _env,
    _establish_relationship,
    _register,
)
from services.session_runtime.profile_service import (
    RUNTIME_PROFILE_PAYLOAD_SCHEMA,
    verify_runtime_profile_payload,
)


class _FrozenClock:
    fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None) -> datetime:
        assert cls.fixed is not None
        return cls.fixed


@pytest.mark.asyncio
async def test_decision_on_stale_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "stale-profile")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "stale-profile-owner")
        child_id = await _bind_family(
            client,
            app,
            owner=owner,
            device_id="device-stale-profile",
            nonce="claim-stale-profile",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        unresolved = await client.get(
            "/v1/devices/device-stale-profile/runtime-profile",
            headers=headers,
        )
        assert unresolved.status_code == 200
        session_id = unresolved.json()["session_id"]

        child_profile = (
            await client.post(
                f"/v1/sessions/{session_id}/active-subject",
                headers=headers,
                json={"person_id": child_id, "confirmation_method": "app_confirm"},
            )
        ).json()
        await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={
                "person_id": owner["user_id"],
                "confirmation_method": "app_confirm",
            },
        )

        stale_decision = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": child_profile["runtime_profile_id"],
                "capability": "chat",
                "data_classification": "public",
            },
        )
        assert stale_decision.status_code == 403
        assert stale_decision.json()["detail"]["code"] == "runtime_profile_rejected"


@pytest.mark.asyncio
async def test_decision_on_expired_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "expired-profile")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "expired-profile-owner")
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-expired-profile",
            nonce="claim-expired-profile",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        profile_response = await client.get(
            "/v1/devices/device-expired-profile/runtime-profile",
            headers=headers,
        )
        assert profile_response.status_code == 200
        profile = profile_response.json()

        _FrozenClock.fixed = datetime.now(UTC) + timedelta(minutes=10)
        monkeypatch.setattr(multi_subject_routes, "datetime", _FrozenClock)
        expired_decision = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": profile["runtime_profile_id"],
                "capability": "chat",
                "data_classification": "public",
            },
        )
        assert expired_decision.status_code == 403
        assert (
            expired_decision.json()["detail"]["code"] == "runtime_profile_rejected"
        )


@pytest.mark.asyncio
async def test_policy_decision_missing_capability_denies_with_audit_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "audited-deny")
    now = datetime.now(UTC)
    identity = app.state.identity_service
    control = app.state.multi_subject_runtime
    owner = await identity.register_person(
        person_id="person-owner",
        display_name="家长",
        timezone="Asia/Shanghai",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=now,
    )
    child = await identity.register_person(
        person_id="person-child",
        display_name="小朋友",
        timezone="Asia/Shanghai",
        subject_category="minor",
        age_band="under_14",
        age_evidence_status="unverified",
        now=now,
    )
    await identity.create_binding(
        device_id="device-audited-deny",
        declared_mode="family_shared",
        account_owner_person_id=owner.person_id,
        primary_subject_ids=(child.person_id,),
        roles=(
            (owner.person_id, "device_admin"),
            (owner.person_id, "member"),
        ),
        family_space_id="family-audited-deny",
        service_profile_version="family_shared-v1",
        policy_bundle_version="multi-subject-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )

    base = await control.ensure_profile(
        device_id="device-audited-deny",
        session_id=None,
        actor_id=owner.person_id,
        now=now,
    )
    child_profile = await control.switch_subject(
        session_id=base.session_id,
        subject_id=child.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    assert child_profile.actor_id == child.person_id
    assert "voice_clone_use" not in child_profile.capabilities
    assert child_profile.subject_category == "minor"
    assert child_profile.age_band == "under_14"

    # The matching actor may decide; the missing capability denies with an
    # audit receipt and does not escalate the profile.
    decision = await control.decide(
        runtime_profile_id=child_profile.runtime_profile_id,
        capability="voice_clone_use",
        actor_id=child.person_id,
        data_classification="biometric",
        safety_state="normal",
        now=now,
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "capability_not_in_runtime_profile"
    assert decision.receipt_id
    assert app.state.policy_receipt_writer.get(decision.receipt_id) is not None
    assert "voice_clone_use" not in child_profile.capabilities

    # The owner confirming the child is not the profile's actor and must not
    # borrow the child's identity for a decision.
    with pytest.raises(PolicyActorMismatchError):
        await control.decide(
            runtime_profile_id=child_profile.runtime_profile_id,
            capability="voice_clone_use",
            actor_id=owner.person_id,
            data_classification="biometric",
            safety_state="normal",
            now=now,
        )


@pytest.mark.asyncio
async def test_serialized_profile_verifies_independently_and_tamper_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "wire-verify")
    now = datetime.now(UTC)
    identity = app.state.identity_service
    control = app.state.multi_subject_runtime
    signing_key = app.state.settings.runtime_profile_signing_key()
    owner = await identity.register_person(
        person_id="person-wire-owner",
        display_name="本人",
        timezone="Asia/Shanghai",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=now,
    )
    await identity.create_binding(
        device_id="device-wire-verify",
        declared_mode="self_use",
        account_owner_person_id=owner.person_id,
        primary_subject_ids=(owner.person_id,),
        roles=(),
        family_space_id=None,
        service_profile_version="self_use-v1",
        policy_bundle_version="multi-subject-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )

    unknown = await control.ensure_profile(
        device_id="device-wire-verify",
        session_id=None,
        actor_id=owner.person_id,
        now=now,
    )
    assert unknown.active_subject_id is None
    serialized_unknown = control.serialize_profile(unknown)
    assert verify_runtime_profile_payload(
        serialized_unknown,
        signing_key=signing_key,
    )
    assert serialized_unknown["signature_schema"] == RUNTIME_PROFILE_PAYLOAD_SCHEMA
    assert serialized_unknown["persona_assignment_id"] == "starlight:v1"

    confirmed = await control.switch_subject(
        session_id=unknown.session_id,
        subject_id=owner.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    serialized_confirmed = control.serialize_profile(confirmed)
    assert verify_runtime_profile_payload(
        serialized_confirmed,
        signing_key=signing_key,
    )

    tampered_variants = [
        {**serialized_confirmed, "service_mode": "adult_archive"},
        {
            **serialized_confirmed,
            "persona": {
                "persona_id": "other",
                "version": 99,
                "relationship_stage": "new",
            },
        },
        {**serialized_confirmed, "persona_assignment_id": "other:v9"},
        {**serialized_confirmed, "session_epoch": 99},
        {**serialized_confirmed, "subject_category": "minor"},
        {**serialized_confirmed, "age_band": "14_17"},
        {**serialized_confirmed, "capabilities": ["chat"]},
        {**serialized_confirmed, "issued_at": "2099-01-01T00:00:00+00:00"},
        {**serialized_confirmed, "signature_schema": "runtime-profile-v0"},
        {**serialized_confirmed, "signature": "0" * 64},
    ]
    for tampered in tampered_variants:
        assert not verify_runtime_profile_payload(
            tampered,
            signing_key=signing_key,
        )


@pytest.mark.asyncio
async def test_cross_actor_policy_decisions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "cross-actor")
    now = datetime.now(UTC)
    identity = app.state.identity_service
    control = app.state.multi_subject_runtime
    owner = await identity.register_person(
        person_id="person-owner",
        display_name="管理员",
        timezone="Asia/Shanghai",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=now,
    )
    guardian = await identity.register_person(
        person_id="person-guardian",
        display_name="监护人",
        timezone="Asia/Shanghai",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=now,
    )
    member = await identity.register_person(
        person_id="person-member",
        display_name="成员",
        timezone="Asia/Shanghai",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=now,
    )
    child = await identity.register_person(
        person_id="person-child",
        display_name="小朋友",
        timezone="Asia/Shanghai",
        subject_category="minor",
        age_band="under_14",
        age_evidence_status="unverified",
        now=now,
    )
    await _establish_relationship(
        identity,
        relation_type="guardian_of",
        source=guardian.person_id,
        target=child.person_id,
        now=now,
    )
    await identity.create_binding(
        device_id="device-cross-actor",
        declared_mode="parent_for_child",
        account_owner_person_id=owner.person_id,
        primary_subject_ids=(child.person_id,),
        roles=(
            (owner.person_id, "device_admin"),
            (guardian.person_id, "guardian"),
            (member.person_id, "member"),
        ),
        family_space_id=None,
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )

    base = await control.ensure_profile(
        device_id="device-cross-actor",
        session_id=None,
        actor_id=owner.person_id,
        now=now,
    )
    child_profile = await control.switch_subject(
        session_id=base.session_id,
        subject_id=child.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    # app_confirm semantics: the profile actor/subject is the confirmed
    # speaker, never the confirmer.
    assert child_profile.actor_id == child.person_id
    assert child_profile.active_subject_id == child.person_id
    assert child_profile.speaker_state == "confirmed"

    # GET/resolve exposes the current profile to members, but that must not
    # allow borrowing it for policy decisions.
    member_view = await control.resolve_subject(
        device_id="device-cross-actor",
        session_id=None,
        actor_id=member.person_id,
        now=now,
    )
    assert member_view.resolution == "confirmed"
    assert (
        member_view.profile.runtime_profile_id == child_profile.runtime_profile_id
    )

    for borrower in (member.person_id, guardian.person_id, owner.person_id):
        with pytest.raises(PolicyActorMismatchError):
            await control.decide(
                runtime_profile_id=child_profile.runtime_profile_id,
                capability="chat",
                actor_id=borrower,
                data_classification="public",
                safety_state="normal",
                now=now,
            )

    # The matching actor may decide, but without a wired consent authority the
    # minor's governed chat stays fail closed (capability never entered the
    # issued profile).  Consent integration is a separate authority task.
    matched = await control.decide(
        runtime_profile_id=child_profile.runtime_profile_id,
        capability="chat",
        actor_id=child.person_id,
        data_classification="public",
        safety_state="normal",
        now=now,
    )
    assert matched.effect == "deny"

    # Owner confirms another member: the resulting profile belongs to that
    # member, not to the owner.
    member_profile = await control.switch_subject(
        session_id=base.session_id,
        subject_id=member.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    assert member_profile.actor_id == member.person_id
    assert member_profile.active_subject_id == member.person_id
    with pytest.raises(PolicyActorMismatchError):
        await control.decide(
            runtime_profile_id=member_profile.runtime_profile_id,
            capability="chat",
            actor_id=owner.person_id,
            data_classification="public",
            safety_state="normal",
            now=now,
        )
    member_matched = await control.decide(
        runtime_profile_id=member_profile.runtime_profile_id,
        capability="chat",
        actor_id=member.person_id,
        data_classification="public",
        safety_state="normal",
        now=now,
    )
    assert member_matched.effect == "allow"


@pytest.mark.asyncio
async def test_matching_actor_can_decide_own_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "matching-actor")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "matching-actor-owner")
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-matching-actor",
            nonce="claim-matching-actor",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        profile_response = await client.get(
            "/v1/devices/device-matching-actor/runtime-profile",
            headers=headers,
        )
        assert profile_response.status_code == 200
        profile = profile_response.json()
        assert profile["actor_id"] == owner["user_id"]

        allowed = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": profile["runtime_profile_id"],
                "capability": "chat",
                "data_classification": "public",
            },
        )
        assert allowed.status_code == 200
        assert allowed.json()["effect"] == "allow_with_obligations"

        denied = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": profile["runtime_profile_id"],
                "capability": "voice_clone_use",
                "data_classification": "biometric",
            },
        )
        assert denied.status_code == 200
        assert denied.json()["effect"] == "deny"
