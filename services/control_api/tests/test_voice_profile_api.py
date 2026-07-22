from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from services.archive.object_store import EncryptedLocalObjectStore
from services.control_api.app.main import create_app
from services.voice_profile.domain import ProviderVoice, VoiceResolution
from services.voice_profile.manager import VoiceProfileManager
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from services.voice_profile.sample_url import VoiceSampleURLSigner


class ProviderStub:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.delete_fails = False

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        assert sample_url.startswith("https://control.test/v1/voices/provider-samples/")
        return ProviderVoice(
            voice_id=f"{target_model}-clone-{prefix}",
            target_model=target_model,
        )

    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)
        if self.delete_fails:
            raise RuntimeError("provider unavailable")


class PreviewStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def render(
        self,
        *,
        text: str,
        model: str | None,
        voice_id: str | None,
    ) -> bytes:
        self.requests.append({"text": text, "model": model, "voice_id": voice_id})
        return b"RIFF-preview"


class LegacyResolutionStub:
    async def resolve(self, *, account_id: str) -> VoiceResolution:
        assert account_id
        return VoiceResolution(
            mode="active",
            profile_id="legacy-cosyvoice-profile",
            model="cosyvoice-v3.5-flash",
            voice_id="cosyvoice-v3.5-flash-clone-owner001",
        )


class ActivationStub:
    def __init__(self) -> None:
        self.called = False

    async def activate(self, *, account_id: str, profile_id: str) -> None:
        self.called = True
        raise AssertionError(f"unexpected activation: {account_id=} {profile_id=}")


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("TTS_PROVIDER", "cosyvoice")


def test_control_api_selects_postgres_voice_profiles_with_archive_dsn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_ARCHIVE_DATABASE_URL",
        "postgresql://postgres:test@postgres.test/memoria",
    )

    app = create_app()

    assert isinstance(app.state.voice_profile_manager, PostgresVoiceProfileManager)


@pytest.mark.asyncio
async def test_legacy_active_clone_resolves_to_selected_doubao_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TTS_PROVIDER", "doubao")
    app = create_app()
    app.state.voice_profile_manager = LegacyResolutionStub()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "legacy-voice-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        selected = await client.put(
            f"/v1/memory/profile/{identity['user_id']}",
            headers=headers,
            json={"companion_id": "taoxi"},
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        resolved = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": session["session_id"]},
        )

    assert selected.status_code == 200
    assert resolved.json() == {
        "mode": "designed",
        "profile_id": "bright_peer",
        "model": "seed-tts-2.0",
        "voice_id": None,
    }


