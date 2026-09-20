from __future__ import annotations

import asyncio
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
        # P2-03: naming the speaker subject requires the exact fence the signed
        # runtime authority issued for this session; a partial claim is stale.
        subject_fence = {
            "active_subject_id": child["user_id"],
            "runtime_profile_id": f"rp-{session.json()['session_id']}",
            "session_epoch": 1,
            "actor_id": child["user_id"],
            "device_id": "device-test-1",
            "binding_id": "binding-test-1",
            "binding_version": 1,
            "subject_revision": 1,
        }
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
                **subject_fence,
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
                **subject_fence,
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
                subject_id=child["user_id"],
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
            "active_subject_id": child["user_id"],
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
        if self.category is None:
            return None
        # Speaker enrollment additionally requires verified adult age evidence;
        # model a realistic profile where only adults carry it.
        return {
            "subject_category": self.category,
            "birth_year_band": "adult" if self.category == "adult" else "unknown",
            "age_evidence_status": "verified" if self.category == "adult" else "unverified",
        }


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


@pytest.mark.asyncio
async def test_accountless_child_person_consent_lifts_and_reverts_the_retention_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04b: 独立使用人（无账号孩子）的 person 级 consent 授予、策略读门提升与撤销闭环。"""

    from datetime import timedelta

    from services.control_api.app.device_binding_token import mint_device_binding_token

    _configure(monkeypatch, tmp_path)
    now = datetime.now(UTC)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 注册合法成人家长并绑定微信身份
        reg = await client.post("/v1/auth/register", json={"username": "guardian_owner", "password": "safe-password-123"})
        assert reg.status_code == 201
        owner = reg.json()
        owner_id = owner["user_id"]
        _mark_verified_adult(app, owner_id)
        app.state.memory_store.bind_external_identities(
            preferred_user_id=owner_id,
            identities={"wechat_openid": "wx-open-owner"},
            now=now.isoformat(),
        )
        login = await client.post("/v1/auth/login", json={"username": "guardian_owner", "password": "safe-password-123"})
        owner_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # 2. 注册 Identity 实体并创建 parent_for_child 绑定，包含独立无账号孩子 draft
        await app.state.identity_service.register_person(
            person_id=owner_id,
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-owner-evidence",
            now=now,
        )
        claim_token = mint_device_binding_token(
            device_id="dev-child-person-consent",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="nonce-person-consent-1",
        )
        bind_res = await client.post(
            "/v1/device-bindings",
            headers={**owner_headers, "Idempotency-Key": "bind-child-person-001"},
            json={
                "device_claim_token": claim_token,
                "declared_mode": "parent_for_child",
                "account_owner_person_id": owner_id,
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
        assert bind_res.status_code == 201, bind_res.text
        child_id = bind_res.json()["primary_subject_ids"][0]

        # 3. 建立会话，并以独立孩子为 active_subject 签发运行时 profile
        session_res = await client.post("/v1/sessions", headers=owner_headers, json={"session_focus": "tutor_english"})
        assert session_res.status_code == 200
        session_id = session_res.json()["session_id"]
        _attach_signed_runtime_profile(
            app,
            user_id=owner_id,
            session_id=session_id,
            active_subject_id=child_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "tutor", "english_practice"),
        )

        # 4. 未授予记忆留存同意前，/session-policy 读门生效：强制 ephemeral_only
        policy_res1 = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "guardian-test-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )
        assert policy_res1.status_code == 200
        assert policy_res1.json()["memory_retention"] == "ephemeral_only"

        # 5. 非绑定拥有者的第三方陌生人尝试授予该孩子 consent，被 403 拒绝
        stranger_reg = await client.post("/v1/auth/register", json={"username": "stranger_guardian", "password": "safe-password-123"})
        stranger_id = stranger_reg.json()["user_id"]
        _mark_verified_adult(app, stranger_id)
        app.state.memory_store.bind_external_identities(
            preferred_user_id=stranger_id,
            identities={"wechat_openid": "wx-open-stranger"},
            now=now.isoformat(),
        )
        stranger_login = await client.post("/v1/auth/login", json={"username": "stranger_guardian", "password": "safe-password-123"})
        stranger_headers = {"Authorization": f"Bearer {stranger_login.json()['access_token']}"}

        evil_grant = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**stranger_headers, "Idempotency-Key": "evil-grant-001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert evil_grant.status_code == 403
        assert evil_grant.json()["detail"]["code"] == "guardian_binding_owner_required"

        # 6. 合法家长（绑定拥有者）通过 HTTP 真实授予 person 级 memory_retention
        grant_res = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**owner_headers, "Idempotency-Key": "owner-grant-001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert grant_res.status_code == 201, grant_res.text
        consent_body = grant_res.json()
        assert consent_body["consent_kind"] == "memory_retention"
        assert consent_body["subject_person_id"] == child_id
        assert consent_body["grantor_person_id"] == owner_id
        assert consent_body["basis"] == "binding_owner_declaration"
        assert consent_body["active"] is True
        consent_id = consent_body["consent_id"]

        # 列表接口验证
        list_res = await client.get(f"/v1/guardian/minors/{child_id}/consents", headers=owner_headers)
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert len(items) == 1
        assert items[0]["consent_id"] == consent_id

        # 7. 授予后，/session-policy 读门识别到有效 person 级 consent：天花板解除（不再含 memory_retention: ephemeral_only）
        policy_res2 = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "guardian-test-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )
        assert policy_res2.status_code == 200
        assert "memory_retention" not in policy_res2.json()

        # 8. Same body + same Idempotency-Key replays the original grant.
        replay_res = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**owner_headers, "Idempotency-Key": "owner-grant-001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert replay_res.status_code == 201, replay_res.text
        assert replay_res.json()["consent_id"] == consent_id
        assert replay_res.json()["granted_at"] == consent_body["granted_at"]
        assert replay_res.json()["revoked_at"] is None

        # 9. Same key + different payload still conflicts.
        conflict_res = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**owner_headers, "Idempotency-Key": "owner-grant-001"},
            json={"consent_kind": "minor_voice_session", "policy_version": "minor-voice-v1"},
        )
        assert conflict_res.status_code == 409
        assert conflict_res.json()["detail"]["code"] == "guardian_consent_conflict"

        # 10. Concurrent replay of one key/body returns one authorization and
        # persists one row.
        concurrent_body = {
            "consent_kind": "minor_voice_session",
            "policy_version": "minor-voice-v1",
        }
        concurrent_headers = {
            **owner_headers,
            "Idempotency-Key": "owner-grant-concurrent-001",
        }
        concurrent = await asyncio.gather(
            client.post(
                f"/v1/guardian/minors/{child_id}/consents",
                headers=concurrent_headers,
                json=concurrent_body,
            ),
            client.post(
                f"/v1/guardian/minors/{child_id}/consents",
                headers=concurrent_headers,
                json=concurrent_body,
            ),
        )
        assert [response.status_code for response in concurrent] == [201, 201]
        concurrent_ids = {response.json()["consent_id"] for response in concurrent}
        assert len(concurrent_ids) == 1
        voice_consent_id = concurrent_ids.pop()
        list_res2 = await client.get(
            f"/v1/guardian/minors/{child_id}/consents", headers=owner_headers
        )
        assert list_res2.status_code == 200
        assert sorted(item["consent_id"] for item in list_res2.json()["items"]) == sorted(
            [consent_id, voice_consent_id]
        )

        # 11. Unbind: the recorded grantor loses the ACTIVE binding but must
        # keep a revoke entry.
        unbind_res = await client.post(
            "/v1/devices/dev-child-person-consent/binding/unbind",
            headers=owner_headers,
            json={"reason": "person consent lifecycle test"},
        )
        assert unbind_res.status_code == 200, unbind_res.text
        assert unbind_res.json()["status"] == "revoked"

        # 12. A stranger still cannot revoke after the unbind.
        stranger_delete = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{consent_id}",
            headers={**stranger_headers, "Idempotency-Key": "stranger-revoke-001"},
        )
        assert stranger_delete.status_code == 404

        # 13. The recorded grantor can revoke its own grant after the unbind.
        revoke_res = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{consent_id}",
            headers={**owner_headers, "Idempotency-Key": "owner-revoke-001"},
        )
        assert revoke_res.status_code == 200, revoke_res.text
        assert revoke_res.json()["active"] is False

        # Same-key revoke replay returns the original revocation.
        revoke_replay = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{consent_id}",
            headers={**owner_headers, "Idempotency-Key": "owner-revoke-001"},
        )
        assert revoke_replay.status_code == 200
        assert revoke_replay.json()["revoked_at"] == revoke_res.json()["revoked_at"]

        # 14. The policy gate closes again after the revoke.
        policy_res3 = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "guardian-test-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )
        assert policy_res3.status_code == 200
        assert policy_res3.json()["memory_retention"] == "ephemeral_only"

        # 15. Granting still requires an ACTIVE binding after the unbind.
        after_unbind_grant = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**owner_headers, "Idempotency-Key": "owner-grant-after-unbind-001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert after_unbind_grant.status_code == 403
        assert (
            after_unbind_grant.json()["detail"]["code"]
            == "guardian_binding_owner_required"
        )

        # 16. The other grant can also be revoked by its recorded grantor.
        voice_revoke = await client.delete(
            f"/v1/guardian/minors/{child_id}/consents/{voice_consent_id}",
            headers={**owner_headers, "Idempotency-Key": "owner-revoke-voice-001"},
        )
        assert voice_revoke.status_code == 200, voice_revoke.text
        assert voice_revoke.json()["active"] is False
