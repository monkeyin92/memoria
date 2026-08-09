from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.object_store import ObjectRef
from services.guardian.corpus import CorpusConsentInactiveError, CorpusSample
from services.guardian.domain import ConsentRecord
from services.guardian.postgres_store import PostgresGuardianStore


def _postgres_dsn(dsn: str, *, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(parsed.username or "postgres")
    password = quote(parsed.password or "")
    credentials = f"{username}:{password}" if password else username
    return urlunsplit(
        (
            parsed.scheme,
            f"{credentials}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
)
async def test_postgres_guardian_schema_and_corpus_consent_fence() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_guardian_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    role_created = False
    store: PostgresGuardianStore | None = None
    try:
        role_exists = bool(
            await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian'"
            )
        )
        if not role_exists:
            await admin.execute("CREATE ROLE memoria_guardian NOLOGIN NOBYPASSRLS")
            role_created = True
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        store = PostgresGuardianStore(dsn)
        await store.initialize()

        now = datetime.now(UTC)
        digest = hashlib.sha256(b"postgres-guardian-binding").hexdigest()
        link = await store.create_link(
            guardian_user_id="postgres-guardian",
            minor_user_id="postgres-minor",
            relation="parent",
            verified_via="wechat_identity",
            binding_code_hash=digest,
            binding_expires_at=now + timedelta(minutes=15),
            now=now,
        )
        await store.confirm_link(
            link_id=link.link_id,
            minor_user_id="postgres-minor",
            binding_code_hash=digest,
            now=now,
        )
        consent = await store.grant_consent(
            ConsentRecord(
                consent_id=str(uuid.uuid4()),
                link_id=link.link_id,
                consent_kind="corpus_recording",
                policy_version="authorized-child-corpus-v1",
                granted_at=now,
                expires_at=now + timedelta(days=2),
                evidence_event_id="postgres-corpus-consent",
            )
        )
        sample = CorpusSample(
            sample_id=str(uuid.uuid4()),
            minor_user_id="postgres-minor",
            consent_id=consent.consent_id,
            source_event_id="postgres-corpus-event",
            reference=ObjectRef(
                account_id="postgres-minor",
                object_key="hash/authorized-child-corpus/postgres-sample.fernet",
                media_type="audio/wav",
                byte_count=10,
                content_sha256="a" * 64,
                encryption_key_version="v1",
                backend="s3",
            ),
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        assert await store.record_corpus_sample(sample) == sample

        await store.revoke_consent(
            consent_id=consent.consent_id,
            guardian_user_id="postgres-guardian",
            revoked_at=now + timedelta(minutes=1),
            revocation_evidence_event_id="postgres-corpus-consent-revoked",
        )
        with pytest.raises(CorpusConsentInactiveError):
            await store.record_corpus_sample(
                CorpusSample(
                    sample_id=str(uuid.uuid4()),
                    minor_user_id="postgres-minor",
                    consent_id=consent.consent_id,
                    source_event_id="postgres-corpus-event-after-revoke",
                    reference=ObjectRef(
                        account_id="postgres-minor",
                        object_key=(
                            "hash/authorized-child-corpus/postgres-after-revoke.fernet"
                        ),
                        media_type="audio/wav",
                        byte_count=10,
                        content_sha256="b" * 64,
                        encryption_key_version="v1",
                        backend="s3",
                    ),
                    created_at=now + timedelta(minutes=2),
                    expires_at=now + timedelta(days=1),
                )
            )

        connection = await asyncpg.connect(dsn)
        try:
            forced_tables = {
                str(row["relname"])
                for row in await connection.fetch(
                    """
                    SELECT relname FROM pg_class
                    WHERE relname = ANY($1::text[]) AND relforcerowsecurity
                    """,
                    [
                        "guardian_links",
                        "guardian_consents",
                        "guardian_corpus_samples",
                        "tutor_practice_sessions",
                        "tutor_study_progress",
                        "guardian_crisis_events",
                        "guardian_notification_outbox",
                    ],
                )
            }
            policy_count = int(
                await connection.fetchval(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE roles @> ARRAY['memoria_guardian']::name[]"
                )
            )
        finally:
            await connection.close()
        assert len(forced_tables) == 7
        assert policy_count == 7
    finally:
        if store is not None:
            await store.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        if role_created:
            await admin.execute("DROP ROLE IF EXISTS memoria_guardian")
        await admin.close()
