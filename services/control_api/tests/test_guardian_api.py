from __future__ import annotations

import base64
import io
import json
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from services.agent.src.providers.crisis_semantic_classifier import CrisisSemanticVerdict
from services.archive.domain import EvidenceEvent
from services.control_api.app.account_gate import (
    SUBJECT_CAPABILITY_RULES,
    SubjectCapability,
    require_capability_for_account_id,
)
from services.control_api.app.main import create_app
from services.control_api.tests.test_interaction_api import (
    _attach_signed_runtime_profile,
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_SPEAKER_DB_PATH", str(tmp_path / "speaker.sqlite3"))
    monkeypatch.setenv("MEMORIA_EVOLUTION_DB_PATH", str(tmp_path / "evolution.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_ARCHIVE_OBJECT_STORE_PATH",
        str(tmp_path / "archive-objects"),
    )
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "guardian-test-auth-secret-that-is-long-enough")
    monkeypatch.setenv(
        "MEMORIA_ARCHIVE_INTERNAL_TOKEN",
        "guardian-test-archive-token-that-is-long-enough",
    )
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN",
        "guardian-test-response-plan-token-that-is-long-enough",
    )
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN",
        "guardian-test-policy-token-that-is-long-enough",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")


async def _register(client: AsyncClient, username: str) -> tuple[dict[str, Any], dict[str, str]]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    identity = response.json()
    return identity, {"Authorization": f"Bearer {identity['access_token']}"}


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _mark_verified_adult(app: Any, user_id: str) -> None:
    app.state.memory_store.update_subject_profile(
        user_id=user_id,
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )


def _wav_base64() -> str:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x00" * 1600)
    return base64.b64encode(output.getvalue()).decode("ascii")


