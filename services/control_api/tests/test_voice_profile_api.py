from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from services.archive.object_store import EncryptedLocalObjectStore
from services.control_api.app.main import create_app
from services.digital_self.domain import (
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    DigitalSelfVersion,
    VoiceProfileManifestRef,
)
from services.legacy.domain import LegacyAccessDeniedError, LegacyAccessSnapshot
from services.voice_profile.domain import ProviderVoice, VoiceProfile, VoiceResolution
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
            provider="alibaba_model_studio",
            voice_kind="personal",
            model="cosyvoice-v3.5-flash",
            resource_id="cosyvoice-v3.5-flash",
            voice_id="cosyvoice-v3.5-flash-clone-owner001",
        )


class ActivationStub:
    def __init__(self) -> None:
        self.called = False

    async def activate(self, *, account_id: str, profile_id: str) -> None:
        self.called = True
        raise AssertionError(f"unexpected activation: {account_id=} {profile_id=}")

    async def profiles(self, *, account_id: str) -> tuple[VoiceProfile, ...]:
        del account_id
        return (
            _voice_profile(
                "legacy-cosyvoice-profile", "alibaba_model_studio", "cosyvoice-v3.5-flash"
            ),
        )


class DoubaoActivationStub:
    def __init__(self) -> None:
        self.called = False
        self.profile = _voice_profile(
            "doubao-personal-profile",
            "volcengine_doubao",
            "seed-icl-2.0",
        )

    async def profiles(self, *, account_id: str) -> tuple[VoiceProfile, ...]:
        del account_id
        return (self.profile,)

    async def activate(self, *, account_id: str, profile_id: str) -> VoiceProfile:
        assert account_id and profile_id == self.profile.profile_id
        self.called = True
        return self.profile


class FrozenPreviewResolutionStub:
    def __init__(self, resolution: VoiceResolution) -> None:
        self.resolution = resolution

    async def resolve(self, *, account_id: str) -> VoiceResolution:
        assert account_id
        return self.resolution


class LegacyAccessStub:
    def __init__(self, access: LegacyAccessSnapshot) -> None:
        self.access = access
        self.available = True
        self.requests: list[dict[str, object]] = []

    async def resolve_access(self, **kwargs: object) -> LegacyAccessSnapshot:
        self.requests.append(kwargs)
        now = kwargs.get("now")
        if not self.available or (isinstance(now, datetime) and self.access.expires_at <= now):
            raise LegacyAccessDeniedError("legacy grant is not available")
        return self.access


class LegacyVersionStub:
    def __init__(self, version: DigitalSelfVersion) -> None:
        self.version = version

    async def get(self, **kwargs: object) -> DigitalSelfVersion:
        assert kwargs == {
            "account_id": self.version.account_id,
            "version_id": self.version.version_id,
        }
        return self.version


class ProviderDeletionConfirmationStub:
    def __init__(self) -> None:
        self.confirmations: list[dict[str, str]] = []

    async def confirm_provider_deletion(
        self,
        *,
        account_id: str,
        profile_id: str,
        evidence_reference: str,
    ) -> VoiceProfile:
        self.confirmations.append(
            {
                "account_id": account_id,
                "profile_id": profile_id,
                "evidence_reference": evidence_reference,
            }
        )
        return replace(
            _voice_profile(profile_id, "volcengine_doubao", "seed-icl-2.0"),
            status="revoked",
            deletion_status="completed",
            revoked_at=datetime.now(UTC),
        )


def _voice_profile(profile_id: str, provider: str, target_model: str) -> VoiceProfile:
    return VoiceProfile(
        profile_id=profile_id,
        sample_id="sample-001",
        version_number=1,
        provider=provider,
        provider_region="cn-beijing",
        target_model=target_model,
        provider_voice_id="smoke-verified-synth-speaker",
        status="candidate",
        evaluation_status="passed",
        quality_status="passed",
        deletion_status="not_requested",
        provider_expires_at=None,
        created_at=datetime.now(UTC),
    )


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("TTS_PROVIDER", "cosyvoice")


