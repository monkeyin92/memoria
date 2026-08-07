from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import uuid
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    LifeArchivePort,
    RawVoiceRevocation,
    SpeakerClass,
)
from services.archive.object_store import ObjectRef
from services.archive.postgres_archive import PostgresLifeArchive
from services.common.companions import designed_voice_speaker_sha256
from services.control_api.app.main import create_app
from services.control_api.app.routes.archive import (
    ResponseProvenanceCreate,
    _canonical_actual_voice,
    _observe_persona,
)
from services.digital_self.domain import (
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    DigitalSelfVersion,
    RelationshipProfileManifestEntry,
)
from services.digital_self.response_planner import PLANNER_POLICY_VERSION
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessSnapshot,
    LegacyAuditTarget,
    LegacyFence,
    LegacyManifestItemRef,
    LegacyShellTurn,
)


class PersonaObservationStub:
    def __init__(self) -> None:
        self.observations = 0

    async def learning_allowed(self, *, account_id: str) -> bool:
        del account_id
        return True

    async def observe(self, evidence: object) -> None:
        del evidence
        self.observations += 1


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


class TrackingArchiveObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef:
        key = f"{account_id}/{purpose}/{uuid.uuid4()}.fernet"
        self.objects[key] = data
        return ObjectRef(
            account_id=account_id,
            object_key=key,
            media_type=media_type,
            byte_count=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            encryption_key_version="archive-test-v1",
            backend="test",
        )

    async def get(self, reference: ObjectRef) -> bytes:
        return self.objects[reference.object_key]

    async def delete(self, reference: ObjectRef) -> None:
        self.objects.pop(reference.object_key, None)


class FailOnceDeleteObjectStore(TrackingArchiveObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def delete(self, reference: ObjectRef) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("simulated object deletion failure")
        await super().delete(reference)


class CancellingBlobArchive:
    def __init__(self, delegate: object, started: asyncio.Event) -> None:
        self._delegate = delegate
        self._started = started

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def record_with_blob(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._started.set()
        await asyncio.Future()


class SnapshotPausingArchive:
    def __init__(self, delegate: LifeArchivePort) -> None:
        self._delegate = delegate
        self.snapshot_taken = asyncio.Event()
        self.continue_revocation = asyncio.Event()

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def revoke_raw_voice_consent(
        self,
        *,
        account_id: str,
        revoked_at: datetime,
    ) -> RawVoiceRevocation:
        revocation = await self._delegate.revoke_raw_voice_consent(
            account_id=account_id,
            revoked_at=revoked_at,
        )
        self.snapshot_taken.set()
        await self.continue_revocation.wait()
        return revocation


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


def _wav(pcm: bytes = b"\x00\x00" * 1600) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(pcm)
    return output.getvalue()


def _response_provenance(
    *,
    session_id: str,
    turn_id: int,
    generation_id: int,
    source_refs: list[dict[str, object]],
    **overrides: object,
) -> dict[str, object]:
    return {
        "fence": {
            "session_id": session_id,
            "turn_id": turn_id,
            "generation_id": generation_id,
            "tool_epoch": 0,
        },
        "planner_policy_version": PLANNER_POLICY_VERSION,
        "interaction_mode": "legacy",
        "mode_policy_version": "caller-forged",
        "digital_self_version_id": None,
        "manifest_sha256": None,
        "relationship_profile_id": None,
        "relationship_profile_version": None,
        "speaker_class": "guest",
        "speaker_reason_code": "caller-forged",
        "speaker_profile_id": "caller-forged",
        "speaker_model_version": "caller-forged",
        "speaker_template_version": 99,
        "source_refs": source_refs,
        "epistemic_status": "fact",
        "epistemic_reason_codes": ["caller-forged"],
        "disclosures": ["inference", "privacy_refusal"],
        "llm_provider": "qwen",
        "llm_model": "qwen-test",
        "tts_provider": "volcengine_doubao",
        "tts_model": "seed-tts-2.0",
        "actual_voice_profile_id": "warm_companion",
        "actual_voice_resource_id": "seed-tts-2.0",
        "actual_voice_speaker_sha256": designed_voice_speaker_sha256("warm_companion"),
        **overrides,
    }


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
    monkeypatch.setenv("MEMORIA_MEMORY_READ_TOKEN", "memory-read-capability")
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
        write_with_read_token = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "memory-read-capability"},
            json={**event, "event_id": "capability-event-denied"},
        )
        read = await client.post(
            "/v1/archive/session-context",
            headers={"X-Memoria-Internal-Token": "memory-read-capability"},
            json={
                "session_id": session["session_id"],
                "speaker_class": "owner",
                "topic": "",
            },
        )
        read_with_write_token = await client.post(
            "/v1/archive/session-context",
            headers={"X-Memoria-Internal-Token": "archive-write-capability"},
            json={
                "session_id": session["session_id"],
                "speaker_class": "owner",
                "topic": "",
            },
        )

    assert (written.status_code, read.status_code) == (201, 200)
    assert (write_with_read_token.status_code, read_with_write_token.status_code) == (
        401,
        401,
    )


@pytest.mark.asyncio
async def test_account_can_append_and_read_an_idempotent_evidence_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    occurred_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        event = {
            "event_id": "event-api-001",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at,
            "speaker_class": "owner",
            "source": "h5.authoritative_transcript",
            "payload": {"text": "我在杭州读过书。"},
        }
        internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        first = await client.post(
            "/v1/archive/session-events", headers=internal_headers, json=event
        )
        duplicate = await client.post(
            "/v1/archive/session-events", headers=internal_headers, json=event
        )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert [item["event_id"] for item in timeline.json()["items"]] == ["event-api-001"]