@pytest.mark.asyncio
async def test_guardian_binding_consent_revocation_and_summary_are_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        parent, parent_headers = await _register(client, "guardian-parent")
        child, child_headers = await _register(client, "guardian-child")
        _mark_verified_adult(app, parent["user_id"])
        parent_headers = await _login(client, "guardian-parent")
        app.state.memory_store.bind_external_identities(
            preferred_user_id=parent["user_id"],
            identities={"wechat_openid": "parent-openid-sha256"},
            now=datetime.now(UTC).isoformat(),
        )

        created = await client.post(
            "/v1/guardian/links",
            headers={**parent_headers, "Idempotency-Key": "guardian-link-request-001"},
            json={"minor_user_id": child["user_id"], "relation": "parent"},
        )
        assert created.status_code == 201
        link = created.json()
        assert link["binding_code"].isdigit() and len(link["binding_code"]) == 8

        confirmed = await client.post(
            f"/v1/guardian/links/{link['link_id']}/confirm",
            headers=child_headers,
            json={
                "binding_code": link["binding_code"],
                "birth_year_band": "under_14",
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["subject"]["subject_category"] == "minor"
        assert confirmed.json()["reauthentication_required"] is True

        stale = await client.post("/v1/sessions", headers=child_headers, json={})
        assert stale.status_code == 401
        child_headers = await _login(client, "guardian-child")

        blocked = await client.post(
            "/v1/sessions",
            headers=child_headers,
            json={"session_focus": "tutor_english"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == {
            "code": "guardian_consent_required",
            "capability": "minor_voice_session",
        }

        voice_consent = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "voice-consent-request-001"},
            json={
                "consent_kind": "minor_voice_session",
                "policy_version": "minor-voice-v1",
            },
        )
        weekly_consent = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "weekly-consent-request-001"},
            json={
                "consent_kind": "weekly_report",
                "policy_version": "guardian-weekly-v1",
            },
        )
        assert voice_consent.status_code == 201
        assert weekly_consent.status_code == 201

        session = await client.post(
            "/v1/sessions",
            headers=child_headers,
            json={"session_focus": "tutor_english"},
        )
        assert session.status_code == 200
        assert session.json()["interaction"]["session_focus"] == "tutor_english"
        _attach_signed_runtime_profile(
            app,
            user_id=child["user_id"],
            session_id=session.json()["session_id"],
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "tutor", "english_practice"),
        )

        policy_without_retention = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "guardian-test-policy-token-that-is-long-enough"},
            json={"session_id": session.json()["session_id"]},
        )
        assert policy_without_retention.status_code == 200, policy_without_retention.text
        unretained = await client.post(
            "/v1/archive/session-events",
            headers={
                "X-Memoria-Internal-Token": ("guardian-test-archive-token-that-is-long-enough")
            },
            json={
                "event_id": "minor-unretained-turn",
                "session_id": session.json()["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test.authoritative-final",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {"text": "没有留存同意时不能保存这句原文"},
            },
        )
        assert policy_without_retention.json()["history_eligible"] is False
        assert policy_without_retention.json()["capabilities"]["private_memory"] is False
        assert unretained.status_code == 201
        unretained_event = await app.state.life_archive.event(
            account_id=child["user_id"],
            event_id="minor-unretained-turn",
        )
        assert unretained_event is not None
        assert "text" not in unretained_event.payload
        assert unretained_event.payload["history_eligible"] is False
        assert unretained_event.payload["memory_retention"] == "ephemeral_only"

        memory_consent = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "memory-consent-request-001"},
            json={
                "consent_kind": "memory_retention",
                "policy_version": "minor-memory-v1",
            },
        )
        assert memory_consent.status_code == 201
        _attach_signed_runtime_profile(
            app,
            user_id=child["user_id"],
            session_id=session.json()["session_id"],
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=(
                "chat",
                "tutor",
                "english_practice",
                "memory_recall_private",
            ),
        )
        policy_with_retention = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "guardian-test-policy-token-that-is-long-enough"},
            json={"session_id": session.json()["session_id"]},
        )
        retained = await client.post(
            "/v1/archive/session-events",
            headers={
                "X-Memoria-Internal-Token": ("guardian-test-archive-token-that-is-long-enough")
            },
            json={
                "event_id": "minor-retained-turn",
                "session_id": session.json()["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test.authoritative-final",
                "turn_id": 2,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {"text": "有留存同意后可以保存安全的学习原文"},
            },
        )
        assert policy_with_retention.json()["history_eligible"] is True
        assert retained.status_code == 201
        retained_event = await app.state.life_archive.event(
            account_id=child["user_id"],
            event_id="minor-retained-turn",
        )
        assert retained_event is not None
        assert retained_event.payload["text"] == "有留存同意后可以保存安全的学习原文"

        now = datetime.now(UTC)
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="guardian-weekly-emotion-1",
                account_id=child["user_id"],
                event_type="emotion_observation",
                occurred_at=now,
                speaker_class="system",
                source="test.authoritative-emotion",
                payload={
                    "label": "happy",
                    "history_eligible": True,
                    "owner_projection_eligible": True,
                    "text": "家长端绝不能看到这句原文",
                },
            )
        )
        summary = await client.get(
            f"/v1/guardian/minors/{child['user_id']}/summary",
            headers=parent_headers,
        )
        assert summary.status_code == 200
        serialized = json.dumps(summary.json(), ensure_ascii=False)
        assert summary.json()["emotion_distribution"] == {"happy": 1}
        assert "家长端绝不能看到这句原文" not in serialized
        assert summary.json()["privacy"] == {
            "contains_transcript": False,
            "diagnostic_assessment": False,
        }

        crisis_query = "我不会做题。我不想活了"
        crisis = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "guardian-test-response-plan-token-that-is-long-enough"
                )
            },
            json={
                "session_id": session.json()["session_id"],
                "query": crisis_query,
                "utterance_intent": "request_hint",
                "fence": {
                    "session_id": session.json()["session_id"],
                    "turn_id": 1,
                    "generation_id": 1,
                    "tool_epoch": 0,
                },
                "speaker_decision": {
                    "classification": "owner",
                    "reason_code": "trusted",
                    "model_version": "test",
                    "profile_id": None,
                    "template_version": None,
                },
            },
        )
        notifications = await client.get(
            "/v1/guardian/notifications",
            headers=parent_headers,
        )
        assert crisis.status_code == 200
        assert "急救或报警" in crisis.json()["direct_text"]
        assert "【导师话轮约束】" not in crisis.json()["instructions"]
        assert notifications.status_code == 200
        notification_text = json.dumps(notifications.json(), ensure_ascii=False)
        assert len(notifications.json()["items"]) == 1
        assert crisis_query not in notification_text
        assert notifications.json()["items"][0]["contains_transcript"] is False
        assert notifications.json()["items"][0]["contains_severity"] is False
        crisis_events = await app.state.life_archive.evidence_window(
            account_id=child["user_id"],
            occurred_after=datetime(1970, 1, 1, tzinfo=UTC),
            occurred_before=datetime.now(UTC),
            event_types=("guardian.crisis_event",),
        )
        assert len(crisis_events) == 1
        assert crisis_query not in json.dumps(dict(crisis_events[0].payload), ensure_ascii=False)

        class SemanticEvidence:
            async def classify(self, *, current_text: str) -> CrisisSemanticVerdict:
                assert current_text == "我真的找不到活下去的理由"
                return CrisisSemanticVerdict.SELF_CRISIS

        class UnavailableNotifications:
            async def record_minor_crisis(self, **_: object) -> None:
                raise RuntimeError("notification outbox unavailable")

        app.state.crisis_semantic_classifier = SemanticEvidence()
        app.state.crisis_notification_service = UnavailableNotifications()
        semantic_crisis = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "guardian-test-response-plan-token-that-is-long-enough"
                )
            },
            json={
                "session_id": session.json()["session_id"],
                "query": "我真的找不到活下去的理由",
                "utterance_intent": "chat",
                "fence": {
                    "session_id": session.json()["session_id"],
                    "turn_id": 2,
                    "generation_id": 2,
                    "tool_epoch": 0,
                },
                "speaker_decision": {
                    "classification": "owner",
                    "reason_code": "test-owner",
                    "profile_id": None,
                    "model_version": "test-speaker-v1",
                    "template_version": None,
                },
            },
        )
        assert semantic_crisis.status_code == 200
        assert "急救或报警" in semantic_crisis.json()["direct_text"]
        assert "【导师话轮约束】" not in semantic_crisis.json()["instructions"]

        revoked = await client.delete(
            (f"/v1/guardian/links/{link['link_id']}/consents/{voice_consent.json()['consent_id']}"),
            headers={**parent_headers, "Idempotency-Key": "voice-revoke-request-001"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["active"] is False
        assert (
            app.state.memory_store.get_voice_session_by_id(session_id=session.json()["session_id"])
            is None
        )

        blocked_after_revoke = await client.post(
            "/v1/sessions",
            headers=child_headers,
            json={"session_focus": "tutor_homework"},
        )
        assert blocked_after_revoke.status_code == 403


@pytest.mark.asyncio
async def test_guardian_summary_requires_exact_active_link_and_weekly_consent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        parent, parent_headers = await _register(client, "guardian-no-link")
        child, _ = await _register(client, "guardian-private-child")
        _mark_verified_adult(app, parent["user_id"])
        parent_headers = await _login(client, "guardian-no-link")
        app.state.memory_store.bind_external_identities(
            preferred_user_id=parent["user_id"],
            identities={"wechat_openid": "parent-no-link-openid"},
            now=datetime.now(UTC).isoformat(),
        )
        app.state.memory_store.update_subject_profile(
            user_id=child["user_id"],
            subject_category="minor",
            birth_year_band="14_17",
            now=datetime.now(UTC).isoformat(),
        )

        response = await client.get(
            f"/v1/guardian/minors/{child['user_id']}/summary",
            headers=parent_headers,
        )

    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "guardian_link_required"}


@pytest.mark.asyncio
async def test_authorized_child_corpus_is_time_bounded_and_revocation_deletes_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    archive_headers = {
        "X-Memoria-Internal-Token": "guardian-test-archive-token-that-is-long-enough"
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        parent, parent_headers = await _register(client, "corpus-parent")
        child, child_headers = await _register(client, "corpus-child")
        _mark_verified_adult(app, parent["user_id"])
        parent_headers = await _login(client, "corpus-parent")
        app.state.memory_store.bind_external_identities(
            preferred_user_id=parent["user_id"],
            identities={"wechat_openid": "corpus-parent-openid"},
            now=datetime.now(UTC).isoformat(),
        )
        link = (
            await client.post(
                "/v1/guardian/links",
                headers={**parent_headers, "Idempotency-Key": "corpus-link-request-001"},
                json={"minor_user_id": child["user_id"], "relation": "parent"},
            )
        ).json()
        confirmed = await client.post(
            f"/v1/guardian/links/{link['link_id']}/confirm",
            headers=child_headers,
            json={"binding_code": link["binding_code"], "birth_year_band": "under_14"},
        )
        assert confirmed.status_code == 200
        child_headers = await _login(client, "corpus-child")

        missing_retention = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "corpus-missing-retention"},
            json={
                "consent_kind": "corpus_recording",
                "policy_version": "authorized-child-corpus-v1",
            },
        )
        assert missing_retention.status_code == 422
        voice = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "corpus-voice-consent"},
            json={
                "consent_kind": "minor_voice_session",
                "policy_version": "minor-voice-v1",
            },
        )
        corpus = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "corpus-audio-consent"},
            json={
                "consent_kind": "corpus_recording",
                "policy_version": "authorized-child-corpus-v1",
                "retention_days": 7,
            },
        )
        assert voice.status_code == 201
        assert corpus.status_code == 201
        assert corpus.json()["expires_at"] is not None

        session = await client.post("/v1/sessions", headers=child_headers, json={})
        session_id = session.json()["session_id"]
        consent_snapshot = await client.get(
            "/v1/archive/session-raw-voice-consent",
            headers=archive_headers,
            params={"session_id": session_id},
        )
        assert consent_snapshot.json()["archive_purpose"] == "corpus_recording"
        event = {
            "event_id": "minor-authorized-corpus-turn",
            "session_id": session_id,
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "test.authoritative-final",
            "turn_id": 1,
            "generation_id": 1,
            "tool_epoch": 0,
            "payload": {"text": "这句话的原始音频仅用于限期授权语料。"},
        }
        transcript = await client.post(
            "/v1/archive/session-events",
            headers=archive_headers,
            json=event,
        )
        audio = await client.post(
            "/v1/archive/session-raw-audio",
            headers=archive_headers,
            json={
                **event,
                "consent_grant_id": corpus.json()["consent_id"],
                "archive_purpose": "corpus_recording",
                "retention_policy": "corpus_time_bounded",
                "audio_base64": _wav_base64(),
                "media_type": "audio/wav",
            },
        )
        assert transcript.status_code == 201
        assert audio.status_code == 201
        samples = await app.state.guardian_store.corpus_samples(minor_user_id=child["user_id"])
        assert len(samples) == 1
        assert await app.state.archive_object_store.get(samples[0].reference)

        revoked = await client.delete(
            f"/v1/guardian/links/{link['link_id']}/consents/{corpus.json()['consent_id']}",
            headers={**parent_headers, "Idempotency-Key": "corpus-revoke-001"},
        )
        assert revoked.status_code == 200

    deleted = await app.state.guardian_store.corpus_sample_by_event(
        minor_user_id=child["user_id"],
        source_event_id=event["event_id"],
    )
    assert deleted is not None and deleted.deleted_at is not None
    with pytest.raises(FileNotFoundError):
        await app.state.archive_object_store.get(deleted.reference)


