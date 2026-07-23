from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.archive.object_store import EncryptedLocalObjectStore, ObjectRef
from services.voice_profile.domain import (
    EvaluationRequiredError,
    ProviderVoice,
    ProviderVoiceDeletionUnsupportedError,
    VoiceEnrollmentReconciliationRequiredError,
    VoiceEnrollmentRequest,
    VoiceEvaluationRequest,
    VoiceQualityMeasurementRequest,
)
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager


class ProviderStub:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.deleted: list[str] = []
        self.delete_fails = False
        self.create_entered: asyncio.Event | None = None
        self.create_release: asyncio.Event | None = None

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        assert sample_url.startswith("https://control.test/")
        self.created.append(
            {"target_model": target_model, "prefix": prefix, "sample_url": sample_url}
        )
        if self.create_entered is not None:
            self.create_entered.set()
        if self.create_release is not None:
            await self.create_release.wait()
        return ProviderVoice(
            voice_id=f"{target_model}-clone-{prefix}",
            target_model=target_model,
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )

    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)
        if self.delete_fails:
            raise RuntimeError("provider unavailable")


class UnsupportedDeleteProvider(ProviderStub):
    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)
        raise ProviderVoiceDeletionUnsupportedError("manual cleanup required")


class AmbiguousPutStore:
    def __init__(self, inner: EncryptedLocalObjectStore) -> None:
        self.inner = inner
        self.reference: ObjectRef | None = None

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef:
        self.reference = await self.inner.put(
            account_id=account_id,
            purpose=purpose,
            data=data,
            media_type=media_type,
        )
        raise RuntimeError("object-store acknowledgement lost")

    async def get(self, reference: ObjectRef) -> bytes:
        return await self.inner.get(reference)

    async def delete(self, reference: ObjectRef) -> None:
        await self.inner.delete(reference)