@pytest.mark.asyncio
async def test_session_prompt_kind_accepts_valid_internal_value_and_defaults_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        base = {
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "test",
        }
        await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **base,
                "event_id": "prompt-leading",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {"text": "是的", "prompt_kind": "leading"},
            },
        )
        await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **base,
                "event_id": "prompt-invalid",
                "turn_id": 2,
                "generation_id": 2,
                "payload": {"text": "另外", "prompt_kind": "client-strong"},
            },
        )
        timeline = await client.get("/v1/archive/timeline", headers=bearer)

    kinds = {item["event_id"]: item["payload"]["prompt_kind"] for item in timeline.json()["items"]}
    assert kinds == {"prompt-invalid": "spontaneous", "prompt-leading": "leading"}


@pytest.mark.asyncio
async def test_explicit_memory_intent_is_server_owned_and_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        base = {
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "source": "test",
        }
        cases = (
            ("explicit-owner", "owner", "请记住我喜欢雨天散步。", 1, 0),
            ("spoofed-ordinary", "owner", "今天天气不错。", 2, 0),
            ("explicit-guest", "guest", "请记住我喜欢咖啡。", 3, 0),
            ("quoted-owner", "owner", "朋友说请记住他喜欢咖啡。", 4, 0),
            ("question-owner", "owner", "请记住我喜欢咖啡吗？", 5, 0),
            ("missing-fence-owner", "owner", "请记住我喜欢咖啡。", 6, None),
        )
        for event_id, speaker_class, text, turn_id, tool_epoch in cases:
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    **base,
                    "event_id": event_id,
                    "turn_id": turn_id,
                    "generation_id": turn_id,
                    "tool_epoch": tool_epoch,
                    "speaker_class": speaker_class,
                    "payload": {
                        "text": text,
                        "memory_write_intent": {
                            "kind": "client_spoof",
                            "policy_version": "untrusted",
                        },
                    },
                },
            )
            assert response.status_code == 201
        archived = {
            event_id: await app.state.life_archive.event(
                account_id=identity["user_id"],
                event_id=event_id,
            )
            for event_id, *_ in cases
        }

    assert all(event is not None for event in archived.values())
    payloads = {
        event_id: dict(event.payload) for event_id, event in archived.items() if event is not None
    }
    assert payloads["explicit-owner"]["memory_write_intent"] == {
        "kind": "explicit_remember",
        "policy_version": "explicit-memory-v2",
    }
    assert "memory_write_intent" not in payloads["spoofed-ordinary"]
    assert "memory_write_intent" not in payloads["explicit-guest"]
    assert "memory_write_intent" not in payloads["quoted-owner"]
    assert "memory_write_intent" not in payloads["question-owner"]
    assert "memory_write_intent" not in payloads["missing-fence-owner"]


@pytest.mark.asyncio
async def test_generic_speech_event_requires_a_session_and_cannot_trigger_persona(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "generic-speech-persona", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        response = await client.post(
            "/v1/archive/events",
            headers=internal,
            json={
                "event_id": "generic-speech-persona",
                "account_id": identity["user_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "payload": {
                    "text": "我会先听完，再认真回答。",
                    "persona_eligible": True,
                    "speech_ms": 8000,
                    "pause_ratio": 0.55,
                    "quality_score": 0.95,
                },
            },
        )
        traits = await client.get("/v1/persona/traits?include_candidates=true", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_bound_event_required"
    assert traits.json() == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "owner.action_recorded",
        "learning.task_created",
        "learning.task_transitioned",
        "memory.claim_reviewed",
    ],
)
async def test_generic_archive_rejects_server_owned_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_type: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/archive/events",
            headers=internal,
            json={
                "event_id": f"forged-{event_type}",
                "account_id": "account-forged",
                "event_type": event_type,
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "forged",
                "payload": {},
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "server_owned_event_required",
        "event_type": event_type,
    }


@pytest.mark.asyncio
async def test_internal_archive_writer_cannot_append_after_deletion_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        app.state.memory_store.begin_account_deletion(
            user_id=identity["user_id"],
            started_at=datetime.now(UTC).isoformat(),
        )
        response = await client.post(
            "/v1/archive/events",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "event_id": "too-late-event",
                "account_id": identity["user_id"],
                "event_type": "account.deletion_fence_probe",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "system",
                "source": "test",
                "payload": {"reason": "不应写入"},
            },
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_late_persona_background_task_is_rejected_by_the_deletion_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    account_id = "persona-deleting-account"
    app.state.memory_store.begin_account_deletion(
        user_id=account_id,
        started_at=datetime.now(UTC).isoformat(),
    )
    engine = PersonaObservationStub()

    await _observe_persona(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        account_id=account_id,
        source_event_id="late-persona-evidence",
        speech_duration_ms=1000,
        pause_ratio=0.2,
        quality_score=0.9,
    )

    assert engine.observations == 0


