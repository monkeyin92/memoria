"""A pre-notice voice-clone consent does not enter the digital-self manifest."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.voice_profile.clone_authority import VoiceCloneAuthority
from services.voice_profile.domain import ProviderVoice
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from testing.postgres_harness import TestDatabase

_WHEN = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


class _UnusedProvider:
    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        raise AssertionError("create_voice")

    async def delete_voice(self, *, voice_id: str) -> None:
        raise AssertionError("delete_voice")


class _UnusedStore:
    async def put(self, **kwargs: object) -> object:
        raise AssertionError("put")

    async def get(self, reference: object) -> bytes:
        raise AssertionError("get")

    async def delete(self, reference: object) -> None:
        raise AssertionError("delete")


@pytest.mark.asyncio
async def test_manifest_omits_a_clone_until_the_separate_notice_is_granted(
    postgres_database: TestDatabase,
) -> None:
    account_id = f"manifest-{uuid.uuid4()}"
    profile_id = uuid.uuid4()
    dsn = postgres_database.role_dsn("memoria_app")
    owner = await asyncpg.connect(postgres_database.owner_dsn())
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    assert pool is not None
    registry = PostgresDigitalSelfRegistry(dsn, pool=pool)
    try:
        await registry.initialize()
        event_id = f"source-{account_id}"
        async with owner.transaction():
            await owner.execute(
                """
                INSERT INTO archive_evidence_events (
                    event_id, account_id, event_type, schema_version, occurred_at,
                    speaker_class, source, payload, content_sha256
                ) VALUES ($1, $2, 'speech.utterance_finalized', 1, $3,
                          'owner', 'digital-self-test', $4::jsonb, $5)
                """,
                event_id,
                account_id,
                _WHEN,
                '{"text":"confirmed owner source","interaction_mode":"companion",'
                '"prompt_kind":"spontaneous","owner_projection_eligible":true}',
                "a" * 64,
            )
            await owner.execute(
                """
                INSERT INTO memory_claims (
                    claim_id, account_id, category, domain_category,
                    subject_key, predicate,
                    value, confidence, status, sensitive_domain,
                    extractor_version, source_event_id, valid_at, observed_at
                ) VALUES ($1, $2, 'life_story', 'life_story', 'owner',
                          'preference', 'owner memory', 0.9, 'confirmed',
                          'personal', 'extractor-v1', $3, $4, $4)
                """,
                uuid.uuid4(),
                account_id,
                event_id,
                _WHEN,
            )
            sample_id = uuid.uuid4()
            await owner.execute(
                """
                INSERT INTO voice_clone_consents (
                    account_id, policy_version, granted_at, grant_event_id
                ) VALUES ($1, 'legacy-policy', $2, 'evt-legacy')
                """,
                account_id,
                _WHEN,
            )
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
                "b" * 64,
                _WHEN,
            )
            await owner.execute(
                """
                INSERT INTO voice_profiles (
                    profile_id, account_id, sample_id, version_number,
                    provider, provider_region, target_model, provider_voice_id,
                    status, evaluation_status, quality_status,
                    provider_expires_at, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, 1,
                    'volcengine_doubao', 'cn-beijing', 'seed-icl-2.0', 'doubao-voice',
                    'active', 'passed', 'passed',
                    $4, $5, $5
                )
                """,
                profile_id,
                account_id,
                sample_id,
                datetime.now(UTC) + timedelta(days=30),
                _WHEN,
            )

        withheld = await registry.build(account_id=account_id)
        assert withheld.manifest.source_summary.voice_profile is None

        manager = PostgresVoiceProfileManager(
            dsn,
            object_store=_UnusedStore(),  # type: ignore[arg-type]
            provider=_UnusedProvider(),
            sample_url_factory=lambda sample_id: f"https://samples.example/{sample_id}",
            provider_region="cn-beijing",
            target_model="seed-icl-2.0",
            provider_name="volcengine_doubao",
            clone=VoiceCloneAuthority(enabled=True),
            pool=pool,
        )
        await manager.grant_consent(account_id=account_id, policy_version="voice-policy")
        admitted = await registry.build(account_id=account_id)
        voice = admitted.manifest.source_summary.voice_profile
        assert voice is not None
        assert voice.profile_id == str(profile_id)
    finally:
        await pool.close()
        await owner.close()
