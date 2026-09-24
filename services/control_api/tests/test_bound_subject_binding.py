"""Binding, guardian toggles and unbind drive the bound subject's standing consents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.consent.bound_subject import (
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectGrant,
)
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.tests.test_guardian_api import _configure, _mark_verified_adult


@dataclass
class _RecordingConsent:
    """Stands in for the PostgreSQL consent authority; records every call."""

    grants: list[BoundSubjectGrant] = field(default_factory=list)
    revokes: list[dict[str, Any]] = field(default_factory=list)

    async def grant(self, request: BoundSubjectGrant) -> tuple[()]:
        self.grants.append(request)
        return ()

    async def revoke(self, **kwargs: Any) -> int:
        self.revokes.append(kwargs)
        return 1


async def _owner(client: AsyncClient, app: Any, username: str) -> tuple[str, dict[str, str]]:
    now = datetime.now(UTC)
    registered = await client.post(
        "/v1/auth/register", json={"username": username, "password": "safe-password-123"}
    )
    assert registered.status_code == 201, registered.text
    owner_id = registered.json()["user_id"]
    _mark_verified_adult(app, owner_id)
    app.state.memory_store.bind_external_identities(
        preferred_user_id=owner_id,
        identities={"wechat_openid": f"wx-{username}"},
        now=now.isoformat(),
    )
    await app.state.identity_service.register_person(
        person_id=owner_id,
        display_name="家长",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id=f"fixture-{username}",
        now=now,
    )
    login = await client.post(
        "/v1/auth/login", json={"username": username, "password": "safe-password-123"}
    )
    return owner_id, {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _bind(
    client: AsyncClient,
    app: Any,
    *,
    owner_id: str,
    headers: dict[str, str],
    device_id: str,
    declared_mode: str,
    relationship: str,
    age_band: str,
    offers: list[str],
    preferences: dict[str, object] | None = None,
) -> Any:
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=datetime.now(UTC),
        ttl=timedelta(minutes=5),
        nonce=f"nonce-{device_id}",
    )
    return await client.post(
        "/v1/device-bindings",
        headers={**headers, "Idempotency-Key": f"bind-{device_id}"},
        json={
            "device_claim_token": token,
            "declared_mode": declared_mode,
            "account_owner_person_id": owner_id,
            "primary_subject": {
                "person_id": "new",
                "relationship": relationship,
                "subject_draft": {"display_name": "使用人", "age_band": age_band},
            },
            "persona_selection": "starlight",
            "service_preferences": preferences or {},
            "consent_offer_ids": offers,
        },
    )


@pytest.mark.asyncio
async def test_child_binding_grants_chat_and_ticked_memory_as_the_guardian(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "bound-child-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-child",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1", "offer_minor_memory_retention_v1"],
            preferences={"memory_level": "growth_summary"},
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]

    assert [(g.kind, g.capabilities) for g in recorder.grants] == [
        ("guardian", MINOR_SESSION_CAPABILITIES),
        ("guardian", MEMORY_CAPABILITIES),
    ]
    assert {(g.actor_person_id, g.subject_person_id) for g in recorder.grants} == {
        (owner_id, child_id)
    }
    # The retention ceiling's Guardian ledger records the same consent.
    assert (
        await app.state.guardian_store.active_consent(
            minor_user_id=child_id, consent_kind="memory_retention"
        )
        is not None
    )


@pytest.mark.asyncio
async def test_child_binding_without_the_memory_box_grants_no_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "bound-child-nomem")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-nomem",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
            preferences={"memory_level": "none"},
        )
        assert created.status_code == 201, created.text
        # Ticking the memory box while declaring memory off is contradictory.
        contradictory = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-contradiction",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1", "offer_minor_memory_retention_v1"],
            preferences={"memory_level": "none"},
        )
        assert contradictory.status_code == 422, contradictory.text

    assert [(g.kind, g.capabilities) for g in recorder.grants] == [
        ("guardian", MINOR_SESSION_CAPABILITIES)
    ]


@pytest.mark.asyncio
async def test_elder_binding_registers_an_adult_and_grants_memory_as_a_delegate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "bound-elder-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-elder",
            declared_mode="child_for_parent",
            relationship="child_of",
            age_band="adult",
            offers=["offer_admin_device_management_v1", "offer_senior_memory_retention_v1"],
            preferences={"memory_level": "personal"},
        )
        assert created.status_code == 201, created.text
        elder_id = created.json()["primary_subject_ids"][0]
        elder = await app.state.identity_service.get_person(elder_id, actor_person_id=owner_id)
        relationships = await app.state.identity_service.list_relationships(person_id=elder_id)

    # The adult child vouched for the parent's age and is their delegate.
    assert (elder.subject_category, elder.age_evidence_status) == ("adult", "verified")
    assert [(r.relation_type, r.status, r.established_evidence_id) for r in relationships] == [
        ("delegate_for", "active", "delegate_attestation_v1:device_binding")
    ]
    assert [(g.kind, g.actor_person_id, g.capabilities) for g in recorder.grants] == [
        ("delegate", owner_id, MEMORY_CAPABILITIES)
    ]


@pytest.mark.asyncio
async def test_unbind_withdraws_consents_and_refuses_an_erasure_it_cannot_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "bound-unbind-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-unbind",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1", "offer_minor_memory_retention_v1"],
            preferences={"memory_level": "growth_summary"},
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]

        refused = await client.post(
            "/v1/devices/device-bound-unbind/binding/unbind",
            headers=headers,
            json={"reason": "unbind", "purge_subject_data": True},
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "subject_deletion_unavailable"
        # Nothing was unbound by the refused request.
        assert recorder.revokes == []

        unbound = await client.post(
            "/v1/devices/device-bound-unbind/binding/unbind",
            headers=headers,
            json={"reason": "unbind", "purge_subject_data": False},
        )
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["consents_withdrawn"] >= 1

    assert [(item["actor_person_id"], item["subject_person_id"]) for item in recorder.revokes] == [
        (owner_id, child_id)
    ]
    assert (
        await app.state.guardian_store.active_consent(
            minor_user_id=child_id, consent_kind="memory_retention"
        )
        is None
    )


@pytest.mark.asyncio
async def test_guardian_memory_toggle_moves_the_consent_authority_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "bound-toggle-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-toggle",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        recorder.grants.clear()

        granted = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**headers, "Idempotency-Key": "toggle-grant-0001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert granted.status_code == 201, granted.text
        assert [(g.kind, g.capabilities) for g in recorder.grants] == [
            ("guardian", MEMORY_CAPABILITIES)
        ]

        revoked = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{granted.json()['consent_id']}",
            headers={**headers, "Idempotency-Key": "toggle-revoke-0001"},
        )
        assert revoked.status_code == 200, revoked.text
        assert [item["capabilities"] for item in recorder.revokes] == [MEMORY_CAPABILITIES]

        exported = await client.post(f"/v1/guardian/minors/{child_id}/export", headers=headers)
        assert exported.status_code == 200, exported.text
        deleted = await client.post(
            f"/v1/guardian/minors/{child_id}/delete",
            headers=headers,
            json={"confirmation": "永久删除孩子的全部数据"},
        )
        # Without a wired subject saga the erase is refused, never faked.
        assert deleted.status_code == 503
        assert deleted.json()["detail"]["code"] == "child_subject_deletion_unavailable"
