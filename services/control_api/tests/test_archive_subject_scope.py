"""Subject privacy at the real HTTP exits, including pre-fence evidence."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.control_api.app.main import create_app
from services.control_api.tests.test_archive_api import (
    PersonaObservationStub,
    _configure,
    _register_minor,
    _register_verified_adult,
    _response_provenance,
    _wav,
)
from services.guardian.domain import PersonConsentRecord

INTERNAL = {"X-Memoria-Internal-Token": "test-internal-archive-token"}


def _speech(session_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "event_id": "scope-speech",
        "session_id": session_id,
        "event_type": "speech.utterance_finalized",
        "occurred_at": datetime.now(UTC).isoformat(),
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "turn_id": 1,
        "generation_id": 1,
        "tool_epoch": 0,
        "payload": {"text": "需要隔离的逐字内容"},
        **overrides,
    }


@pytest.mark.asyncio
async def test_missing_subject_is_not_an_adult_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="missing-subject")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        response = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json=_speech(session["session_id"]),
        )
        assert response.status_code == 201, response.text
        event = await app.state.life_archive.event(
            account_id=owner["user_id"], event_id="scope-speech"
        )
        assert event.subject_id is None
        assert event.payload["memory_retention"] == "ephemeral_only"
        assert "text" not in event.payload
        assert event.payload["history_eligible"] is False


@pytest.mark.asyncio
async def test_assistant_inherits_parent_subject_and_rejects_substitution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="reply-subject")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        await app.state.identity_service.register_person(
            person_id="reply-child", display_name="孩子", timezone="Asia/Shanghai",
            subject_category="minor", age_band="under_14",
            age_evidence_status="unverified", now=datetime.now(UTC),
        )
        parent = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json=_speech(session["session_id"], active_subject_id="reply-child"),
        )
        assert parent.status_code == 201, parent.text
        reply = _speech(
            session["session_id"], event_id="scope-reply", speaker_class="assistant",
            event_type="assistant.playout_stopped",
            payload={"text": "给孩子的回复", "actual_heard": True},
        )
        substituted = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json={**reply, "active_subject_id": owner["user_id"]},
        )
        assert substituted.status_code == 409
        assert substituted.json()["detail"]["code"] == "parent_subject_mismatch"
        accepted = await client.post(
            "/v1/archive/session-events", headers=INTERNAL, json=reply
        )
        assert accepted.status_code == 201, accepted.text
        event = await app.state.life_archive.event(
            account_id=owner["user_id"], event_id="scope-reply"
        )
        assert event.subject_id == "reply-child"
        assert event.payload["memory_retention"] == "ephemeral_only"
        assert "text" not in event.payload


@pytest.mark.asyncio
async def test_same_account_history_and_timeline_exclude_other_and_unknown_subjects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-history")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        now = datetime.now(UTC)
        # Seed pre-fix retained rows, including an unknown speaker and a newer
        # unrelated session. The read fence must precede LIMIT in storage.
        for i, (subject, session) in enumerate(
            [(owner["user_id"], "shared"), ("child", "shared"), (None, "shared")]
            + [(owner["user_id"], "unrelated")] * 101
        ):
            await app.state.life_archive.record(EvidenceEvent(
                event_id=f"scope-{i}", account_id=owner["user_id"], subject_id=subject,
                event_type="speech.utterance_finalized", occurred_at=now + timedelta(seconds=i),
                speaker_class="owner", source="test.pre-fence",
                session_id=session, turn_id=i, generation_id=1,
                payload={"text": f"subject-canary-{i}", "history_eligible": True,
                         "owner_projection_eligible": True},
            ))
        history = await client.get(
            "/v1/archive/conversation-history", headers=headers,
            params={"session_id": "shared"},
        )
        assert history.status_code == 200, history.text
        assert [x["owner_event_id"] for x in history.json()["turns"]] == ["scope-0"]
        # Use a newer mixed-subject tail to exercise the account-wide exit.
        for i, subject in enumerate(("child", None), 200):
            await app.state.life_archive.record(EvidenceEvent(
                event_id=f"tail-{i}", account_id=owner["user_id"], subject_id=subject,
                event_type="speech.utterance_finalized", occurred_at=now + timedelta(seconds=i),
                speaker_class="owner", source="test.pre-fence",
                payload={"text": "OTHER-SUBJECT-CANARY"},
            ))
        timeline = await client.get("/v1/archive/timeline", headers=headers)
        assert timeline.status_code == 200, timeline.text
        assert "OTHER-SUBJECT-CANARY" not in timeline.text
        assert all(x["subject_id"] == owner["user_id"] for x in timeline.json()["items"])


@pytest.mark.asyncio
async def test_revoked_consent_blocks_every_private_read_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        minor = await _register_minor(client, app, username="scope-minor")
        headers = {"Authorization": f"Bearer {minor['access_token']}"}
        consent = PersonConsentRecord(
            consent_id="scope-consent", subject_person_id=minor["user_id"],
            grantor_person_id="guardian", consent_kind="memory_retention",
            policy_version="test-v1", granted_at=datetime.now(UTC), evidence_event_id="grant",
        )
        await app.state.guardian_store.grant_person_consent(consent, actor_person_id="guardian")
        await app.state.life_archive.record(EvidenceEvent(
            event_id="retained-before-revoke", account_id=minor["user_id"],
            subject_id=minor["user_id"], event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC), speaker_class="owner", source="test.pre-fence",
            session_id="prior-session", turn_id=1, generation_id=1,
            payload={"text": "REVOKED-CANARY", "history_eligible": True,
                     "owner_projection_eligible": True},
        ))
        before = await client.get("/v1/archive/timeline", headers=headers)
        assert before.status_code == 200, before.text
        assert "REVOKED-CANARY" in before.text
        await app.state.guardian_store.revoke_person_consent(
            consent_id=consent.consent_id, grantor_person_id="guardian",
            subject_person_id=minor["user_id"], revoked_at=datetime.now(UTC),
            revocation_evidence_event_id="revoke",
        )
        for path in ("timeline", "conversation-review",
                     "conversation-history?session_id=prior-session", "search",
                     "life-timeline", "people", "review-queue"):
            result = await client.get(f"/v1/archive/{path}", headers=headers)
            assert result.status_code == 403, (path, result.text)
            assert "REVOKED-CANARY" not in result.text
        for path, body in (("exports", {"password": "safe-password"}),
                           ("memories/guess-id/review", {"action": "confirm"})):
            result = await client.post(f"/v1/archive/{path}", headers=headers, json=body)
            assert result.status_code == 403, (path, result.text)


@pytest.mark.asyncio
async def test_session_context_uses_current_authority_and_not_the_managing_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-context")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        app.state.session_runtime_service = object()

        async def current(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"active_subject_id": "other-adult", "subject_category": "adult"}

        async def forbidden(**kwargs: Any) -> None:
            pytest.fail("account catalog was touched for a different subject")

        from services.control_api.app.routes import interaction
        monkeypatch.setattr(interaction, "_current_persistent_runtime_profile", current)
        monkeypatch.setattr(app.state.memory_catalog, "people", forbidden)
        result = await client.post(
            "/v1/archive/session-context", headers=INTERNAL,
            json={"session_id": session["session_id"], "speaker_class": "owner", "topic": "记忆"},
        )
        assert result.status_code == 200, result.text
        assert result.json() == {"items": []}


@pytest.mark.asyncio
async def test_session_archive_checks_all_existing_runtime_identity_fences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.control_api.app.routes import interaction

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-fences")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        fences = {
            "active_subject_id": owner["user_id"], "runtime_profile_id": "profile-current",
            "session_epoch": 2, "actor_id": owner["user_id"], "device_id": "device-current",
            "binding_id": "binding-current", "binding_version": 2, "subject_revision": 2,
        }
        app.state.session_runtime_service = object()
        monkeypatch.setattr(
            interaction, "_current_persistent_runtime_profile", AsyncMock(return_value=fences)
        )
        positive = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json=_speech(session["session_id"], **fences),
        )
        assert positive.status_code == 201, positive.text
        for i, (field, value) in enumerate(fences.items(), 2):
            changed = 1 if isinstance(value, int) else "stale-or-forged"
            rejected = await client.post(
                "/v1/archive/session-events", headers=INTERNAL,
                json=_speech(
                    session["session_id"], event_id=f"stale-{field}", turn_id=i,
                    **{**fences, field: changed},
                ),
            )
            assert rejected.status_code == 409, (field, rejected.text)
            assert rejected.json()["detail"]["code"] == "archive_subject_fence_stale"
            assert await app.state.life_archive.event(
                account_id=owner["user_id"], event_id=f"stale-{field}"
            ) is None


@pytest.mark.asyncio
async def test_configured_runtime_failure_cannot_fall_back_to_adult_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-outage")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        app.state.session_runtime_service = SimpleNamespace(
            current=AsyncMock(side_effect=RuntimeError("authority unavailable"))
        )
        result = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json=_speech(session["session_id"], active_subject_id=owner["user_id"]),
        )
        assert result.status_code == 503, result.text
        assert result.json()["detail"]["code"] == "session_runtime_authority_unavailable"
        assert await app.state.life_archive.event(
            account_id=owner["user_id"], event_id="scope-speech"
        ) is None


@pytest.mark.asyncio
async def test_retained_foreign_speech_cannot_train_the_account_persona(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    persona = PersonaObservationStub()
    app.state.persona_engine = persona
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-persona")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        await app.state.identity_service.register_person(
            person_id="other-persona-adult", display_name="另一人", timezone="Asia/Shanghai",
            subject_category="adult", age_band="adult", age_evidence_status="verified",
            age_evidence_id="test-adult-evidence",
            now=datetime.now(UTC),
        )
        for i, subject in enumerate(("other-persona-adult", owner["user_id"]), 1):
            result = await client.post(
                "/v1/archive/session-events", headers=INTERNAL,
                json=_speech(
                    session["session_id"], event_id=f"persona-{i}", turn_id=i,
                    active_subject_id=subject,
                    payload={"text": "我习惯先思考。", "persona_eligible": True},
                ),
            )
            assert result.status_code == 201, result.text
            event = await app.state.life_archive.event(
                account_id=owner["user_id"], event_id=f"persona-{i}"
            )
            assert event.payload["history_eligible"] is True
            assert persona.observations == i - 1


@pytest.mark.asyncio
async def test_account_raw_audio_consent_cannot_attach_to_foreign_or_unknown_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-audio")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        consent = await app.state.life_archive.grant_raw_voice_consent(
            account_id=owner["user_id"], policy_version="test-v1",
            retention_policy="account_lifetime", granted_at=datetime.now(UTC),
        )
        for i, subject in enumerate(("child", None), 1):
            await app.state.life_archive.record(EvidenceEvent(
                event_id=f"raw-parent-{i}", account_id=owner["user_id"], subject_id=subject,
                session_id=session["session_id"], turn_id=i, generation_id=1,
                event_type="speech.utterance_finalized", speaker_class="owner",
                source="test.pre-fence", occurred_at=datetime.now(UTC),
                consent_grant_id=consent.consent_grant_id,
                payload={"history_eligible": True, "owner_projection_eligible": True,
                         "interaction": {"history_eligible": True,
                                         "owner_projection_eligible": True}},
            ))
            result = await client.post(
                "/v1/archive/session-raw-audio", headers=INTERNAL,
                json={
                    "event_id": f"raw-parent-{i}", "session_id": session["session_id"],
                    "turn_id": i, "generation_id": 1, "speaker_class": "owner",
                    "event_type": "speech.utterance_finalized",
                    "source": "test", "payload": {},
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "consent_grant_id": consent.consent_grant_id,
                    "retention_policy": "account_lifetime",
                    "audio_base64": base64.b64encode(_wav()).decode(),
                },
            )
            assert result.status_code == 409, result.text
            assert result.json()["detail"]["code"] == "raw_audio_parent_mismatch"
        assert await app.state.life_archive.raw_voice_blobs(account_id=owner["user_id"]) == ()


@pytest.mark.asyncio
async def test_real_catalog_http_exits_and_review_ids_are_subject_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-catalog")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        now = datetime.now(UTC)
        for i, (subject, name) in enumerate(
            ((owner["user_id"], "李梅"), ("child", "王芳"), (None, "赵兰"))
        ):
            await app.state.life_archive.record(EvidenceEvent(
                event_id=f"catalog-{i}", account_id=owner["user_id"], subject_id=subject,
                event_type="speech.utterance_finalized", occurred_at=now + timedelta(days=i),
                speaker_class="owner", source="test.pre-fence",
                session_id=f"catalog-session-{i}", turn_id=1, generation_id=1,
                payload={"text": f"我妈妈叫{name}，今年60岁。",
                         "interaction_mode": "companion", "history_eligible": True,
                         "owner_projection_eligible": True},
            ))
        await app.state.memory_catalog.compile_pending()
        unscoped = await app.state.memory_catalog.review_queue(account_id=owner["user_id"])
        assert {item.source_event_id for item in unscoped} == {"catalog-0", "catalog-1", "catalog-2"}
        for path in ("search", "people", "life-timeline", "review-queue"):
            result = await client.get(f"/v1/archive/{path}", headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["items"], path  # A real positive control, not an empty filter.
            assert {item["source_event_id"] for item in result.json()["items"]} == {"catalog-0"}
            assert "王芳" not in result.text and "赵兰" not in result.text
        for claim in unscoped:
            result = await client.post(
                f"/v1/archive/memories/{claim.item_id}/review", headers=headers,
                json={"action": "confirm"},
            )
            assert result.status_code == (200 if claim.source_event_id == "catalog-0" else 404)
        missing = await client.post(
            "/v1/archive/memories/nonexistent/review", headers=headers, json={"action": "confirm"}
        )
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_read_authority_failure_returns_unavailable_without_reading_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        minor = await _register_minor(client, app, username="scope-read-unavailable")
        headers = {"Authorization": f"Bearer {minor['access_token']}"}
        unavailable = AsyncMock(side_effect=RuntimeError("consent authority offline"))
        read = AsyncMock(side_effect=AssertionError("evidence must not be read"))
        monkeypatch.setattr(app.state.guardian_store, "active_consent", unavailable)
        monkeypatch.setattr(app.state.life_archive, "context", read)
        result = await client.get("/v1/archive/timeline", headers=headers)
        assert result.status_code == 503, result.text
        assert result.json()["detail"]["code"] == "subject_retention_authority_unavailable"
        read.assert_not_awaited()


@pytest.mark.asyncio
async def test_companion_provenance_requires_every_source_to_match_parent_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-provenance")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        parent = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json=_speech(session["session_id"], active_subject_id=owner["user_id"]),
        )
        assert parent.status_code == 201, parent.text
        for i, subject in enumerate(("child", None, owner["user_id"])):
            await app.state.life_archive.record(EvidenceEvent(
                event_id=f"provenance-source-{i}", account_id=owner["user_id"], subject_id=subject,
                event_type="speech.utterance_finalized", occurred_at=datetime.now(UTC),
                speaker_class="owner", source="test.pre-fence",
                payload={"text": "来源文本", "owner_projection_eligible": True},
            ))
            result = await client.post(
                "/v1/archive/session-events", headers=INTERNAL,
                json=_speech(
                    session["session_id"], event_id=f"provenance-reply-{i}",
                    event_type="assistant.playout_stopped", speaker_class="assistant",
                    payload={"text": "回复", "actual_heard": True,
                             "response_provenance": _response_provenance(
                                 session_id=session["session_id"], turn_id=1, generation_id=1,
                                 source_refs=[{"kind": "memory_claim", "item_id": "claim",
                                               "source_event_ids": ["scope-speech", f"provenance-source-{i}"]}],
                             )},
                ),
            )
            assert result.status_code == (201 if i == 2 else 409), result.text
            if i != 2:
                assert result.json()["detail"]["code"] == "response_provenance_source_invalid"
                assert await app.state.life_archive.event(
                    account_id=owner["user_id"], event_id=f"provenance-reply-{i}"
                ) is None