class _SubjectStore:
    def __init__(self, category: str | None) -> None:
        self.category = category

    def get_subject_profile(self, *, user_id: str) -> dict[str, Any] | None:
        del user_id
        return {"subject_category": self.category} if self.category is not None else None


@pytest.mark.parametrize("capability", tuple(SUBJECT_CAPABILITY_RULES))
def test_subject_capability_matrix_covers_adult_minor_and_missing(
    capability: SubjectCapability,
) -> None:
    allowed = SUBJECT_CAPABILITY_RULES[capability]
    for category in ("adult", "minor", "unknown"):
        if category in allowed:
            profile = require_capability_for_account_id(
                "account-1",
                capability,
                store=_SubjectStore(category),
            )
            assert profile["subject_category"] == category
        else:
            with pytest.raises(HTTPException) as exc_info:
                require_capability_for_account_id(
                    "account-1",
                    capability,
                    store=_SubjectStore(category),
                )
            assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        require_capability_for_account_id(
            "account-1",
            capability,
            store=_SubjectStore(None),
        )
    assert missing.value.detail["code"] == "subject_category_unavailable"


def test_unconfigured_subject_capability_fails_closed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_capability_for_account_id(
            "account-1",
            cast(SubjectCapability, "new_unreviewed_capability"),
            store=_SubjectStore("adult"),
        )
    assert exc_info.value.detail["code"] == "subject_capability_unconfigured"
