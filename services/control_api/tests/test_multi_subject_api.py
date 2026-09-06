from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.app.multi_subject_runtime import (
    PolicyActorMismatchError,
    SubjectSwitchForbiddenError,
)
from services.control_api.app.routes import multi_subject as multi_subject_routes
from services.control_api.tests.identity_test_helpers import (
    install_test_identity_authority,
)
from services.session_runtime.profile_service import (
    RUNTIME_PROFILE_PAYLOAD_SCHEMA,
    RuntimeProfileRejected,
    verify_runtime_profile_payload,
)


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str):
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / f"{name}.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_IDENTITY_DB_PATH", str(tmp_path / f"{name}-identity.sqlite3")
    )
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", f"{name}-auth-secret-long-enough-0123456789")
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        f"{name}-transfer-evidence-secret-32-bytes",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    install_test_identity_authority(
        app,
        secret=app.state.settings.transfer_evidence_key(),
    )
    return app


async def _register(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    return response.json()


async def _bind_self(
    client: AsyncClient,
    app,
    *,
    owner: dict,
    device_id: str,
    nonce: str,
) -> None:
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=nonce,
    )
    response = await client.post(
        "/v1/device-bindings",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
        json={
            "device_claim_token": token,
            "declared_mode": "self_use",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": owner["user_id"],
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


@pytest.mark.asyncio
async def test_device_binding_recovery_and_lookup_are_actor_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "binding-recovery")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "binding-recovery-owner")
        stranger = await _register(client, "binding-recovery-stranger")
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-recovery-a",
            nonce="claim-recovery-a",
        )
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-recovery-b",
            nonce="claim-recovery-b",
        )

        identity = app.state.identity_service
        expected = [
            await identity.get_active_manifest(
                device_id,
                actor_person_id=owner["user_id"],
            )
            for device_id in ("device-recovery-a", "device-recovery-b")
        ]
        assert all(manifest is not None for manifest in expected)

        original_list = identity.list_active_manifests_for_person
        list_calls: list[tuple[str, str]] = []

        async def require_list_actor(
            person_id: str,
            now=None,
            *,
            actor_person_id: str | None = None,
        ):
            if person_id != owner["user_id"] or actor_person_id != owner["user_id"]:
                raise AssertionError("list recovery requires the authenticated owner")
            list_calls.append((person_id, actor_person_id))
            return await original_list(
                person_id,
                now=now,
                actor_person_id=actor_person_id,
            )

        monkeypatch.setattr(
            identity, "list_active_manifests_for_person", require_list_actor
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        unauthorized = await client.get("/v1/device-bindings")
        assert unauthorized.status_code == 401
        listed = await client.get("/v1/device-bindings", headers=headers)
        assert listed.status_code == 200
        assert listed.json() == {
            "bindings": [manifest.to_dict() for manifest in expected]
        }
        assert list_calls == [(owner["user_id"], owner["user_id"])]

        original_get = identity.get_active_manifest
        get_calls: list[tuple[str, str]] = []

        async def require_get_actor(
            device_id: str,
            *,
            actor_person_id: str | None = None,
        ):
            if actor_person_id is None:
                raise AssertionError("single-device lookup requires an identity actor")
            get_calls.append((device_id, actor_person_id))
            return await original_get(
                device_id,
                actor_person_id=actor_person_id,
            )

        monkeypatch.setattr(identity, "get_active_manifest", require_get_actor)
        owner_binding = await client.get(
            "/v1/devices/device-recovery-a/binding",
            headers=headers,
        )
        assert owner_binding.status_code == 200
        assert owner_binding.json() == expected[0].to_dict()

        stranger_binding = await client.get(
            "/v1/devices/device-recovery-a/binding",
            headers={"Authorization": f"Bearer {stranger['access_token']}"},
        )
        # SQLite mirrors fail closed as binding_forbidden; PostgreSQL RLS
        # makes the row invisible and the route emits binding_not_found.
        assert stranger_binding.status_code in (403, 404)
        assert stranger_binding.json()["detail"]["code"] in (
            "binding_forbidden",
            "binding_not_found",
        )
        assert ("device-recovery-a", owner["user_id"]) in get_calls
        assert ("device-recovery-a", stranger["user_id"]) in get_calls


async def _establish_relationship(
    identity,
    *,
    relation_type: str,
    source: str,
    target: str,
    now: datetime,
) -> None:
    """Two-party confirmed relationship required by binding role checks."""
    proposed = await identity.propose_relationship(
        source_person_id=source,
        target_person_id=target,
        relation_type=relation_type,  # type: ignore[arg-type]
        established_evidence_id=f"evidence-{relation_type}",
        actor_person_id=source,
        now=now,
    )
    await identity.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=source,
        now=now,
    )
    await identity.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=target,
        now=now,
    )


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


