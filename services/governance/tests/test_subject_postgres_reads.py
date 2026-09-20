"""PostgreSQL side of the four subject read exports (P2-03).

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.  Every database test builds
a throwaway database, so the shared test database is never written to.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.postgres_archive import PostgresLifeArchive
from services.digital_self.migrations import account_projection as digital_self_migration
from services.governance import subject_postgres_reads as reads
from services.governance.subject_migrations import MIGRATION_NAMES
from services.identity.migrations import durable_subject as durable_subject_migration
from services.memory_scope.migrations import legacy_archive as memory_scope_migration
from services.persona import subject_projection

_SERVICES = Path(__file__).parents[2]
_ARCHIVE_SCHEMA = _SERVICES / "archive" / "postgres_archive_schema.sql"
_PERSONA_SCHEMA = _SERVICES / "persona" / "postgres_schema.sql"
_DIGITAL_SELF_SCHEMA = _SERVICES / "digital_self" / "postgres_schema.sql"
_MEMORY_SCHEMA = _SERVICES / "memory_scope" / "postgres_schema.sql"

_SKIP_WITHOUT_DSN = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL subject read contract",
)


def _database_dsn(dsn: str, database: str) -> str:
    """Point a DSN at a throwaway database without touching the shared one."""

    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = f"{quote(parsed.username or '')}:{quote(parsed.password or '')}@"
    return urlunsplit((parsed.scheme, f"{credentials}{host}", f"/{database}", parsed.query, ""))


def _credentials_dsn(dsn: str, user: str, password: str) -> str:
    """The same database under another role's credentials."""

    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = f"{quote(user)}:{quote(password)}@"
    return urlunsplit((parsed.scheme, f"{credentials}{host}", parsed.path, "", ""))


