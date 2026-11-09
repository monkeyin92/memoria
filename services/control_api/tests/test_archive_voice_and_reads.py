"""Control API archive: voice provenance, consent and subject-scoped reads."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from services.archive.domain import (
    ContextBundle,
    EvidenceEvent,
)
from services.archive.memory_domain import MemorySearchResult, ReviewedClaim
from services.archive.postgres_archive import PostgresLifeArchive
from services.common.companions import designed_voice_speaker_sha256
from services.control_api.app.archive_event_conflicts import (
    ResponseProvenanceCreate,
    _canonical_actual_voice,
)
from services.control_api.app.archive_items import (
    _actual_heard_item,
    _owner_turn_item,
)
from services.control_api.app.main import create_app
from services.control_api.tests.archive_api_test_support import (
    _configure,
    _register_minor,
    _register_verified_adult,
    _response_provenance,
)
from services.guardian.domain import PersonConsentRecord


def test_retained_minor_and_unknown_rows_never_project_verbatim_turns() -> None:
    base = {
        "history_eligible": True,
        "owner_projection_eligible": True,
        "memory_retention": "retained",
    }
    owner = EvidenceEvent(
        event_id="retained-minor-owner",
        account_id="account",
        subject_id="child",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        speaker_class="owner",
        source="test",
        session_id="session",
        turn_id=1,
        generation_id=1,
        payload={**base, "subject_category": "minor", "text": "child text"},
    )
    assistant = EvidenceEvent(
        event_id="retained-unknown-assistant",
        account_id="account",
        subject_id="unknown",
        event_type="assistant.playout_stopped",
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        speaker_class="assistant",
        source="test",
        session_id="session",
        turn_id=1,
        generation_id=1,
        payload={
            **base,
            "subject_category": "unknown",
            "actual_heard": True,
            "text": "assistant text",
        },
    )
    assert _owner_turn_item(owner) is None
    assert _actual_heard_item(assistant) is None


def test_personal_voice_archive_requires_exact_frozen_speaker_digest() -> None:
    frozen_digest = "1" * 64
    provider_expires_at = "2027-07-23T00:00:00+00:00"
    session = {
        "interaction_mode": "self_preview",
        "mode_policy_version": "s8-v1",
        "voice_profile_id": "voice-profile-1",
        "voice_profile_version": 1,
        "voice_provider": "volcengine_doubao",
        "voice_model": "seed-icl-2.0",
        "voice_resource_id": "seed-icl-2.0",
        "voice_provider_expires_at": provider_expires_at,
        "voice_speaker_sha256": frozen_digest,
    }
    submitted = ResponseProvenanceCreate.model_validate(
        _response_provenance(
            session_id="voice-digest-session",
            turn_id=1,
            generation_id=1,
            source_refs=[],
            interaction_mode="self_preview",
            tts_model="seed-icl-2.0",
            actual_voice_profile_id="voice-profile-1",
            actual_voice_profile_version=1,
            actual_voice_resource_id="seed-icl-2.0",
            actual_voice_provider_expires_at=provider_expires_at,
            actual_voice_speaker_sha256=frozen_digest,
        )
    )

    assert _canonical_actual_voice(
        submitted=submitted,
        session=session,
        interaction_mode="self_preview",
    ) == (
        "voice-profile-1",
        1,
        "seed-icl-2.0",
        provider_expires_at,
        frozen_digest,
    )

    for update in (
        {"actual_voice_profile_version": 2},
        {"actual_voice_provider_expires_at": "2027-07-24T00:00:00+00:00"},
        {"actual_voice_speaker_sha256": "2" * 64},
    ):
        with pytest.raises(HTTPException) as exc_info:
            _canonical_actual_voice(
                submitted=submitted.model_copy(update=update),
                session=session,
                interaction_mode="self_preview",
            )
        assert exc_info.value.status_code == 409


@pytest.mark.parametrize(
    "missing",
    ["actual_voice_profile_version", "actual_voice_provider_expires_at"],
)
def test_personal_voice_provenance_requires_version_and_expiry(missing: str) -> None:
    values = _response_provenance(
        session_id="incomplete-personal-provenance",
        turn_id=1,
        generation_id=1,
        source_refs=[],
        interaction_mode="self_preview",
        tts_model="seed-icl-2.0",
        actual_voice_profile_id="voice-profile-1",
        actual_voice_profile_version=1,
        actual_voice_resource_id="seed-icl-2.0",
        actual_voice_provider_expires_at="2027-07-23T00:00:00+00:00",
        actual_voice_speaker_sha256="1" * 64,
    )
    values.pop(missing)

    with pytest.raises(ValueError, match="requires version and expiry"):
        ResponseProvenanceCreate.model_validate(values)


@pytest.mark.parametrize("missing", ["voice_profile_version", "voice_provider_expires_at"])
def test_personal_voice_archive_rejects_an_incomplete_frozen_contract(
    missing: str,
) -> None:
    provider_expires_at = "2027-07-23T00:00:00+00:00"
    session = {
        "interaction_mode": "self_preview",
        "mode_policy_version": "s8-v1",
        "voice_profile_id": "voice-profile-1",
        "voice_profile_version": 1,
        "voice_provider": "volcengine_doubao",
        "voice_model": "seed-icl-2.0",
        "voice_resource_id": "seed-icl-2.0",
        "voice_provider_expires_at": provider_expires_at,
        "voice_speaker_sha256": "1" * 64,
    }
    session.pop(missing)
    submitted = ResponseProvenanceCreate.model_validate(
        _response_provenance(
            session_id="incomplete-personal-voice-session",
            turn_id=1,
            generation_id=1,
            source_refs=[],
            interaction_mode="self_preview",
            tts_model="seed-icl-2.0",
            actual_voice_profile_id="voice-profile-1",
            actual_voice_profile_version=1,
            actual_voice_resource_id="seed-icl-2.0",
            actual_voice_provider_expires_at=provider_expires_at,
            actual_voice_speaker_sha256="1" * 64,
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        _canonical_actual_voice(
            submitted=submitted,
            session=session,
            interaction_mode="self_preview",
        )
    assert exc_info.value.status_code == 409


@pytest.mark.parametrize(
    ("interaction_mode", "session", "profile_id"),
    [
        (
            "companion",
            {
                "interaction_mode": "companion",
                "mode_policy_version": "s2-v1",
                "companion_style_id": "starlight",
            },
            "warm_companion",
        ),
        (
            "self_preview",
            {
                "interaction_mode": "self_preview",
                "mode_policy_version": "s8-v1",
                "fallback_voice_profile_id": "bright_peer",
                "fallback_voice_provider": "volcengine_doubao",
                "fallback_voice_model": "seed-tts-2.0",
                "fallback_voice_resource_id": "seed-tts-2.0",
            },
            "bright_peer",
        ),
    ],
)
def test_designed_voice_archive_requires_the_canonical_speaker_digest(
    interaction_mode: str,
    session: dict[str, object],
    profile_id: str,
) -> None:
    canonical_digest = designed_voice_speaker_sha256(profile_id)
    submitted = ResponseProvenanceCreate.model_validate(
        _response_provenance(
            session_id="designed-voice-session",
            turn_id=1,
            generation_id=1,
            source_refs=[],
            interaction_mode=interaction_mode,
            actual_voice_profile_id=profile_id,
            actual_voice_speaker_sha256=canonical_digest,
        )
    )

    assert _canonical_actual_voice(
        submitted=submitted,
        session=session,
        interaction_mode=interaction_mode,
    ) == (profile_id, None, "seed-tts-2.0", None, canonical_digest)

    with pytest.raises(HTTPException) as exc_info:
        _canonical_actual_voice(
            submitted=submitted.model_copy(update={"actual_voice_speaker_sha256": "f" * 64}),
            session=session,
            interaction_mode=interaction_mode,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_internal_capability_tokens_are_not_interchangeable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "")
    monkeypatch.setenv("MEMORIA_ARCHIVE_WRITE_TOKEN", "archive-write-capability")
    monkeypatch.setenv("MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-capability")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        event = {
            "event_id": "capability-event",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "test",
            "payload": {"text": "只允许档案写 token 写入。"},
        }
        written = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "archive-write-capability"},
            json=event,
        )
        write_with_plan_token = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "response-plan-capability"},
            json={**event, "event_id": "capability-event-denied"},
        )

    assert written.status_code == 201
    assert write_with_plan_token.status_code == 401


@pytest.mark.asyncio
async def test_raw_voice_consent_grant_revoke_and_upload_route_is_gone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="raw-voice-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        empty = await client.get("/v1/archive/raw-voice-consent", headers=headers)
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        granted = await client.post(
            "/v1/archive/raw-voice-consent",
            headers=headers,
            json={
                "policy_version": "raw-voice-archive-v1",
                "retention_policy": "account_lifetime",
            },
        )
        grant = granted.json()
        session_grant = await client.get(
            "/v1/archive/session-raw-voice-consent",
            headers=internal,
            params={"session_id": session["session_id"]},
        )
        removed = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={
                "event_id": "raw-audio-gone",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": grant["consent_grant_id"],
                "payload": {"text": "upload route removed"},
                "audio_base64": "AA==",
                "media_type": "audio/wav",
                "retention_policy": "account_lifetime",
            },
        )
        revoked = await client.delete("/v1/archive/raw-voice-consent", headers=headers)
        after_revoke = await client.get("/v1/archive/raw-voice-consent", headers=headers)

    assert empty.json() == {"consent": None}
    assert granted.status_code == 201
    assert session_grant.json() == {
        "allowed": True,
        "consent_grant_id": grant["consent_grant_id"],
        "policy_version": "raw-voice-archive-v1",
        "retention_policy": "account_lifetime",
    }
    assert removed.status_code == 404
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert after_revoke.json() == {"consent": None}


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL HTTP raw voice contract",
)
async def test_postgres_control_api_raw_voice_consent_matches_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    app = create_app()
    app.state.life_archive = archive
    account_id: str | None = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            identity = await _register_verified_adult(
                client,
                app,
                username="postgres-http-raw-owner",
            )
            account_id = str(identity["user_id"])
            headers = {"Authorization": f"Bearer {identity['access_token']}"}
            internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
            session = (await client.post("/v1/sessions", headers=headers, json={})).json()
            granted = await client.post(
                "/v1/archive/raw-voice-consent",
                headers=headers,
                json={
                    "policy_version": "raw-voice-archive-v1",
                    "retention_policy": "account_lifetime",
                },
            )
            grant = granted.json()
            session_consent = await client.get(
                "/v1/archive/session-raw-voice-consent",
                headers=internal,
                params={"session_id": session["session_id"]},
            )
            removed = await client.post(
                "/v1/archive/session-raw-audio",
                headers=internal,
                json={
                    "event_id": "postgres-http-raw-event",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "funasr.authoritative_final",
                    "consent_grant_id": grant["consent_grant_id"],
                    "payload": {"text": "upload route removed"},
                    "audio_base64": "AA==",
                    "media_type": "audio/wav",
                    "retention_policy": "account_lifetime",
                },
            )
            revoked = await client.delete(
                "/v1/archive/raw-voice-consent",
                headers=headers,
            )

        assert granted.status_code == 201
        assert session_consent.json()["consent_grant_id"] == grant["consent_grant_id"]
        assert removed.status_code == 404
        assert revoked.status_code == 200
        assert await archive.raw_voice_blobs(account_id=account_id) == ()
    finally:
        if account_id is not None:
            connection = await asyncpg.connect(dsn)
            try:
                await connection.execute(
                    "DELETE FROM archive_evidence_events WHERE account_id = $1",
                    account_id,
                )
                await connection.execute(
                    "DELETE FROM archive_consent_grants WHERE account_id = $1",
                    account_id,
                )
            finally:
                await connection.close()
        await archive.close()


@pytest.mark.asyncio
async def test_account_can_search_review_and_trace_compiled_life_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="compiled-memory-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        for index, text in enumerate(
            (
                "我们家的家训是答应别人的事一定做到。",
                "我妈妈叫李梅，今年60岁。",
            )
        ):
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal_headers,
                json={
                    "event_id": f"memory-api-{index}",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 9, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "active_subject_id": identity["user_id"],
                    "payload": {"text": text},
                    "turn_id": index + 1,
                },
            )
            assert response.status_code == 201
        await app.state.memory_catalog.compile_pending()

        default_search = await client.get("/v1/archive/search?q=家训", headers=headers)
        candidate_search = await client.get(
            "/v1/archive/search?q=家训&include_candidates=true",
            headers=headers,
        )
        queue = await client.get("/v1/archive/review-queue", headers=headers)
        people = await client.get("/v1/archive/people", headers=headers)
        life_timeline = await client.get("/v1/archive/life-timeline", headers=headers)
        claim = next(item for item in queue.json()["items"] if item["kind"] == "claim")
        reviewed = await client.post(
            f"/v1/archive/memories/{claim['item_id']}/review",
            headers=headers,
            json={"action": "confirm"},
        )
        confirmed_search = await client.get("/v1/archive/search?q=家训", headers=headers)

    assert default_search.status_code == 200
    assert default_search.json() == {"items": []}
    assert candidate_search.status_code == 200
    assert candidate_search.json()["items"][0]["source_event_id"] == "memory-api-0"
    assert reviewed.json()["status"] == "confirmed"
    assert confirmed_search.status_code == 200
    assert confirmed_search.json()["items"][0]["source_event_id"] == "memory-api-0"
    assert people.json()["items"][0]["display_name"] == "李梅"
    assert {item["source_event_id"] for item in life_timeline.json()["items"]} == {
        "memory-api-0",
        "memory-api-1",
    }


@pytest.mark.asyncio
async def test_conversation_review_projects_actual_heard_and_separates_claim_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await _register_verified_adult(
            client,
            app,
            username="conversation-review-first",
        )
        second = await _register_verified_adult(
            client,
            app,
            username="conversation-review-second",
        )
        first_headers = {"Authorization": f"Bearer {first['access_token']}"}
        second_headers = {"Authorization": f"Bearer {second['access_token']}"}
        first_session = (
            await client.post("/v1/sessions", headers=first_headers, json={})
        ).json()
        second_session = (
            await client.post("/v1/sessions", headers=second_headers, json={})
        ).json()

        async def append_turn(
            *,
            event_id: str,
            session_id: str,
            text: str,
            turn_id: int,
            subject_id: str | None = None,
            speaker_class: str = "owner",
            assistant_text: str | None = None,
            approximate: bool | None = None,
        ) -> None:
            occurred_at = datetime(2026, 8, 17, 8, turn_id, tzinfo=UTC)
            parent = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": event_id,
                    "session_id": session_id,
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": occurred_at.isoformat(),
                    "speaker_class": speaker_class,
                    "source": "test",
                    "turn_id": turn_id,
                    "generation_id": turn_id,
                    "active_subject_id": subject_id,
                    "payload": {"text": text},
                },
            )
            assert parent.status_code == 201
            if assistant_text is None:
                return
            payload: dict[str, object] = {
                "text": assistant_text,
                "actual_heard": True,
            }
            if approximate is not None:
                payload["approximate"] = approximate
            assistant = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": f"{event_id}-heard",
                    "session_id": session_id,
                    "event_type": "assistant.playout_stopped",
                    "occurred_at": (occurred_at + timedelta(seconds=1)).isoformat(),
                    "speaker_class": "assistant",
                    "source": "generation_fence.actual_heard",
                    "turn_id": turn_id,
                    "generation_id": turn_id,
                    "tool_epoch": 0,
                    "payload": payload,
                },
            )
            assert assistant.status_code == 201

        await append_turn(
            event_id="first-confirmed",
            session_id=first_session["session_id"],
            text="我们家的家训是答应别人的事一定做到。",
            turn_id=1,
            subject_id=first["user_id"],
            assistant_text="我会记住这条家训。",
            approximate=False,
        )
        await append_turn(
            event_id="first-candidate",
            session_id=first_session["session_id"],
            text="我在杭州读过书。",
            turn_id=2,
            subject_id=first["user_id"],
            assistant_text="你曾在杭州读书。",
        )
        await append_turn(
            event_id="first-guest",
            session_id=first_session["session_id"],
            text="访客内容不能进入主人回顾。",
            turn_id=3,
            speaker_class="guest",
            assistant_text="这条回复也不能进入主人回顾。",
        )
        await append_turn(
            event_id="second-confirmed",
            session_id=second_session["session_id"],
            text="我最喜欢蓝色。",
            turn_id=4,
            subject_id=second["user_id"],
            assistant_text="第二个账号实际听到的内容。",
        )
        await append_turn(
            event_id="second-candidate",
            session_id=second_session["session_id"],
            text="我曾在北京生活。",
            turn_id=5,
            subject_id=second["user_id"],
        )
        await app.state.memory_catalog.compile_pending()

        first_queue = (
            await client.get("/v1/archive/review-queue", headers=first_headers)
        ).json()["items"]
        second_queue = (
            await client.get("/v1/archive/review-queue", headers=second_headers)
        ).json()["items"]
        first_confirmed_claim = next(
            item for item in first_queue if item["source_event_id"] == "first-confirmed"
        )
        second_confirmed_claim = next(
            item for item in second_queue if item["source_event_id"] == "second-confirmed"
        )
        second_candidate_claim = next(
            item for item in second_queue if item["source_event_id"] == "second-candidate"
        )
        for headers, claim in (
            (first_headers, first_confirmed_claim),
            (second_headers, second_confirmed_claim),
        ):
            response = await client.post(
                f"/v1/archive/memories/{claim['item_id']}/review",
                headers=headers,
                json={"action": "confirm"},
            )
            assert response.status_code == 200

        cross_account_review = await client.post(
            f"/v1/archive/memories/{second_candidate_claim['item_id']}/review",
            headers=first_headers,
            json={"action": "confirm"},
        )
        projection = await client.get(
            "/v1/archive/conversation-review",
            headers=first_headers,
        )
        injected_account = await client.get(
            "/v1/archive/conversation-review",
            headers=first_headers,
            params={"account_id": second["user_id"]},
        )
        second_projection = await client.get(
            "/v1/archive/conversation-review",
            headers=second_headers,
        )

    assert cross_account_review.status_code == 404
    assert projection.status_code == 200
    body = projection.json()
    heard_by_text = {item["text"]: item for item in body["actual_heard"]}
    assert set(heard_by_text) == {
        "我会记住这条家训。",
        "你曾在杭州读书。",
    }
    assert heard_by_text["我会记住这条家训。"]["approximate"] is False
    assert heard_by_text["你曾在杭州读书。"]["approximate"] is True
    assert all(item["status"] in {"candidate", "disputed"} for item in body["memory_candidates"])
    assert {item["status"] for item in body["confirmed_memories"]} == {"confirmed"}
    candidate_ids = {item["claim_id"] for item in body["memory_candidates"]}
    confirmed_ids = {item["memory_id"] for item in body["confirmed_memories"]}
    assert first_confirmed_claim["item_id"] in confirmed_ids
    assert first_confirmed_claim["item_id"] not in candidate_ids
    assert second_confirmed_claim["item_id"] not in confirmed_ids
    assert second_candidate_claim["item_id"] not in candidate_ids
    assert injected_account.json() == body
    assert second_candidate_claim["item_id"] in {
        item["claim_id"] for item in second_projection.json()["memory_candidates"]
    }


@pytest.mark.asyncio
async def test_conversation_review_subject_matrix_precedes_private_reads_and_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        adult = await _register_verified_adult(
            client,
            app,
            username="conversation-review-adult",
        )
        minor = await _register_minor(
            client,
            app,
            username="conversation-review-minor",
        )
        unknown = (
            await client.post(
                "/v1/auth/register",
                json={"username": "conversation-review-unknown", "password": "safe-password"},
            )
        ).json()
        missing = (
            await client.post(
                "/v1/auth/register",
                json={"username": "conversation-review-missing", "password": "safe-password"},
            )
        ).json()
        headers = {
            name: {"Authorization": f"Bearer {identity['access_token']}"}
            for name, identity in (
                ("adult", adult),
                ("minor", minor),
                ("unknown", unknown),
                ("missing", missing),
            )
        }

        # A minor read is only reachable with a real memory-retention consent
        # naming the subject; the matrix below still proves the missing-consent
        # exits stay closed through the unknown/missing accounts.
        await app.state.guardian_store.grant_person_consent(
            PersonConsentRecord(
                consent_id=str(uuid.uuid4()),
                subject_person_id=minor["user_id"],
                grantor_person_id="matrix-guardian",
                consent_kind="memory_retention",
                policy_version="minor-memory-v1",
                granted_at=datetime.now(UTC),
                evidence_event_id="matrix-minor-retention-grant",
            ),
            actor_person_id="matrix-guardian",
        )

        archive_context = AsyncMock(return_value=ContextBundle())
        review_queue = AsyncMock(return_value=())
        search = AsyncMock(return_value=MemorySearchResult())
        review = AsyncMock(
            return_value=ReviewedClaim(
                claim_id="claim-allowed",
                status="confirmed",
                value="允许确认",
                review_event_id="review-allowed",
            )
        )
        monkeypatch.setattr(app.state.life_archive, "context", archive_context)
        monkeypatch.setattr(app.state.memory_catalog, "review_queue", review_queue)
        monkeypatch.setattr(app.state.memory_catalog, "search", search)
        monkeypatch.setattr(app.state.memory_catalog, "review", review)

        for category in ("adult", "minor"):
            response = await client.get(
                "/v1/archive/conversation-review",
                headers=headers[category],
            )
            assert response.status_code == 200
            reviewed = await client.post(
                "/v1/archive/memories/claim-allowed/review",
                headers=headers[category],
                json={"action": "confirm"},
            )
            assert reviewed.status_code == 200

        assert archive_context.await_count == 2
        assert review_queue.await_count == 2
        assert search.await_count == 2
        assert review.await_count == 2
        for mock in (archive_context, review_queue, search, review):
            mock.reset_mock()

        original_get_subject_profile = app.state.memory_store.get_subject_profile

        def get_subject_profile(*, user_id: str) -> dict[str, Any] | None:
            if user_id == missing["user_id"]:
                return None
            return original_get_subject_profile(user_id=user_id)

        monkeypatch.setattr(
            app.state.memory_store,
            "get_subject_profile",
            get_subject_profile,
        )
        denied = {}
        for category in ("unknown", "missing"):
            denied[(category, "read")] = await client.get(
                "/v1/archive/conversation-review",
                headers=headers[category],
            )
            denied[(category, "write")] = await client.post(
                "/v1/archive/memories/claim-denied/review",
                headers=headers[category],
                json={"action": "confirm"},
            )

    for operation in ("read", "write"):
        assert denied[("unknown", operation)].status_code == 403
        assert denied[("unknown", operation)].json()["detail"] == {
            "code": "subject_capability_forbidden",
            "capability": "conversation_review",
        }
        assert denied[("missing", operation)].status_code == 403
        assert denied[("missing", operation)].json()["detail"] == {
            "code": "subject_category_unavailable",
            "capability": "conversation_review",
        }
    archive_context.assert_not_awaited()
    review_queue.assert_not_awaited()
    search.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_memory_routes_never_accept_an_account_id_from_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # The reader is a verified adult so the read exit is reachable; the
        # client-supplied account id still names an unrelated unknown account.
        first = await _register_verified_adult(
            client,
            app,
            username="memory-routes-owner",
        )
        second = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {first['access_token']}"}

        search = await client.get(
            f"/v1/archive/search?q=x&account_id={second['user_id']}",
            headers=headers,
        )

    assert search.status_code == 200
    assert search.json() == {"items": []}


@pytest.mark.asyncio
async def test_archive_search_rejects_invalid_typed_filters_as_422(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="search-filters-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}

        invalid_entity = await client.get(
            "/v1/archive/search?entity_id=not-a-uuid",
            headers=headers,
        )
        invalid_range = await client.get(
            "/v1/archive/search"
            "?occurred_after=2026-07-20T00%3A00%3A00%2B00%3A00"
            "&occurred_before=2026-07-19T00%3A00%3A00%2B00%3A00",
            headers=headers,
        )

    assert invalid_entity.status_code == 422
    assert invalid_range.status_code == 422


@pytest.mark.asyncio
async def test_owner_acoustic_metrics_reach_persona_through_an_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="persona-metrics-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()

        good_event_ids: list[str] = []
        for index, text in enumerate(
            (
                "我会先听完，再认真回答。",
                "我会先想清楚，再慢慢说明。",
                "我会先核对事实，再给出意见。",
            )
        ):
            event_id = f"persona-metrics-owner-{index}"
            good_event_ids.append(event_id)
            recorded = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": event_id,
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 16, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "funasr.authoritative_final",
                    "active_subject_id": identity["user_id"],
                    "payload": {
                        "text": text,
                        "persona_eligible": True,
                        "speech_ms": 8000,
                        "pause_ratio": 0.55,
                        "quality_score": 0.95,
                        "speech_duration_ms": 1,
                        "learning_allowed": False,
                        "scene": "payload-injection",
                        "contamination_flags": ["guest"],
                        "rate": 999,
                    },
                    "turn_id": index + 1,
                },
            )
            assert recorded.status_code == 201

        for event_id, speaker_class, quality_score in (
            ("persona-metrics-low-quality", "owner", 0.2),
            ("persona-metrics-invalid", "owner", "0.95"),
            ("persona-metrics-guest", "guest", 0.95),
            ("persona-metrics-uncertain", "uncertain", 0.95),
        ):
            recorded = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": event_id,
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 16, 10, tzinfo=UTC).isoformat(),
                    "speaker_class": speaker_class,
                    "source": "funasr.authoritative_final",
                    **(
                        {"active_subject_id": identity["user_id"]}
                        if speaker_class == "owner"
                        else {}
                    ),
                    "payload": {
                        "text": "这条样本不能进入主人的人格学习。",
                        "persona_eligible": True,
                        "speech_ms": 8000,
                        "pause_ratio": 0.55,
                        "quality_score": quality_score,
                    },
                    "turn_id": 10,
                },
            )
            assert recorded.status_code == 201

        traits = (
            await client.get(
                "/v1/persona/traits?include_candidates=true",
                headers=headers,
            )
        ).json()["items"]

    speech_rate = next(item for item in traits if item["category"] == "speech_rate")
    pause_style = next(item for item in traits if item["category"] == "pause_style")
    assert "从容" in speech_rate["description"]
    assert "较多自然停顿" in pause_style["description"]
    assert speech_rate["status"] == pause_style["status"] == "confirmed"
    assert set(speech_rate["source_event_ids"]) == set(good_event_ids)
    assert set(pause_style["source_event_ids"]) == set(good_event_ids)