@pytest.mark.asyncio
async def test_raw_voice_consent_archives_owner_audio_and_revocation_deletes_blobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    objects = TrackingArchiveObjectStore()
    app.state.archive_object_store = objects
    wav = _wav()
    changed_wav = _wav(b"\x01\x00" * 1600)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "raw-voice-owner", "password": "safe-password"},
            )
        ).json()
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
        event = {
            "event_id": "raw-audio-owner-event",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "consent_grant_id": grant["consent_grant_id"],
            "payload": {"text": "保存这一段主人声音。"},
        }
        transcript = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json=event,
        )
        transcript_payload = (await client.get("/v1/archive/timeline", headers=headers)).json()[
            "items"
        ][0]["payload"]
        body = {
            **event,
            "payload": transcript_payload,
            "audio_base64": base64.b64encode(wav).decode("ascii"),
            "media_type": "audio/wav",
            "retention_policy": "account_lifetime",
        }
        guest_audio = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={**body, "event_id": "guest-raw-audio", "speaker_class": "guest"},
        )
        invalid_audio = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={
                **body,
                "event_id": "invalid-raw-audio",
                "audio_base64": base64.b64encode(b"RIFF-not-a-wav").decode("ascii"),
            },
        )
        archived = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json=body,
        )
        duplicate = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json=body,
        )
        conflict = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={
                **body,
                "audio_base64": base64.b64encode(changed_wav).decode("ascii"),
            },
        )
        revoked = await client.delete("/v1/archive/raw-voice-consent", headers=headers)
        revoked_retry = await client.delete(
            "/v1/archive/raw-voice-consent",
            headers=headers,
        )
        after_revoke = await client.get("/v1/archive/raw-voice-consent", headers=headers)
        late = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={**body, "event_id": "late-raw-audio"},
        )

    assert empty.json() == {"consent": None}
    assert granted.status_code == 201
    assert session_grant.json() == {
        "allowed": True,
        "consent_grant_id": grant["consent_grant_id"],
        "policy_version": "raw-voice-archive-v1",
        "retention_policy": "account_lifetime",
    }
    assert transcript.status_code == 201
    assert guest_audio.status_code == 422
    assert invalid_audio.status_code == 422
    assert archived.status_code == 200
    assert archived.json()["blob_archived"] is True
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert conflict.status_code == 409
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert revoked_retry.status_code == 200
    assert revoked_retry.json() == revoked.json()
    assert after_revoke.json() == {"consent": None}
    assert late.status_code == 410
    assert objects.objects == {}


@pytest.mark.asyncio
async def test_raw_audio_waits_for_an_existing_canonical_owner_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    objects = TrackingArchiveObjectStore()
    app.state.archive_object_store = objects
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "missing-raw-parent", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        grant = (
            await client.post(
                "/v1/archive/raw-voice-consent",
                headers=headers,
                json={"policy_version": "raw-voice-archive-v1"},
            )
        ).json()
        raw_audio = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={
                "event_id": "missing-raw-parent",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": grant["consent_grant_id"],
                "payload": {"text": "不能由音频创建话轮。", "persona_eligible": True},
                "audio_base64": base64.b64encode(_wav()).decode("ascii"),
            },
        )
        traits = await client.get("/v1/persona/traits?include_candidates=true", headers=headers)

    assert raw_audio.status_code == 425
    assert raw_audio.json()["detail"] == {"code": "parent_turn_not_recorded"}
    assert objects.objects == {}
    assert traits.json() == {"items": []}


@pytest.mark.asyncio
async def test_raw_voice_revocation_cannot_orphan_a_concurrent_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    objects = TrackingArchiveObjectStore()
    app.state.archive_object_store = objects
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as setup:
        identity = (
            await setup.post(
                "/v1/auth/register",
                json={"username": "raw-voice-race", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        session = (await setup.post("/v1/sessions", headers=headers, json={})).json()
        grant = (
            await setup.post(
                "/v1/archive/raw-voice-consent",
                headers=headers,
                json={
                    "policy_version": "raw-voice-archive-v1",
                    "retention_policy": "account_lifetime",
                },
            )
        ).json()

    delegate = app.state.life_archive
    pausing_archive = SnapshotPausingArchive(delegate)
    app.state.life_archive = pausing_archive
    body = {
        "event_id": "raw-audio-revocation-race",
        "session_id": session["session_id"],
        "event_type": "speech.utterance_finalized",
        "occurred_at": datetime.now(UTC).isoformat(),
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "consent_grant_id": grant["consent_grant_id"],
        "payload": {"text": "撤销并发上传不能留下对象。"},
        "audio_base64": base64.b64encode(_wav()).decode("ascii"),
        "media_type": "audio/wav",
        "retention_policy": "account_lifetime",
    }
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as agent,
    ):
        revocation = asyncio.create_task(
            owner.delete("/v1/archive/raw-voice-consent", headers=headers)
        )
        await asyncio.wait_for(pausing_archive.snapshot_taken.wait(), timeout=1)
        try:
            upload = await agent.post(
                "/v1/archive/session-raw-audio",
                headers=internal,
                json=body,
            )
        finally:
            pausing_archive.continue_revocation.set()
        revoked = await asyncio.wait_for(revocation, timeout=1)

    assert revoked.status_code == 200
    assert upload.status_code == 410
    assert objects.objects == {}
    assert await delegate.raw_voice_blobs(account_id=identity["user_id"]) == ()


@pytest.mark.asyncio
async def test_raw_voice_revocation_keeps_manifest_until_object_deletion_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    objects = FailOnceDeleteObjectStore()
    app.state.archive_object_store = objects
    archive = app.state.life_archive
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "raw-voice-retry", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        grant = (
            await client.post(
                "/v1/archive/raw-voice-consent",
                headers=headers,
                json={
                    "policy_version": "raw-voice-archive-v1",
                    "retention_policy": "account_lifetime",
                },
            )
        ).json()
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "raw-audio-revocation-retry",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": grant["consent_grant_id"],
                "payload": {"text": "对象删除失败时保留清单供重试。"},
            },
        )
        uploaded = await client.post(
            "/v1/archive/session-raw-audio",
            headers=internal,
            json={
                "event_id": "raw-audio-revocation-retry",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": grant["consent_grant_id"],
                "payload": {"text": "对象删除失败时保留清单供重试。"},
                "audio_base64": base64.b64encode(_wav()).decode("ascii"),
                "media_type": "audio/wav",
                "retention_policy": "account_lifetime",
            },
        )
        failed = await client.delete("/v1/archive/raw-voice-consent", headers=headers)
        retained = await archive.raw_voice_blobs(account_id=identity["user_id"])
        objects_after_failed = dict(objects.objects)
        retried = await client.delete("/v1/archive/raw-voice-consent", headers=headers)

    assert parent.status_code == 201
    assert uploaded.status_code == 200
    assert failed.status_code == 503
    assert len(retained) == 1
    assert retained[0].object_key in objects_after_failed
    assert retried.status_code == 200
    assert objects.objects == {}
    assert await archive.raw_voice_blobs(account_id=identity["user_id"]) == ()


