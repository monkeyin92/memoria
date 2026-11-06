"""Runtime-control membership, subject-switch and edge-notification tests
for the multi-subject API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.device_control import RuntimeProfileLedger
from services.control_api.app.main import create_app
from services.control_api.tests.identity_test_helpers import (
    assert_stranger_denied,
    install_test_identity_authority,
)
from services.control_api.tests.multi_subject_test_support import (
    _bind_family,
    _bind_self,
    _env,
    _register,
)


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
        assert_stranger_denied(blocked)

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=stranger_headers,
            json={"device_id": "device-membership", "environment": {}},
        )
        assert_stranger_denied(resolution)

        switch = await client.post(
            f"/v1/sessions/{profile['session_id']}/active-subject",
            headers=stranger_headers,
            json={
                "person_id": owner["user_id"],
                "confirmation_method": "app_confirm",
            },
        )
        assert_stranger_denied(switch)

        decision = await client.post(
            "/v1/policy/decisions",
            headers=stranger_headers,
            json={
                "runtime_profile_id": profile["runtime_profile_id"],
                "capability": "chat",
                "data_classification": "public",
            },
        )
        assert_stranger_denied(decision)


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
async def test_switch_active_subject_advances_device_profile_and_notifies_edge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "subject-device-notify")
    delivered: list[dict[str, object]] = []

    async def invalidate(**payload: object) -> dict[str, object]:
        delivered.append(dict(payload))
        return {"delivered": True}

    app.state.device_runtime_invalidator = invalidate
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "subject-device-notify-owner")
        child_id = await _bind_family(
            client,
            app,
            owner=owner,
            device_id="device-subject-notify",
            nonce="claim-subject-notify",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        unresolved = await client.get(
            "/v1/devices/device-subject-notify/runtime-profile",
            headers=headers,
        )
        assert unresolved.status_code == 200
        session_id = unresolved.json()["session_id"]
        store = app.state.memory_store
        before = RuntimeProfileLedger(store).current("device-subject-notify")
        assert before is not None

        switched = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": child_id, "confirmation_method": "app_confirm"},
        )
        assert switched.status_code == 200
        profile = switched.json()
        assert profile["active_subject_id"] == child_id

        after = RuntimeProfileLedger(store).current("device-subject-notify")
        assert after is not None
        assert after.profile_version == before.profile_version + 1
        assert after.runtime_profile_id == profile["runtime_profile_id"]
        assert delivered == [
            {
                "device_id": "device-subject-notify",
                "profile_version": after.profile_version,
                "apply_at": "next_safe_point",
            }
        ]


@pytest.mark.asyncio
async def test_repeated_switch_to_same_subject_does_not_renotify_edge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "subject-idempotent")
    delivered: list[dict[str, object]] = []

    async def invalidate(**payload: object) -> dict[str, object]:
        delivered.append(dict(payload))
        return {"delivered": True}

    app.state.device_runtime_invalidator = invalidate
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "subject-idempotent-owner")
        child_id = await _bind_family(
            client,
            app,
            owner=owner,
            device_id="device-subject-idempotent",
            nonce="claim-subject-idempotent",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        unresolved = await client.get(
            "/v1/devices/device-subject-idempotent/runtime-profile",
            headers=headers,
        )
        session_id = unresolved.json()["session_id"]

        first = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": child_id, "confirmation_method": "app_confirm"},
        )
        assert first.status_code == 200
        assert len(delivered) == 1

        # Re-confirming the already active subject rotates the epoch but leaves
        # the stable profile identity unchanged, so the device version must not
        # advance and Edge must not be woken again.
        store = app.state.memory_store
        version_after_first = RuntimeProfileLedger(store).current(
            "device-subject-idempotent"
        )
        assert version_after_first is not None
        second = await client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": child_id, "confirmation_method": "app_confirm"},
        )
        assert second.status_code == 200
        assert second.json()["session_epoch"] > first.json()["session_epoch"]
        version_after_second = RuntimeProfileLedger(store).current(
            "device-subject-idempotent"
        )
        assert version_after_second is not None
        assert version_after_second.profile_version == version_after_first.profile_version
        assert len(delivered) == 1


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
