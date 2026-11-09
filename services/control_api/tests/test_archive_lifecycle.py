"""Control API archive: legacy shell, account lifecycle, sessions and deletion status."""

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    SpeakerClass,
)
from services.control_api.app.main import create_app
from services.control_api.app.routes.archive import (
    _deletion_status_response,
)
from services.control_api.tests.archive_api_test_support import (
    _configure,
    _register_verified_adult,
    _response_provenance,
)
from services.digital_self.domain import (
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    DigitalSelfVersion,
    RelationshipProfileManifestEntry,
)
from services.guardian.domain import PersonConsentRecord
from services.identity.domain import IdentityNotFoundError
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessSnapshot,
    LegacyAuditTarget,
    LegacyFence,
    LegacyManifestItemRef,
    LegacyShellTurn,
)


class LegacyArchiveStub:
    def __init__(self, access: LegacyAccessSnapshot) -> None:
        self.access = access
        self.available = True
        self.turns: dict[str, LegacyShellTurn] = {}
        self.runtime_audits: list[dict[str, object]] = []

    async def resolve_access(self, **kwargs: object) -> LegacyAccessSnapshot:
        now = kwargs["now"]
        if not self.available or not isinstance(now, datetime) or now >= self.access.expires_at:
            raise LegacyAccessDeniedError("legacy grant is not available")
        return self.access

    async def append_shell_turn(self, **kwargs: object) -> LegacyShellTurn:
        key = str(kwargs["idempotency_key"])
        existing = self.turns.get(key)
        if existing is not None:
            return existing
        fence = kwargs["fence"]
        turn = LegacyShellTurn(
            shell_turn_id=f"shell-turn-{len(self.turns) + 1}",
            shell_id=str(kwargs["shell_id"]),
            grant_id=self.access.grant_id,
            actor_role=kwargs["actor_role"],  # type: ignore[arg-type]
            actual_heard_text=str(kwargs["actual_heard_text"]),
            fence=fence,  # type: ignore[arg-type]
            occurred_at=kwargs["now"],  # type: ignore[arg-type]
        )
        self.turns[key] = turn
        return turn

    async def append_runtime_audit(self, **kwargs: object) -> None:
        self.runtime_audits.append(kwargs)


class LegacyVersionStub:
    def __init__(self, version: DigitalSelfVersion) -> None:
        self.version = version

    async def get(self, **kwargs: object) -> DigitalSelfVersion:
        assert kwargs == {
            "account_id": self.version.account_id,
            "version_id": self.version.version_id,
        }
        return self.version


def _conversation_session_event(
    *,
    account_id: str,
    event_id: str,
    subject_id: str,
    session_id: str,
    occurred_at: datetime,
    text: str,
    turn_id: int = 1,
    speaker_class: SpeakerClass = "owner",
) -> EvidenceEvent:
    assistant = speaker_class == "assistant"
    return EvidenceEvent(
        event_id=event_id,
        account_id=account_id,
        subject_id=subject_id,
        event_type="assistant.playout_stopped" if assistant else "speech.utterance_finalized",
        occurred_at=occurred_at,
        speaker_class=speaker_class,
        source="test.demo03",
        session_id=session_id,
        turn_id=turn_id,
        generation_id=1,
        payload={
            "text": text,
            "history_eligible": True,
            "owner_projection_eligible": True,
            **({"actual_heard": True} if assistant else {}),
        },
    )


def _legacy_access(*, actor_role: str = "grantee", expired: bool = False) -> LegacyAccessSnapshot:
    return LegacyAccessSnapshot(
        actor_role=actor_role,  # type: ignore[arg-type]
        resource_owner_account_id="legacy-owner",
        grantee_account_id="legacy-grantee",
        grant_id="legacy-grant",
        shell_id="legacy-shell" if actor_role == "grantee" else None,
        version_id="legacy-version",
        version_number=7,
        manifest_sha256="a" * 64,
        grant_snapshot_sha256="b" * 64,
        scope_sha256="c" * 64,
        allowed_items=(LegacyManifestItemRef("relationship_profile", "relationship-1"),),
        relationship_profile_id="relationship-1",
        relationship_profile_version=3,
        voice_allowed=False,
        expires_at=datetime.now(UTC) + timedelta(days=-1 if expired else 30),
    )


def _legacy_version(access: LegacyAccessSnapshot) -> DigitalSelfVersion:
    relationship = RelationshipProfileManifestEntry(
        profile_id=access.relationship_profile_id,
        version_number=access.relationship_profile_version,
        person_id="person-1",
        relationship_id="relationship-id",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=(),
        support_source_event_ids=(),
        counterexample_source_event_ids=(),
    )
    return DigitalSelfVersion(
        version_id=access.version_id,
        account_id=access.resource_owner_account_id,
        version_number=access.version_number,
        status="frozen",
        manifest=DigitalSelfManifest(
            schema_version="digital-self-manifest-v3",
            compiler_version="compiler-v3",
            policy_version="policy-v3",
            parent_version_id=None,
            rollback_target_version_id=None,
            entries=(relationship,),
            source_summary=DigitalSelfSourceSummary(
                memory_claim_count=0,
                persona_trait_count=0,
                persona_version_id=None,
                source_summary_sha256="summary",
                relationship_profile_count=1,
            ),
        ),
        manifest_sha256=access.manifest_sha256,
        created_at=datetime.now(UTC),
    )