@pytest.mark.asyncio
async def test_doubao_runtime_rejects_new_cosyvoice_clone_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TTS_PROVIDER", "doubao")
    app = create_app()
    manager = ActivationStub()
    app.state.voice_profile_manager = manager

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "doubao-voice-owner", "password": "safe-password"},
            )
        ).json()
        response = await client.post(
            "/v1/voices/profiles/legacy-cosyvoice-profile/activate",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "当前豆包语音链路不支持激活历史 CosyVoice 克隆音色"
    assert not manager.called


@pytest.mark.asyncio
async def test_voice_clone_consent_candidate_evaluation_activation_and_revoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    provider = ProviderStub()
    preview_renderer = PreviewStub()
    signer = VoiceSampleURLSigner(
        secret="voice-sample-signing-secret-long-enough",
        public_base_url="https://control.test",
        ttl_s=300,
    )
    app.state.voice_sample_signer = signer
    app.state.voice_preview_renderer = preview_renderer
    app.state.voice_profile_manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        ),
        provider=provider,
        sample_url_factory=signer.url,
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "voice-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        consent = await client.post(
            "/v1/voices/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "voice-clone-v1"},
        )
        sample_audio = b"RIFF" + b"\x01\x02" * 16_000
        enrolled = await client.post(
            "/v1/voices/enrollments",
            headers=headers,
            json={
                "audio_base64": base64.b64encode(sample_audio).decode("ascii"),
                "media_type": "audio/wav",
                "duration_ms": 12_000,
                "sample_rate": 24_000,
            },
        )
        profile = enrolled.json()
        listing = await client.get("/v1/voices/profiles", headers=headers)
        sample_url = app.state.voice_sample_signer.url(profile["sample_id"])
        sample_path = sample_url.removeprefix("https://control.test")
        provider_sample = await client.get(sample_path)
        trial = await client.post(
            f"/v1/voices/profiles/{profile['profile_id']}/blind-trials",
            headers=headers,
        )
        previews = [
            await client.post(
                f"/v1/voices/blind-trials/{trial.json()['trial_id']}/preview",
                headers=headers,
                json={"slot": slot, "text": "今天也想听你讲讲。"},
            )
            for slot in ("A", "B")
        ]
        candidate_slot = "A" if preview_renderer.requests[0]["model"] else "B"
        evaluated = await client.post(
            f"/v1/voices/profiles/{profile['profile_id']}/evaluations",
            headers=headers,
            json={
                "trial_id": trial.json()["trial_id"],
                "preferred_slot": candidate_slot,
                "similarity": 4.2,
                "naturalness": 4.4,
                "accent_similarity": 4.1,
                "emotion_adherence": 4.0,
                "instruction_adherence": 4.3,
                "uncanny": 1.4,
                "notes": "本人确认候选自然",
            },
        )
        measured = await client.post(
            f"/v1/voices/profiles/{profile['profile_id']}/quality-measurements",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "first_audio_ms": 650,
                "cancel_tail_ms": 110,
                "timestamp_error_ms": 90,
                "long_sentence_chars": 240,
                "long_sentence_completion_ratio": 0.99,
                "source_run_id": "provider-smoke-lifecycle-001",
            },
        )
        activated = await client.post(
            f"/v1/voices/profiles/{profile['profile_id']}/activate",
            headers=headers,
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        resolved = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": session["session_id"]},
        )
        revoked = await client.delete(
            f"/v1/voices/profiles/{profile['profile_id']}",
            headers=headers,
        )
        fallback = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": session["session_id"]},
        )
        selected = await client.put(
            f"/v1/memory/profile/{identity['user_id']}",
            headers=headers,
            json={"companion_id": "xuanmo"},
        )
        designed = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": session["session_id"]},
        )
        unavailable_sample = await client.get(sample_path)

    assert consent.status_code == 201
    assert enrolled.status_code == 201
    assert profile["status"] == "candidate"
    assert profile["quality_status"] == "pending"
    assert "provider_voice_id" not in profile
    assert listing.json()["consent"]["policy_version"] == "voice-clone-v1"
    assert provider_sample.status_code == 200
    assert provider_sample.content == sample_audio
    assert provider_sample.headers["cache-control"] == "no-store"
    assert trial.status_code == 201
    assert "candidate_slot" not in trial.json()
    assert all(preview.status_code == 200 for preview in previews)
    assert all(preview.content == b"RIFF-preview" for preview in previews)
    assert {item["model"] for item in preview_renderer.requests} == {
        None,
        "cosyvoice-v3.5-flash",
    }
    assert next(
        item["voice_id"] for item in preview_renderer.requests if item["model"] is not None
    ) == resolved.json()["voice_id"]
    assert evaluated.json()["status"] == "passed"
    assert measured.json()["status"] == "passed"
    assert activated.json()["status"] == "active"
    assert resolved.json()["mode"] == "active"
    assert resolved.json()["profile_id"] == profile["profile_id"]
    assert resolved.json()["model"] == "cosyvoice-v3.5-flash"
    assert resolved.json()["voice_id"].startswith("cosyvoice-v3.5-flash-clone-")
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["deletion_status"] == "completed"
    assert fallback.json() == {
        "mode": "fallback",
        "profile_id": None,
        "model": None,
        "voice_id": None,
    }
    assert selected.status_code == 200
    assert designed.json() == {
        "mode": "designed",
        "profile_id": "low_magnetic",
        "model": "seed-tts-2.0",
        "voice_id": None,
    }
    assert unavailable_sample.status_code == 404
    assert provider.deleted == [resolved.json()["voice_id"]]