async def _cleanup(dsn: str, *account_ids: str) -> None:
    connection = await asyncpg.connect(dsn)
    for account_id in account_ids:
        await connection.execute(
            "DELETE FROM voice_clone_consents WHERE account_id = $1",
            account_id,
        )
        await connection.execute(
            "DELETE FROM archive_evidence_events WHERE account_id = $1",
            account_id,
        )
    await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL voice contract test",
)
async def test_postgres_persists_doubao_provider_and_keeps_unconfirmed_delete_pending(
    tmp_path: Path,
) -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-doubao-voice-provider"
    provider = UnsupportedDeleteProvider()
    manager = PostgresVoiceProfileManager(
        dsn,
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "doubao-voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-postgres-test-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="seed-icl-2.0",
        provider_name="volcengine_doubao",
    )
    try:
        await manager.grant_consent(account_id=account_id, policy_version="voice-clone-v1")
        candidate = await manager.enroll(
            VoiceEnrollmentRequest(
                account_id=account_id,
                audio=b"RIFF" + b"\x01\x02" * 16_000,
                media_type="audio/wav",
                duration_ms=12_000,
                sample_rate=24_000,
            )
        )
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                """
                UPDATE voice_profiles
                SET evaluation_status = 'passed', quality_status = 'passed',
                    provider_expires_at = NULL
                WHERE profile_id = $1::uuid
                """,
                candidate.profile_id,
            )
        finally:
            await connection.close()
        with pytest.raises(EvaluationRequiredError, match="unexpired Doubao"):
            await manager.activate(
                account_id=account_id,
                profile_id=candidate.profile_id,
            )
        assert candidate.provider_expires_at is not None
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "UPDATE voice_profiles SET provider_expires_at = $1 WHERE profile_id = $2::uuid",
                candidate.provider_expires_at,
                candidate.profile_id,
            )
        finally:
            await connection.close()
        await manager.activate(account_id=account_id, profile_id=candidate.profile_id)
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "UPDATE voice_profiles SET provider_expires_at = NULL WHERE profile_id = $1::uuid",
                candidate.profile_id,
            )
        finally:
            await connection.close()
        assert (await manager.resolve(account_id=account_id)).mode == "fallback"
        revoked = await manager.revoke_profile(
            account_id=account_id,
            profile_id=candidate.profile_id,
        )

        assert candidate.provider == "volcengine_doubao"
        assert candidate.target_model == "seed-icl-2.0"
        assert revoked.status == "revoked"
        assert revoked.deletion_status == "pending"
        assert (await manager.resolve(account_id=account_id)).mode == "fallback"
        assert provider.deleted == [candidate.provider_voice_id]
        confirmed = await manager.confirm_provider_deletion(
            account_id=account_id,
            profile_id=candidate.profile_id,
            evidence_reference="doubao-console-ticket/postgres-cleanup-001",
        )
        assert confirmed.deletion_status == "completed"
    finally:
        await manager.close()
        await _cleanup(dsn, account_id)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL voice contract test",
)
async def test_postgres_voice_profile_matches_lifecycle_contract_and_forces_rls(
    tmp_path: Path,
) -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-voice-account"
    other_account = "postgres-voice-other"
    provider = ProviderStub()
    manager = PostgresVoiceProfileManager(
        dsn,
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-postgres-test-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.initialize()
    await _cleanup(dsn, account_id, other_account)

    await manager.grant_consent(
        account_id=account_id,
        policy_version="voice-clone-v1",
    )
    candidates = []
    for suffix in (b"one", b"two"):
        candidate = await manager.enroll(
            VoiceEnrollmentRequest(
                account_id=account_id,
                audio=b"RIFF" + suffix * 12_000,
                media_type="audio/wav",
                duration_ms=12_000,
                sample_rate=24_000,
            )
        )
        sample = await manager.provider_sample(sample_id=candidate.sample_id)
        preview = await manager.preview_target(
            account_id=account_id,
            profile_id=candidate.profile_id,
        )
        assert sample.data.startswith(b"RIFF")
        assert (preview.model, preview.voice_id) == (
            candidate.target_model,
            candidate.provider_voice_id,
        )
        trial = await manager.create_blind_trial(
            account_id=account_id,
            profile_id=candidate.profile_id,
        )
        blind_targets = {
            slot: await manager.blind_preview_target(
                account_id=account_id,
                trial_id=trial.trial_id,
                slot=slot,
                text="同一句 PostgreSQL 盲测文本",
            )
            for slot in ("A", "B")
        }
        candidate_slot = next(
            slot for slot, target in blind_targets.items() if target.model is not None
        )
        candidate_preferred = await manager.resolve_blind_preference(
            account_id=account_id,
            profile_id=candidate.profile_id,
            trial_id=trial.trial_id,
            preferred_slot=candidate_slot,
        )
        await manager.evaluate(
            VoiceEvaluationRequest(
                account_id=account_id,
                profile_id=candidate.profile_id,
                similarity=4.2,
                naturalness=4.1,
                accent_similarity=4.0,
                emotion_adherence=4.0,
                instruction_adherence=4.0,
                uncanny=1.4,
                candidate_preferred=candidate_preferred,
            )
        )
        await manager.record_quality_measurement(
            VoiceQualityMeasurementRequest(
                account_id=account_id,
                profile_id=candidate.profile_id,
                source_run_id=f"postgres-probe-{candidate.version_number}",
                first_audio_ms=700,
                cancel_tail_ms=120,
                timestamp_error_ms=90,
                long_sentence_chars=240,
                long_sentence_completion_ratio=0.99,
            )
        )
        candidates.append(
            await manager.activate(
                account_id=account_id,
                profile_id=candidate.profile_id,
            )
        )

    profiles = await manager.profiles(account_id=account_id)
    resolution = await manager.resolve(account_id=account_id)
    isolated = await manager.profiles(account_id=other_account)

    assert [item.version_number for item in profiles] == [2, 1]
    assert [item.status for item in profiles] == ["active", "candidate"]
    assert resolution.profile_id == candidates[-1].profile_id
    assert isolated == ()

    await manager.record_quality_measurement(
        VoiceQualityMeasurementRequest(
            account_id=account_id,
            profile_id=candidates[-1].profile_id,
            source_run_id="postgres-probe-active-regression",
            first_audio_ms=700,
            cancel_tail_ms=120,
            timestamp_error_ms=90,
            long_sentence_chars=240,
            long_sentence_completion_ratio=0.50,
        )
    )
    assert (await manager.resolve(account_id=account_id)).mode == "fallback"

    revoked = await manager.revoke_profile(
        account_id=account_id,
        profile_id=candidates[-1].profile_id,
    )
    assert revoked.status == "revoked"
    assert revoked.deletion_status == "completed"
    assert (await manager.resolve(account_id=account_id)).mode == "fallback"
    assert provider.deleted == [candidates[-1].provider_voice_id]

    connection = await asyncpg.connect(dsn)
    rls = await connection.fetch(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE relname = ANY($1::text[])
        """,
        [
            "voice_clone_consents",
            "voice_samples",
            "voice_enrollment_operations",
            "voice_profiles",
            "voice_blind_trials",
            "voice_evaluations",
            "voice_quality_measurements",
        ],
    )
    await connection.close()
    assert len(rls) == 7
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls)

    await _cleanup(dsn, account_id, other_account)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL voice contract test",
)
async def test_postgres_consent_revocation_retries_incomplete_provider_deletion(
    tmp_path: Path,
) -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-voice-consent-retry"
    provider = ProviderStub()
    manager = PostgresVoiceProfileManager(
        dsn,
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-postgres-test-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://control.test/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.initialize()
    await _cleanup(dsn, account_id)
    await manager.grant_consent(
        account_id=account_id,
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id=account_id,
            audio=b"RIFF" + b"\x01\x02" * 16_000,
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    provider.delete_fails = True

    with pytest.raises(
        VoiceEnrollmentReconciliationRequiredError,
        match="voice profile deletion incomplete",
    ):
        await manager.revoke_consent(account_id=account_id)

    revoked_consent = await manager.consent(account_id=account_id)
    assert revoked_consent is not None
    assert revoked_consent.revoked_at is not None
    assert (await manager.profiles(account_id=account_id))[0].deletion_status == "failed"

    provider.delete_fails = False
    retried = await manager.revoke_consent(account_id=account_id)

    assert retried.revoked_at == revoked_consent.revoked_at
    assert (await manager.profiles(account_id=account_id))[0].deletion_status == "completed"
    assert provider.deleted == [candidate.provider_voice_id, candidate.provider_voice_id]

    await _cleanup(dsn, account_id)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL voice contract test",
)
async def test_postgres_consent_revocation_waits_for_orphan_sample_reconciliation(
    tmp_path: Path,
) -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-voice-orphan-revoke"
    provider = ProviderStub()
    store = AmbiguousPutStore(
        EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-postgres-test-v1",
        )
    )
    manager = PostgresVoiceProfileManager(
        dsn,
        object_store=store,
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://control.test/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.initialize()
    await _cleanup(dsn, account_id)
    await manager.grant_consent(
        account_id=account_id,
        policy_version="voice-clone-v1",
    )
    request = VoiceEnrollmentRequest(
        account_id=account_id,
        audio=b"RIFF" + b"\x0f\x10" * 16_000,
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="postgres-orphan-upload-revoke",
    )
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await manager.enroll(request)

    with pytest.raises(
        VoiceEnrollmentReconciliationRequiredError,
        match="voice profile deletion incomplete",
    ):
        await manager.revoke_consent(account_id=account_id)

    revoked_consent = await manager.consent(account_id=account_id)
    assert revoked_consent is not None
    assert revoked_consent.revoked_at is not None
    assert (await manager.profiles(account_id=account_id))[0].deletion_status == "failed"

    assert store.reference is not None
    await store.delete(store.reference)
    await manager.reconcile_enrollment(
        account_id=account_id,
        enrollment_key=request.enrollment_key or "",
        sample_asset_absent=True,
    )
    retried = await manager.revoke_consent(account_id=account_id)

    assert retried.revoked_at == revoked_consent.revoked_at
    assert await manager.pending_enrollments(account_id=account_id) == ()
    assert await manager.profiles(account_id=account_id) == ()

    await _cleanup(dsn, account_id)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL voice contract test",
)
async def test_postgres_enrollment_retry_reuses_persisted_provider_result(
    tmp_path: Path,
) -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-voice-saga-retry"
    provider = ProviderStub()
    manager = PostgresVoiceProfileManager(
        dsn,
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-postgres-test-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.initialize()
    await _cleanup(dsn, account_id)
    await manager.grant_consent(
        account_id=account_id,
        policy_version="voice-clone-v1",
    )
    request = VoiceEnrollmentRequest(
        account_id=account_id,
        audio=b"RIFF" + b"\x09\x0a" * 16_000,
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="postgres-stable-enrollment-001",
    )
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        """
        CREATE OR REPLACE FUNCTION memoria_test_fail_voice_candidate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'simulated candidate persistence failure';
        END;
        $$;
        DROP TRIGGER IF EXISTS fail_voice_candidate_persistence ON voice_profiles;
        CREATE TRIGGER fail_voice_candidate_persistence
        BEFORE UPDATE OF provider_voice_id ON voice_profiles
        FOR EACH ROW WHEN (NEW.provider_voice_id IS NOT NULL)
        EXECUTE FUNCTION memoria_test_fail_voice_candidate();
        """
    )
    await connection.close()
    try:
        with pytest.raises(asyncpg.PostgresError, match="candidate persistence"):
            await manager.enroll(request)
    finally:
        connection = await asyncpg.connect(dsn)
        await connection.execute(
            """
            DROP TRIGGER IF EXISTS fail_voice_candidate_persistence ON voice_profiles;
            DROP FUNCTION IF EXISTS memoria_test_fail_voice_candidate();
            """
        )
        await connection.close()

    candidate = await manager.enroll(request)

    assert candidate.status == "candidate"
    assert len(provider.created) == 1
    assert len(await manager.profiles(account_id=account_id)) == 1

    ambiguous_request = VoiceEnrollmentRequest(
        account_id=account_id,
        audio=b"RIFF" + b"\x0b\x0c" * 16_000,
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="postgres-stable-enrollment-ambiguous",
    )
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        """
        CREATE OR REPLACE FUNCTION memoria_test_fail_voice_provider_result()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'simulated provider result persistence failure';
        END;
        $$;
        DROP TRIGGER IF EXISTS fail_voice_provider_result
        ON voice_enrollment_operations;
        CREATE TRIGGER fail_voice_provider_result
        BEFORE UPDATE OF provider_voice_id ON voice_enrollment_operations
        FOR EACH ROW WHEN (NEW.provider_voice_id IS NOT NULL)
        EXECUTE FUNCTION memoria_test_fail_voice_provider_result();
        """
    )
    await connection.close()
    try:
        with pytest.raises(asyncpg.PostgresError, match="provider result persistence"):
            await manager.enroll(ambiguous_request)
    finally:
        connection = await asyncpg.connect(dsn)
        await connection.execute(
            """
            DROP TRIGGER IF EXISTS fail_voice_provider_result
            ON voice_enrollment_operations;
            DROP FUNCTION IF EXISTS memoria_test_fail_voice_provider_result();
            """
        )
        await connection.close()

    with pytest.raises(VoiceEnrollmentReconciliationRequiredError):
        await manager.enroll(ambiguous_request)
    pending = await manager.pending_enrollments(account_id=account_id)
    ambiguous = next(
        item for item in pending if item.enrollment_key == "postgres-stable-enrollment-ambiguous"
    )
    recovered = ProviderVoice(
        voice_id=f"cosyvoice-v3.5-flash-clone-{ambiguous.provider_prefix}",
        target_model="cosyvoice-v3.5-flash",
    )
    reconciled = await manager.reconcile_enrollment(
        account_id=account_id,
        enrollment_key=ambiguous.enrollment_key,
        provider_voice=recovered,
    )

    assert reconciled.status == "candidate"
    assert len(provider.created) == 2

    await _cleanup(dsn, account_id)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL voice contract test",
)
async def test_postgres_revocation_during_enrollment_tracks_failed_late_deletion(
    tmp_path: Path,
) -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-voice-late-revocation"
    provider = ProviderStub()
    provider.create_entered = asyncio.Event()
    provider.create_release = asyncio.Event()
    provider.delete_fails = True
    manager = PostgresVoiceProfileManager(
        dsn,
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-postgres-test-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.initialize()
    await _cleanup(dsn, account_id)
    await manager.grant_consent(
        account_id=account_id,
        policy_version="voice-clone-v1",
    )
    enrollment = asyncio.create_task(
        manager.enroll(
            VoiceEnrollmentRequest(
                account_id=account_id,
                audio=b"RIFF" + b"\x01\x02" * 16_000,
                media_type="audio/wav",
                duration_ms=12_000,
                sample_rate=24_000,
            )
        )
    )
    await provider.create_entered.wait()
    enrolling = (await manager.profiles(account_id=account_id))[0]
    await manager.revoke_profile(
        account_id=account_id,
        profile_id=enrolling.profile_id,
    )

    provider.create_release.set()
    with pytest.raises(RuntimeError, match="late provider voice deletion failed"):
        await enrollment

    profile = (await manager.profiles(account_id=account_id))[0]
    expected_voice_id = f"cosyvoice-v3.5-flash-clone-m{enrolling.profile_id.replace('-', '')[:9]}"
    assert provider.deleted == [expected_voice_id]
    assert profile.status == "revoked"
    assert profile.deletion_status == "failed"
    assert profile.provider_voice_id == expected_voice_id

    await _cleanup(dsn, account_id)
    await manager.close()
