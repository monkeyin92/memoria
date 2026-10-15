"""Consent notice column, kill switch, and erasure against PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.archive.object_store import EncryptedLocalObjectStore
from services.voice_profile.clone_authority import (
    VoiceCloneAuthority,
    VoiceCloneDisabledError,
    consent_standing,
)
from services.voice_profile.domain import (
    ProviderVoice,
    VoiceConsentRequiredError,
    VoiceEnrollmentRequest,
)
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from services.voice_profile.testing_audio import voice_sample_wav
from testing.postgres_harness import TestDatabase

_GRANTED = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


class _Provider:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.voice_ids: list[str] = []
        self.deleted: list[str] = []

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        voice_id = f"{target_model}-clone-{prefix}"
        self.created.append(sample_url)
        self.voice_ids.append(voice_id)
        return ProviderVoice(voice_id=voice_id, target_model=target_model)

    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)


class _Store:
    def __init__(self) -> None:
        self.puts: list[bytes] = []

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> object:
        self.puts.append(data)
        raise AssertionError("object store put")

    async def get(self, reference: object) -> bytes:
        raise AssertionError("object store get")

    async def delete(self, reference: object) -> None:
        return None


def _sample_url(sample_id: str) -> str:
    return f"https://control.test/samples/{sample_id}"


def _manager(
    database: TestDatabase,
    *,
    enabled: bool,
    provider: _Provider,
    store: object,
    pool: asyncpg.Pool,
) -> PostgresVoiceProfileManager:
    return PostgresVoiceProfileManager(
        database.role_dsn("memoria_app"),
        object_store=store,  # type: ignore[arg-type]
        provider=provider,
        sample_url_factory=_sample_url,
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
        clone=VoiceCloneAuthority(enabled=enabled),
        pool=pool,
    )


def _enrollment(account_id: str, *, audio: bytes | None = None) -> VoiceEnrollmentRequest:
    return VoiceEnrollmentRequest(
        account_id=account_id,
        audio=audio if audio is not None else voice_sample_wav(),
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=16_000,
    )


async def _counts(owner: asyncpg.Connection, account_id: str) -> tuple[int, int]:
    consents = int(
        await owner.fetchval(
            "SELECT count(*) FROM voice_clone_consents WHERE account_id = $1",
            account_id,
        )
    )
    events = int(
        await owner.fetchval(
            "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
            account_id,
        )
    )
    return consents, events


@pytest.mark.asyncio
async def test_pre_notice_rows_stay_and_do_not_authorize_a_clone(
    postgres_database: TestDatabase,
) -> None:
    account_id = f"pre-notice-{uuid.uuid4()}"
    owner = await asyncpg.connect(postgres_database.owner_dsn())
    pool = await asyncpg.create_pool(postgres_database.role_dsn("memoria_app"), min_size=1, max_size=4)
    assert pool is not None
    provider = _Provider()
    store = _Store()
    try:
        await owner.execute("ALTER TABLE voice_clone_consents DROP COLUMN notice_version")
        await owner.execute(
            """
            INSERT INTO voice_clone_consents (
                account_id, policy_version, granted_at, revoked_at, grant_event_id
            ) VALUES ($1, 'legacy-policy', $2, NULL, 'evt-legacy')
            """,
            account_id,
            _GRANTED,
        )
        first = _manager(
            postgres_database, enabled=True, provider=provider, store=store, pool=pool
        )
        second = _manager(
            postgres_database, enabled=True, provider=provider, store=store, pool=pool
        )
        await first.initialize()
        await second.initialize()
        row = await owner.fetchrow(
            """
            SELECT policy_version, granted_at, revoked_at, notice_version
            FROM voice_clone_consents WHERE account_id = $1
            """,
            account_id,
        )
        assert row is not None
        assert row["policy_version"] == "legacy-policy"
        assert row["granted_at"] == _GRANTED
        assert row["revoked_at"] is None
        assert row["notice_version"] == "pre-separate-notice"
        standing = consent_standing(await first.consent(account_id=account_id))
        assert standing == "notice_required"

        with pytest.raises(VoiceConsentRequiredError, match="separate notice"):
            await first.enroll(_enrollment(account_id))
        assert provider.created == []
        assert store.puts == []

        profile_id = uuid.uuid4()
        sample_id = uuid.uuid4()
        await owner.execute(
            """
            INSERT INTO voice_samples (
                sample_id, account_id, object_key, media_type, byte_count,
                content_sha256, encryption_key_version, object_backend,
                duration_ms, sample_rate, created_at
            ) VALUES ($1, $2, $3, 'audio/wav', 100, $4, 'k', 'local', 12000, 16000, $5)
            """,
            sample_id,
            account_id,
            f"obj-{sample_id}",
            "a" * 64,
            _GRANTED,
        )
        await owner.execute(
            """
            INSERT INTO voice_profiles (
                profile_id, account_id, sample_id, version_number,
                provider, provider_region, target_model, provider_voice_id,
                status, evaluation_status, quality_status, created_at, updated_at
            ) VALUES (
                $1, $2, $3, 1,
                'alibaba_model_studio', 'cn-beijing', 'cosyvoice-v3.5-flash', 'seeded-voice',
                'active', 'passed', 'passed', $4, $4
            )
            """,
            profile_id,
            account_id,
            sample_id,
            _GRANTED,
        )
        assert (await first.resolve(account_id=account_id)).mode == "fallback"

        granted = await first.grant_consent(account_id=account_id, policy_version="voice-policy")
        assert granted.notice_version == VoiceCloneAuthority(enabled=True).admit().notice_version
        assert consent_standing(granted) == "valid"
        resolved = await first.resolve(account_id=account_id)
        assert resolved.mode == "active"
        assert resolved.voice_id == "seeded-voice"

        third = _manager(
            postgres_database, enabled=True, provider=provider, store=store, pool=pool
        )
        await third.initialize()
        assert consent_standing(await third.consent(account_id=account_id)) == "valid"
    finally:
        await pool.close()
        await owner.close()


@pytest.mark.asyncio
async def test_enabled_grant_stamps_the_notice_and_enrollment_calls_the_provider(
    postgres_database: TestDatabase,
    tmp_path: object,
) -> None:
    from pathlib import Path

    account_id = f"enabled-{uuid.uuid4()}"
    pool = await asyncpg.create_pool(postgres_database.role_dsn("memoria_app"), min_size=1, max_size=4)
    assert pool is not None
    provider = _Provider()
    store = EncryptedLocalObjectStore(
        root=Path(str(tmp_path)) / "voice",
        key=Fernet.generate_key().decode("ascii"),
        key_version="voice-key-v1",
    )
    try:
        manager = _manager(
            postgres_database, enabled=True, provider=provider, store=store, pool=pool
        )
        granted = await manager.grant_consent(account_id=account_id, policy_version="voice-policy")
        assert granted.notice_version == VoiceCloneAuthority(enabled=True).admit().notice_version
        assert consent_standing(granted) == "valid"
        profile = await manager.enroll(_enrollment(account_id))
        assert profile.status == "candidate"
        assert provider.created == [_sample_url(profile.sample_id)]
        assert profile.provider_voice_id == provider.voice_ids[0]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_disabled_grant_writes_no_consent_and_no_evidence(
    postgres_database: TestDatabase,
) -> None:
    account_id = f"disabled-{uuid.uuid4()}"
    owner = await asyncpg.connect(postgres_database.owner_dsn())
    pool = await asyncpg.create_pool(postgres_database.role_dsn("memoria_app"), min_size=1, max_size=4)
    assert pool is not None
    try:
        before = await _counts(owner, account_id)
        manager = _manager(
            postgres_database,
            enabled=False,
            provider=_Provider(),
            store=_Store(),
            pool=pool,
        )
        with pytest.raises(VoiceCloneDisabledError):
            await manager.grant_consent(account_id=account_id, policy_version="voice-policy")
        assert await _counts(owner, account_id) == before == (0, 0)
    finally:
        await pool.close()
        await owner.close()


@pytest.mark.asyncio
async def test_revoke_deletes_the_provider_voice_while_cloning_is_off(
    postgres_database: TestDatabase,
    tmp_path: object,
) -> None:
    from pathlib import Path

    account_id = f"revoke-{uuid.uuid4()}"
    pool = await asyncpg.create_pool(postgres_database.role_dsn("memoria_app"), min_size=1, max_size=4)
    assert pool is not None
    provider = _Provider()
    store = EncryptedLocalObjectStore(
        root=Path(str(tmp_path)) / "voice",
        key=Fernet.generate_key().decode("ascii"),
        key_version="voice-key-v1",
    )
    try:
        enabled = _manager(
            postgres_database, enabled=True, provider=provider, store=store, pool=pool
        )
        await enabled.grant_consent(account_id=account_id, policy_version="voice-policy")
        profile = await enabled.enroll(_enrollment(account_id))
        assert profile.provider_voice_id
        disabled = _manager(
            postgres_database, enabled=False, provider=provider, store=store, pool=pool
        )
        await disabled.revoke_consent(account_id=account_id)
        assert provider.deleted == [profile.provider_voice_id]
        assert consent_standing(await disabled.consent(account_id=account_id)) == "revoked"
    finally:
        await pool.close()