async def _verified_adult_headers(
    app: object,
    client: AsyncClient,
    *,
    user_id: str,
    username: str,
) -> dict[str, str]:
    app.state.memory_store.update_subject_profile(  # type: ignore[attr-defined]
        user_id=user_id,
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )
    login = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": "safe-password"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _legacy_access(*, voice_allowed: bool, expired: bool = False) -> LegacyAccessSnapshot:
    return LegacyAccessSnapshot(
        actor_role="grantee",
        resource_owner_account_id="legacy-owner",
        grantee_account_id="legacy-grantee",
        grant_id="legacy-grant",
        shell_id="legacy-shell",
        version_id="legacy-version",
        version_number=7,
        manifest_sha256="a" * 64,
        grant_snapshot_sha256="b" * 64,
        scope_sha256="c" * 64,
        allowed_items=(),
        relationship_profile_id="relationship-1",
        relationship_profile_version=3,
        voice_allowed=voice_allowed,
        expires_at=datetime.now(UTC) + timedelta(days=-1 if expired else 30),
    )


def _legacy_version(access: LegacyAccessSnapshot, *, voice_id: str) -> DigitalSelfVersion:
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
            entries=(),
            source_summary=DigitalSelfSourceSummary(
                memory_claim_count=0,
                persona_trait_count=0,
                persona_version_id=None,
                source_summary_sha256="summary",
                voice_profile=VoiceProfileManifestRef(
                    profile_id="voice-profile-1",
                    version_number=3,
                    provider="volcengine_doubao",
                    target_model="seed-icl-2.0",
                    resource_id="seed-icl-2.0",
                    provider_expires_at="2027-07-23T00:00:00+00:00",
                    speaker_sha256=hashlib.sha256(voice_id.encode()).hexdigest(),
                ),
            ),
        ),
        manifest_sha256=access.manifest_sha256,
        created_at=datetime.now(UTC),
    )


def _add_legacy_voice_session(app: object, access: LegacyAccessSnapshot) -> str:
    voice = _legacy_version(
        access, voice_id="provider-secret-id"
    ).manifest.source_summary.voice_profile
    assert voice is not None
    session_id = "legacy-voice-session"
    for account_id in (
        access.resource_owner_account_id,
        access.grantee_account_id,
    ):
        app.state.memory_store.update_subject_profile(  # type: ignore[attr-defined]
            user_id=account_id,
            subject_category="adult",
            birth_year_band="adult",
            age_evidence_status="verified",
            now=datetime.now(UTC).isoformat(),
        )
    app.state.memory_store.add_voice_session(  # type: ignore[attr-defined]
        session_id=session_id,
        user_id=access.grantee_account_id,
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
        voice_profile_id=voice.profile_id if access.voice_allowed else None,
        voice_profile_version=voice.version_number if access.voice_allowed else None,
        voice_provider=voice.provider if access.voice_allowed else None,
        voice_model=voice.target_model if access.voice_allowed else None,
        voice_resource_id=voice.resource_id if access.voice_allowed else None,
        voice_provider_expires_at=voice.provider_expires_at if access.voice_allowed else None,
        voice_speaker_sha256=voice.speaker_sha256 if access.voice_allowed else None,
        fallback_voice_profile_id="warm_companion",
        fallback_voice_provider="volcengine_doubao",
        fallback_voice_model="seed-tts-2.0",
        fallback_voice_resource_id="seed-tts-2.0",
    )
    return session_id


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
async def test_manual_provider_cleanup_requires_dedicated_token_and_is_audited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_VOICE_CLEANUP_TOKEN",
        "test-voice-cleanup-material-long-enough",
    )
    app = create_app()
    manager = ProviderDeletionConfirmationStub()
    app.state.voice_profile_manager = manager
    body = {
        "account_id": "voice-owner",
        "evidence_reference": "doubao-console-ticket/cleanup-001",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.post(
            "/v1/voices/profiles/personal-v1/provider-deletion-confirmations",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json=body,
        )
        confirmed = await client.post(
            "/v1/voices/profiles/personal-v1/provider-deletion-confirmations",
            headers={"X-Memoria-Voice-Cleanup-Token": ("test-voice-cleanup-material-long-enough")},
            json=body,
        )

    assert rejected.status_code == 401
    assert confirmed.status_code == 200
    assert confirmed.json()["deletion_status"] == "completed"
    assert manager.confirmations == [
        {
            "account_id": "voice-owner",
            "profile_id": "personal-v1",
            "evidence_reference": "doubao-console-ticket/cleanup-001",
        }
    ]


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
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="legacy-voice-owner",
        )
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
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": "seed-tts-2.0",
        "resource_id": "seed-tts-2.0",
        "voice_id": None,
        "speaker_sha256": None,
    }