@pytest.mark.asyncio
async def test_raw_voice_upload_rejects_another_accounts_consent_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    objects = TrackingArchiveObjectStore()
    app.state.archive_object_store = objects
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = (
            await client.post(
                "/v1/auth/register",
                json={"username": "raw-voice-first", "password": "safe-password"},
            )
        ).json()
        second = (
            await client.post(
                "/v1/auth/register",
                json={"username": "raw-voice-second", "password": "safe-password"},
            )
        ).json()
        first_headers = {"Authorization": f"Bearer {first['access_token']}"}
        second_headers = {"Authorization": f"Bearer {second['access_token']}"}
        session = (await client.post("/v1/sessions", headers=first_headers, json={})).json()
        grant_body = {
            "policy_version": "raw-voice-archive-v1",
            "retention_policy": "account_lifetime",
        }
        await client.post(
            "/v1/archive/raw-voice-consent",
            headers=first_headers,
            json=grant_body,
        )
        second_grant = (
            await client.post(
                "/v1/archive/raw-voice-consent",
                headers=second_headers,
                json=grant_body,
            )
        ).json()

        response = await client.post(
            "/v1/archive/session-raw-audio",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "event_id": "cross-account-raw-audio",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": second_grant["consent_grant_id"],
                "payload": {"text": "不能借用另一账户的授权。"},
                "audio_base64": base64.b64encode(_wav()).decode("ascii"),
                "media_type": "audio/wav",
                "retention_policy": "account_lifetime",
            },
        )

    assert response.status_code == 403
    assert objects.objects == {}


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL HTTP raw voice contract",
)
async def test_postgres_control_api_raw_voice_contract_matches_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    app = create_app()
    app.state.life_archive = archive
    objects = TrackingArchiveObjectStore()
    app.state.archive_object_store = objects
    account_id: str | None = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            identity = (
                await client.post(
                    "/v1/auth/register",
                    json={
                        "username": "postgres-http-raw-owner",
                        "password": "safe-password",
                    },
                )
            ).json()
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
            event = {
                "event_id": "postgres-http-raw-event",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": grant["consent_grant_id"],
                "payload": {"text": "验证 PostgreSQL HTTP 原始语音合同。"},
            }
            transcript = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json=event,
            )
            raw_audio = await client.post(
                "/v1/archive/session-raw-audio",
                headers=internal,
                json={
                    **event,
                    "audio_base64": base64.b64encode(_wav()).decode("ascii"),
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
        assert transcript.status_code == 201
        assert raw_audio.status_code == 200
        assert raw_audio.json()["blob_archived"] is True
        assert revoked.status_code == 200
        assert await archive.raw_voice_blobs(account_id=account_id) == ()
        assert objects.objects == {}
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
async def test_cancelled_raw_audio_request_deletes_the_uncommitted_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    objects = TrackingArchiveObjectStore()
    app.state.archive_object_store = objects
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "cancel-raw-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        grant = (
            await client.post(
                "/v1/archive/raw-voice-consent",
                headers=headers,
                json={
                    "policy_version": "raw-voice-archive-v1",
                    "retention_policy": "account_lifetime",
                },
            )
        ).json()
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "cancelled-raw-audio",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "consent_grant_id": grant["consent_grant_id"],
                "payload": {"text": "请求取消仍需补偿对象。"},
            },
        )
        assert parent.status_code == 201
        started = asyncio.Event()
        app.state.life_archive = CancellingBlobArchive(app.state.life_archive, started)
        request_task = asyncio.create_task(
            client.post(
                "/v1/archive/session-raw-audio",
                headers=internal,
                json={
                    "event_id": "cancelled-raw-audio",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "funasr.authoritative_final",
                    "consent_grant_id": grant["consent_grant_id"],
                    "payload": {"text": "请求取消仍需补偿对象。"},
                    "audio_base64": base64.b64encode(_wav()).decode("ascii"),
                    "media_type": "audio/wav",
                    "retention_policy": "account_lifetime",
                },
            )
        )
        await started.wait()
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    assert objects.objects == {}