async def _bind_family(
    client: AsyncClient,
    app,
    *,
    owner: dict,
    device_id: str,
    nonce: str,
) -> str:
    now = datetime.now(UTC)
    # Fixture authority: pre-register the owner as a verified adult with an
    # explicit evidence id BEFORE the API binding call lazy-registers the
    # account person, so the API path itself never claims verified adult.
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
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=nonce,
    )
    response = await client.post(
        "/v1/device-bindings",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
        json={
            "device_claim_token": token,
            "declared_mode": "family_shared",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": "new",
                "relationship": "family_member_of",
                "subject_draft": {
                    "display_name": "小朋友",
                    "age_band": "under_14",
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
    assert response.status_code == 201
    child_id = response.json()["primary_subject_ids"][0]
    return child_id


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
async def test_family_runtime_profile_switches_child_parent_child_with_epoch_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_IDENTITY_DB_PATH", str(tmp_path / "identity.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "family-runtime-auth-secret-long-enough-0123456789")
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        "family-runtime-transfer-evidence-secret-32-bytes",
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
            json={"username": "family-owner", "password": "safe-password"},
        )
        identity = registered.json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        # Fixture authority: pre-register the owner as a verified adult with
        # an explicit evidence id before the binding call lazy-registers the
        # account person (the API path itself never claims verified adult).
        await app.state.identity_service.register_person(
            person_id=identity["user_id"],
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=now,
        )
        token = mint_device_binding_token(
            device_id="device-family",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="claim-family",
        )
        bound = await client.post(
            "/v1/device-bindings",
            headers=headers,
            json={
                "device_claim_token": token,
                "declared_mode": "family_shared",
                "account_owner_person_id": identity["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "family_member_of",
                    "subject_draft": {
                        "display_name": "小朋友",
                        "age_band": "under_14",
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
        assert bound.status_code == 201
        child_id = bound.json()["primary_subject_ids"][0]
        parent_id = identity["user_id"]

        unresolved = await client.get(
            "/v1/devices/device-family/runtime-profile",
            headers=headers,
        )
        assert unresolved.status_code == 200
        unresolved_profile = unresolved.json()
        assert unresolved_profile["service_mode"] == "unknown_safe"
        assert unresolved_profile["speaker_state"] == "unconfirmed"
        assert unresolved_profile["session_epoch"] == 1
        assert unresolved_profile["signature"]

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={"device_id": "device-family", "environment": {}},
        )
        assert resolution.status_code == 200
        assert resolution.json()["resolution"] == "confirmation_required"
        assert {item["person_id"] for item in resolution.json()["candidate_subjects"]} == {
            child_id,
            parent_id,
        }

        profiles = []
        for expected_epoch, subject_id in enumerate(
            (child_id, parent_id, child_id),
            start=2,
        ):
            switched = await client.post(
                f"/v1/sessions/{unresolved_profile['session_id']}/active-subject",
                headers=headers,
                json={
                    "person_id": subject_id,
                    "confirmation_method": "app_confirm",
                },
            )
            assert switched.status_code == 200
            profile = switched.json()
            assert profile["active_subject_id"] == subject_id
            assert profile["speaker_state"] == "confirmed"
            assert profile["session_epoch"] == expected_epoch
            profiles.append(profile)

        assert profiles[0]["service_mode"] == "student_minor"
        assert profiles[1]["service_mode"] == "family_shared"
        assert len({profile["runtime_profile_id"] for profile in profiles}) == 3

        # The owner must not borrow the child's signed profile for decisions.
        borrowed = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": profiles[-1]["runtime_profile_id"],
                "capability": "memory_recall_private",
                "data_classification": "private",
            },
        )
        assert borrowed.status_code == 403
        assert borrowed.json()["detail"]["code"] == "policy_actor_mismatch"

        # A matching actor (the owner confirming themselves) may decide.
        parent_again = await client.post(
            f"/v1/sessions/{unresolved_profile['session_id']}/active-subject",
            headers=headers,
            json={"person_id": parent_id, "confirmation_method": "app_confirm"},
        )
        assert parent_again.status_code == 200
        assert parent_again.json()["session_epoch"] == 5
        denied = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": parent_again.json()["runtime_profile_id"],
                "capability": "voice_clone_use",
                "data_classification": "biometric",
            },
        )
        assert denied.status_code == 200
        assert denied.json()["effect"] == "deny"


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
async def test_runtime_control_requires_binding_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "membership")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "membership-owner")
        stranger = await _register(client, "membership-stranger")
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-membership",
            nonce="claim-membership",
        )
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        stranger_headers = {"Authorization": f"Bearer {stranger['access_token']}"}

        profile_response = await client.get(
            "/v1/devices/device-membership/runtime-profile",
            headers=owner_headers,
        )
        assert profile_response.status_code == 200
        profile = profile_response.json()

        blocked = await client.get(
            "/v1/devices/device-membership/runtime-profile",
            headers=stranger_headers,
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "binding_forbidden"

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=stranger_headers,
            json={"device_id": "device-membership", "environment": {}},
        )
        assert resolution.status_code == 403

        switch = await client.post(
            f"/v1/sessions/{profile['session_id']}/active-subject",
            headers=stranger_headers,
            json={
                "person_id": owner["user_id"],
                "confirmation_method": "app_confirm",
            },
        )
        assert switch.status_code == 403

        decision = await client.post(
            "/v1/policy/decisions",
            headers=stranger_headers,
            json={
                "runtime_profile_id": profile["runtime_profile_id"],
                "capability": "chat",
                "data_classification": "public",
            },
        )
        assert decision.status_code == 403


