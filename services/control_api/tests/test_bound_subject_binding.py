"""Binding, guardian toggles and unbind drive the bound subject's standing consents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.consent.bound_subject import (
    GUARDIAN_MEMORY_CAPABILITIES,
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
        ("guardian", GUARDIAN_MEMORY_CAPABILITIES),
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
async def test_unbind_withdraws_consents_and_refuses_erasure_when_unwired(
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

        # A deployment without the subject saga must refuse, not pretend.
        wired = app.state.subject_deletion
        app.state.subject_deletion = None
        refused = await client.post(
            "/v1/devices/device-bound-unbind/binding/unbind",
            headers=headers,
            json={"reason": "unbind", "purge_subject_data": True},
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "subject_deletion_unavailable"
        # Nothing was unbound by the refused request.
        assert recorder.revokes == []
        app.state.subject_deletion = wired

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
            ("guardian", GUARDIAN_MEMORY_CAPABILITIES)
        ]

        revoked = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{granted.json()['consent_id']}",
            headers={**headers, "Idempotency-Key": "toggle-revoke-0001"},
        )
        assert revoked.status_code == 200, revoked.text
        assert [item["capabilities"] for item in recorder.revokes] == [GUARDIAN_MEMORY_CAPABILITIES]

        exported = await client.post(f"/v1/guardian/minors/{child_id}/export", headers=headers)
        assert exported.status_code == 200, exported.text
        from services.control_api.app.response_plan_cache import ResponsePlanCache

        cache = ResponsePlanCache()
        app.state.response_plan_cache = cache
        child_plan = ("session-child", 1, 1, 0, "v1", child_id)
        owner_plan = ("session-owner", 1, 1, 0, "v1", owner_id)
        await cache.put(child_plan, "fp", {"private": "child"})
        await cache.put(owner_plan, "fp", {"private": "owner"})
        deleted = await client.post(
            f"/v1/guardian/minors/{child_id}/delete",
            headers=headers,
            json={"confirmation": "永久删除孩子的全部数据"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "completed"
        # The child's cached plans are gone; the owner's are untouched.
        assert await cache.get(child_plan, "fp") is None
        assert await cache.get(owner_plan, "fp") == {"private": "owner"}


_ARCHIVE_TOKEN = {"X-Memoria-Internal-Token": "guardian-test-archive-token-that-is-long-enough"}


async def _speech(client: AsyncClient, *, session_id: str, event_id: str, subject: str | None) -> None:
    body: dict[str, Any] = {
        "event_id": event_id,
        "session_id": session_id,
        "event_type": "speech.utterance_finalized",
        "occurred_at": datetime.now(UTC).isoformat(),
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "turn_id": 1,
        "generation_id": 1,
        "tool_epoch": 0,
        "payload": {"text": f"{event_id} 的原话"},
    }
    if subject is not None:
        body["active_subject_id"] = subject
    response = await client.post("/v1/archive/session-events", headers=_ARCHIVE_TOKEN, json=body)
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_deleting_a_childs_data_keeps_the_parents_and_unbind_redacts_the_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.identity.service import REDACTED_DISPLAY_NAME

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, headers = await _owner(client, app, "bound-erase-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-erase",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        child_session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        await _speech(client, session_id=child_session["session_id"], event_id="child-turn", subject=child_id)
        parent_session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        await _speech(client, session_id=parent_session["session_id"], event_id="parent-turn", subject=None)

        deleted = await client.post(
            f"/v1/guardian/minors/{child_id}/delete",
            headers=headers,
            json={"confirmation": "永久删除孩子的全部数据"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "completed"

        archive = app.state.life_archive
        assert deleted.json()["deleted_counts"]["lineage.owner_events"] == 1, deleted.text
        assert (await archive.event(account_id=owner_id, event_id="child-turn")) is None
        assert (await archive.event(account_id=owner_id, event_id="parent-turn")) is not None
        # The device still serves the child, so their name stays until unbind.
        child = await app.state.identity_service.get_person(child_id, actor_person_id=owner_id)
        assert child.display_name == "使用人"

        unbound = await client.post(
            "/v1/devices/device-bound-erase/binding/unbind",
            headers=headers,
            json={"reason": "unbind", "purge_subject_data": True},
        )
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["subject_deletion"] == "completed"
        child = await app.state.identity_service.get_person(child_id, actor_person_id=owner_id)
        assert (child.display_name, child.status) == (REDACTED_DISPLAY_NAME, "disabled")


@pytest.mark.asyncio
async def test_accountless_child_weekly_summary_follows_long_term_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The weekly summary opens with the child's long-term memory (2026-09-26).

    A one-to-one device stores the child's turns in the binding owner's
    account, attributed to the child; the summary aggregates exactly those
    rows and never the owner's own.
    """

    from services.archive.domain import EvidenceEvent

    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "bound-summary-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-bound-summary",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        now = datetime.now(UTC)
        eligible = {"history_eligible": True, "owner_projection_eligible": True}
        for event_id, subject, label in (
            ("summary-child-1", child_id, "happy"),
            ("summary-child-2", child_id, "sad"),
            ("summary-owner", owner_id, "angry"),
        ):
            await app.state.life_archive.record(
                EvidenceEvent(
                    event_id=event_id,
                    account_id=owner_id,
                    subject_id=subject,
                    event_type="emotion_observation",
                    occurred_at=now,
                    speaker_class="owner",
                    source="test",
                    payload={"label": label, **eligible},
                )
            )

        closed = await client.get(f"/v1/guardian/minors/{child_id}/summary", headers=headers)
        granted = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**headers, "Idempotency-Key": "summary-grant-0001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert granted.status_code == 201, granted.text
        opened = await client.get(f"/v1/guardian/minors/{child_id}/summary", headers=headers)
        revoked = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{granted.json()['consent_id']}",
            headers={**headers, "Idempotency-Key": "summary-revoke-0001"},
        )
        assert revoked.status_code == 200, revoked.text
        reclosed = await client.get(f"/v1/guardian/minors/{child_id}/summary", headers=headers)

    assert closed.status_code == 403, closed.text
    assert closed.json()["detail"]["code"] == "guardian_consent_required"
    assert opened.status_code == 200, opened.text
    summary = opened.json()
    assert summary["minor_user_id"] == child_id
    assert summary["source_event_count"] == 2
    assert summary["emotion_distribution"].get("happy") == 1
    assert summary["emotion_distribution"].get("sad") == 1
    # The binding owner's own row never enters the child's summary.
    assert "angry" not in {k for k, v in summary["emotion_distribution"].items() if v}
    assert reclosed.status_code == 403, reclosed.text