@pytest.mark.asyncio
async def test_guest_and_uncertain_evidence_is_recorded_but_hidden_from_owner_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        classified_session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        responses = []
        for speaker_class in ("guest", "uncertain"):
            responses.append(
                await client.post(
                    "/v1/archive/session-events",
                    headers=internal,
                    json={
                        "event_id": f"discard-{speaker_class}",
                        "session_id": session["session_id"],
                        "event_type": "speech.utterance_finalized",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "speaker_class": speaker_class,
                        "source": "funasr.authoritative_final",
                        "payload": {"text": "不得进入主人长期档案。"},
                    },
                )
            )
            responses.append(
                await client.post(
                    "/v1/archive/session-events",
                    headers=internal,
                    json={
                        "event_id": f"discard-direct-{speaker_class}",
                        "session_id": classified_session["session_id"],
                        "event_type": "speaker.classified",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "speaker_class": speaker_class,
                        "source": "speaker.authority",
                        "payload": {"classification": speaker_class},
                    },
                )
            )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert [response.status_code for response in responses] == [201, 201, 201, 201]
    assert {response.json()["event_id"] for response in responses} == {
        "discard-guest",
        "discard-direct-guest",
        "discard-uncertain",
        "discard-direct-uncertain",
    }
    speaker_classes: tuple[SpeakerClass, ...] = ("guest", "uncertain")
    for speaker_class in speaker_classes:
        isolated = await app.state.life_archive.context(
            ContextQuery(
                account_id=identity["user_id"],
                session_id=session["session_id"],
                speaker_class=speaker_class,
            )
        )
        assert [item.event_id for item in isolated.evidence] == [f"discard-{speaker_class}"]
    assert timeline.json() == {"items": []}


@pytest.mark.asyncio
async def test_agent_records_session_event_without_trusting_an_account_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-parent-001",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "turn_id": 1,
                "generation_id": 2,
                "payload": {"text": "先保存这句用户话轮。"},
            },
        )
        recorded = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-001",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "你实际听到了这一句。",
                    "actual_heard": True,
                    "history_eligible": False,
                    "owner_projection_eligible": False,
                    "response_provenance": {
                        "fence": {
                            "session_id": session["session_id"],
                            "turn_id": 1,
                            "generation_id": 2,
                            "tool_epoch": 0,
                        },
                        "planner_policy_version": PLANNER_POLICY_VERSION,
                        "interaction_mode": "legacy",
                        "mode_policy_version": "caller-forged",
                        "digital_self_version_id": None,
                        "manifest_sha256": None,
                        "relationship_profile_id": None,
                        "relationship_profile_version": None,
                        "speaker_class": "guest",
                        "speaker_reason_code": "caller-forged",
                        "speaker_profile_id": "caller-forged",
                        "speaker_model_version": "caller-forged",
                        "speaker_template_version": 99,
                        "source_refs": [
                            {
                                "kind": "persona_trait",
                                "item_id": "trait-1",
                                "source_event_ids": ["session-event-parent-001"],
                            }
                        ],
                        "epistemic_status": "fact",
                        "epistemic_reason_codes": ["caller-forged"],
                        "disclosures": ["inference", "privacy_refusal"],
                        "llm_provider": "qwen",
                        "llm_model": "qwen-test",
                        "tts_provider": "volcengine_doubao",
                        "tts_model": "seed-tts-2.0",
                        "actual_voice_profile_id": "warm_companion",
                        "actual_voice_resource_id": "seed-tts-2.0",
                        "actual_voice_speaker_sha256": designed_voice_speaker_sha256(
                            "warm_companion"
                        ),
                    },
                },
            },
        )
        mismatched_fence = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-fence-mismatch",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条回答携带了错误的完整 fence。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=99,
                        source_refs=[
                            {
                                "kind": "persona_trait",
                                "item_id": "trait-1",
                                "source_event_ids": ["session-event-parent-001"],
                            }
                        ],
                    ),
                },
            },
        )
        forged_planner = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-planner-mismatch",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条回答携带了未知规划器版本。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=2,
                        source_refs=[],
                        planner_policy_version="forged-planner",
                    ),
                },
            },
        )
        mismatched_voice = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-voice-mismatch",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条回答伪造了实际使用的音色。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=2,
                        source_refs=[],
                        actual_voice_profile_id="forged-profile",
                    ),
                },
            },
        )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert (parent.status_code, recorded.status_code) == (201, 201)
    assert mismatched_fence.status_code == 409
    assert mismatched_fence.json()["detail"] == {"code": "response_provenance_fence_mismatch"}
    assert forged_planner.status_code == 409
    assert forged_planner.json()["detail"] == {"code": "response_provenance_planner_invalid"}
    assert mismatched_voice.status_code == 409
    assert mismatched_voice.json()["detail"] == {"code": "response_provenance_voice_mismatch"}
    assistant_payload = timeline.json()["items"][0]["payload"]
    assert assistant_payload["actual_heard"] is True
    assert assistant_payload["history_eligible"] is True
    assert assistant_payload["owner_projection_eligible"] is True
    provenance = assistant_payload["response_provenance"]
    assert provenance["interaction_mode"] == "companion"
    assert provenance["mode_policy_version"] == "s2-v1"
    assert provenance["speaker_class"] == "owner"
    assert provenance["speaker_reason_code"] == "unavailable"
    assert provenance["speaker_model_version"] == "unavailable"
    assert provenance["speaker_profile_id"] is None
    assert provenance["speaker_template_version"] is None
    assert provenance["source_refs"][0]["source_event_ids"] == ["session-event-parent-001"]
    assert provenance["epistemic_status"] == "not_applicable"
    assert provenance["epistemic_reason_codes"] == ["no_grounded_items"]
    assert provenance["disclosures"] == []
    assert provenance["tts_provider"] == "volcengine_doubao"
    assert provenance["tts_model"] == "seed-tts-2.0"
    assert provenance["actual_voice_profile_id"] == "warm_companion"
    assert provenance["actual_voice_resource_id"] == "seed-tts-2.0"
    assert provenance["actual_voice_speaker_sha256"] == designed_voice_speaker_sha256(
        "warm_companion"
    )
    assert "score" not in provenance