@pytest.mark.asyncio
async def test_unknown_device_runtime_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "unknown-device")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "unknown-device-owner")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        profile_response = await client.get(
            "/v1/devices/no-such-device/runtime-profile",
            headers=headers,
        )
        assert profile_response.status_code == 404
        assert profile_response.json()["detail"]["code"] == "binding_not_found"

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={"device_id": "no-such-device", "environment": {}},
        )
        assert resolution.status_code == 404


@pytest.mark.asyncio
async def test_offline_and_multiple_speakers_fail_closed_to_unknown_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "device-state")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "device-state-owner")
        await _bind_self(
            client,
            app,
            owner=owner,
            device_id="device-state",
            nonce="claim-state",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        offline_profile = await client.get(
            "/v1/devices/device-state/runtime-profile",
            headers=headers,
            params={"offline": "true"},
        )
        assert offline_profile.status_code == 200
        body = offline_profile.json()
        assert body["service_mode"] == "unknown_safe"
        assert body["speaker_state"] == "unconfirmed"
        assert body["active_subject_id"] is None
        assert body["subject_category"] == "unknown"
        assert body["age_band"] == "unknown"
        assert "voice_clone_use" not in body["capabilities"]
        assert "issued_at" in body and "expires_at" in body
        assert body["runtime_profile_id"].startswith("rp_")

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": "device-state",
                "environment": {"offline": True},
            },
        )
        assert resolution.status_code == 200
        assert resolution.json()["resolution"] == "confirmation_required"
        assert resolution.json()["temporary_service_mode"] == "unknown_safe"

        multi = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": "device-state",
                "environment": {"multiple_speakers": True},
            },
        )
        assert multi.status_code == 200
        assert multi.json()["temporary_service_mode"] == "unknown_safe"


