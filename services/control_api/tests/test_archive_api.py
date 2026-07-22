from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import ContextQuery, LifeArchivePort, RawVoiceRevocation
from services.archive.object_store import ObjectRef
from services.archive.postgres_archive import PostgresLifeArchive
from services.control_api.app.main import create_app
from services.control_api.app.routes.archive import _observe_persona


class PersonaObservationStub:
    def __init__(self) -> None:
        self.observations = 0

    async def learning_allowed(self, *, account_id: str) -> bool:
        del account_id
        return True

    async def observe(self, evidence: object) -> None:
        del evidence
        self.observations += 1


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
        event = {
            "event_id": "event-api-001",
            "account_id": identity["user_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at,
            "speaker_class": "owner",
            "source": "h5.authoritative_transcript",
            "payload": {"text": "我在杭州读过书。"},
        }
        internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        first = await client.post("/v1/archive/events", headers=internal_headers, json=event)
        duplicate = await client.post("/v1/archive/events", headers=internal_headers, json=event)
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert [item["event_id"] for item in timeline.json()["items"]] == ["event-api-001"]


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
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {"text": "不应写入"},
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
        body = {
            **event,
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

    assert uploaded.status_code == 201
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
                    "/v1/archive/events",
                    headers=internal,
                    json={
                        "event_id": f"discard-direct-{speaker_class}",
                        "account_id": identity["user_id"],
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
    for speaker_class in ("guest", "uncertain"):
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
                "payload": {"text": "你实际听到了这一句。", "actual_heard": True},
            },
        )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert recorded.status_code == 201
    assert timeline.json()["items"][0]["payload"]["actual_heard"] is True


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
        other_session = (
            await client.post("/v1/sessions", headers=owner_headers, json={})
        ).json()
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
        for index, text in enumerate(
            (
                "我们家的家训是答应别人的事一定做到。",
                "我妈妈叫李梅，今年60岁。",
            )
        ):
            response = await client.post(
                "/v1/archive/events",
                headers=internal_headers,
                json={
                    "event_id": f"memory-api-{index}",
                    "account_id": identity["user_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 9, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "payload": {"text": text},
                    "session_id": "memory-api-session",
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

        events = (
            ("first-confirmed", first["user_id"], "我们家的家训是答应别人的事一定做到。"),
            ("first-candidate", first["user_id"], "我在杭州读过书。"),
            ("second-confirmed", second["user_id"], "我们家的家训是每天早睡。"),
        )
        for index, (event_id, account_id, text) in enumerate(events):
            response = await client.post(
                "/v1/archive/events",
                headers=internal_headers,
                json={
                    "event_id": event_id,
                    "account_id": account_id,
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
        (item["kind"], item["source_event_id"], item["status"])
        for item in owner.json()["items"]
    } == {
        ("claim", "first-confirmed", "confirmed"),
        ("knowledge", "first-confirmed", "confirmed"),
        ("timeline", "first-confirmed", "confirmed"),
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
        for identity, headers, text in (
            (owner, owner_headers, "请记住我喜欢雨天散步。"),
            (other, other_headers, "另一账户的私密内容。"),
        ):
            saved = await client.post(
                "/v1/memory/messages",
                headers=headers,
                json={"user_id": identity["user_id"], "role": "user", "text": text},
            )
            assert saved.status_code == 201
            recorded = await client.post(
                "/v1/archive/events",
                headers=internal_headers,
                json={
                    "event_id": f"export-{identity['user_id']}",
                    "account_id": identity["user_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "payload": {"text": text},
                },
            )
            assert recorded.status_code == 201

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
            "/v1/archive/events",
            headers=internal_headers,
            json={
                "event_id": "delete-owner-evidence",
                "account_id": owner["user_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {"text": "这条内容应被删除。"},
            },
        )
        assert recorded.status_code == 201

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
        replacement_headers = {
            "Authorization": f"Bearer {replacement_body['access_token']}"
        }
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
