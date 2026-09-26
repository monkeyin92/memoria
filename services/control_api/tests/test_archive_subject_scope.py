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
        # catalog-1 names a different person.  Without an identity row the
        # compiler refuses to project that evidence, which would hide the leak
        # this test is checking.  Register the child so the claim exists, then
        # prove the logged-in account still cannot read or confirm it.
        await app.state.identity_service.register_person(
            person_id="child",
            display_name="孩子",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="test-child-evidence",
            now=datetime.now(UTC),
        )
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
            suffix = "?include_candidates=true" if path == "search" else ""
            result = await client.get(f"/v1/archive/{path}{suffix}", headers=headers)
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


def _memory_claim_ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["item_id"] for item in items if item["kind"] == "memory_claim"]


async def _read_subject_memory(
    client: AsyncClient,
    *,
    app: Any,
    user_id: str,
    session_id: str,
    subject_id: str,
    subject_category: str,
    token: dict[str, str],
    turn_id: int,
) -> tuple[Any, Any]:
    """Read the two subject-scoped memory seams for one signed runtime profile."""

    from services.control_api.tests.test_interaction_api import (
        _attach_signed_runtime_profile,
        _response_plan_body,
    )

    _attach_signed_runtime_profile(
        app,
        user_id=user_id,
        session_id=session_id,
        active_subject_id=subject_id,
        subject_category=subject_category,
        age_band="under_14" if subject_category == "minor" else "adult",
        service_mode="student_minor" if subject_category == "minor" else "adult_companion",
        capabilities=("chat", "memory_recall_private"),
    )
    body = _response_plan_body(session_id)
    body["query"] = "我喜欢什么？"
    body["fence"] = {**body["fence"], "turn_id": turn_id, "generation_id": turn_id}
    planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
    prefetched = await client.post(
        "/v1/interaction/context-prefetch",
        headers=token,
        json={
            "session_id": session_id,
            "query": "我们以前聊过什么？",
            "speaker_decision": body["speaker_decision"],
        },
    )
    return planned, prefetched