@pytest.mark.asyncio
async def test_response_provenance_rejects_guest_or_assistant_source_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        guest_source = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "guest-source-forged-provenance",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "guest",
                "source": "funasr.authoritative_final",
                "turn_id": 8,
                "generation_id": 8,
                "payload": {"text": "访客提供的内容不能成为主人回答来源。"},
            },
        )
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "owner-parent-forged-provenance",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "turn_id": 9,
                "generation_id": 9,
                "payload": {"text": "主人当前问题。"},
            },
        )
        rejected = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "assistant-forged-provenance",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 9,
                "generation_id": 9,
                "tool_epoch": 0,
                "payload": {
                    "text": "不能归档为有主人来源的回答。",
                    "actual_heard": True,
                    "response_provenance": {
                        "fence": {
                            "session_id": session["session_id"],
                            "turn_id": 9,
                            "generation_id": 9,
                            "tool_epoch": 0,
                        },
                        "planner_policy_version": PLANNER_POLICY_VERSION,
                        "interaction_mode": "companion",
                        "mode_policy_version": "s2-v1",
                        "digital_self_version_id": None,
                        "manifest_sha256": None,
                        "relationship_profile_id": None,
                        "relationship_profile_version": None,
                        "speaker_class": "owner",
                        "speaker_reason_code": "owner_match",
                        "speaker_profile_id": "speaker-profile",
                        "speaker_model_version": "campplus-test",
                        "speaker_template_version": 1,
                        "source_refs": [
                            {
                                "kind": "memory_claim",
                                "item_id": "forged-claim",
                                "source_event_ids": ["guest-source-forged-provenance"],
                            }
                        ],
                        "epistemic_status": "fact",
                        "epistemic_reason_codes": ["grounded_manifest"],
                        "disclosures": [],
                        "llm_provider": "qwen",
                        "llm_model": "qwen-test",
                        "tts_provider": "volcengine_doubao",
                        "tts_model": "seed-tts-2.0",
                        "actual_voice_profile_id": "warm_companion",
                        "actual_voice_resource_id": "seed-tts-2.0",
                        "actual_voice_speaker_sha256": designed_voice_speaker_sha256(
                            "warm_companion"
                        ),
                    },
                },
            },
        )

    assert (guest_source.status_code, parent.status_code) == (201, 201)
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {"code": "response_provenance_source_invalid"}


@pytest.mark.asyncio
async def test_response_provenance_requires_a_nonempty_source_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "empty-source-parent",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {"text": "主人问题。"},
            },
        )
        rejected = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "empty-source-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条来源为空。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=1,
                        source_refs=[
                            {
                                "kind": "memory_claim",
                                "item_id": "empty-source-claim",
                                "source_event_ids": [],
                            }
                        ],
                    ),
                },
            },
        )

    assert parent.status_code == 201
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "response_provenance_invalid"


@pytest.mark.asyncio
async def test_response_provenance_requires_projection_eligibility_and_allows_owner_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="source-without-projection",
                account_id=identity["user_id"],
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="test",
                payload={"text": "缺少投影资格。"},
            )
        )
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="owner-action-source",
                account_id=identity["user_id"],
                event_type="owner.action_recorded",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="user.growth_feedback",
                payload={"owner_projection_eligible": True},
            )
        )
        parent = {
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "payload": {"text": "主人问题。"},
        }
        first_parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **parent,
                "event_id": "missing-projection-parent",
                "turn_id": 1,
                "generation_id": 1,
            },
        )
        missing_projection = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "missing-projection-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {
                    "text": "不应采用缺少资格的来源。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=1,
                        source_refs=[
                            {
                                "kind": "memory_claim",
                                "item_id": "missing-projection-claim",
                                "source_event_ids": ["source-without-projection"],
                            }
                        ],
                    ),
                },
            },
        )
        second_parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**parent, "event_id": "owner-action-parent", "turn_id": 2, "generation_id": 2},
        )
        owner_action = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "owner-action-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "允许已验证的主人行动来源。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=2,
                        generation_id=2,
                        source_refs=[
                            {
                                "kind": "memory_claim",
                                "item_id": "owner-action-claim",
                                "source_event_ids": ["owner-action-source"],
                            }
                        ],
                    ),
                },
            },
        )

    assert (first_parent.status_code, second_parent.status_code) == (201, 201)
    assert missing_projection.json()["detail"] == {"code": "response_provenance_source_invalid"}
    assert owner_action.status_code == 201


@pytest.mark.asyncio
async def test_shadow_owner_candidate_archives_style_only_persona_snapshot_without_source_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        shadow_parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "shadow-style-parent",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "uncertain",
                "source": "funasr.authoritative_final",
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "可能是主人。",
                    "speaker_reason_code": "shadow_owner_candidate",
                },
            },
        )
        response = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "shadow-style-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "只采用低敏风格。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=2,
                        generation_id=2,
                        source_refs=[],
                        persona_version_id="persona-version-1",
                        persona_version_number=1,
                        persona_style_only=True,
                    ),
                },
            },
        )

    assert (shadow_parent.status_code, response.status_code) == (201, 201)
    provenance = (
        await app.state.life_archive.event(
            account_id=identity["user_id"], event_id="shadow-style-response"
        )
    ).payload["response_provenance"]
    assert provenance["epistemic_status"] == "not_applicable"
    assert provenance["disclosures"] == []
    assert provenance["source_refs"] == []
    assert provenance["persona_version_id"] == "persona-version-1"
    assert provenance["persona_version_number"] == 1
    assert provenance["persona_style_only"] is True


