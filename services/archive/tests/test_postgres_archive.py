from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    MemoryReview,
    RawVoiceConsentRequiredError,
)
from services.archive.migration import migrate_legacy_sqlite_to_postgres
from services.archive.object_store import ObjectRef
from services.archive.postgres_archive import PostgresLifeArchive


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL contract test",
)
async def test_postgres_archive_matches_the_idempotent_public_contract() -> None:
    archive = PostgresLifeArchive(os.environ["MEMORIA_TEST_POSTGRES_DSN"])
    await archive.initialize()
    connection = await asyncpg.connect(os.environ["MEMORIA_TEST_POSTGRES_DSN"])
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        "postgres-contract-account",
    )
    await connection.close()
    event = EvidenceEvent(
        event_id="postgres-contract-event",
        account_id="postgres-contract-account",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
        speaker_class="owner",
        source="contract-test",
        payload={"text": "PostgreSQL 合同测试"},
    )

    first = await archive.record(event)
    duplicate = await archive.record(event)
    await archive.record(
        EvidenceEvent(
            event_id="postgres-contract-legacy-guest",
            account_id="postgres-contract-account",
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 9, 0, 30, tzinfo=UTC),
            speaker_class="guest",
            source="legacy-import",
            payload={"text": "不得出现在主人长期 context"},
        )
    )
    reviewed = await archive.review(
        MemoryReview(
            review_event_id="postgres-contract-correction",
            account_id="postgres-contract-account",
            target_id="postgres-contract-event",
            action="correct",
            corrected_text="PostgreSQL 更正合同测试",
            occurred_at=datetime(2026, 7, 19, 9, 1, tzinfo=UTC),
        )
    )
    context = await archive.context(
        ContextQuery(account_id="postgres-contract-account", speaker_class="owner")
    )

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.outbox_id == first.outbox_id
    assert reviewed.current_text == "PostgreSQL 更正合同测试"
    assert {item.event_id for item in context.evidence} == {
        "postgres-contract-event",
        "postgres-contract-correction",
    }
    connection = await asyncpg.connect(os.environ["MEMORIA_TEST_POSTGRES_DSN"])
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        "postgres-contract-account",
    )
    await connection.close()
    await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL raw voice contract",
)
async def test_postgres_raw_voice_consent_and_blob_match_sqlite_contract() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    account_id = "postgres-raw-voice-account"
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        account_id,
    )
    await connection.execute(
        "DELETE FROM archive_consent_grants WHERE account_id = $1",
        account_id,
    )
    await connection.close()
    with pytest.raises(ValueError, match="timezone"):
        await archive.grant_raw_voice_consent(
            account_id=account_id,
            policy_version="raw-voice-v1",
            retention_policy="account_lifetime",
            granted_at=datetime(2026, 7, 19, 9, 0),
        )
    consent = await archive.grant_raw_voice_consent(
        account_id=account_id,
        policy_version="raw-voice-v1",
        retention_policy="account_lifetime",
        granted_at=datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
    )
    event = EvidenceEvent(
        event_id="postgres-raw-voice-event",
        account_id=account_id,
        session_id="postgres-raw-session",
        turn_id=1,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 9, 1, tzinfo=UTC),
        speaker_class="owner",
        source="raw-voice-contract",
        consent_grant_id=consent.consent_grant_id,
        payload={"text": "PostgreSQL 原始声音合同。"},
    )
    reference = ObjectRef(
        account_id=account_id,
        object_key="account/raw-voice/postgres.fernet",
        media_type="audio/wav",
        byte_count=32044,
        content_sha256="b" * 64,
        encryption_key_version="archive-v1",
        backend="archive",
    )

    first = await archive.record_with_blob(
        event,
        reference,
        retention_policy="account_lifetime",
    )
    event_after_blob = await archive.record(event)
    retry = await archive.record_with_blob(
        event,
        reference,
        retention_policy="account_lifetime",
    )
    assert await archive.raw_voice_blobs(account_id=account_id) == (reference,)
    replacement_consent = await archive.grant_raw_voice_consent(
        account_id=account_id,
        policy_version="raw-voice-v2",
        retention_policy="account_lifetime",
        granted_at=datetime(2026, 7, 19, 9, 1, 30, tzinfo=UTC),
    )
    replacement_event = EvidenceEvent(
        event_id="postgres-raw-voice-event-v2",
        account_id=account_id,
        session_id="postgres-raw-session",
        turn_id=2,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 9, 1, 30, tzinfo=UTC),
        speaker_class="owner",
        source="raw-voice-contract",
        consent_grant_id=replacement_consent.consent_grant_id,
        payload={"text": "PostgreSQL 新授权版本。"},
    )
    replacement_reference = ObjectRef(
        account_id=account_id,
        object_key="account/raw-voice/postgres-v2.fernet",
        media_type="audio/wav",
        byte_count=16044,
        content_sha256="c" * 64,
        encryption_key_version="archive-v1",
        backend="archive",
    )
    event_before_blob = await archive.record(replacement_event)
    blob_after_event = await archive.record_with_blob(
        replacement_event,
        replacement_reference,
        retention_policy="account_lifetime",
    )
    assert event_before_blob.duplicate is False
    assert event_before_blob.blob_duplicate is None
    assert blob_after_event.duplicate is True
    assert blob_after_event.blob_duplicate is False
    assert blob_after_event.retained_object_key == replacement_reference.object_key
    assert await archive.raw_voice_blobs(account_id=account_id) == (
        reference,
        replacement_reference,
    )
    with pytest.raises(ValueError, match="timezone"):
        await archive.revoke_raw_voice_consent(
            account_id=account_id,
            revoked_at=datetime(2026, 7, 19, 9, 2),
        )
    revocation = await archive.revoke_raw_voice_consent(
        account_id=account_id,
        revoked_at=datetime(2026, 7, 19, 9, 2, tzinfo=UTC),
    )

    assert first.duplicate is False
    assert first.blob_duplicate is False
    assert first.retained_object_key == reference.object_key
    assert event_after_blob.duplicate is True
    assert event_after_blob.blob_duplicate is None
    assert event_after_blob.retained_object_key is None
    assert retry.duplicate is True
    assert retry.blob_duplicate is True
    assert retry.retained_object_key == reference.object_key
    assert revocation.consent.revoked_at is not None
    assert revocation.references == (reference, replacement_reference)
    assert await archive.raw_voice_blobs(account_id=account_id) == (
        reference,
        replacement_reference,
    )
    await archive.purge_raw_voice_blobs(
        account_id=account_id,
        object_keys=tuple(item.object_key for item in revocation.references),
    )
    assert await archive.raw_voice_blobs(account_id=account_id) == ()
    assert [
        item.event_id
        for item in (
            await archive.context(ContextQuery(account_id=account_id, speaker_class="owner"))
        ).evidence
    ] == ["postgres-raw-voice-event-v2", "postgres-raw-voice-event"]
    retry_revocation = await archive.revoke_raw_voice_consent(
        account_id=account_id,
        revoked_at=datetime(2026, 7, 19, 9, 2, tzinfo=UTC),
    )
    assert retry_revocation.consent == revocation.consent
    assert retry_revocation.references == ()
    with pytest.raises(RawVoiceConsentRequiredError):
        await archive.record_with_blob(
            EvidenceEvent(
                event_id="postgres-late-raw-voice-event",
                account_id=account_id,
                session_id="postgres-raw-session",
                turn_id=2,
                event_type="speech.utterance_finalized",
                occurred_at=datetime(2026, 7, 19, 9, 3, tzinfo=UTC),
                speaker_class="owner",
                source="raw-voice-contract",
                consent_grant_id=consent.consent_grant_id,
                payload={"text": "撤销后拒绝。"},
            ),
            reference,
            retention_policy="account_lifetime",
        )
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        account_id,
    )
    await connection.execute(
        "DELETE FROM archive_consent_grants WHERE account_id = $1",
        account_id,
    )
    await connection.close()
    await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL raw voice contract",
)
async def test_postgres_raw_voice_consent_expires_and_grants_are_concurrency_safe() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    expired_account = "postgres-expired-raw-consent"
    concurrent_account = "postgres-concurrent-raw-consent"
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM archive_consent_grants WHERE account_id = ANY($1::text[])",
        [expired_account, concurrent_account],
    )
    await connection.execute(
        """
        INSERT INTO archive_consent_grants (
            consent_grant_id, account_id, purpose, policy_version,
            retention_policy, granted_at, expires_at
        ) VALUES ($1, $2, 'raw_voice_archive', $3, 'account_lifetime', $4, $5)
        """,
        "postgres-expired-consent",
        expired_account,
        "raw-voice-v1",
        datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
        datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
    )
    await connection.close()

    assert await archive.active_raw_voice_consent(account_id=expired_account) is None
    replacement = await archive.grant_raw_voice_consent(
        account_id=expired_account,
        policy_version="raw-voice-v1",
        retention_policy="account_lifetime",
        granted_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
    )
    grants = await asyncio.gather(
        *(
            archive.grant_raw_voice_consent(
                account_id=concurrent_account,
                policy_version="raw-voice-v1",
                retention_policy="account_lifetime",
                granted_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
            )
            for _ in range(8)
        )
    )

    assert replacement.consent_grant_id != "postgres-expired-consent"
    assert len({grant.consent_grant_id for grant in grants}) == 1
    assert await archive.active_raw_voice_consent(account_id=concurrent_account) == grants[0]

    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM archive_consent_grants WHERE account_id = ANY($1::text[])",
        [expired_account, concurrent_account],
    )
    await connection.close()
    await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL migration test",
)
async def test_sqlite_to_postgres_migration_is_repeatable_and_hides_uncertain_owner_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL, emotion TEXT, local_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO messages VALUES
                (1, 'postgres-migration-account', 'user', '迁移到 PostgreSQL。', NULL,
                 '2026-07-19', '2026-07-19T10:00:00Z');
            """
        )
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        "postgres-migration-account",
    )
    await connection.close()

    first = await migrate_legacy_sqlite_to_postgres(source, archive)
    second = await migrate_legacy_sqlite_to_postgres(source, archive)

    assert (first.inserted, first.duplicates) == (1, 0)
    assert (second.inserted, second.duplicates) == (0, 1)
    context = await archive.context(
        ContextQuery(account_id="postgres-migration-account", speaker_class="owner")
    )
    assert context.evidence == ()
    await archive.close()