@pytest.mark.asyncio
async def test_same_account_self_claims_stay_on_their_own_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two self-claims in one account stay with the speaker who said them.

    Both rows use subject_key="self".  Reading as the other person, then back
    as the account, must each return only that person's row, and the catalog
    query must name that subject.
    """

    from services.archive.domain import EvidenceEvent
    from services.control_api.app.routes import interaction

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-token-that-is-long-enough"
    )
    app = create_app()
    seen_subjects: list[str | None] = []
    catalog = app.state.memory_catalog
    original_context = catalog.context

    async def recording_context(query: Any) -> Any:
        seen_subjects.append(getattr(query, "subject_id", None))
        return await original_context(query)

    catalog.context = recording_context
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-self-claims")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        now = datetime.now(UTC)
        await app.state.identity_service.register_person(
            person_id="person-other-adult",
            display_name="另一人",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="test-adult-evidence",
            now=now,
        )
        for index, (subject, text) in enumerate(
            (
                (owner["user_id"], "请记住我喜欢散步。"),
                ("person-other-adult", "请记住我喜欢阅读。"),
            )
        ):
            await app.state.life_archive.record(
                EvidenceEvent(
                    event_id=f"self-claim-{index}",
                    account_id=str(owner["user_id"]),
                    subject_id=str(subject),
                    session_id=session["session_id"],
                    turn_id=index + 1,
                    generation_id=index + 1,
                    event_type="speech.utterance_finalized",
                    occurred_at=now + timedelta(minutes=index),
                    speaker_class="owner",
                    source="test.pre-fence",
                    payload={
                        "text": text,
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                        "tool_epoch": 0,
                        "memory_write_intent": {
                            "kind": "explicit_remember",
                            "policy_version": "explicit-memory-v2",
                        },
                    },
                )
            )
        await app.state.memory_catalog.compile_pending()
        other_plan, other_prefetch = await _read_subject_memory(
            client,
            app=app,
            user_id=str(owner["user_id"]),
            session_id=session["session_id"],
            subject_id="person-other-adult",
            subject_category="adult",
            token=token,
            turn_id=7,
        )
        owner_plan, owner_prefetch = await _read_subject_memory(
            client,
            app=app,
            user_id=str(owner["user_id"]),
            session_id=session["session_id"],
            subject_id=str(owner["user_id"]),
            subject_category="adult",
            token=token,
            turn_id=8,
        )

    assert other_plan.status_code == 200, other_plan.text
    assert other_prefetch.status_code == 200, other_prefetch.text
    assert owner_plan.status_code == 200, owner_plan.text
    assert owner_prefetch.status_code == 200, owner_prefetch.text
    assert "散步" not in other_plan.text
    assert "阅读" in other_plan.text
    assert "阅读" not in owner_plan.text
    assert "散步" in owner_plan.text
    assert seen_subjects
    assert set(seen_subjects) == {"person-other-adult", owner["user_id"]}
    del interaction


@pytest.mark.asyncio
async def test_minor_without_retention_reads_nothing_and_names_the_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A minor without memory_retention is empty on every subject-scoped read.

    The consent lookup, when it happens, names the current subject and never
    the login account.  Revoking a previously granted consent closes the same
    three reads.
    """

    from services.guardian.domain import PersonConsentRecord

    class _RecordingGuardian:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.minor_ids: list[str] = []

        async def active_consent(self, *, minor_user_id: str, consent_kind: str) -> Any:
            self.minor_ids.append(minor_user_id)
            return await self.inner.active_consent(
                minor_user_id=minor_user_id, consent_kind=consent_kind
            )

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-token-that-is-long-enough"
    )
    app = create_app()
    guardian = _RecordingGuardian(app.state.guardian_store)
    app.state.guardian_store = guardian
    catalog_calls = {"count": 0}
    catalog = app.state.memory_catalog
    original_context = catalog.context

    async def counting_context(query: Any) -> Any:
        catalog_calls["count"] += 1
        return await original_context(query)

    catalog.context = counting_context
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-minor-read")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        now = datetime.now(UTC)
        await app.state.identity_service.register_person(
            person_id="person-minor-child",
            display_name="孩子",
            timezone="Asia/Shanghai",
            subject_category="minor",
            age_band="under_14",
            age_evidence_status="unverified",
            now=now,
        )
        planned, prefetched = await _read_subject_memory(
            client,
            app=app,
            user_id=str(owner["user_id"]),
            session_id=session["session_id"],
            subject_id="person-minor-child",
            subject_category="minor",
            token=token,
            turn_id=7,
        )
        assert planned.status_code == 200, planned.text
        assert prefetched.status_code == 200, prefetched.text
        assert _memory_claim_ids(planned.json()["grounded_items"]) == []
        assert _memory_claim_ids(prefetched.json()["grounded_items"]) == []
        assert catalog_calls["count"] == 0
        assert guardian.minor_ids
        assert set(guardian.minor_ids) == {"person-minor-child"}
        assert owner["user_id"] not in guardian.minor_ids

        consent = await app.state.guardian_store.grant_person_consent(
            PersonConsentRecord(
                consent_id="minor-retention",
                subject_person_id="person-minor-child",
                grantor_person_id="guardian",
                consent_kind="memory_retention",
                policy_version="test-v1",
                granted_at=now,
                evidence_event_id="grant-minor-retention",
            ),
            actor_person_id="guardian",
        )
        granted_plan, _ = await _read_subject_memory(
            client,
            app=app,
            user_id=str(owner["user_id"]),
            session_id=session["session_id"],
            subject_id="person-minor-child",
            subject_category="minor",
            token=token,
            turn_id=8,
        )
        assert granted_plan.status_code == 200, granted_plan.text
        await app.state.guardian_store.revoke_person_consent(
            consent_id=consent.consent_id,
            grantor_person_id="guardian",
            subject_person_id="person-minor-child",
            revoked_at=datetime.now(UTC),
            revocation_evidence_event_id="revoke-minor-retention",
        )
        calls_before_revoke = catalog_calls["count"]
        revoked_plan, revoked_prefetch = await _read_subject_memory(
            client,
            app=app,
            user_id=str(owner["user_id"]),
            session_id=session["session_id"],
            subject_id="person-minor-child",
            subject_category="minor",
            token=token,
            turn_id=9,
        )

    assert revoked_plan.status_code == 200, revoked_plan.text
    assert revoked_prefetch.status_code == 200, revoked_prefetch.text
    assert _memory_claim_ids(revoked_plan.json()["grounded_items"]) == []
    assert _memory_claim_ids(revoked_prefetch.json()["grounded_items"]) == []
    assert catalog_calls["count"] == calls_before_revoke