@pytest.mark.asyncio
async def test_assistant_evidence_waits_for_its_canonical_parent_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        response = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "missing-parent-assistant",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 7,
                "generation_id": 3,
                "payload": {"text": "迟到的回复。", "actual_heard": True},
            },
        )

    assert response.status_code == 425
    assert response.json()["detail"] == {"code": "parent_turn_not_recorded"}


@pytest.mark.parametrize(
    (
        "speaker_class",
        "reason_code",
        "expected_history",
        "expected_owner_projection",
        "expected_private_authority",
        "expected_low_sensitivity",
    ),
    (
        pytest.param("owner", None, True, True, True, False, id="owner"),
        pytest.param(
            "guest",
            "shadow_owner_candidate",
            False,
            False,
            False,
            False,
            id="guest-forged-shadow-reason",
        ),
        pytest.param(
            "uncertain",
            "shadow_owner_candidate",
            False,
            False,
            False,
            True,
            id="shadow-owner-candidate",
        ),
        pytest.param(
            "uncertain",
            "shadow_ambiguous_candidate",
            False,
            False,
            False,
            False,
            id="ambiguous",
        ),
    ),
)
@pytest.mark.asyncio
async def test_session_events_canonicalize_the_user_and_assistant_permission_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    speaker_class: SpeakerClass,
    reason_code: str | None,
    expected_history: bool,
    expected_owner_projection: bool,
    expected_private_authority: bool,
    expected_low_sensitivity: bool,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    user_event_id = f"interaction-user-{speaker_class}-{reason_code or 'formal'}"
    assistant_event_id = f"interaction-assistant-{speaker_class}-{reason_code or 'formal'}"
    forged_interaction = {
        "interaction_mode": "legacy",
        "mode_policy_version": "caller-forged",
        "simulated_output": True,
        "history_eligible": not expected_history,
        "owner_projection_eligible": not expected_owner_projection,
        "capabilities": {"private_memory": True, "persona": True, "tools": True},
        "caller_forged": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        user_event = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": user_event_id,
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": speaker_class,
                "source": "test",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {
                    "text": user_event_id,
                    "speaker_reason_code": reason_code,
                    "interaction_mode": "legacy",
                    "mode_policy_version": "caller-forged",
                    "simulated_output": True,
                    "history_eligible": not expected_history,
                    "owner_projection_eligible": not expected_owner_projection,
                    "interaction": forged_interaction,
                },
            },
        )
        assistant_event = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": assistant_event_id,
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {
                    "text": assistant_event_id,
                    "actual_heard": True,
                    "interaction_mode": "legacy",
                    "mode_policy_version": "caller-forged",
                    "simulated_output": True,
                    "history_eligible": expected_history,
                    "owner_projection_eligible": expected_owner_projection,
                    "interaction": forged_interaction,
                },
            },
        )

    assert (user_event.status_code, assistant_event.status_code) == (201, 201)
    recorded = await app.state.life_archive.context(
        ContextQuery(
            account_id=identity["user_id"],
            session_id=session["session_id"],
            speaker_class=speaker_class,
        )
    )
    events = {event.event_id: event for event in recorded.evidence}
    assert {user_event_id, assistant_event_id}.issubset(events)

    for event_id in (user_event_id, assistant_event_id):
        payload = events[event_id].payload
        interaction = payload["interaction"]
        assert payload["interaction_mode"] == interaction["interaction_mode"] == "companion"
        assert payload["mode_policy_version"] == interaction["mode_policy_version"] == "s2-v1"
        assert payload["simulated_output"] is interaction["simulated_output"] is False
        assert payload["history_eligible"] is expected_history
        assert interaction["history_eligible"] is expected_history
        assert payload["owner_projection_eligible"] is expected_owner_projection
        assert interaction["owner_projection_eligible"] is expected_owner_projection
        assert "caller_forged" not in interaction

    user_capabilities = events[user_event_id].payload["interaction"]["capabilities"]
    assert user_capabilities["private_memory"] is expected_private_authority
    assert user_capabilities["persona"] is expected_private_authority
    assert user_capabilities["tools"] is expected_private_authority
    assert user_capabilities["persona_low_sensitivity"] is expected_low_sensitivity
    assert user_capabilities["history"] is expected_history
    assert user_capabilities["learning"] is expected_history
    assistant_capabilities = events[assistant_event_id].payload["interaction"]["capabilities"]
    assert assistant_capabilities["private_memory"] is False
    assert assistant_capabilities["persona"] is False
    assert assistant_capabilities["persona_low_sensitivity"] is False
    assert assistant_capabilities["tools"] is False


@pytest.mark.asyncio
async def test_session_event_retry_ignores_delivery_timestamp_but_rejects_semantic_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    occurred_at = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (await client.post("/v1/auth/anonymous")).json()
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
        other_session = (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
        other_owner = (await client.post("/v1/auth/anonymous")).json()
        other_owner_headers = {"Authorization": f"Bearer {other_owner['access_token']}"}
        other_owner_session = (
            await client.post("/v1/sessions", headers=other_owner_headers, json={})
        ).json()
        event = {
            "event_id": "session-event-retry-001",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at.isoformat(),
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "turn_id": 1,
            "generation_id": 0,
            "payload": {"text": "这是一段稳定的权威转写。"},
        }
        first = await client.post("/v1/archive/session-events", headers=internal, json=event)
        retry = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "occurred_at": (occurred_at.replace(minute=1)).isoformat()},
        )
        changed_payload = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "payload": {"text": "内容已被替换。"}},
        )
        changed_session = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "session_id": other_session["session_id"]},
        )
        changed_owner = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "session_id": other_owner_session["session_id"]},
        )

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert changed_payload.status_code == 409
    assert changed_session.status_code == 409
    assert changed_owner.status_code == 409


