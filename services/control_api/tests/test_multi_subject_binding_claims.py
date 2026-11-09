"""Binding-manifest, subject-confirmation and claim-containment tests for
the multi-subject API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.app.multi_subject_runtime import (
    PolicyActorMismatchError,
    SubjectSwitchForbiddenError,
)
from services.control_api.tests.identity_test_helpers import (
    install_test_identity_authority,
)
from services.control_api.tests.multi_subject_test_support import (
    _bind_self,
    _env,
    _establish_relationship,
    _register,
)
from services.identity.domain import ModeConstraintError
from services.session_runtime.profile_service import (
    RuntimeProfileRejected,
)
from testing import app_store


@pytest.mark.asyncio
async def test_self_binding_never_reads_identity_without_owner_actor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "binding-identity-actor")
    identity = app.state.identity_service
    original_get_person = identity.get_person
    calls: list[tuple[str, str | None]] = []

    async def require_actor(person_id: str, actor_person_id: str | None = None):
        calls.append((person_id, actor_person_id))
        if actor_person_id is None:
            raise AssertionError("production FORCE RLS requires an identity actor")
        return await original_get_person(
            person_id,
            actor_person_id=actor_person_id,
        )

    monkeypatch.setattr(identity, "get_person", require_actor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "binding-actor-owner")
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-binding-actor",
            nonce="claim-binding-actor",
        )

    assert calls
    assert all(actor_id == owner["user_id"] for _person_id, actor_id in calls)


@pytest.mark.asyncio
async def test_self_binding_creates_versioned_manifest_without_adult_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_IDENTITY_DB_PATH", str(tmp_path / "identity.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "multi-subject-api-auth-secret-long-enough-0123456789")
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        "multi-subject-transfer-evidence-secret-32-bytes",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    install_test_identity_authority(
        app,
        secret=app.state.settings.transfer_evidence_key(),
    )
    now = datetime.now(UTC)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "binding-owner", "password": "safe-password"},
        )
        identity = registered.json()
        token = mint_device_binding_token(
            device_id="device-1",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="claim-1",
        )
        response = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "self_use",
                "account_owner_person_id": identity["user_id"],
                "primary_subject": {
                    "person_id": identity["user_id"],
                    "relationship": "self",
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "personal",
                    "interview_frequency": "low",
                },
                "consent_offer_ids": ["offer_self_memory_retention_v1"],
            },
        )

    assert response.status_code == 201
    manifest = response.json()
    assert manifest["device_id"] == "device-1"
    assert manifest["declared_mode"] == "self_use"
    assert manifest["binding_version"] == 1
    assert manifest["primary_subject_ids"] == [identity["user_id"]]
    assert manifest["policy_bundle_version"] == "multi-subject-v1"


@pytest.mark.asyncio
async def test_subject_draft_cannot_claim_adult_or_verified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "draft-age-fence")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "draft-age-owner")
        now = datetime.now(UTC)
        # Fixture authority: the owner is a verified adult, but the API draft
        # path must never let a NEW subject claim adult/verified through the
        # request body.
        await app.state.identity_service.register_person(
            person_id=owner["user_id"],
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=now,
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        token = mint_device_binding_token(
            device_id="device-draft-age",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="claim-draft-age",
        )
        # Injecting age_evidence_status into the draft is rejected by the
        # strict contract (extra=forbid).
        forged = await client.post(
            "/v1/device-bindings",
            headers=headers,
            json={
                "device_claim_token": token,
                "declared_mode": "family_shared",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "family_member_of",
                    "subject_draft": {
                        "display_name": "谎称成人",
                        "age_band": "adult",
                        "age_evidence_status": "verified",
                    },
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "family_shared",
                    "shared_persona_enabled": True,
                },
                "consent_offer_ids": ["offer_family_space_v1"],
            },
        )
        assert forged.status_code == 422
        # Declaring age_band=adult in the draft fails closed into an unknown,
        # unverified subject.
        token = mint_device_binding_token(
            device_id="device-draft-age-2",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="claim-draft-age-2",
        )
        declared = await client.post(
            "/v1/device-bindings",
            headers=headers,
            json={
                "device_claim_token": token,
                "declared_mode": "family_shared",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "family_member_of",
                    "subject_draft": {
                        "display_name": "谎称成人",
                        "age_band": "adult",
                    },
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "family_shared",
                    "shared_persona_enabled": True,
                },
                "consent_offer_ids": ["offer_family_space_v1"],
            },
        )
        assert declared.status_code == 201
        subject_id = declared.json()["primary_subject_ids"][0]
        person = await app.state.identity_service.get_person(
            subject_id, actor_person_id=owner["user_id"]
        )
        assert person.subject_category == "unknown"
        assert person.age_evidence_status == "unverified"


@pytest.mark.asyncio
async def test_plain_member_can_confirm_self_but_not_switch_others(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "plain-member")
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
        relation_type="family_member_of",
        source=member.person_id,
        target=owner.person_id,
        now=now,
    )
    await identity.create_binding(
        device_id="device-plain-member",
        declared_mode="family_shared",
        account_owner_person_id=owner.person_id,
        primary_subject_ids=(child.person_id,),
        roles=(
            (owner.person_id, "device_admin"),
            (owner.person_id, "member"),
            (member.person_id, "member"),
        ),
        family_space_id="family-plain-member",
        service_profile_version="family_shared-v1",
        policy_bundle_version="multi-subject-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )

    base = await control.ensure_profile(
        device_id="device-plain-member",
        session_id=None,
        actor_id=member.person_id,
        now=now,
    )
    session_id = base.session_id

    self_confirmed = await control.switch_subject(
        session_id=session_id,
        subject_id=member.person_id,
        actor_id=member.person_id,
        now=now,
    )
    assert self_confirmed.active_subject_id == member.person_id
    assert self_confirmed.speaker_state == "confirmed"

    with pytest.raises(SubjectSwitchForbiddenError):
        await control.switch_subject(
            session_id=session_id,
            subject_id=child.person_id,
            actor_id=member.person_id,
            now=now,
        )

    owner_confirmed = await control.switch_subject(
        session_id=session_id,
        subject_id=child.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    assert owner_confirmed.active_subject_id == child.person_id


@pytest.mark.asyncio
async def test_parent_for_child_binding_attests_the_guardianship(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The binding owner's declaration is the guardianship, recorded as theirs.

    Product decision (2026-09-25): a phone-verified adult who binds a device
    for a child with no account and declares guardianship is that child's
    guardian. The child cannot confirm an endpoint, so the relationship is
    active on the guardian's attestation alone and says so: no confirmation
    is attributed to the child, no guardian link is manufactured, and memory
    consent exists only when the guardian ticked it.
    """

    app = _env(monkeypatch, tmp_path, "guardian-declaration")
    now = datetime.now(UTC)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "guardian-declaration-owner")
        await app.state.identity_service.register_person(
            person_id=owner["user_id"],
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=now,
        )
        token = mint_device_binding_token(
            device_id="device-guardian-declaration",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="guardian-declaration-1",
        )
        created = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {owner['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "parent_for_child",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "guardian_of",
                    "subject_draft": {
                        "display_name": "独立小明",
                        "age_band": "under_14",
                    },
                },
                "persona_selection": "starlight",
                "consent_offer_ids": ["offer_minor_voice_session_v1"],
            },
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        assert child_id != owner["user_id"]

        relationships = await app.state.identity_service.list_relationships(
            person_id=child_id
        )
        assert len(relationships) == 1
        declaration = relationships[0]
        assert declaration.relation_type == "guardian_of"
        assert declaration.source_person_id == owner["user_id"]
        assert declaration.target_person_id == child_id
        assert declaration.status == "active"
        assert declaration.confirmed_by_source_at is not None
        assert declaration.confirmed_by_target_at is None
        assert declaration.established_evidence_id == "guardian_attestation_v1:device_binding"

        # Only the guardian's own attestation is audited; nothing is
        # attributed to the child.
        # The action vocabulary differs by backend (the service names it
        # relationship.attest; PostgreSQL's audit trigger records the insert
        # as relationship.proposed), so assert the substance: one event on
        # this relationship, attributed to the guardian.
        actions = (
            app_store.fetch_all(
                "",
                "SELECT action, actor_person_id FROM identity_audit_events "
                "WHERE relationship_id = ?",
                (declaration.relationship_id,),
            )
            if app_store.postgres_active()
            else [
                (event.action, event.actor_person_id)
                for event in app.state.identity_store.audit_events()
                if event.relationship_id == declaration.relationship_id
            ]
        )
        assert len(actions) == 1
        assert actions[0][0] in {"relationship.attest", "relationship.proposed"}
        assert actions[0][1] == owner["user_id"]

        # The attestation must not manufacture a guardian link, and memory
        # consent exists only when the guardian ticked it (not here).
        guardian_store = app.state.guardian_store
        assert (
            await guardian_store.active_link(
                guardian_user_id=owner["user_id"],
                minor_user_id=child_id,
            )
            is None
        )
        assert (
            await guardian_store.active_consent(
                minor_user_id=child_id,
                consent_kind="memory_retention",
            )
            is None
        )
        assert (
            await guardian_store.active_consent(
                minor_user_id=child_id,
                consent_kind="minor_voice_session",
            )
            is None
        )
        # The notification path sees the declaring guardian under that name.
        assert await app.state.identity_service.declared_guardians(
            subject_person_id=child_id
        ) == (owner["user_id"],)