@pytest.mark.asyncio
async def test_binder_sees_only_style_labels_and_can_reset_the_bound_persona(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1-03: the binder's view of a child's or elder's persona."""

    from services.persona.domain import PersonaCapsule, PersonaCapsuleEntry

    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    requests: list[Any] = []
    forgotten: list[tuple[str, str]] = []

    async def capsule(request: Any) -> Any:
        requests.append(request)
        return PersonaCapsule(
            version_id="child-v1",
            version_number=2,
            entries=(
                PersonaCapsuleEntry(
                    trait_id="t1", category="sentence_length",
                    description="日常表达偏好短句，先给出核心意思", context="",
                    counterexample="", confidence=0.9, source_event_ids=(),
                ),
            ),
            prompt_fragment="",
        )

    async def forget_subject(*, account_id: str, subject_id: str) -> int:
        forgotten.append((account_id, subject_id))
        return 7

    monkeypatch.setattr(app.state.persona_engine, "capsule", capsule)
    monkeypatch.setattr(app.state.persona_engine, "forget_subject", forget_subject)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "persona-view-owner")
        created = await _bind(
            client, app, owner_id=owner_id, headers=headers, device_id="device-persona-view",
            declared_mode="parent_for_child", relationship="guardian_of", age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        _, other_headers = await _owner(client, app, "persona-view-stranger")

        style = await client.get(f"/v1/persona/subjects/{child_id}/style", headers=headers)
        stranger = await client.get(f"/v1/persona/subjects/{child_id}/style", headers=other_headers)
        own = await client.get(f"/v1/persona/subjects/{owner_id}/style", headers=headers)
        stranger_reset = await client.post(
            f"/v1/persona/subjects/{child_id}/reset", headers=other_headers
        )
        reset = await client.post(f"/v1/persona/subjects/{child_id}/reset", headers=headers)

    assert style.status_code == 200, style.text
    assert style.json() == {
        "subject_id": child_id,
        "version_number": 2,
        "style_labels": ["日常表达偏好短句，先给出核心意思"],
        "descriptions_included": False,
    }
    sent = requests[0]
    assert (sent.account_id, sent.subject_id) == (owner_id, child_id)
    assert sent.speaker_class == "uncertain" and sent.confirmed_style_only is True
    assert stranger.status_code == 403
    assert own.status_code == 404
    assert stranger_reset.status_code == 403
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"subject_id": child_id, "deleted_rows": 7}
    assert forgotten == [(owner_id, child_id)]