@pytest.mark.asyncio
async def test_voice_profile_revocation_returns_503_until_provider_cleanup_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    provider = ProviderStub()
    app.state.voice_profile_manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "voice-profile-revoke", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/voices/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "voice-clone-v1"},
        )
        enrolled = await client.post(
            "/v1/voices/enrollments",
            headers=headers,
            json={
                "audio_base64": base64.b64encode(b"RIFF" + b"\x01\x02" * 16_000).decode("ascii"),
                "media_type": "audio/wav",
                "duration_ms": 12_000,
                "sample_rate": 24_000,
            },
        )
        profile_id = enrolled.json()["profile_id"]
        provider.delete_fails = True

        failed = await client.delete(f"/v1/voices/profiles/{profile_id}", headers=headers)
        failed_state = await client.get("/v1/voices/profiles", headers=headers)

        provider.delete_fails = False
        retried = await client.delete(f"/v1/voices/profiles/{profile_id}", headers=headers)

    assert failed.status_code == 503
    assert failed.json()["detail"] == "声音资产删除未完成，请稍后重试"
    assert failed_state.json()["items"][0]["status"] == "revoked"
    assert failed_state.json()["items"][0]["deletion_status"] == "failed"
    assert retried.status_code == 200
    assert retried.json()["deletion_status"] == "completed"


@pytest.mark.asyncio
async def test_voice_consent_revocation_returns_503_until_provider_cleanup_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    provider = ProviderStub()
    app.state.voice_profile_manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "voice-revoke-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/voices/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "voice-clone-v1"},
        )
        enrolled = await client.post(
            "/v1/voices/enrollments",
            headers=headers,
            json={
                "audio_base64": base64.b64encode(b"RIFF" + b"\x01\x02" * 16_000).decode("ascii"),
                "media_type": "audio/wav",
                "duration_ms": 12_000,
                "sample_rate": 24_000,
            },
        )
        provider.delete_fails = True

        failed = await client.delete("/v1/voices/consent", headers=headers)
        failed_state = await client.get("/v1/voices/profiles", headers=headers)

        provider.delete_fails = False
        retried = await client.delete("/v1/voices/consent", headers=headers)
        completed_state = await client.get("/v1/voices/profiles", headers=headers)

    assert failed.status_code == 503
    assert failed.json()["detail"] == "声音资产删除未完成，请稍后重试"
    assert failed_state.json()["consent"]["revoked_at"] is not None
    assert failed_state.json()["items"][0]["deletion_status"] == "failed"
    assert retried.status_code == 200
    assert retried.json()["revoked_at"] == failed_state.json()["consent"]["revoked_at"]
    assert completed_state.json()["items"][0]["deletion_status"] == "completed"
    assert enrolled.status_code == 201
    assert len(provider.deleted) == 2
    assert provider.deleted[0] == provider.deleted[1]