@pytest.mark.asyncio
async def test_third_party_self_declared_guardian_is_excluded_from_notifications(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """P0-04：知道孩子 person id 不等于获得监护授权。

    A logged-in stranger can still invite ``guardian_of`` to a known person id
    and accept it themselves — that is a real pending declaration in Identity.
    It must not, however, make them a declared guardian for the subject, nor
    earn them the ``guardian`` role on the owner's device binding.
    """

    app = _env(monkeypatch, tmp_path, "guardian-source-constraint")
    now = datetime.now(UTC)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "source-constraint-owner")
        await app.state.identity_service.register_person(
            person_id=owner["user_id"],
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=now,
        )
        token = mint_device_binding_token(
            device_id="device-source-constraint",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="source-constraint-1",
        )
        created = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {owner['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "parent_for_child",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "guardian_of",
                    "subject_draft": {
                        "display_name": "独立小明",
                        "age_band": "under_14",
                    },
                },
                "persona_selection": "starlight",
                "consent_offer_ids": ["offer_minor_voice_session_v1"],
            },
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        assert await app.state.identity_service.declared_guardians(
            subject_person_id=child_id
        ) == (owner["user_id"],)

        stranger = await _register(client, "source-constraint-stranger")
        await app.state.identity_service.register_person(
            person_id=stranger["user_id"],
            display_name="外人",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=now,
        )
        stranger_headers = {"Authorization": f"Bearer {stranger['access_token']}"}
        invited = await client.post(
            "/v1/relationships/invites",
            headers=stranger_headers,
            json={
                "target_person_id": child_id,
                "relation_type": "guardian_of",
                "established_evidence_id": "self-asserted-evidence",
            },
        )
        assert invited.status_code == 201, invited.text
        accepted = await client.post(
            f"/v1/relationships/{invited.json()['relationship_id']}/accept",
            headers=stranger_headers,
            json={},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "pending"

        # The declaration exists and is source-confirmed, yet it is not a
        # binding-scoped declaration, so the notification path never sees it.
        assert await app.state.identity_service.declared_guardians(
            subject_person_id=child_id
        ) == (owner["user_id"],)

        # The stranger also cannot take the guardian role on the owner's
        # parent_for_child binding.
        with pytest.raises(ModeConstraintError, match="account owner"):
            await app.state.identity_service.create_binding(
                device_id="device-stranger-guardian",
                declared_mode="parent_for_child",
                account_owner_person_id=owner["user_id"],
                primary_subject_ids=(child_id,),
                roles=((stranger["user_id"], "guardian"),),
                service_profile_version="parent_for_child-v1",
                policy_bundle_version="multi-subject-v1",
                now=now,
            )


@pytest.mark.asyncio
async def test_guardian_can_confirm_primary_subject_but_not_other_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "guardian-only")
    now = datetime.now(UTC)
    identity = app.state.identity_service
    control = app.state.multi_subject_runtime
    owner = await identity.register_person(
        person_id="person-owner",
        display_name="设备管理员",
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
        person_id="person-other",
        display_name="其他成员",
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
        device_id="device-guardian-only",
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
        device_id="device-guardian-only",
        session_id=None,
        actor_id=guardian.person_id,
        now=now,
    )
    session_id = base.session_id

    child_confirmed = await control.switch_subject(
        session_id=session_id,
        subject_id=child.person_id,
        actor_id=guardian.person_id,
        now=now,
    )
    assert child_confirmed.active_subject_id == child.person_id

    with pytest.raises(SubjectSwitchForbiddenError):
        await control.switch_subject(
            session_id=session_id,
            subject_id=member.person_id,
            actor_id=guardian.person_id,
            now=now,
        )

    with pytest.raises(SubjectSwitchForbiddenError):
        await control.switch_subject(
            session_id=session_id,
            subject_id=child.person_id,
            actor_id=member.person_id,
            now=now,
        )

    member_self = await control.switch_subject(
        session_id=session_id,
        subject_id=member.person_id,
        actor_id=member.person_id,
        now=now,
    )
    assert member_self.active_subject_id == member.person_id

    admin_confirmed = await control.switch_subject(
        session_id=session_id,
        subject_id=child.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    assert admin_confirmed.active_subject_id == child.person_id


@pytest.mark.asyncio
async def test_unauthorized_claim_is_ignored_and_cannot_cut_off_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "claim-ignored")
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
        relation_type="family_member_of",
        source=member.person_id,
        target=owner.person_id,
        now=now,
    )
    await identity.create_binding(
        device_id="device-claim-ignored",
        declared_mode="family_shared",
        account_owner_person_id=owner.person_id,
        primary_subject_ids=(child.person_id,),
        roles=(
            (owner.person_id, "device_admin"),
            (owner.person_id, "member"),
            (member.person_id, "member"),
        ),
        family_space_id="family-claim-ignored",
        service_profile_version="family_shared-v1",
        policy_bundle_version="multi-subject-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )

    base = await control.ensure_profile(
        device_id="device-claim-ignored",
        session_id=None,
        actor_id=owner.person_id,
        now=now,
    )
    session_id = base.session_id
    confirmed_child = await control.switch_subject(
        session_id=session_id,
        subject_id=child.person_id,
        actor_id=owner.person_id,
        now=now,
    )
    assert confirmed_child.active_subject_id == child.person_id
    assert confirmed_child.session_epoch == 2

    # A plain member claiming another person has no switch authority: the
    # claim is ignored and the confirmed session is not cut off.
    ignored = await control.resolve_subject(
        device_id="device-claim-ignored",
        session_id=None,
        actor_id=member.person_id,
        now=now,
        client_claimed_person_id=owner.person_id,
    )
    # The claim is ignored: the session remains confirmed, and the response
    # must not start a confirmation flow the state cannot satisfy.
    assert ignored.resolution == "confirmed"
    assert ignored.profile.active_subject_id == child.person_id
    assert ignored.profile.session_epoch == 2

    # A member claiming themselves is a legitimate mismatch signal and may
    # transition to unknown-safe.
    self_transition = await control.resolve_subject(
        device_id="device-claim-ignored",
        session_id=None,
        actor_id=member.person_id,
        now=now,
        client_claimed_person_id=member.person_id,
    )
    assert self_transition.resolution == "confirmation_required"
    assert self_transition.profile.active_subject_id is None
    assert self_transition.profile.speaker_state == "unconfirmed"
    assert self_transition.profile.service_mode == "unknown_safe"
    assert self_transition.profile.session_epoch == 3
    # The member who initiated the authorized transition becomes the profile's
    # actor; the old subject's identity is not frozen into the unknown-safe
    # profile.
    assert self_transition.profile.actor_id == member.person_id

    # The transition actor may decide on the unknown-safe profile...
    matched = await control.decide(
        runtime_profile_id=self_transition.profile.runtime_profile_id,
        capability="chat",
        actor_id=member.person_id,
        data_classification="public",
        safety_state="normal",
        now=now,
    )
    assert matched.effect == "allow_with_obligations"
    # ...but the previous subject or the owner cannot borrow it.
    with pytest.raises(PolicyActorMismatchError):
        await control.decide(
            runtime_profile_id=self_transition.profile.runtime_profile_id,
            capability="chat",
            actor_id=child.person_id,
            data_classification="public",
            safety_state="normal",
            now=now,
        )
    with pytest.raises(PolicyActorMismatchError):
        await control.decide(
            runtime_profile_id=self_transition.profile.runtime_profile_id,
            capability="chat",
            actor_id=owner.person_id,
            data_classification="public",
            safety_state="normal",
            now=now,
        )

    # The pre-transition profile is stale and its decisions fail closed.
    with pytest.raises(RuntimeProfileRejected):
        await control.decide(
            runtime_profile_id=confirmed_child.runtime_profile_id,
            capability="chat",
            actor_id=owner.person_id,
            data_classification="public",
            safety_state="normal",
            now=now,
        )


@pytest.mark.asyncio
async def test_advertised_subject_confirmation_methods_match_the_write_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A resolution must not advertise a confirmation method the write API rejects.

    ``voice_question`` used to be advertised while ``SwitchActiveSubjectRequest``
    only accepts ``app_confirm``, so a client could offer a flow whose request
    came back 422. A voice answer is not identity evidence until a trusted
    voice-evidence channel exists, so the advertisement is the part that had to
    change.
    """

    app = _env(monkeypatch, tmp_path, "confirmation-methods")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "confirmation-methods-owner")
        await app.state.identity_service.register_person(
            person_id=owner["user_id"],
            display_name="主人",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=datetime.now(UTC),
        )
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-confirmation-methods",
            nonce="confirmation-methods-1",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        profile = await client.get(
            "/v1/devices/device-confirmation-methods/runtime-profile",
            headers=headers,
        )
        assert profile.status_code == 200, profile.text
        session_id = profile.json()["session_id"]

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={"device_id": "device-confirmation-methods", "environment": {}},
        )
        assert resolution.status_code == 200, resolution.text
        advertised = set(resolution.json()["allowed_confirmation_methods"])
        assert advertised == {"app_confirm"}

        accepted = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": owner["user_id"], "confirmation_method": "app_confirm"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["active_subject_id"] == owner["user_id"]

        # The method that is no longer advertised stays rejected, so the
        # advertisement is the only lie that could have existed.
        rejected = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": owner["user_id"], "confirmation_method": "voice_question"},
        )
        assert rejected.status_code == 422