def _add_legacy_session(app: object, access: LegacyAccessSnapshot) -> str:
    session_id = f"legacy-session-{access.actor_role}-{uuid.uuid4()}"
    actor = (
        access.resource_owner_account_id
        if access.actor_role == "owner_preview"
        else access.grantee_account_id
    )
    app.state.memory_store.add_voice_session(  # type: ignore[attr-defined]
        session_id=session_id,
        user_id=actor,
        resource_owner_account_id=access.resource_owner_account_id,
        room_name=f"room-{session_id}",
        voice_backend="cascade",
        created_at=datetime.now(UTC).isoformat(),
        interaction_mode="legacy",
        mode_policy_version="s9-v1",
        digital_self_version_id=access.version_id,
        digital_self_manifest_sha256=access.manifest_sha256,
        relationship_profile_id=access.relationship_profile_id,
        relationship_profile_version=access.relationship_profile_version,
        legacy_grant_id=access.grant_id,
        legacy_actor_role=access.actor_role,
        legacy_grantee_account_id=access.grantee_account_id,
        legacy_shell_id=access.shell_id,
        legacy_grant_snapshot_sha256=access.grant_snapshot_sha256,
        legacy_scope_sha256=access.scope_sha256,
        legacy_voice_allowed=access.voice_allowed,
        legacy_expires_at=access.expires_at.isoformat(),
        fallback_voice_profile_id="warm_companion",
        fallback_voice_provider="volcengine_doubao",
        fallback_voice_model="seed-tts-2.0",
        fallback_voice_resource_id="seed-tts-2.0",
    )
    return session_id


@pytest.mark.asyncio
async def test_registered_account_exports_only_its_portable_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(
            client,
            app,
            username="archive-owner",
            password="safe-passphrase",
        )
        other = await _register_verified_adult(
            client,
            app,
            username="archive-other",
            password="other-passphrase",
        )
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        for index, (identity, headers, text) in enumerate(
            (
                (owner, owner_headers, "请记住我喜欢雨天散步。"),
                (other, other_headers, "另一账户的私密内容。"),
            ),
            start=1,
        ):
            saved = await client.post(
                "/v1/memory/messages",
                headers=headers,
                json={
                    "user_id": identity["user_id"],
                    "client_message_id": f"00000000-0000-4000-8000-{index:012d}",
                    "role": "user",
                    "text": text,
                },
            )
            assert saved.status_code == 201
            session = (
                await client.post(
                    "/v1/sessions",
                    headers=headers,
                    json={"user_id": identity["user_id"], "voice_backend": "cascade"},
                )
            ).json()
            recorded = await client.post(
                "/v1/archive/session-events",
                headers=internal_headers,
                json={
                    "event_id": f"export-{identity['user_id']}",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "active_subject_id": identity["user_id"],
                    "payload": {"text": text},
                },
            )
            assert recorded.status_code == 201

        growth_task = await client.post(
            "/v1/growth/tasks",
            headers=owner_headers,
            json={"event_id": "export-growth-task", "kind": "natural_chat"},
        )
        growth_feedback = await client.post(
            "/v1/growth/owner-actions",
            headers=owner_headers,
            json={
                "event_id": "export-growth-feedback",
                "action": "not_me",
                "target_kind": "source_event",
                "target_id": f"export-{owner['user_id']}",
            },
        )
        assert (growth_task.status_code, growth_feedback.status_code) == (201, 201)

        wrong_password = await client.post(
            "/v1/archive/exports",
            headers=owner_headers,
            json={"password": "wrong-passphrase"},
        )
        exported = await client.post(
            "/v1/archive/exports",
            headers=owner_headers,
            json={"password": "safe-passphrase"},
        )

    assert wrong_password.status_code == 403
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].startswith("attachment;")
    body = exported.json()
    # P2-03: a self export is a subject-scoped partial export, not the old
    # account-wide snapshot, and it declares what it withheld.
    assert body["format_version"] == 2
    assert body["account_id"] == owner["user_id"]
    assert body["scope"]["export"] == "subject"
    assert body["scope"]["subject_id"] == owner["user_id"]
    assert body["scope"]["audience"] == "self"
    assert body["scope"]["partial"] is True
    assert body["manifest_sha256"]
    serialized = exported.text
    assert "请记住我喜欢雨天散步。" in serialized
    assert "另一账户的私密内容。" not in serialized
    assert "password_hash" not in serialized
    assert "template_ciphertext" not in serialized
    evidence = body["sections"]["evidence"]
    items = {item["event_id"]: item for item in evidence["items"]}
    # Owner control-plane receipts are written by the authenticated owner, so
    # they carry the owner's subject and stay in the owner's own export.
    assert set(items) == {
        f"export-{owner['user_id']}",
        "export-growth-task",
        "export-growth-feedback",
    }
    speech = items[f"export-{owner['user_id']}"]
    assert speech["content_id"] == f"export-{owner['user_id']}"
    assert speech["service_provider"] == "Memoria"
    assert speech["ai_generated"] is False
    assert body["sections"]["account"]["profile"]["user_id"] == owner["user_id"]
    omissions = {
        entry["name"]: entry["reason_code"] for entry in body["omitted_sections"]
    }
    assert {
        "conversation",
        "archive",
        "speaker",
        "evolution",
        "legacy",
        "guardian",
    } <= set(omissions)
    assert all(entry["reason"] for entry in body["omitted_sections"])
    # The growth receipts are the owner's own actions, not a member's speech.
    assert "export-growth-task" in serialized
    assert "export-growth-feedback" in serialized