@pytest.mark.asyncio
async def test_voice_profile_is_account_isolated_and_internal_resolution_rejects_account_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": "missing", "account_id": "attacker-chosen"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_blind_voice_trial_requires_server_quality_evidence_before_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    preview_renderer = PreviewStub()
    signer = VoiceSampleURLSigner(
        secret="voice-sample-signing-secret-long-enough",
        public_base_url="https://control.test",
        ttl_s=300,
    )
    app.state.voice_sample_signer = signer
    app.state.voice_preview_renderer = preview_renderer
    app.state.voice_profile_manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        ),
        provider=ProviderStub(),
        sample_url_factory=signer.url,
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "blind-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/voices/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "voice-clone-v1"},
        )
        enrolled = await client.post(
            "/v1/voices/enrollments",
            headers=headers,
            json={
                "audio_base64": base64.b64encode(b"RIFF" + b"\x01\x02" * 16_000).decode(),
                "media_type": "audio/wav",
                "duration_ms": 12_000,
                "sample_rate": 24_000,
            },
        )
        profile_id = enrolled.json()["profile_id"]

        forged = await client.post(
            f"/v1/voices/profiles/{profile_id}/evaluations",
            headers=headers,
            json={
                "similarity": 5,
                "naturalness": 5,
                "uncanny": 1,
                "first_audio_ms": 1,
                "cancel_tail_ms": 0,
                "timestamp_error_ms": 0,
                "notes": "客户端伪造客观指标",
            },
        )
        trial = await client.post(
            f"/v1/voices/profiles/{profile_id}/blind-trials",
            headers=headers,
        )
        trial_body = trial.json()
        for slot in ("A", "B"):
            response = await client.post(
                f"/v1/voices/blind-trials/{trial_body['trial_id']}/preview",
                headers=headers,
                json={"slot": slot, "text": "这是同一句盲测文本。"},
            )
            assert response.status_code == 200
        candidate_slot = "A" if preview_renderer.requests[0]["model"] else "B"
        weak_evaluation = await client.post(
            f"/v1/voices/profiles/{profile_id}/evaluations",
            headers=headers,
            json={
                "trial_id": trial_body["trial_id"],
                "preferred_slot": candidate_slot,
                "similarity": 4.4,
                "naturalness": 4.5,
                "accent_similarity": 3.4,
                "emotion_adherence": 4.2,
                "instruction_adherence": 4.1,
                "uncanny": 1.3,
                "notes": "口音相似度未达标",
            },
        )
        blocked_by_subjective_gate = await client.post(
            f"/v1/voices/profiles/{profile_id}/activate",
            headers=headers,
        )
        evaluated = await client.post(
            f"/v1/voices/profiles/{profile_id}/evaluations",
            headers=headers,
            json={
                "trial_id": trial_body["trial_id"],
                "preferred_slot": candidate_slot,
                "similarity": 4.4,
                "naturalness": 4.5,
                "accent_similarity": 4.0,
                "emotion_adherence": 4.2,
                "instruction_adherence": 4.1,
                "uncanny": 1.3,
                "notes": "未知 A/B 身份下完成",
            },
        )
        blocked_without_quality = await client.post(
            f"/v1/voices/profiles/{profile_id}/activate",
            headers=headers,
        )
        weak_measurement = await client.post(
            f"/v1/voices/profiles/{profile_id}/quality-measurements",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "first_audio_ms": 650,
                "cancel_tail_ms": 110,
                "timestamp_error_ms": 90,
                "long_sentence_chars": 240,
                "long_sentence_completion_ratio": 0.97,
                "source_run_id": "provider-smoke-weak-long-sentence",
            },
        )
        blocked_by_quality_gate = await client.post(
            f"/v1/voices/profiles/{profile_id}/activate",
            headers=headers,
        )
        measured = await client.post(
            f"/v1/voices/profiles/{profile_id}/quality-measurements",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "first_audio_ms": 650,
                "cancel_tail_ms": 110,
                "timestamp_error_ms": 90,
                "long_sentence_chars": 240,
                "long_sentence_completion_ratio": 0.99,
                "source_run_id": "provider-smoke-20260719-001",
            },
        )
        activated = await client.post(
            f"/v1/voices/profiles/{profile_id}/activate",
            headers=headers,
        )

    assert forged.status_code == 422
    assert trial.status_code == 201
    assert set(trial_body["slots"]) == {"A", "B"}
    assert "candidate_slot" not in trial_body
    assert weak_evaluation.json()["status"] == "failed"
    assert blocked_by_subjective_gate.status_code == 409
    assert evaluated.json()["status"] == "passed"
    assert blocked_without_quality.status_code == 409
    assert weak_measurement.json()["status"] == "failed"
    assert blocked_by_quality_gate.status_code == 409
    assert measured.json()["status"] == "passed"
    assert activated.status_code == 200