@pytest.mark.asyncio
async def test_switch_to_non_member_and_claimed_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "family-claims")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "family-claims-owner")
        stranger = await _register(client, "family-claims-stranger")
        child_id = await _bind_family(
            client,
            app,
            owner=owner,
            device_id="device-family-claims",
            nonce="claim-family-claims",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        profile_response = await client.get(
            "/v1/devices/device-family-claims/runtime-profile",
            headers=headers,
        )
        assert profile_response.status_code == 200
        session_id = profile_response.json()["session_id"]

        denied_switch = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={
                "person_id": stranger["user_id"],
                "confirmation_method": "app_confirm",
            },
        )
        assert denied_switch.status_code == 403
        assert (
            denied_switch.json()["detail"]["code"] == "subject_not_binding_member"
        )

        claimed = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": "device-family-claims",
                "session_id": "ses-claimed",
                "client_claimed_person_id": child_id,
                "environment": {},
            },
        )
        assert claimed.status_code == 200
        # A client claim is only a hint and must never self-confirm a subject.
        assert claimed.json()["resolution"] == "confirmation_required"
        assert claimed.json()["temporary_service_mode"] == "unknown_safe"
        assert child_id in {
            item["person_id"] for item in claimed.json()["candidate_subjects"]
        }

        no_confirmed_profile = await client.get(
            "/v1/devices/device-family-claims/runtime-profile",
            headers=headers,
            params={"session_id": "ses-claimed"},
        )
        assert no_confirmed_profile.status_code == 200
        assert no_confirmed_profile.json()["speaker_state"] == "unconfirmed"
        assert no_confirmed_profile.json()["active_subject_id"] is None


@pytest.mark.asyncio
async def test_claimed_person_cannot_change_existing_confirmed_subject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "claim-confirmed")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "claim-confirmed-owner")
        child_id = await _bind_family(
            client,
            app,
            owner=owner,
            device_id="device-claim-confirmed",
            nonce="claim-claim-confirmed",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        unresolved = await client.get(
            "/v1/devices/device-claim-confirmed/runtime-profile",
            headers=headers,
        )
        session_id = unresolved.json()["session_id"]
        confirmed_child = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": child_id, "confirmation_method": "app_confirm"},
        )
        assert confirmed_child.status_code == 200
        assert confirmed_child.json()["session_epoch"] == 2

        claimed_parent = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": "device-claim-confirmed",
                "client_claimed_person_id": owner["user_id"],
                "environment": {},
            },
        )
        assert claimed_parent.status_code == 200
        assert claimed_parent.json()["resolution"] == "confirmation_required"

        transitioned = await client.get(
            "/v1/devices/device-claim-confirmed/runtime-profile",
            headers=headers,
        )
        assert transitioned.status_code == 200
        # An authorized mismatch claim transitions to unknown-safe: the
        # previous subject's private capabilities are paused, not kept.
        assert transitioned.json()["active_subject_id"] is None
        assert transitioned.json()["speaker_state"] == "unconfirmed"
        assert transitioned.json()["service_mode"] == "unknown_safe"
        assert transitioned.json()["session_epoch"] == 3

        stale_decision = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": confirmed_child.json()["runtime_profile_id"],
                "capability": "chat",
                "data_classification": "public",
            },
        )
        assert stale_decision.status_code == 403
        assert stale_decision.json()["detail"]["code"] == "runtime_profile_rejected"

        claimed_child = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": "device-claim-confirmed",
                "client_claimed_person_id": child_id,
                "environment": {},
            },
        )
        assert claimed_child.status_code == 200
        # A claim never confirms on its own, even after a transition.
        assert claimed_child.json()["resolution"] == "confirmation_required"
        no_new_switch = await client.get(
            "/v1/devices/device-claim-confirmed/runtime-profile",
            headers=headers,
        )
        assert no_new_switch.json()["session_epoch"] == 3


@pytest.mark.asyncio
async def test_voice_question_confirmation_method_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "voice-question")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "voice-question-owner")
        child_id = await _bind_family(
            client,
            app,
            owner=owner,
            device_id="device-voice-question",
            nonce="claim-voice-question",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}

        unresolved = await client.get(
            "/v1/devices/device-voice-question/runtime-profile",
            headers=headers,
        )
        session_id = unresolved.json()["session_id"]
        switch = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={
                "person_id": child_id,
                "confirmation_method": "voice_question",
            },
        )
        assert switch.status_code == 422


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


class _FrozenClock:
    fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None) -> datetime:
        assert cls.fixed is not None
        return cls.fixed


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