@pytest.mark.asyncio
async def test_registered_account_deletion_revokes_login_token_session_and_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "delete-owner", "password": "safe-passphrase"},
            )
        ).json()
        other = (
            await client.post(
                "/v1/auth/register",
                json={"username": "delete-other", "password": "other-passphrase"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        session = (
            await client.post(
                "/v1/sessions",
                headers=owner_headers,
                json={"user_id": owner["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        recorded = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "delete-owner-evidence",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {"text": "这条内容应被删除。"},
            },
        )
        assert recorded.status_code == 201
        task = await client.post(
            "/v1/growth/tasks",
            headers=owner_headers,
            json={"event_id": "delete-growth-task", "kind": "natural_chat"},
        )
        feedback = await client.post(
            "/v1/growth/owner-actions",
            headers=owner_headers,
            json={
                "event_id": "delete-growth-feedback",
                "action": "not_me",
                "target_kind": "source_event",
                "target_id": "delete-owner-evidence",
            },
        )
        assert (task.status_code, feedback.status_code) == (201, 201)

        # Production registers an identity person for every account; the
        # deletion must reach it too (#283).
        await app.state.identity_service.register_person(
            person_id=owner["user_id"],
            display_name="账户使用人",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-archive-deletion-owner",
            now=datetime.now(UTC),
        )
        assert await app.state.identity_service.get_person(
            owner["user_id"], actor_person_id=owner["user_id"]
        )

        wrong_confirmation = await client.post(
            "/v1/archive/deletion-requests",
            headers=owner_headers,
            json={"password": "safe-passphrase", "confirmation": "删除"},
        )
        deleted = await client.post(
            "/v1/archive/deletion-requests",
            headers=owner_headers,
            json={"password": "safe-passphrase", "confirmation": "永久删除我的全部数据"},
        )
        old_token = await client.get("/v1/auth/me", headers=owner_headers)
        old_login = await client.post(
            "/v1/auth/login",
            json={"username": "delete-owner", "password": "safe-passphrase"},
        )
        replacement = await client.post(
            "/v1/auth/register",
            json={"username": "delete-owner", "password": "safe-passphrase"},
        )
        replacement_body = replacement.json()
        replacement_headers = {"Authorization": f"Bearer {replacement_body['access_token']}"}
        replacement_profile = await client.get(
            f"/v1/memory/profile/{replacement_body['user_id']}",
            headers=replacement_headers,
        )
        replacement_days = await client.get(
            "/v1/memory/days",
            headers=replacement_headers,
            params={"user_id": replacement_body["user_id"]},
        )
        deleted_session_event = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "deleted-session-event",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {"text": "删除后不得重新落账"},
            },
        )
        other_still_exists = await client.get("/v1/auth/me", headers=other_headers)
        remaining_growth_events = await app.state.life_archive.context(
            ContextQuery(account_id=owner["user_id"], speaker_class="owner", limit=100)
        )
        try:
            identity_row = await app.state.identity_service.get_person(
                owner["user_id"], actor_person_id=owner["user_id"]
            )
        except IdentityNotFoundError:
            identity_row = None

    assert wrong_confirmation.status_code == 422
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "completed"
    assert deleted.json()["terminate_sessions"] is True
    assert old_token.status_code == 401
    assert old_login.status_code == 401
    assert replacement.status_code == 201
    assert replacement_body["user_id"] != owner["user_id"]
    assert replacement_profile.status_code == 200
    assert replacement_profile.json()["companion_id"] is None
    assert replacement_days.status_code == 200
    assert replacement_days.json()["items"] == []
    assert deleted_session_event.status_code == 410
    assert other_still_exists.status_code == 200
    assert remaining_growth_events.evidence == ()
    # #283: account deletion erases the identity person row itself, so nothing
    # that names the usage person survives the deletion.
    assert identity_row is None
    assert (
        await app.state.identity_store.remaining_account_identity(
            account_person_id=owner["user_id"]
        )
        == {}
    )


@pytest.mark.asyncio
async def test_legacy_session_events_only_append_actual_heard_turns_to_the_grantee_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    access = _legacy_access()
    legacy = LegacyArchiveStub(access)
    app.state.legacy_registry = legacy
    app.state.digital_self_registry = LegacyVersionStub(_legacy_version(access))
    session_id = _add_legacy_session(app, access)
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    now = datetime.now(UTC).isoformat()
    user_event = {
        "event_id": "legacy-user-final",
        "session_id": session_id,
        "event_type": "speech.utterance_finalized",
        "occurred_at": now,
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "turn_id": 1,
        "generation_id": 2,
        "tool_epoch": 0,
        "payload": {"text": "这是受赠人实际说出的话。"},
    }
    provenance = _response_provenance(
        session_id=session_id,
        turn_id=1,
        generation_id=2,
        source_refs=[],
        planner_policy_version="local-safe-fallback-v1",
        interaction_mode="legacy",
        mode_policy_version="s9-v1",
        digital_self_version_id=access.version_id,
        manifest_sha256=access.manifest_sha256,
        relationship_profile_id=access.relationship_profile_id,
        relationship_profile_version=access.relationship_profile_version,
        actor_account_id=access.grantee_account_id,
        resource_owner_account_id=access.resource_owner_account_id,
        legacy_actor_role="grantee",
        legacy_grantee_account_id=access.grantee_account_id,
        legacy_grant_id=access.grant_id,
        legacy_grant_snapshot_sha256=access.grant_snapshot_sha256,
        legacy_scope_sha256=access.scope_sha256,
        legacy_shell_id=access.shell_id,
        legacy_voice_allowed=False,
        legacy_expires_at=access.expires_at.isoformat(),
    )
    assistant_event = {
        "event_id": "legacy-assistant-playout",
        "session_id": session_id,
        "event_type": "assistant.playout_stopped",
        "occurred_at": now,
        "speaker_class": "assistant",
        "source": "generation_fence.actual_heard",
        "turn_id": 1,
        "generation_id": 2,
        "tool_epoch": 0,
        "payload": {
            "text": "这是安全拒答。",
            "actual_heard": True,
            "response_provenance": provenance,
        },
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user = await client.post("/v1/archive/session-events", headers=internal, json=user_event)
        duplicate = await client.post(
            "/v1/archive/session-events", headers=internal, json=user_event
        )
        assistant = await client.post(
            "/v1/archive/session-events", headers=internal, json=assistant_event
        )

    assert (user.status_code, duplicate.status_code, assistant.status_code) == (201, 201, 201)
    assert [turn.actor_role for turn in legacy.turns.values()] == ["grantee", "digital_self"]
    assert all(turn.fence.tool_epoch == 0 for turn in legacy.turns.values())
    assert [audit["action"] for audit in legacy.runtime_audits] == [
        "select_voice",
        "refuse",
    ]
    assert legacy.runtime_audits[0]["reason"] == "voice_selected"
    assert legacy.runtime_audits[0]["target"] == LegacyAuditTarget(
        "voice_profile", "warm_companion"
    )
    assert legacy.runtime_audits[1]["reason"] == "privacy_refusal"
    assert all(
        audit["fence"] == LegacyFence(session_id, "1", "2", 0) for audit in legacy.runtime_audits
    )
    assert provenance["source_refs"] == []
    for account_id in (access.grantee_account_id, access.resource_owner_account_id):
        context = await app.state.life_archive.context(
            ContextQuery(account_id=account_id, session_id=session_id, speaker_class="owner")
        )
        assert context.evidence == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked", "expired"])
async def test_legacy_archive_fails_closed_after_grant_revocation_or_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    denial: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    access = _legacy_access(expired=denial == "expired")
    legacy = LegacyArchiveStub(access)
    legacy.available = denial != "revoked"
    app.state.legacy_registry = legacy
    session_id = _add_legacy_session(app, access)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "event_id": f"legacy-{denial}",
                "session_id": session_id,
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {"text": "不得写入。"},
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "legacy_access_unavailable"}
    assert legacy.turns == {}


@pytest.mark.asyncio
async def test_legacy_owner_preview_never_creates_a_relationship_shell_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    access = _legacy_access(actor_role="owner_preview")
    legacy = LegacyArchiveStub(access)
    app.state.legacy_registry = legacy
    session_id = _add_legacy_session(app, access)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "event_id": "legacy-owner-preview",
                "session_id": session_id,
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {"text": "预演不形成关系外壳。"},
            },
        )
    assert response.status_code == 200
    assert response.json()["shell_turn_id"] is None
    assert legacy.turns == {}


@pytest.mark.asyncio
async def test_conversation_history_returns_paired_turns_and_rejects_cross_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P1-05: one account-scoped session's paired turns, traceable to evidence.

    A user turn and its actual-heard assistant reply share turn 1; an
    ineligible owner turn and an unheard assistant event are omitted, never
    fabricated. Each turn carries its source event ids and the assistant
    approximate-delivery flag. A second account's session is invisible to the
    first account, and the paired turns only surface for the account's own
    subject (P2-03: same-account subject switching is now fenced by the
    evidence subject, retention-scoped temporary readback stays open, see
    TODOLIST P1-05).
    """
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="history-owner")
        other = await _register_verified_adult(client, app, username="history-other")
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        session = (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
        other_session = (
            await client.post("/v1/sessions", headers=other_headers, json={})
        ).json()
        occurred_at = datetime.now(UTC).isoformat()
        user_event = {
            "event_id": "history-user-1",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at,
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "turn_id": 1,
            "generation_id": 1,
            "tool_epoch": 0,
            "active_subject_id": owner["user_id"],
            "payload": {"text": "明天南京天气如何？"},
        }
        assistant_event = {
            "event_id": "history-assistant-1",
            "session_id": session["session_id"],
            "event_type": "assistant.playout_stopped",
            "occurred_at": occurred_at,
            "speaker_class": "assistant",
            "source": "generation_fence.actual_heard",
            "turn_id": 1,
            "generation_id": 1,
            "tool_epoch": 0,
            "payload": {"text": "明天有雨。", "actual_heard": True},
        }
        other_user = {
            **user_event,
            "event_id": "history-other-user-1",
            "session_id": other_session["session_id"],
            "active_subject_id": other["user_id"],
        }
        for event in (user_event, assistant_event, other_user):
            response = await client.post(
                "/v1/archive/session-events", headers=internal, json=event
            )
            assert response.status_code == 201, response.text
        # Omission is a read-path property, not a write rejection: the write
        # gate stores the raw evidence, and the history exit only surfaces
        # eligible owner turns plus actual-heard replies. Flip the stored
        # eligibility bits directly to prove the read path omits them.
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="history-user-ineligible",
                account_id=owner["user_id"],
                subject_id=owner["user_id"],
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="test",
                payload={
                    "text": "这句不应出现。",
                    "history_eligible": False,
                    "owner_projection_eligible": False,
                },
                session_id=session["session_id"],
                turn_id=2,
                generation_id=2,
            )
        )
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="history-assistant-unheard",
                account_id=owner["user_id"],
                subject_id=owner["user_id"],
                event_type="assistant.playout_stopped",
                occurred_at=datetime.now(UTC),
                speaker_class="assistant",
                source="test",
                payload={
                    "text": "这句也不应出现。",
                    "actual_heard": False,
                    "history_eligible": True,
                    "owner_projection_eligible": True,
                },
                session_id=session["session_id"],
                turn_id=2,
                generation_id=2,
            )
        )
        history = await client.get(
            "/v1/archive/conversation-history",
            headers=owner_headers,
            params={"session_id": session["session_id"]},
        )
        assert history.status_code == 200, history.text
        assert history.json() == {
            "session_id": session["session_id"],
            "turns": [
                {
                    "turn_id": 1,
                    "generation_id": 1,
                    "owner_text": "明天南京天气如何？",
                    "assistant_text": "明天有雨。",
                    "owner_event_id": "history-user-1",
                    "assistant_event_id": "history-assistant-1",
                    "assistant_approximate": True,
                }
            ],
        }
        cross = await client.get(
            "/v1/archive/conversation-history",
            headers=owner_headers,
            params={"session_id": other_session["session_id"]},
        )
        assert cross.status_code == 200, cross.text
        assert cross.json() == {
            "session_id": other_session["session_id"],
            "turns": [],
        }
        sessions = await client.get(
            "/v1/archive/conversation-sessions",
            headers=owner_headers,
        )
        assert sessions.status_code == 200, sessions.text
        assert sessions.json() == {
            "items": [
                {
                    "session_id": session["session_id"],
                    "occurred_at": occurred_at,
                    "turn_count": 1,
                    "preview": "明天南京天气如何？",
                }
            ]
        }


@pytest.mark.asyncio
async def test_conversation_sessions_preview_uses_latest_nonblank_eligible_text_and_caps_at_80(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC) - timedelta(minutes=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(
            client,
            app,
            username="conversation-session-preview",
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        subject_id = str(owner["user_id"])
        await app.state.life_archive.record(
            _conversation_session_event(
                account_id=subject_id,
                event_id="preview-real",
                subject_id=subject_id,
                session_id=session["session_id"],
                occurred_at=now,
                text="孩子说了今天的事。",
            )
        )
        await app.state.life_archive.record(
            _conversation_session_event(
                account_id=subject_id,
                event_id="preview-latest-assistant",
                subject_id=subject_id,
                session_id=session["session_id"],
                occurred_at=now + timedelta(seconds=1),
                text="答" * 81,
                speaker_class="assistant",
            )
        )
        await app.state.life_archive.record(
            _conversation_session_event(
                account_id=subject_id,
                event_id="preview-blank",
                subject_id=subject_id,
                session_id=session["session_id"],
                occurred_at=now + timedelta(seconds=2),
                text="   ",
                turn_id=2,
            )
        )
        response = await client.get("/v1/archive/conversation-sessions", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["items"] == [
        {
            "session_id": session["session_id"],
            "occurred_at": (now + timedelta(seconds=2)).isoformat(),
            "turn_count": 2,
            "preview": "答" * 80,
        }
    ]


@pytest.mark.asyncio
async def test_conversation_sessions_list_turns_that_carry_no_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # #282: archived turns hold metadata only, so the session list must not
    # depend on a preview text that no real turn has.
    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(
            client, app, username="conversation-session-no-text"
        )
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        subject_id = str(owner["user_id"])
        for event in (
            _conversation_session_event(
                account_id=subject_id,
                event_id="no-text-user",
                subject_id=subject_id,
                session_id=session["session_id"],
                occurred_at=now,
                text="",
                turn_id=1,
            ),
            _conversation_session_event(
                account_id=subject_id,
                event_id="no-text-assistant",
                subject_id=subject_id,
                session_id=session["session_id"],
                occurred_at=now,
                text="",
                turn_id=1,
                speaker_class="assistant",
            ),
        ):
            await app.state.life_archive.record(
                replace(event, payload={k: v for k, v in event.payload.items() if k != "text"})
            )
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="no-text-ephemeral",
                account_id=subject_id,
                subject_id=subject_id,
                event_type="speech.utterance_finalized",
                occurred_at=now,
                speaker_class="owner",
                source="test",
                session_id="no-text-ephemeral-session",
                turn_id=1,
                generation_id=1,
                payload={
                    "history_eligible": True,
                    "owner_projection_eligible": True,
                    "memory_retention": "ephemeral_only",
                },
            )
        )
        response = await client.get("/v1/archive/conversation-sessions", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["items"] == [
        {
            "session_id": session["session_id"],
            "occurred_at": now.isoformat(),
            "turn_count": 1,
            "preview": "",
        }
    ]


@pytest.mark.asyncio
async def test_conversation_sessions_are_subject_scoped_newest_first_and_limited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(
            client,
            app,
            username="conversation-session-owner",
        )
        other = await _register_verified_adult(
            client,
            app,
            username="conversation-session-other",
        )
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        owner_sessions = [
            (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
            for _ in range(3)
        ]
        other_session = (
            await client.post("/v1/sessions", headers=other_headers, json={})
        ).json()
        for index, session in enumerate(owner_sessions):
            await app.state.life_archive.record(
                _conversation_session_event(
                    account_id=str(owner["user_id"]),
                    event_id=f"ordered-{index}",
                    subject_id=str(owner["user_id"]),
                    session_id=session["session_id"],
                    occurred_at=now - timedelta(hours=index),
                    text=f"第 {index} 次对话",
                )
            )
        await app.state.life_archive.record(
            _conversation_session_event(
                account_id=str(other["user_id"]),
                event_id="other-account-session",
                subject_id=str(other["user_id"]),
                session_id=other_session["session_id"],
                occurred_at=now,
                text="别人家的会话。",
            )
        )
        await app.state.life_archive.record(
            _conversation_session_event(
                account_id=str(owner["user_id"]),
                event_id="same-account-other-subject",
                subject_id=str(other["user_id"]),
                session_id="same-account-other-subject-session",
                occurred_at=now,
                text="同账号下其他主体的会话。",
            )
        )

        owner_result = await client.get(
            "/v1/archive/conversation-sessions",
            headers=owner_headers,
            params={"limit": 2},
        )
        owner_all = await client.get(
            "/v1/archive/conversation-sessions",
            headers=owner_headers,
        )
        other_result = await client.get(
            "/v1/archive/conversation-sessions",
            headers=other_headers,
        )
        too_many = await client.get(
            "/v1/archive/conversation-sessions",
            headers=owner_headers,
            params={"limit": 21},
        )
        zero = await client.get(
            "/v1/archive/conversation-sessions",
            headers=owner_headers,
            params={"limit": 0},
        )
        anonymous = await client.get("/v1/archive/conversation-sessions")

    assert owner_result.status_code == 200, owner_result.text
    assert [item["session_id"] for item in owner_result.json()["items"]] == [
        owner_sessions[0]["session_id"],
        owner_sessions[1]["session_id"],
    ]
    assert [item["preview"] for item in owner_result.json()["items"]] == [
        "第 0 次对话",
        "第 1 次对话",
    ]
    assert owner_all.status_code == 200, owner_all.text
    assert [item["session_id"] for item in owner_all.json()["items"]] == [
        session["session_id"] for session in owner_sessions
    ]
    assert "同账号下其他主体" not in owner_all.text
    assert other_result.status_code == 200, other_result.text
    assert [item["session_id"] for item in other_result.json()["items"]] == [
        other_session["session_id"]
    ]
    assert too_many.status_code == zero.status_code == 422
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_retention_ceiling_follows_the_speaking_subject_not_the_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P2-03: a child's evidence row stays metadata-only after consent.

    Before the subject column existed, the ingest decided retention from the
    ACCOUNT's category, so a minor speaking on an adult's family device was
    stored as retained adult content and surfaced by the review exits. Consent
    may retain metadata, but it must never retain verbatim turn text.
    """
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="subject-fence-owner")
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        now = datetime.now(UTC)
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
        from services.control_api.app.device_binding_token import mint_device_binding_token

        token = mint_device_binding_token(
            device_id="subject-fence-device",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="subject-fence-nonce",
        )
        bound = await client.post(
            "/v1/device-bindings",
            headers=owner_headers,
            json={
                "device_claim_token": token,
                "declared_mode": "family_shared",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "family_member_of",
                    "subject_draft": {"display_name": "小朋友", "age_band": "under_14"},
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "family_shared",
                    "shared_persona_enabled": True,
                },
                "consent_offer_ids": ["offer_family_space_v1"],
            },
        )
        assert bound.status_code == 201, bound.text
        child_id = bound.json()["primary_subject_ids"][0]
        await app.state.guardian_store.grant_person_consent(
            PersonConsentRecord(
                consent_id=str(uuid.uuid4()),
                subject_person_id=child_id,
                grantor_person_id="subject-fence-guardian",
                consent_kind="memory_retention",
                policy_version="subject-fence-minor-memory-v1",
                granted_at=datetime.now(UTC),
                evidence_event_id="subject-fence-minor-retention-grant",
            ),
            actor_person_id="subject-fence-guardian",
        )
        session = (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}

        async def append(
            event_id: str, text: str, subject_id: str, turn_id: int
        ) -> None:
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": event_id,
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "funasr.authoritative_final",
                    "turn_id": turn_id,
                    "generation_id": 1,
                    "tool_epoch": 0,
                    "active_subject_id": subject_id,
                    "runtime_profile_id": "profile-subject-fence",
                    "payload": {"text": text},
                },
            )
            assert response.status_code == 201, f"{event_id}: {response.text}"

        await append("subject-fence-child", "我今天考试没考好。", child_id, turn_id=1)
        await append("subject-fence-owner", "我今天有点累。", owner["user_id"], turn_id=2)

    child_event = await app.state.life_archive.event(
        account_id=owner["user_id"], event_id="subject-fence-child"
    )
    owner_event = await app.state.life_archive.event(
        account_id=owner["user_id"], event_id="subject-fence-owner"
    )
    assert child_event is not None and owner_event is not None
    assert child_event.subject_id == child_id
    assert child_event.payload.get("memory_retention") == "retained"
    assert not {
        "text",
        "transcript",
        "user_text",
        "assistant_text",
    }.intersection(child_event.payload)
    # 同一账户、同一会话：成人本人说话仍按账户保留，证明天花板随说话主体。
    assert owner_event.subject_id == owner["user_id"]
    assert owner_event.payload.get("history_eligible") is True
    assert owner_event.payload.get("text") == "我今天有点累。"