@pytest.mark.asyncio
async def test_unresolved_identity_does_not_project_the_other_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identity being down withholds the other subject's projection only.

    The archive evidence stays.  The catalog receipt is ignored, so a later
    read as that person cannot see the account owner's confirmed row, and the
    owner's own row is still projected.
    """

    from services.archive.domain import EvidenceEvent
    from services.archive.memory_catalog import SubjectCategoryUnresolved

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-token-that-is-long-enough"
    )
    app = create_app()

    def refuse_other(event: EvidenceEvent) -> str | None:
        subject_id = event.subject_id.strip() if isinstance(event.subject_id, str) else ""
        if subject_id and subject_id != event.account_id:
            raise SubjectCategoryUnresolved(subject_id)
        return "adult"

    app.state.memory_catalog._evidence_subject_category_resolver = refuse_other
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-unresolved")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        now = datetime.now(UTC)
        for index, (subject, text) in enumerate(
            (
                (owner["user_id"], "请记住我喜欢散步。"),
                ("person-unresolved", "请记住我喜欢阅读。"),
            )
        ):
            await app.state.life_archive.record(
                EvidenceEvent(
                    event_id=f"unresolved-{index}",
                    account_id=str(owner["user_id"]),
                    subject_id=str(subject),
                    session_id=session["session_id"],
                    turn_id=index + 1,
                    generation_id=index + 1,
                    event_type="speech.utterance_finalized",
                    occurred_at=now + timedelta(minutes=index),
                    speaker_class="owner",
                    source="test.pre-fence",
                    payload={
                        "text": text,
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                        "tool_epoch": 0,
                        "memory_write_intent": {
                            "kind": "explicit_remember",
                            "policy_version": "explicit-memory-v2",
                        },
                    },
                )
            )
        report = await app.state.memory_catalog.compile_pending()
        other_plan, _ = await _read_subject_memory(
            client,
            app=app,
            user_id=str(owner["user_id"]),
            session_id=session["session_id"],
            subject_id="person-unresolved",
            subject_category="adult",
            token=token,
            turn_id=7,
        )
        stored = await app.state.life_archive.event(
            account_id=str(owner["user_id"]), event_id="unresolved-1"
        )

    assert report.compiled_events == 1
    assert report.ignored_events == 1
    assert stored is not None
    assert other_plan.status_code == 200, other_plan.text
    assert "散步" not in other_plan.text
    assert "阅读" not in other_plan.text
    import sqlite3

    with sqlite3.connect(app.state.memory_catalog._path) as connection:
        receipt = connection.execute(
            "SELECT outcome FROM memory_compile_receipts WHERE event_id = ?",
            ("unresolved-1",),
        ).fetchone()
        projected = connection.execute(
            "SELECT 1 FROM memory_claims WHERE source_event_id = ?",
            ("unresolved-1",),
        ).fetchone()
    assert receipt is not None and receipt[0] == "ignored"
    assert projected is None


@pytest.mark.asyncio
async def test_stale_session_event_fence_creates_no_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale subject fence is rejected before any claim is written."""

    from services.control_api.app.routes import interaction

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-stale-fence")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        app.state.session_runtime_service = object()
        monkeypatch.setattr(
            interaction,
            "_current_persistent_runtime_profile",
            AsyncMock(
                return_value={
                    "active_subject_id": owner["user_id"],
                    "runtime_profile_id": "profile-current",
                    "session_epoch": 2,
                    "actor_id": owner["user_id"],
                    "device_id": "device-current",
                    "binding_id": "binding-current",
                    "binding_version": 2,
                    "subject_revision": 2,
                }
            ),
        )
        rejected = await client.post(
            "/v1/archive/session-events",
            headers=INTERNAL,
            json=_speech(
                session["session_id"],
                event_id="stale-fence-speech",
                active_subject_id=owner["user_id"],
                runtime_profile_id="profile-stale",
                session_epoch=1,
                actor_id=owner["user_id"],
                device_id="device-current",
                binding_id="binding-current",
                binding_version=2,
                subject_revision=2,
                payload={"text": "请记住我喜欢散步。"},
            ),
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "archive_subject_fence_stale"
        assert (
            await app.state.life_archive.event(
                account_id=owner["user_id"], event_id="stale-fence-speech"
            )
            is None
        )
        await app.state.memory_catalog.compile_pending()
        queue = await app.state.memory_catalog.review_queue(account_id=owner["user_id"])
        assert all(item.source_event_id != "stale-fence-speech" for item in queue)


@pytest.mark.asyncio
async def test_device_bound_owner_claim_needs_a_confirming_runtime_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No Session Runtime means no signed profile names the device's subject, so
    # the binding cannot vouch for anyone: the claim is refused, not archived.
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="bound-claim")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        rejected = await client.post(
            "/v1/archive/session-events", headers=INTERNAL,
            json=_speech(
                session["session_id"],
                payload={"text": "设备绑定声明", "speaker_reason_code": "device_bound_subject"},
            ),
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "device_bound_subject_unverified"


_ACCOUNT_TRAIT = "账号本人习惯先说“我觉得”。"
# A fixed style label, so it also survives the uncertain-speaker style filter.
_SUBJECT_TRAIT = "日常表达偏好短句，先给出核心意思"


def _account_capsule() -> Any:
    from services.persona.domain import PersonaCapsule, PersonaCapsuleEntry

    return PersonaCapsule(
        version_id="account-persona-v1",
        version_number=1,
        entries=(
            PersonaCapsuleEntry(
                trait_id="trait-account-tic",
                category="verbal_tic",
                description=_ACCOUNT_TRAIT,
                context="conversation",
                counterexample="",
                confidence=0.9,
                source_event_ids=("account-event",),
            ),
        ),
        prompt_fragment=_ACCOUNT_TRAIT,
    )


def _projected_subject_persona(db_path: object, subject_id: str) -> dict[str, Any] | None:
    if subject_id != "person-projected-adult":
        return None
    return {
        "version_id": "projected-subject-v3",
        "version_number": 3,
        "snapshot": [
            {
                "trait_id": "trait-subject-tic",
                "category": "verbal_tic",
                "description": _SUBJECT_TRAIT,
                "context": "conversation",
                "counterexample": "",
                "confidence": 0.9,
                "status": "confirmed",
                "source_event_ids": ["subject-event"],
            }
        ],
        "source_account_id": "subject-account",
        "projected_at": "2026-09-20T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_response_plan_persona_follows_the_confirmed_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1-03: persona is the current subject's own, never the account holder's.

    The account-keyed learner only learns from the account holder's own turns,
    so a child, elder or household member in front of the device must get its
    projected persona or none -- never the account holder's traits.
    """

    from services.control_api.app import subject_persona

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-token-that-is-long-enough"
    )
    app = create_app()
    account_reads: list[str] = []

    async def account_capsule(request: Any) -> Any:
        account_reads.append(request.account_id)
        return _account_capsule()

    monkeypatch.setattr(app.state.persona_engine, "capsule", account_capsule)
    monkeypatch.setattr(subject_persona, "read_active_version", _projected_subject_persona)
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="scope-persona")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        now = datetime.now(UTC)
        for person_id in ("person-plain-adult", "person-projected-adult"):
            await app.state.identity_service.register_person(
                person_id=person_id,
                display_name="另一人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id=f"evidence-{person_id}",
                now=now,
            )
        reads = {}
        for turn_id, subject in enumerate(
            (str(owner["user_id"]), "person-plain-adult", "person-projected-adult"), start=7
        ):
            reads[subject] = await _read_subject_memory(
                client,
                app=app,
                user_id=str(owner["user_id"]),
                session_id=session["session_id"],
                subject_id=subject,
                subject_category="adult",
                token=token,
                turn_id=turn_id,
            )

    owner_plan, owner_prefetch = reads[str(owner["user_id"])]
    plain_plan, plain_prefetch = reads["person-plain-adult"]
    projected_plan, projected_prefetch = reads["person-projected-adult"]
    for response in (*reads[str(owner["user_id"])], *reads["person-plain-adult"]):
        assert response.status_code == 200, response.text
    for response in reads["person-projected-adult"]:
        assert response.status_code == 200, response.text

    # The account holder is the subject: the account-keyed persona answers.
    assert _ACCOUNT_TRAIT in owner_plan.text
    assert owner_prefetch.json()["persona_version_id"] == "account-persona-v1"
    # Another subject without a projection gets no persona at all.
    assert _ACCOUNT_TRAIT not in plain_plan.text
    assert _ACCOUNT_TRAIT not in plain_prefetch.text
    assert plain_prefetch.json()["persona_version_id"] is None
    # Another subject with a projection gets its own persona only.
    assert _SUBJECT_TRAIT in projected_plan.text
    assert _ACCOUNT_TRAIT not in projected_plan.text
    assert projected_prefetch.json()["persona_version_id"] == "projected-subject-v3"
    # The account learner was only ever asked while the account was the subject.
    assert set(account_reads) == {str(owner["user_id"])}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speaker_class", "low_sensitivity_only", "expects_projection_read"),
    [
        ("owner", False, True),
        ("uncertain", True, True),
        ("uncertain", False, False),
        ("guest", True, False),
        ("guest", False, False),
    ],
)
async def test_subject_persona_applies_the_account_engine_speaker_gate(
    monkeypatch: pytest.MonkeyPatch,
    speaker_class: str,
    low_sensitivity_only: bool,
    expects_projection_read: bool,
) -> None:
    from services.control_api.app import subject_persona

    projection_reads: list[str] = []

    def read(db_path: object, subject_id: str) -> dict[str, Any] | None:
        projection_reads.append(subject_id)
        return _projected_subject_persona(db_path, subject_id)

    monkeypatch.setattr(subject_persona, "read_active_version", read)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(memoria_db_path="unused.sqlite3"),
                persona_engine=SimpleNamespace(capsule=AsyncMock()),
            )
        )
    )

    capsule = await subject_persona.subject_persona_capsule(
        request,  # type: ignore[arg-type]
        account_id="account-holder",
        subject_id="person-projected-adult",
        speaker_class=speaker_class,  # type: ignore[arg-type]
        topic="表达看法",
        low_sensitivity_only=low_sensitivity_only,
    )

    request.app.state.persona_engine.capsule.assert_not_awaited()
    assert projection_reads == (["person-projected-adult"] if expects_projection_read else [])
    if expects_projection_read:
        assert capsule.version_id == "projected-subject-v3"
    else:
        assert capsule.entries == ()
        assert capsule.version_id is None