async def _drop_database(admin_dsn: str, database: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def throwaway_postgres_dsn() -> AsyncIterator[str]:
    """A throwaway database: the shared test database is never written to."""

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_subject_read_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    try:
        yield _database_dsn(admin_dsn, database)
    finally:
        await _drop_database(admin_dsn, database)


def test_postgres_read_tables_come_from_the_seams() -> None:
    """The PostgreSQL read may not invent a second spelling of the same rows."""

    assert reads.POSTGRES_MIGRATIONS == MIGRATION_NAMES
    assert set(reads._DURABLE_EVIDENCE_TABLES) == set(
        durable_subject_migration._KNOWN_EVIDENCE_TABLES
    )
    assert [table for table, _ in reads._DIGITAL_SELF_TABLES] == [
        digital_self_migration._PROJECTED_VERSION_TABLE,
        digital_self_migration._PROJECTED_AUDIT_TABLE,
    ]
    assert reads._MEMORY_SCOPE == memory_scope_migration._MEMORY_SCOPE
    assert [reads._MEMORY_RECORDS, reads._MEMORY_STATUS_EVENTS] == list(
        memory_scope_migration._TARGET_TABLES
    )
    assert [table for table, _ in reads._PERSONA_TABLES] == [
        subject_projection.PROJECTED_TABLES[name]
        for name in subject_projection._READ_TABLES
    ]


@_SKIP_WITHOUT_DSN
async def test_postgres_read_refuses_what_it_cannot_answer() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    with pytest.raises(reads.PostgresSubjectReadError, match="PostgreSQL DSN"):
        await reads.read_subject(
            "persona", dsn="sqlite:///tmp/memoria.sqlite3", subject_id="p1", limit=10
        )
    with pytest.raises(reads.PostgresSubjectReadError, match="subject_id"):
        await reads.read_subject("persona", dsn=dsn, subject_id="   ", limit=10)
    with pytest.raises(reads.PostgresSubjectReadError, match="limit"):
        await reads.read_subject("persona", dsn=dsn, subject_id="p1", limit=0)
    with pytest.raises(reads.PostgresSubjectReadError, match="unknown migration"):
        await reads.read_subject("personas", dsn=dsn, subject_id="p1", limit=10)
    with pytest.raises(reads.PostgresSubjectReadError, match="account scope"):
        await reads.read_subject(
            "memory_scope", dsn=dsn, subject_id="p1", limit=10, account_id="a1"
        )


@_SKIP_WITHOUT_DSN
async def test_postgres_read_refuses_a_role_that_cannot_see_forced_rls_rows(
    throwaway_postgres_dsn: str,
) -> None:
    """An unreadable table must fail loudly, never answer "zero rows"."""

    dsn = throwaway_postgres_dsn
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    suffix = uuid.uuid4().hex[:10]
    role = f"memoria_subject_read_{suffix}"
    password = f"probe-{suffix}"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(
            f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' NOSUPERUSER NOBYPASSRLS'
        )
        await admin.execute(f'GRANT SELECT ON archive_evidence_events TO "{role}"')
    finally:
        await admin.close()
    try:
        probe_dsn = _credentials_dsn(dsn, role, password)
        with pytest.raises(reads.PostgresSubjectReadError, match="row-level security"):
            await reads.read_subject(
                "durable_subject", dsn=probe_dsn, subject_id="someone", limit=10
            )
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP OWNED BY "{role}"')
            await admin.execute(f'DROP ROLE "{role}"')
        finally:
            await admin.close()


async def _seed_evidence(
    connection: asyncpg.Connection,
    *,
    event_id: str,
    account_id: str,
    subject_id: str | None,
) -> None:
    await connection.execute(
        """
        INSERT INTO archive_evidence_events (
            event_id, account_id, event_type, schema_version, occurred_at,
            subject_id, speaker_class, source, payload, content_sha256
        ) VALUES ($1, $2, 'speech.utterance_finalized', 1, $3, $4, 'owner',
                  'test', $5::jsonb, $6)
        """,
        event_id,
        account_id,
        datetime.now(UTC),
        subject_id,
        '{"text":"postgres subject read"}',
        "a" * 64,
    )


async def _evidence_rows(connection: asyncpg.Connection) -> int:
    return int(await connection.fetchval("SELECT count(*) FROM archive_evidence_events"))


@_SKIP_WITHOUT_DSN
async def test_postgres_durable_subject_read_returns_only_that_subject(
    throwaway_postgres_dsn: str,
) -> None:
    dsn = throwaway_postgres_dsn
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    suffix = uuid.uuid4().hex[:8]
    account = f"account-{suffix}"
    subject = f"subject-{suffix}"
    other = f"other-{suffix}"
    admin = await asyncpg.connect(dsn)
    try:
        await _seed_evidence(
            admin, event_id="ev-subject-a", account_id=account, subject_id=subject
        )
        await _seed_evidence(
            admin, event_id="ev-subject-b", account_id=account, subject_id=subject
        )
        await _seed_evidence(
            admin, event_id="ev-foreign", account_id=account, subject_id=other
        )
        await _seed_evidence(
            admin, event_id="ev-unclaimed", account_id=account, subject_id=None
        )
        before = await _evidence_rows(admin)

        report = await reads.read_subject(
            "durable_subject", dsn=dsn, subject_id=subject, limit=10
        )

        assert report["scope"] == "durable_subject"
        assert report["engine"] == "postgresql"
        assert report["target"].startswith("postgresql://")
        assert account not in report["target"]
        assert report["journal_present"] is False
        assert report["receipts"] == {"total": 0, "by_outcome": {}, "rows": []}
        assert report["truncated"] is False
        table = report["tables"]["archive_evidence_events"]
        assert table["count"] == 2
        assert [row["event_id"] for row in table["rows"]] == [
            "ev-subject-a",
            "ev-subject-b",
        ]
        assert {row["account_id"] for row in table["rows"]} == {account}

        foreign = await reads.read_subject(
            "durable_subject", dsn=dsn, subject_id=other, limit=10
        )
        assert foreign["tables"]["archive_evidence_events"]["count"] == 1
        assert await _evidence_rows(admin) == before
    finally:
        await admin.close()


@_SKIP_WITHOUT_DSN
async def test_postgres_account_scoped_read_sees_only_that_account(
    throwaway_postgres_dsn: str,
) -> None:
    """The archive policies read the account context, so scope the read by it."""

    dsn = throwaway_postgres_dsn
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    suffix = uuid.uuid4().hex[:8]
    subject = f"subject-{suffix}"
    account_a = f"account-a-{suffix}"
    account_b = f"account-b-{suffix}"
    role = f"memoria_subject_read_{suffix}"
    password = f"probe-{suffix}"
    admin = await asyncpg.connect(dsn)
    try:
        await _seed_evidence(
            admin, event_id="ev-account-a", account_id=account_a, subject_id=subject
        )
        await _seed_evidence(
            admin, event_id="ev-account-b", account_id=account_b, subject_id=subject
        )
        await admin.execute(
            f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' NOSUPERUSER NOBYPASSRLS'
        )
        await admin.execute(f'GRANT SELECT ON archive_evidence_events TO "{role}"')
    finally:
        await admin.close()
    probe_dsn = _credentials_dsn(dsn, role, password)
    try:
        for account, expected in (
            (account_a, "ev-account-a"),
            (account_b, "ev-account-b"),
        ):
            report = await reads.read_subject(
                "durable_subject",
                dsn=probe_dsn,
                subject_id=subject,
                limit=10,
                account_id=account,
            )
            assert report["account_id"] == account
            table = report["tables"]["archive_evidence_events"]
            assert table["count"] == 1
            assert [row["event_id"] for row in table["rows"]] == [expected]
            assert {row["account_id"] for row in table["rows"]} == {account}
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP OWNED BY "{role}"')
            await admin.execute(f'DROP ROLE "{role}"')
        finally:
            await admin.close()


@_SKIP_WITHOUT_DSN
async def test_postgres_durable_subject_read_truncates_without_losing_the_count(
    throwaway_postgres_dsn: str,
) -> None:
    dsn = throwaway_postgres_dsn
    archive = PostgresLifeArchive(dsn)
    await archive.initialize()
    subject = f"subject-{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(dsn)
    try:
        for index in range(3):
            await _seed_evidence(
                admin,
                event_id=f"ev-{index}",
                account_id="account-truncated",
                subject_id=subject,
            )
        report = await reads.read_subject(
            "durable_subject", dsn=dsn, subject_id=subject, limit=2
        )
        table = report["tables"]["archive_evidence_events"]
        assert table["count"] == 3
        assert len(table["rows"]) == 2
        assert report["truncated"] is True
    finally:
        await admin.close()


async def _seed_memory_record(
    connection: asyncpg.Connection,
    *,
    record_id: str,
    subject_id: str,
    scope: str = "legacy_archive",
) -> None:
    await connection.execute(
        """
        INSERT INTO memory_records (
            record_id, scope, subject_id, resource_owner_id, family_space_id,
            policy_receipt_id, consent_snapshot_id, memory_type, confidence,
            retention, payload, created_by_actor_id, created_at
        ) VALUES ($1, $2, $3, $3, NULL, $6, $7, 'semantic',
                  0.9, 'indefinite', $4::jsonb, $3, $5)
        """,
        record_id,
        scope,
        subject_id,
        '{"title":"postgres subject read"}',
        datetime.now(UTC),
        f"receipt-{record_id}",
        f"consent-{record_id}",
    )
    await connection.execute(
        """
        INSERT INTO memory_status_events (
            event_id, record_id, status, reason_code, created_at
        ) VALUES ($1, $2, 'confirmed', 'test_confirmed', $3)
        """,
        f"{record_id}-status",
        record_id,
        datetime.now(UTC),
    )


@_SKIP_WITHOUT_DSN
async def test_postgres_memory_scope_read_returns_only_the_subject_legacy_rows(
    throwaway_postgres_dsn: str,
) -> None:
    dsn = throwaway_postgres_dsn
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(_MEMORY_SCHEMA.read_text(encoding="utf-8"))
        suffix = uuid.uuid4().hex[:8]
        subject = f"subject-{suffix}"
        await _seed_memory_record(
            admin, record_id=f"record-{suffix}", subject_id=subject
        )
        await _seed_memory_record(
            admin,
            record_id=f"record-other-{suffix}",
            subject_id=f"other-{suffix}",
        )
        await _seed_memory_record(
            admin,
            record_id=f"record-private-{suffix}",
            subject_id=subject,
            scope="personal_private",
        )

        report = await reads.read_subject(
            "memory_scope", dsn=dsn, subject_id=subject, limit=10
        )

        assert report["scope"] == "legacy_archive"
        assert report["engine"] == "postgresql"
        assert report["target_present"] is True
        assert report["truncated"] is False
        assert report["records"]["count"] == 1
        record = report["records"]["rows"][0]
        assert record["record_id"] == f"record-{suffix}"
        assert record["scope"] == "legacy_archive"
        assert record["subject_id"] == subject
        assert record["payload"] == {"title": "postgres subject read"}
        assert record["co_subject_ids"] == []
        assert report["status_events"]["count"] == 1
        assert report["status_events"]["rows"][0]["status"] == "confirmed"

        foreign = await reads.read_subject(
            "memory_scope", dsn=dsn, subject_id=f"nobody-{suffix}", limit=10
        )
        assert foreign["records"] == {"count": 0, "rows": []}
        assert foreign["status_events"] == {"count": 0, "rows": []}
    finally:
        await admin.close()


@_SKIP_WITHOUT_DSN
async def test_postgres_projection_reads_never_fall_back_to_account_keyed_rows(
    throwaway_postgres_dsn: str,
) -> None:
    """No PostgreSQL projection exists yet, and the account rows stay private."""

    dsn = throwaway_postgres_dsn
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(_ARCHIVE_SCHEMA.read_text(encoding="utf-8"))
        await admin.execute(_PERSONA_SCHEMA.read_text(encoding="utf-8"))
        await admin.execute(_DIGITAL_SELF_SCHEMA.read_text(encoding="utf-8"))
        suffix = uuid.uuid4().hex[:8]
        account = f"account-{suffix}"
        await admin.execute(
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason, snapshot
            ) VALUES ($1, $2, 1, 'active', 'test', '[]'::jsonb)
            """,
            uuid.uuid4(),
            account,
        )
        await admin.execute(
            """
            INSERT INTO digital_self_versions (
                version_id, account_id, version_number, status, manifest_json,
                manifest_sha256, source_summary_sha256
            ) VALUES ($1, $2, 1, 'draft', '{}', $3, $3)
            """,
            uuid.uuid4(),
            account,
            "a" * 64,
        )

        for migration, scope in (
            ("persona", "persona_subject_projection"),
            ("digital_self", "digital_self_subject_projection"),
        ):
            report = await reads.read_subject(
                migration, dsn=dsn, subject_id=account, limit=10
            )
            assert report["scope"] == scope
            assert report["projection_present"] is False
            assert report["reason"] == "projection_missing"
            assert report["tables"] == {}
            assert report["truncated"] is False

        # The account-keyed rows are still there: nothing was read or written.
        assert await admin.fetchval(
            "SELECT count(*) FROM persona_versions WHERE account_id = $1", account
        ) == 1
        assert await admin.fetchval(
            "SELECT count(*) FROM digital_self_versions WHERE account_id = $1", account
        ) == 1
    finally:
        await admin.close()