# -- GET /v1/archive/deletion-requests ----------------------------------------

_DELETION_STATUS_RESPONSE_KEYS = frozenset(
    {
        "request_id",
        "status",
        "phase",
        "retryable",
        "step",
        "started_at",
        "updated_at",
        "completed_at",
        "progress",
        "deleted_counts",
        "last_error",
    }
)


def _assert_deletion_status_is_an_allowlist(
    payload: dict[str, Any],
    raw_body: str,
    *,
    account_ids: tuple[str, ...],
) -> None:
    """The status body must be a closed field set without account identifiers."""

    _assert_deletion_status_drops_identifiers(payload)
    for account_id in account_ids:
        assert account_id not in raw_body
        assert hashlib.sha256(account_id.encode("utf-8")).hexdigest() not in raw_body
    assert "user_id" not in raw_body


def _assert_deletion_status_drops_identifiers(payload: dict[str, Any]) -> None:
    """The mapping is an allowlist: account keys never reach the response."""

    assert set(payload) == set(_DELETION_STATUS_RESPONSE_KEYS)
    assert "user_id" not in payload
    assert "user_id_hash" not in payload
    assert "account_id" not in payload


@pytest.mark.asyncio
async def test_archive_deletion_request_status_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get("/v1/archive/deletion-requests")
        malformed = await client.get(
            "/v1/archive/deletion-requests",
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"
    assert malformed.status_code == 401


@pytest.mark.asyncio
async def test_archive_deletion_request_status_reports_no_request_without_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "status-owner", "password": "safe-passphrase"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        first = await client.get("/v1/archive/deletion-requests", headers=owner_headers)
        second = await client.get("/v1/archive/deletion-requests", headers=owner_headers)

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert second.headers["Cache-Control"] == "no-store"
    body = first.json()
    assert body["status"] == "not_requested"
    assert body["phase"] == "not_requested"
    assert body["retryable"] is False
    assert body["request_id"] is None
    assert body["step"] is None
    assert (body["started_at"], body["updated_at"], body["completed_at"]) == (None, None, None)
    assert body["progress"] == {}
    assert body["deleted_counts"] == {}
    assert body["last_error"] is None
    assert first.json() == second.json()
    _assert_deletion_status_is_an_allowlist(
        body, first.text, account_ids=(owner["user_id"],)
    )


@pytest.mark.asyncio
async def test_archive_deletion_request_status_fails_closed_while_deleting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A caller with a live deletion row is fenced before the status body."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "status-deleting", "password": "safe-passphrase"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        started = app.state.memory_store.begin_account_deletion(
            user_id=owner["user_id"], started_at=now
        )
        app.state.memory_store.update_account_deletion(
            user_id=owner["user_id"],
            request_id=started["request_id"],
            step="archive_rows",
            updated_at=now,
            progress={"steps_done": 3},
            last_error="object store timeout",
        )
        response = await client.get("/v1/archive/deletion-requests", headers=owner_headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "access session is unavailable"
    stored = app.state.memory_store.get_account_deletion(user_id=owner["user_id"])
    assert stored is not None
    assert stored["status"] == "deleting"
    assert stored["last_error"] == "object store timeout"
    # The row the fenced caller cannot read is exactly what the body would drop.
    retryable = _deletion_status_response(stored)
    assert retryable["status"] == "deleting"
    assert retryable["phase"] == "in_progress"
    assert retryable["retryable"] is True
    assert retryable["last_error"] == "object store timeout"
    assert retryable["progress"] == {"steps_done": 3}
    assert retryable["step"] == "archive_rows"
    _assert_deletion_status_drops_identifiers(retryable)
    assert owner["user_id"] not in str(retryable)
    assert hashlib.sha256(owner["user_id"].encode("utf-8")).hexdigest() not in str(
        retryable
    )


@pytest.mark.asyncio
async def test_archive_deletion_request_status_reports_a_completed_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Completion is reported by the pipeline status, not by the retired token."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "status-completed", "password": "safe-passphrase"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        completed = await client.post(
            "/v1/archive/deletion-requests",
            headers=owner_headers,
            json={"password": "safe-passphrase", "confirmation": "永久删除我的全部数据"},
        )
        fenced = await client.get("/v1/archive/deletion-requests", headers=owner_headers)

    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert fenced.status_code == 401
    stored = app.state.memory_store.get_account_deletion(user_id=owner["user_id"])
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored["step"] == "completed"
    # The store reader itself never surfaces the account id or its hash.
    assert "user_id" not in stored
    assert "user_id_hash" not in stored
    assert stored["last_error"] is None
    assert isinstance(stored["completed_at"], str) and stored["completed_at"]
    body = _deletion_status_response(stored)
    assert body["status"] == "completed"
    assert body["phase"] == "completed"
    assert body["retryable"] is False
    assert body["request_id"] == stored["request_id"]
    assert body["completed_at"] == stored["completed_at"]
    assert body["deleted_counts"] == stored["deleted_counts"]
    assert body["progress"] == {}
    _assert_deletion_status_drops_identifiers(body)
    assert owner["user_id"] not in str(body)