@pytest.mark.asyncio
async def test_custom_persona_companion_session_resolves_the_frozen_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    voice_id = "cosyvoice-v3.5-flash-clone-owner001"
    app.state.voice_profile_manager = FrozenPreviewResolutionStub(
        VoiceResolution(
            mode="active",
            profile_id="voice-profile-personal",
            version_number=2,
            provider="alibaba_model_studio",
            voice_kind="personal",
            model="cosyvoice-v3.5-flash",
            resource_id="cosyvoice-v3.5-flash",
            voice_id=voice_id,
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "custom-persona-owner", "password": "safe-password"},
            )
        ).json()
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="custom-persona-owner",
        )
        saved = await client.put(
            f"/v1/memory/profile/{identity['user_id']}",
            headers=headers,
            json={
                "companion_id": "taoxi",
                "bio": "[memoria.custom_persona.v1]\nname: 小北\n---\n说话短一点，像朋友。",
            },
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

    assert saved.status_code == 200
    frozen = app.state.memory_store.get_voice_session_by_id(session_id=session["session_id"])
    assert frozen is not None
    assert frozen["companion_style_id"] == "taoxi"
    assert frozen["voice_provider"] == "alibaba_model_studio"
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["mode"] == "active"
    assert body["voice_kind"] == "personal"
    assert body["provider"] == "alibaba_model_studio"
    assert body["voice_id"] == voice_id
    assert body["speaker_sha256"] == hashlib.sha256(voice_id.encode()).hexdigest()


@pytest.mark.asyncio
async def test_self_preview_resolution_requires_exact_frozen_voice_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    expires_at = "2027-07-23T00:00:00+00:00"
    manager = FrozenPreviewResolutionStub(
        VoiceResolution(
            mode="active",
            profile_id="voice-profile-1",
            version_number=3,
            provider="volcengine_doubao",
            voice_kind="personal",
            model="seed-icl-2.0",
            resource_id="seed-icl-2.0",
            voice_id="provider-secret-id",
            provider_expires_at=datetime.fromisoformat(expires_at),
        )
    )
    app.state.voice_profile_manager = manager

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "frozen-preview-owner", "password": "safe-password"},
            )
        ).json()
        await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="frozen-preview-owner",
        )
        app.state.memory_store.add_voice_session(
            session_id="frozen-preview-session",
            user_id=identity["user_id"],
            room_name="room-frozen-preview-session",
            voice_backend="cascade",
            created_at=datetime.now(UTC).isoformat(),
            interaction_mode="self_preview",
            mode_policy_version="s8-v1",
            digital_self_version_id="digital-self-1",
            digital_self_manifest_sha256="manifest-1",
            preview_grant_id="grant-1",
            self_preview_perspective="owner",
            voice_profile_id="voice-profile-1",
            voice_profile_version=3,
            voice_provider="volcengine_doubao",
            voice_model="seed-icl-2.0",
            voice_resource_id="seed-icl-2.0",
            voice_provider_expires_at=expires_at,
            voice_speaker_sha256=(
                "5235c7027839d3b116078b4f0f00e87c91437c81836a347f3c2a8f48e56f9558"
            ),
            fallback_voice_profile_id="bright_peer",
            fallback_voice_provider="volcengine_doubao",
            fallback_voice_model="seed-tts-2.0",
            fallback_voice_resource_id="seed-tts-2.0",
        )
        exact = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": "frozen-preview-session"},
        )
        manager.resolution = VoiceResolution(
            mode="active",
            profile_id="voice-profile-1",
            version_number=4,
            provider="volcengine_doubao",
            voice_kind="personal",
            model="seed-icl-2.0",
            resource_id="seed-icl-2.0",
            voice_id="new-provider-secret-id",
            provider_expires_at=datetime.fromisoformat(expires_at),
        )
        mismatch = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": "frozen-preview-session"},
        )

    assert exact.json() == {
        "mode": "active",
        "profile_id": "voice-profile-1",
        "provider": "volcengine_doubao",
        "voice_kind": "personal",
        "model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "voice_id": "provider-secret-id",
        "speaker_sha256": ("5235c7027839d3b116078b4f0f00e87c91437c81836a347f3c2a8f48e56f9558"),
    }
    assert mismatch.json() == {
        "mode": "designed",
        "profile_id": "bright_peer",
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": "seed-tts-2.0",
        "resource_id": "seed-tts-2.0",
        "voice_id": None,
        "speaker_sha256": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voice_allowed", "resolved_profile_id", "expected_mode", "expected_profile_id"),
    (
        (True, "voice-profile-1", "active", "voice-profile-1"),
        (True, "other-personal-profile", "designed", "warm_companion"),
        (False, "voice-profile-1", "designed", "warm_companion"),
    ),
)
async def test_legacy_voice_resolution_revalidates_frozen_access_and_selects_authorized_voice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    voice_allowed: bool,
    resolved_profile_id: str,
    expected_mode: str,
    expected_profile_id: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    access = _legacy_access(voice_allowed=voice_allowed)
    legacy = LegacyAccessStub(access)
    voice_id = "provider-secret-id"
    app.state.legacy_registry = legacy
    app.state.digital_self_registry = LegacyVersionStub(_legacy_version(access, voice_id=voice_id))
    app.state.voice_profile_manager = FrozenPreviewResolutionStub(
        VoiceResolution(
            mode="active",
            profile_id=resolved_profile_id,
            version_number=3,
            provider="volcengine_doubao",
            voice_kind="personal",
            model="seed-icl-2.0",
            resource_id="seed-icl-2.0",
            voice_id=voice_id,
            provider_expires_at=datetime.fromisoformat("2027-07-23T00:00:00+00:00"),
        )
    )
    session_id = _add_legacy_voice_session(app, access)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": session_id},
        )

    assert response.status_code == 200
    assert response.json()["mode"] == expected_mode
    assert response.json()["profile_id"] == expected_profile_id
    assert response.json()["voice_kind"] == (
        "personal" if expected_mode == "active" else "designed"
    )
    assert legacy.requests == [
        {
            "actor_account_id": access.grantee_account_id,
            "grant_id": access.grant_id,
            "purpose": "grantee_session",
            "now": legacy.requests[0]["now"],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked", "expired", "wrong_actor"])
async def test_legacy_voice_resolution_fails_closed_when_live_access_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    denial: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    access = _legacy_access(voice_allowed=True, expired=denial == "expired")
    legacy = LegacyAccessStub(access)
    app.state.legacy_registry = legacy
    app.state.digital_self_registry = LegacyVersionStub(
        _legacy_version(access, voice_id="provider-secret-id")
    )
    app.state.voice_profile_manager = FrozenPreviewResolutionStub(
        VoiceResolution(
            mode="active",
            profile_id="voice-profile-1",
            version_number=3,
            provider="volcengine_doubao",
            voice_kind="personal",
            model="seed-icl-2.0",
            resource_id="seed-icl-2.0",
            voice_id="provider-secret-id",
            provider_expires_at=datetime.fromisoformat("2027-07-23T00:00:00+00:00"),
        )
    )
    session_id = _add_legacy_voice_session(app, access)
    if denial == "revoked":
        legacy.available = False
    elif denial == "wrong_actor":
        legacy.access = replace(access, grantee_account_id="different-grantee")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/voices/session-resolution",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={"session_id": session_id},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "legacy_voice_unavailable"}


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
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="doubao-voice-owner",
        )
        response = await client.post(
            "/v1/voices/profiles/legacy-cosyvoice-profile/activate",
            headers=headers,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "当前豆包语音链路不支持激活历史 CosyVoice 克隆音色"
    assert not manager.called


@pytest.mark.asyncio
async def test_doubao_runtime_allows_seed_icl_personal_clone_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TTS_PROVIDER", "doubao")
    app = create_app()
    manager = DoubaoActivationStub()
    app.state.voice_profile_manager = manager

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "doubao-seed-icl-owner", "password": "safe-password"},
            )
        ).json()
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="doubao-seed-icl-owner",
        )
        response = await client.post(
            "/v1/voices/profiles/doubao-personal-profile/activate",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["provider"] == "volcengine_doubao"
    assert manager.called


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
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="voice-owner",
        )
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
    candidate_preview_voice_id = next(
        item["voice_id"] for item in preview_renderer.requests if item["model"] is not None
    )
    assert candidate_preview_voice_id is not None
    assert evaluated.json()["status"] == "passed"
    assert measured.json()["status"] == "passed"
    assert activated.json()["status"] == "active"
    assert resolved.json() == {
        "mode": "designed",
        "profile_id": "warm_companion",
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": "seed-tts-2.0",
        "resource_id": "seed-tts-2.0",
        "voice_id": None,
        "speaker_sha256": None,
    }
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["deletion_status"] == "completed"
    assert fallback.json() == {
        "mode": "designed",
        "profile_id": "warm_companion",
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": "seed-tts-2.0",
        "resource_id": "seed-tts-2.0",
        "voice_id": None,
        "speaker_sha256": None,
    }
    assert selected.status_code == 200
    assert designed.json() == {
        "mode": "designed",
        "profile_id": "warm_companion",
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": "seed-tts-2.0",
        "resource_id": "seed-tts-2.0",
        "voice_id": None,
        "speaker_sha256": None,
    }
    assert unavailable_sample.status_code == 404
    assert provider.deleted == [candidate_preview_voice_id]


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
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="voice-profile-revoke",
        )
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
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="voice-revoke-owner",
        )
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
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="blind-owner",
        )
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


@pytest.mark.asyncio
async def test_ready_for_device_enrollment_activates_without_in_app_ab(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signer = VoiceSampleURLSigner(
        secret="voice-sample-signing-secret-long-enough",
        public_base_url="https://control.test",
        ttl_s=300,
    )
    app.state.voice_sample_signer = signer
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
                json={"username": "device-ready-owner", "password": "safe-password"},
            )
        ).json()
        headers = await _verified_adult_headers(
            app,
            client,
            user_id=identity["user_id"],
            username="device-ready-owner",
        )
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
                "ready_for_device": True,
            },
        )
        profile = enrolled.json()
        listing = await client.get("/v1/voices/profiles", headers=headers)
        ready_again = await client.post(
            f"/v1/voices/profiles/{profile['profile_id']}/ready-for-device",
            headers=headers,
        )

    assert enrolled.status_code == 201
    assert profile["status"] == "active"
    assert profile["evaluation_status"] == "passed"
    assert profile["quality_status"] == "passed"
    assert profile["activated_at"]
    assert listing.status_code == 200
    assert listing.json()["items"][0]["status"] == "active"
    assert ready_again.status_code == 200
    assert ready_again.json()["status"] == "active"

