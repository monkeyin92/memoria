from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.archive.object_store import EncryptedLocalObjectStore
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import hash_password
from services.evolution.account_repository import PostgresEvolutionAccountRepository
from services.evolution.postgres_store import PostgresEvolutionStore
from services.governance.account_data import (
    AccountDataGovernance,
    PostgresAccountRepository,
)
from services.governance.tests.test_account_data import (
    SessionTerminatorStub,
    VoiceProviderStub,
)
from services.voice_profile.domain import VoiceEnrollmentRequest
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from services.voice_profile.testing_audio import voice_sample_wav

_SPEAKER_SCHEMA = Path(__file__).parents[2] / "speaker" / "postgres_schema.sql"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL governance contract",
)
async def test_postgres_account_repository_exports_and_deletes_every_projection() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"governance-{uuid.uuid4()}"
    other_id = f"governance-{uuid.uuid4()}"
    event_id = f"event-{uuid.uuid4()}"
    other_event_id = f"event-{uuid.uuid4()}"
    speaker_identity = uuid.uuid4()
    speaker_profile = uuid.uuid4()
    speaker_sample = uuid.uuid4()
    voice_sample = uuid.uuid4()
    voice_profile = uuid.uuid4()
    root = Path(__file__).parents[2]
    connection = await asyncpg.connect(dsn)
    try:
        for schema in (
            root / "archive" / "postgres_schema.sql",
            root / "persona" / "postgres_schema.sql",
            root / "digital_self" / "postgres_schema.sql",
            root / "self_model" / "postgres_schema.sql",
            root / "speaker" / "postgres_schema.sql",
            root / "voice_profile" / "postgres_schema.sql",
        ):
            await connection.execute(schema.read_text(encoding="utf-8"))
        for current_account, current_event in (
            (account_id, event_id),
            (other_id, other_event_id),
        ):
            claim_id = uuid.uuid4()
            decision_id = uuid.uuid4()
            person_id = uuid.uuid4()
            relationship_id = uuid.uuid4()
            relationship_profile_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO archive_evidence_events (
                    event_id, account_id, event_type, schema_version, occurred_at,
                    speaker_class, source, payload, content_sha256
                ) VALUES ($1, $2, 'speech.utterance_finalized', 1, $3,
                          'owner', 'test', $4::jsonb, $5)
                """,
                current_event,
                current_account,
                datetime.now(UTC),
                '{"text":"postgres governance"}',
                "a" * 64,
            )
            outbox_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO archive_processing_outbox (
                    outbox_id, account_id, event_id, task_type, status
                ) VALUES ($1, $2, $3, 'compile_memory', 'dead')
                """,
                outbox_id,
                current_account,
                current_event,
            )
            await connection.execute(
                """
                INSERT INTO archive_outbox_replay_audit (
                    replay_id, outbox_id, account_id, actor_id, reason,
                    previous_attempts
                ) VALUES ($1, $2, $3, 'governance-test', 'account lifecycle', 8)
                """,
                uuid.uuid4(),
                outbox_id,
                current_account,
            )
            await connection.execute(
                """
                INSERT INTO person_entities (
                    person_id, account_id, canonical_key, display_name,
                    relationship_to_owner, source_event_id, created_at
                ) VALUES ($1, $2, $3, '家人', 'family', $4, $5)
                """,
                person_id,
                current_account,
                f"family:{person_id}",
                current_event,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO relationships (
                    relationship_id, account_id, person_id, relationship_type,
                    source_event_id, valid_at
                ) VALUES ($1, $2, $3, 'family', $4, $5)
                """,
                relationship_id,
                current_account,
                person_id,
                current_event,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_cognitive_claims (
                    claim_id, account_id, claim_type, statement, context,
                    confidence, sharing_scope, status, owner_reviewed_at,
                    version, created_at, updated_at
                ) VALUES (
                    $1, $2, 'belief', '认知模型必须随账户删除', '',
                    0.9, 'private', 'confirmed', $3, 2, $3, $3
                )
                """,
                claim_id,
                current_account,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_cognitive_claim_sources (
                    claim_id, account_id, source_event_id, relation,
                    adopted, negative, occurred_at
                ) VALUES ($1, $2, $3, 'support', true, false, $4)
                """,
                claim_id,
                current_account,
                current_event,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_decision_cases (
                    case_id, account_id, kind, context, options, constraints,
                    chosen_option, rejected_options, outcome, reflection,
                    still_endorsed, sharing_scope, status, owner_reviewed_at,
                    version, created_at, updated_at
                ) VALUES (
                    $1, $2, 'real', '是否完整删除派生模型',
                    '["完整删除","只隐藏"]'::jsonb, '["必须可验证"]'::jsonb,
                    '完整删除', '["只隐藏"]'::jsonb, '等待验证',
                    '所有派生数据都要进入治理闭环', true, 'private',
                    'confirmed', $3, 2, $3, $3
                )
                """,
                decision_id,
                current_account,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_decision_case_sources (
                    case_id, account_id, source_event_id, relation,
                    adopted, negative, occurred_at
                ) VALUES ($1, $2, $3, 'support', true, false, $4)
                """,
                decision_id,
                current_account,
                current_event,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_relationship_profiles (
                    profile_id, account_id, version_number, person_id,
                    relationship_id, salutation, tone, advice_style,
                    sharing_scope, boundaries, status, owner_reviewed_at,
                    step_up_verified, created_at
                ) VALUES (
                    $1, $2, 1, $3, $4, '家人', '温和坦诚',
                    '先听完再建议', 'private',
                    '["不透露第三方私密内容"]'::jsonb, 'approved',
                    $5, true, $5
                )
                """,
                relationship_profile_id,
                current_account,
                person_id,
                relationship_id,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_relationship_profile_sources (
                    profile_id, profile_version, account_id, source_event_id,
                    relation, adopted, negative, occurred_at
                ) VALUES ($1, 1, $2, $3, 'support', true, false, $4)
                """,
                relationship_profile_id,
                current_account,
                current_event,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_audit_events (
                    event_id, account_id, actor_account_id, transaction_id,
                    action, target_kind, target_id, details, occurred_at
                ) VALUES (
                    $1, $2, $2, $3, 'create_claim', 'cognitive_claim',
                    $4, '{}'::jsonb, $5
                )
                """,
                uuid.uuid4(),
                current_account,
                uuid.uuid4(),
                claim_id,
                datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO self_model_command_receipts (
                    account_id, idempotency_key, command_type, payload_sha256,
                    result_kind, result_id, result_version, created_at
                ) VALUES (
                    $1, 'governance-create-claim', 'create_cognitive_claim',
                    $2, 'cognitive_claim', $3, 2, $4
                )
                """,
                current_account,
                "e" * 64,
                claim_id,
                datetime.now(UTC),
            )
        await connection.execute(
            """
            INSERT INTO archive_evidence_blobs (
                blob_id, account_id, evidence_event_id, object_key, media_type,
                byte_count, content_sha256, encryption_key_version, retention_policy
            ) VALUES ($1, $2, $3, $4, 'audio/wav', 5, $5, 'archive-v1', 'account')
            """,
            uuid.uuid4(),
            account_id,
            event_id,
            "archive/test-object.fernet",
            "b" * 64,
        )
        await connection.execute(
            """
            INSERT INTO persona_traits (
                trait_id, account_id, category, normalized_key, description,
                context, confidence
            ) VALUES ($1, $2, 'expression', 'direct', '表达直接', 'daily', 0.8)
            """,
            uuid.uuid4(),
            account_id,
        )
        await connection.execute(
            """
            INSERT INTO voice_clone_consents (
                account_id, policy_version, granted_at, grant_event_id
            ) VALUES ($1, 'voice-v1', $2, $3)
            """,
            account_id,
            datetime.now(UTC),
            event_id,
        )
        await connection.execute(
            """
            INSERT INTO voice_samples (
                sample_id, account_id, object_key, media_type, byte_count,
                content_sha256, encryption_key_version, object_backend,
                duration_ms, sample_rate, created_at
            ) VALUES ($1, $2, 'voice/test.fernet', 'audio/wav', 5, $3,
                      'voice-v1', 's3', 1000, 24000, $4)
            """,
            voice_sample,
            account_id,
            "c" * 64,
            datetime.now(UTC),
        )
        await connection.execute(
            """
            INSERT INTO voice_profiles (
                profile_id, account_id, sample_id, version_number, provider,
                provider_region, target_model, provider_voice_id, status,
                created_at, updated_at
            ) VALUES ($1, $2, $3, 1, 'cosyvoice', 'cn-beijing',
                      'cosyvoice-v3.5-flash', 'provider-secret-id', 'candidate', $4, $4)
            """,
            voice_profile,
            account_id,
            voice_sample,
            datetime.now(UTC),
        )
        await connection.execute(
            """
            INSERT INTO speaker_identities (
                identity_id, account_id, identity_type, label
            ) VALUES ($1, $2, 'owner', 'owner')
            """,
            speaker_identity,
            account_id,
        )
        await connection.execute(
            """
            INSERT INTO speaker_profiles (
                profile_id, account_id, identity_id, model_version,
                template_version, template_ciphertext, owner_threshold,
                guest_threshold, consent_grant_id, status
            ) VALUES ($1, $2, $3, 'campplus-v1', 1, $4, 0.8, 0.4,
                      'speaker-consent', 'shadow')
            """,
            speaker_profile,
            account_id,
            speaker_identity,
            b"encrypted-template",
        )
        await connection.execute(
            """
            INSERT INTO speaker_enrollment_samples (
                sample_id, profile_id, account_id, content_sha256, speech_ms,
                snr_db, quality_score, replay_risk, synthetic_risk, device, scene
            ) VALUES ($1, $2, $3, $4, 1800, 20, 0.95, 0.01, 0.01,
                      'phone', 'quiet')
            """,
            speaker_sample,
            speaker_profile,
            account_id,
            "d" * 64,
        )
        await connection.execute(
            """
            INSERT INTO speaker_enrollment_intents (
                intent_id, account_id, consent_policy_version, state,
                created_at, expires_at
            ) VALUES ($1, $2, 'speaker-consent-v1', 'requested', $3, $4)
            """,
            f"intent-{uuid.uuid4()}",
            account_id,
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
        )

        archive = PostgresAccountRepository.archive(dsn)
        speaker = PostgresAccountRepository.speaker(dsn)
        exported = await archive.export_account(account_id)
        speaker_export = await speaker.export_account(account_id)
        serialized = str({"archive": exported, "speaker": speaker_export})

        assert "postgres governance" in serialized
        assert "表达直接" in serialized
        assert "认知模型必须随账户删除" in serialized
        assert "是否完整删除派生模型" in serialized
        assert "温和坦诚" in serialized
        assert "self_model_command_receipts" in serialized
        assert "archive_outbox_replay_audit" in serialized
        assert "account lifecycle" in serialized
        assert "provider_voice_id" not in serialized
        assert "template_ciphertext" not in serialized
        assert "speaker_enrollment_intents" in serialized
        references = await archive.object_references(account_id)
        assert [reference.object_key for reference in references] == ["archive/test-object.fernet"]

        await archive.delete_account(account_id)
        await speaker.delete_account(account_id)

        assert await archive.remaining_account_rows(account_id) == {}
        assert await speaker.remaining_account_rows(account_id) == {}
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
                account_id,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM speaker_profiles WHERE account_id = $1",
                account_id,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
                other_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM archive_outbox_replay_audit WHERE account_id = $1",
                account_id,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM archive_outbox_replay_audit WHERE account_id = $1",
                other_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM self_model_cognitive_claims WHERE account_id = $1",
                account_id,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM self_model_cognitive_claims WHERE account_id = $1",
                other_id,
            )
            == 1
        )
    finally:
        await connection.execute(
            "SELECT set_config('app.self_model_delete', 'true', false)"
        )
        for table in (
            "self_model_cognitive_claim_sources",
            "self_model_decision_case_sources",
            "self_model_relationship_profile_sources",
            "self_model_command_receipts",
            "self_model_audit_events",
            "self_model_relationship_profiles",
            "self_model_decision_cases",
            "self_model_cognitive_claims",
        ):
            await connection.execute(
                f"DELETE FROM {table} WHERE account_id = ANY($1::text[])",
                [account_id, other_id],
            )
        await connection.execute(
            "DELETE FROM relationships WHERE account_id = ANY($1::text[])",
            [account_id, other_id],
        )
        await connection.execute(
            "DELETE FROM person_entities WHERE account_id = ANY($1::text[])",
            [account_id, other_id],
        )
        await connection.execute(
            "DELETE FROM speaker_identities WHERE account_id = ANY($1::text[])",
            [account_id, other_id],
        )
        await connection.execute(
            "DELETE FROM archive_evidence_events WHERE account_id = ANY($1::text[])",
            [account_id, other_id],
        )
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL deletion saga",
)
async def test_postgres_deletion_saga_removes_rows_objects_and_provider_voice(
    tmp_path: Path,
) -> None:
    """The whole account-deletion saga on PostgreSQL, not one repository alone.

    The repository contract is covered above; this pins the saga itself, where
    the PostgreSQL store is wired in: provider voice, encrypted objects, the
    archive/speaker row order, the evolution tombstone and the operator receipt.
    """

    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"governance-saga-{uuid.uuid4()}"
    event_id = f"event-{uuid.uuid4()}"
    suffix = uuid.uuid4().hex[:8]
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    store.register_account(
        user_id=account_id,
        username=f"saga-{suffix}",
        username_normalized=f"saga-{suffix}",
        password_hash=hash_password("safe-passphrase"),
        now=datetime.now(UTC).isoformat(),
    )
    PostgresEvolutionStore(dsn).initialize()
    provider = VoiceProviderStub()
    session_terminator = SessionTerminatorStub()
    archive_objects = EncryptedLocalObjectStore(
        root=tmp_path / "archive-objects",
        key=Fernet.generate_key().decode("ascii"),
        key_version="archive-key-v1",
    )
    voice_objects = EncryptedLocalObjectStore(
        root=tmp_path / "voice-objects",
        key=Fernet.generate_key().decode("ascii"),
        key_version="voice-key-v1",
    )
    voice = PostgresVoiceProfileManager(
        dsn,
        object_store=voice_objects,
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://control.test/samples/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await voice.initialize()
    await voice.grant_consent(account_id=account_id, policy_version="voice-clone-v1")
    enrollment = await voice.enroll(
        VoiceEnrollmentRequest(
            account_id=account_id,
            audio=voice_sample_wav(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    assert enrollment.provider_voice_id

    archive_reference = await archive_objects.put(
        account_id=account_id,
        purpose="source-audio",
        data=b"archive-audio",
        media_type="audio/wav",
    )
    speaker_identity = uuid.uuid4()
    speaker_profile = uuid.uuid4()
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(_SPEAKER_SCHEMA.read_text(encoding="utf-8"))
        await connection.execute(
            """
            INSERT INTO archive_evidence_events (
                event_id, account_id, event_type, schema_version, occurred_at,
                speaker_class, source, payload, content_sha256
            ) VALUES ($1, $2, 'speech.utterance_finalized', 1, $3, 'owner',
                      'test', $4::jsonb, $5)
            """,
            event_id,
            account_id,
            datetime.now(UTC),
            '{"text":"postgres deletion saga"}',
            "b" * 64,
        )
        await connection.execute(
            """
            INSERT INTO archive_evidence_blobs (
                blob_id, account_id, evidence_event_id, object_key, media_type,
                byte_count, content_sha256, encryption_key_version,
                retention_policy
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'account-lifetime')
            """,
            uuid.uuid4(),
            account_id,
            event_id,
            archive_reference.object_key,
            archive_reference.media_type,
            archive_reference.byte_count,
            archive_reference.content_sha256,
            archive_reference.encryption_key_version,
        )
        await connection.execute(
            """
            INSERT INTO speaker_identities (
                identity_id, account_id, identity_type, label
            ) VALUES ($1, $2, 'owner', 'owner')
            """,
            speaker_identity,
            account_id,
        )
        await connection.execute(
            """
            INSERT INTO speaker_profiles (
                profile_id, account_id, identity_id, model_version,
                template_version, template_ciphertext, owner_threshold,
                guest_threshold, consent_grant_id, status
            ) VALUES ($1, $2, $3, 'campplus-v1', 1, $4, 0.8, 0.4,
                      'speaker-consent', 'shadow')
            """,
            speaker_profile,
            account_id,
            speaker_identity,
            b"encrypted-template",
        )
    finally:
        await connection.close()

    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=PostgresAccountRepository.archive(dsn),
        speaker_repository=PostgresAccountRepository.speaker(dsn),
        evolution_repository=PostgresEvolutionAccountRepository(dsn),
        voice_profiles=voice,
        archive_object_store=archive_objects,
        session_terminator=session_terminator,
    )
    report = await governance.delete_account(account_id)

    assert report["status"] == "completed"
    assert report["terminate_sessions"] is True
    assert session_terminator.accounts == [account_id]
    assert provider.deleted == [enrollment.provider_voice_id]
    assert store.get_account(user_id=account_id) is None
    assert store.is_account_deleted(user_id=account_id) is True
    receipt = store.get_account_deletion_receipt(request_id=str(report["request_id"]))
    assert receipt is not None
    assert receipt["status"] == "completed"
    assert account_id not in str(receipt)
    with pytest.raises(FileNotFoundError):
        await archive_objects.get(archive_reference)
    assert [path for path in (tmp_path / "voice-objects").rglob("*") if path.is_file()] == []

    connection = await asyncpg.connect(dsn)
    try:
        for table in (
            "archive_evidence_events",
            "archive_evidence_blobs",
            "speaker_profiles",
            "speaker_identities",
            "voice_profiles",
            "voice_samples",
        ):
            remaining = await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE account_id = $1", account_id
            )
            assert remaining == 0, table
        await connection.execute(
            "DELETE FROM evolution_account_deletion_fences WHERE account_id = $1",
            account_id,
        )
    finally:
        await connection.close()
    archive_repository = PostgresAccountRepository.archive(dsn)
    speaker_repository = PostgresAccountRepository.speaker(dsn)
    assert await archive_repository.remaining_account_rows(account_id) == {}
    assert await speaker_repository.remaining_account_rows(account_id) == {}

    # Idempotent: the completed row is reused and nothing is deleted twice.
    again = await governance.delete_account(account_id)
    assert again["status"] == "completed"
    assert again["request_id"] == report["request_id"]
    assert provider.deleted == [enrollment.provider_voice_id]