def test_deletion_status_response_clamps_unknown_states_to_deleting() -> None:
    """Only 'deleting' and 'completed' are reachable; anything else stays retryable."""

    body = _deletion_status_response(
        {
            "request_id": "req-clamped",
            "status": "paused",
            "step": "paused",
            "progress": None,
            "deleted_counts": None,
            "last_error": None,
            "user_id": "status-owner",
            "user_id_hash": "0" * 64,
        }
    )

    assert body["status"] == "deleting"
    assert body["phase"] == "in_progress"
    assert body["retryable"] is True
    assert body["request_id"] == "req-clamped"
    assert body["progress"] == {}
    assert body["deleted_counts"] == {}
    assert body["last_error"] is None
    _assert_deletion_status_drops_identifiers(body)


@pytest.mark.asyncio
async def test_archive_deletion_request_status_ignores_another_accounts_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """?account_id= cannot be used to read somebody else's deletion request."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "status-borrower", "password": "safe-passphrase"},
            )
        ).json()
        other = (
            await client.post(
                "/v1/auth/register",
                json={"username": "status-target", "password": "other-passphrase"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        other_row = app.state.memory_store.begin_account_deletion(
            user_id=other["user_id"], started_at=now
        )
        borrowed = await client.get(
            "/v1/archive/deletion-requests",
            headers=owner_headers,
            params={"account_id": other["user_id"]},
        )
        own = await client.get("/v1/archive/deletion-requests", headers=owner_headers)

    assert borrowed.status_code == 200
    body = borrowed.json()
    assert body["status"] == "not_requested"
    assert body["request_id"] is None
    assert borrowed.json() == own.json()
    assert other_row["status"] == "deleting"
    assert body["request_id"] != other_row["request_id"]
    assert other["user_id"] not in borrowed.text
    assert other_row["request_id"] not in borrowed.text
    _assert_deletion_status_is_an_allowlist(
        body,
        borrowed.text,
        account_ids=(owner["user_id"], other["user_id"]),
    )


@pytest.mark.asyncio
async def test_operator_deletion_receipt_answers_after_the_owner_sessions_are_gone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P2-03: the receipt stays queryable once the owner can no longer sign in."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "receipt-owner", "password": "safe-passphrase"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        completed = await client.post(
            "/v1/archive/deletion-requests",
            headers=owner_headers,
            json={"password": "safe-passphrase", "confirmation": "永久删除我的全部数据"},
        )
        request_id = completed.json()["request_id"]
        fenced = await client.get("/v1/archive/deletion-requests", headers=owner_headers)
        unauthorised = await client.get(f"/v1/archive/deletion-receipts/{request_id}")
        unknown = await client.get(
            "/v1/archive/deletion-receipts/00000000-0000-4000-8000-000000000000",
            headers=internal,
        )
        receipt = await client.get(
            f"/v1/archive/deletion-receipts/{request_id}", headers=internal
        )
        completed_only = await client.get(
            "/v1/archive/deletion-receipts",
            headers=internal,
            params={"status": "completed"},
        )
        pending_only = await client.get(
            "/v1/archive/deletion-receipts",
            headers=internal,
            params={"status": "deleting"},
        )

    assert completed.status_code == 200
    assert fenced.status_code == 401
    assert unauthorised.status_code == 401
    assert unknown.status_code == 404
    assert receipt.status_code == 200
    body = receipt.json()
    assert body["request_id"] == request_id
    assert body["status"] == "completed"
    assert body["step"] == "completed"
    assert body["progress"] == {}
    assert isinstance(body["deleted_counts"], dict)
    assert body["last_error"] is None
    # A finalized receipt has no account reference left, and never a raw id.
    assert body["account_ref"] is None
    assert owner["user_id"] not in receipt.text
    assert (
        hashlib.sha256(owner["user_id"].encode("utf-8")).hexdigest()[:16]
        not in receipt.text
    )
    assert [item["request_id"] for item in completed_only.json()["items"]] == [request_id]
    assert completed_only.json()["count"] == 1
    assert pending_only.json() == {"items": [], "count": 0}


@pytest.mark.asyncio
async def test_operator_deletion_receipt_reports_a_pending_run_with_an_audit_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stuck saga stays diagnosable: status, step, progress and last error."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    now = datetime.now(UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "receipt-pending", "password": "safe-passphrase"},
            )
        ).json()
        row = app.state.memory_store.begin_account_deletion(
            user_id=owner["user_id"], started_at=now
        )
        app.state.memory_store.update_account_deletion(
            user_id=owner["user_id"],
            request_id=row["request_id"],
            step="archive.objects",
            updated_at=now,
            progress={"archive.objects": 3},
            last_error="RuntimeError",
        )
        pending = await client.get(
            "/v1/archive/deletion-receipts",
            headers=internal,
            params={"status": "deleting"},
        )
        receipt = await client.get(
            f"/v1/archive/deletion-receipts/{row['request_id']}", headers=internal
        )
        rejected = await client.get(
            "/v1/archive/deletion-receipts",
            headers=internal,
            params={"status": "paused"},
        )

    assert pending.json()["count"] == 1
    item = pending.json()["items"][0]
    assert item["status"] == "deleting"
    assert item["step"] == "archive.objects"
    assert item["progress"] == {"archive.objects": 3}
    assert item["last_error"] == "RuntimeError"
    assert item["account_ref"] == hashlib.sha256(
        owner["user_id"].encode("utf-8")
    ).hexdigest()[:16]
    assert receipt.json() == item
    assert owner["user_id"] not in receipt.text
    assert rejected.status_code == 422