@pytest.mark.asyncio
async def test_account_can_search_review_and_trace_compiled_life_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
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
                    "payload": {"text": text},
                    "turn_id": index + 1,
                },
            )
            assert response.status_code == 201
        await app.state.memory_catalog.compile_pending()

        search = await client.get("/v1/archive/search?q=家训", headers=headers)
        queue = await client.get("/v1/archive/review-queue", headers=headers)
        people = await client.get("/v1/archive/people", headers=headers)
        life_timeline = await client.get("/v1/archive/life-timeline", headers=headers)
        claim = next(item for item in queue.json()["items"] if item["kind"] == "claim")
        reviewed = await client.post(
            f"/v1/archive/memories/{claim['item_id']}/review",
            headers=headers,
            json={"action": "confirm"},
        )

    assert search.status_code == 200
    assert search.json()["items"][0]["source_event_id"] == "memory-api-0"
    assert reviewed.json()["status"] == "confirmed"
    assert people.json()["items"][0]["display_name"] == "李梅"
    assert {item["source_event_id"] for item in life_timeline.json()["items"]} == {
        "memory-api-0",
        "memory-api-1",
    }


@pytest.mark.asyncio
async def test_archive_memory_routes_never_accept_an_account_id_from_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.post("/v1/auth/anonymous")).json()
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
        identity = (await client.post("/v1/auth/anonymous")).json()
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
async def test_agent_gets_only_confirmed_owner_memory_from_the_session_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.post("/v1/auth/anonymous")).json()
        second = (await client.post("/v1/auth/anonymous")).json()
        first_headers = {"Authorization": f"Bearer {first['access_token']}"}
        second_headers = {"Authorization": f"Bearer {second['access_token']}"}
        first_session = (
            await client.post(
                "/v1/sessions",
                headers=first_headers,
                json={"user_id": first["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        second_session = (
            await client.post(
                "/v1/sessions",
                headers=second_headers,
                json={"user_id": second["user_id"], "voice_backend": "cascade"},
            )
        ).json()

        events = (
            (
                "first-confirmed",
                first_session["session_id"],
                "我们家的家训是答应别人的事一定做到。",
            ),
            ("first-candidate", first_session["session_id"], "我在杭州读过书。"),
            ("second-confirmed", second_session["session_id"], "我们家的家训是每天早睡。"),
        )
        for index, (event_id, session_id, text) in enumerate(events):
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal_headers,
                json={
                    "event_id": event_id,
                    "session_id": session_id,
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 10, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "payload": {"text": text},
                },
            )
            assert response.status_code == 201
        await app.state.memory_catalog.compile_pending()

        for headers, source_event_id in (
            (first_headers, "first-confirmed"),
            (second_headers, "second-confirmed"),
        ):
            queue = await client.get("/v1/archive/review-queue", headers=headers)
            claim = next(
                item for item in queue.json()["items"] if item["source_event_id"] == source_event_id
            )
            reviewed = await client.post(
                f"/v1/archive/memories/{claim['item_id']}/review",
                headers=headers,
                json={"action": "confirm"},
            )
            assert reviewed.status_code == 200

        owner = await client.post(
            "/v1/archive/session-context",
            headers=internal_headers,
            json={
                "session_id": first_session["session_id"],
                "speaker_class": "owner",
                "topic": "",
                "limit": 10,
            },
        )
        guest = await client.post(
            "/v1/archive/session-context",
            headers=internal_headers,
            json={
                "session_id": first_session["session_id"],
                "speaker_class": "guest",
                "topic": "",
                "limit": 10,
            },
        )
        injected_account = await client.post(
            "/v1/archive/session-context",
            headers=internal_headers,
            json={
                "session_id": first_session["session_id"],
                "speaker_class": "owner",
                "topic": "",
                "limit": 10,
                "account_id": second["user_id"],
            },
        )

    assert owner.status_code == 200
    assert {
        (item["kind"], item["source_event_id"], item["status"]) for item in owner.json()["items"]
    } == {
        ("claim", "first-confirmed", "confirmed"),
        ("knowledge", "first-confirmed", "confirmed"),
        ("episode", "first-confirmed", "confirmed"),
    }
    assert guest.json() == {"items": []}
    assert injected_account.status_code == 422


@pytest.mark.asyncio
async def test_owner_acoustic_metrics_reach_persona_through_an_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "persona-metrics-owner", "password": "safe-password"},
            )
        ).json()
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


@pytest.mark.asyncio
async def test_registered_account_exports_only_its_portable_archive(
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
                json={"username": "archive-owner", "password": "safe-passphrase"},
            )
        ).json()
        other = (
            await client.post(
                "/v1/auth/register",
                json={"username": "archive-other", "password": "other-passphrase"},
            )
        ).json()
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
    assert body["format_version"] == 1
    assert body["account_id"] == owner["user_id"]
    assert body["manifest_sha256"]
    serialized = exported.text
    assert "请记住我喜欢雨天散步。" in serialized
    assert "另一账户的私密内容。" not in serialized
    assert "password_hash" not in serialized
    assert "template_ciphertext" not in serialized
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
        deleted_session = await client.post(
            "/v1/archive/session-context",
            headers=internal_headers,
            json={
                "session_id": session["session_id"],
                "speaker_class": "owner",
                "topic": "",
                "limit": 8,
            },
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
    assert deleted_session.status_code == 410
    assert deleted_session_event.status_code == 410
    assert other_still_exists.status_code == 200
    assert remaining_growth_events.evidence == ()


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
