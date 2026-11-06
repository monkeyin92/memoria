"""Guardian PostgreSQL schema and guardian-scope isolation contract tests.

Covers the RLS policy text, the corpus consent fence, and the core tables'
per-guardian and per-minor isolation."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import (
    UTC,
    datetime,
    timedelta,
)

import asyncpg
import pytest
from services.archive.object_store import ObjectRef
from services.guardian.corpus import (
    CorpusConsentInactiveError,
    CorpusSample,
)
from services.guardian.domain import ConsentRecord
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.tests.guardian_postgres_support import (
    _ensure_roles,
    _postgres_dsn,
    _role_dsn,
)


def test_guardian_core_policies_require_actor_and_subject_context() -> None:
    schema = (
        __import__("pathlib").Path(__file__).parents[1]
        .joinpath("postgres_schema.sql")
        .read_text(encoding="utf-8")
    )
    for _table, policy_name in (
        ("guardian_links", "guardian_controller_links"),
        ("guardian_consents", "guardian_controller_consents"),
        ("guardian_corpus_samples", "guardian_controller_corpus_samples"),
        ("guardian_crisis_events", "guardian_controller_crisis_events"),
        (
            "guardian_notification_outbox",
            "guardian_controller_notification_outbox",
        ),
        (
            "guardian_notification_reads",
            "guardian_controller_notification_reads",
        ),
        (
            "guardian_push_subscriptions",
            "guardian_controller_push_subscriptions",
        ),
        (
            "guardian_push_subscription_events",
            "guardian_controller_push_subscription_events",
        ),
    ):
        policy_start = schema.index(f"CREATE POLICY {policy_name}")
        policy_end = schema.find(";", policy_start)
        policy = schema[policy_start:policy_end]
        assert "USING (true)" not in policy
        assert "WITH CHECK (true)" not in policy
        assert "memoria.guardian_actor_id" in policy
        assert "memoria.guardian_subject_id" in policy


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
)
async def test_postgres_guardian_schema_and_corpus_consent_fence() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_guardian_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    role_created, maintenance_role_created, worker_role_created = False, False, False
    store: PostgresGuardianStore | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        (
            role_created,
            maintenance_role_created,
            worker_role_created,
        ) = await _ensure_roles(admin, database=database)
        store = PostgresGuardianStore(
            dsn=_role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian",
                password="api-role-password",
            ),
            bootstrap_dsn=dsn,
            maintenance_dsn=_role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_maintenance",
                password="maintenance-role-password",
            ),
            worker_dsn=_role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_worker",
                password="worker-role-password",
            ),
        )
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
            ),
            actor_user_id="postgres-guardian",
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

        # Revocation purges the sample: the maintenance role marks it deleted
        # (the corpus retention service, both on revoke and on expiry).
        deleted = await store.mark_corpus_sample_deleted(
            sample_id=sample.sample_id, deleted_at=now + timedelta(minutes=3)
        )
        assert deleted.deleted_at is not None

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
                        "guardian_person_consents",
                        "guardian_corpus_samples",
                        "tutor_practice_sessions",
                        "tutor_study_progress",
                        "tutor_practice_evidence",
                        "tutor_commit_outbox",
                        "guardian_crisis_events",
                        "guardian_notification_outbox",
                        "guardian_notification_reads",
                        "guardian_push_subscriptions",
                        "guardian_push_subscription_events",
                    ],
                )
            }
            policy_names = {
                (str(row["tablename"]), str(row["policyname"]))
                for row in await connection.fetch(
                    """
                    SELECT tablename, policyname FROM pg_policies
                    WHERE roles @> ARRAY['memoria_guardian']::name[]
                    """
                )
            }
            maintenance_owned = {
                (str(row["proname"]))
                for row in await connection.fetch(
                    """
                    SELECT p.proname
                    FROM pg_proc p
                    JOIN pg_roles r ON r.oid = p.proowner
                    WHERE p.proname LIKE 'guardian_tutor_%'
                      AND r.rolname = 'memoria_guardian_maintenance'
                    """
                )
            }
            worker_owned = {
                str(row["proname"])
                for row in await connection.fetch(
                    """
                    SELECT p.proname
                    FROM pg_proc p
                    JOIN pg_roles r ON r.oid = p.proowner
                    WHERE p.proname LIKE 'guardian_tutor_%'
                      AND r.rolname = 'memoria_guardian_worker'
                    """
                )
            }
        finally:
            await connection.close()
        assert len(forced_tables) == 13
        assert policy_names == {
            ("guardian_links", "guardian_controller_links"),
            ("guardian_links", "guardian_controller_links_insert"),
            ("guardian_links", "guardian_controller_links_update"),
            ("guardian_consents", "guardian_controller_consents"),
            ("guardian_person_consents", "guardian_controller_person_consents"),
            ("guardian_corpus_samples", "guardian_controller_corpus_samples"),
            ("tutor_practice_sessions", "guardian_controller_tutor_sessions"),
            ("tutor_study_progress", "guardian_controller_tutor_progress"),
            ("tutor_practice_evidence", "guardian_controller_tutor_evidence"),
            ("tutor_commit_outbox", "guardian_controller_tutor_outbox"),
            ("guardian_crisis_events", "guardian_controller_crisis_events"),
            (
                "guardian_notification_outbox",
                "guardian_controller_notification_outbox",
            ),
            (
                "guardian_notification_reads",
                "guardian_controller_notification_reads",
            ),
            (
                "guardian_push_subscriptions",
                "guardian_controller_push_subscriptions",
            ),
            (
                "guardian_push_subscription_events",
                "guardian_controller_push_subscription_events",
            ),
        }
        assert maintenance_owned == {
            "guardian_tutor_account_scope_export",
            "guardian_tutor_account_scope_delete",
            "guardian_tutor_account_scope_remaining",
        }
        assert worker_owned == {
            "guardian_tutor_outbox_claim",
            "guardian_tutor_outbox_mark_delivered",
            "guardian_tutor_outbox_release",
        }
    finally:
        if store is not None:
            await store.close()
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
                database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            if maintenance_role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian_maintenance")
            if worker_role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian_worker")
            if role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian")
            await admin.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
)
async def test_postgres_guardian_core_tables_are_isolated_by_guardian_and_minor_scope() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_guardian_rls_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    role_created, maintenance_role_created, worker_role_created = False, False, False
    store: PostgresGuardianStore | None = None
    api: asyncpg.Connection | None = None
    now = datetime.now(UTC)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        (
            role_created,
            maintenance_role_created,
            worker_role_created,
        ) = await _ensure_roles(admin, database=database)
        store = PostgresGuardianStore(
            dsn=_role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian",
                password="api-role-password",
            ),
            bootstrap_dsn=dsn,
            maintenance_dsn=_role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_maintenance",
                password="maintenance-role-password",
            ),
            worker_dsn=_role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_worker",
                password="worker-role-password",
            ),
        )
        await store.initialize()

        async def seed_link(*, guardian: str, minor: str, suffix: str) -> ConsentRecord:
            digest = hashlib.sha256(f"binding-{suffix}".encode()).hexdigest()
            link = await store.create_link(
                guardian_user_id=guardian,
                minor_user_id=minor,
                relation="parent",
                verified_via="wechat_identity",
                binding_code_hash=digest,
                binding_expires_at=now + timedelta(minutes=15),
                now=now,
            )
            await store.confirm_link(
                link_id=link.link_id,
                minor_user_id=minor,
                binding_code_hash=digest,
                now=now,
            )
            return await store.grant_consent(
                ConsentRecord(
                    consent_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"consent-{suffix}")),
                    link_id=link.link_id,
                    consent_kind="corpus_recording",
                    policy_version="authorized-child-corpus-v1",
                    granted_at=now,
                    expires_at=now + timedelta(days=2),
                    evidence_event_id=f"consent-{suffix}",
                ),
                actor_user_id=guardian,
            )

        consent_a = await seed_link(
            guardian="guardian-a", minor="minor-a", suffix="a"
        )
        consent_b = await seed_link(
            guardian="guardian-b", minor="minor-b", suffix="b"
        )
        for suffix, minor, consent in (
            ("a", "minor-a", consent_a),
            ("b", "minor-b", consent_b),
        ):
            await store.record_corpus_sample(
                CorpusSample(
                    sample_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sample-{suffix}")),
                    minor_user_id=minor,
                    consent_id=consent.consent_id,
                    source_event_id=f"sample-event-{suffix}",
                    reference=ObjectRef(
                        account_id=minor,
                        object_key=f"hash/authorized-child-corpus/{suffix}.fernet",
                        media_type="audio/wav",
                        byte_count=10,
                        content_sha256=(suffix * 64)[:64],
                        encryption_key_version="v1",
                        backend="s3",
                    ),
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
            await store.enqueue_crisis_event(
                crisis_event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"crisis-{suffix}")),
                evidence_event_id=f"crisis-event-{suffix}",
                minor_user_id=minor,
                occurred_at=now,
                script_version="crisis-transfer-v1",
                reply_template_id="crisis-support-reply",
            )

        api = await asyncpg.connect(
            _role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian",
                password="api-role-password",
            )
        )

        async def counts(*, actor: str | None, subject: str | None) -> dict[str, int]:
            result: dict[str, int] = {}
            async with api.transaction():
                await api.execute("SET ROLE memoria_guardian")
                if actor is not None and subject is not None:
                    await api.execute(
                        """
                        SELECT set_config('memoria.guardian_actor_id', $1, true),
                               set_config('memoria.guardian_subject_id', $2, true)
                        """,
                        actor,
                        subject,
                    )
                for table in (
                    "guardian_links",
                    "guardian_consents",
                    "guardian_corpus_samples",
                    "guardian_crisis_events",
                    "guardian_notification_outbox",
                ):
                    result[table] = int(await api.fetchval(f"SELECT count(*) FROM {table}"))
            return result

        assert await counts(actor="guardian-a", subject="guardian-a") == {
            "guardian_links": 1,
            "guardian_consents": 1,
            "guardian_corpus_samples": 0,
            "guardian_crisis_events": 1,
            "guardian_notification_outbox": 1,
        }
        assert await counts(actor="guardian-a", subject="minor-a") == {
            "guardian_links": 1,
            "guardian_consents": 1,
            "guardian_corpus_samples": 1,
            "guardian_crisis_events": 1,
            "guardian_notification_outbox": 1,
        }
        assert await counts(actor="guardian-a", subject="minor-b") == {
            "guardian_links": 0,
            "guardian_consents": 0,
            "guardian_corpus_samples": 0,
            "guardian_crisis_events": 0,
            "guardian_notification_outbox": 0,
        }
        assert await counts(actor="guardian-b", subject="minor-a") == {
            "guardian_links": 0,
            "guardian_consents": 0,
            "guardian_corpus_samples": 0,
            "guardian_crisis_events": 0,
            "guardian_notification_outbox": 0,
        }
        assert await counts(actor="minor-a", subject="minor-b") == {
            "guardian_links": 0,
            "guardian_consents": 0,
            "guardian_corpus_samples": 0,
            "guardian_crisis_events": 0,
            "guardian_notification_outbox": 0,
        }
        assert await counts(actor=None, subject=None) == {
            "guardian_links": 0,
            "guardian_consents": 0,
            "guardian_corpus_samples": 0,
            "guardian_crisis_events": 0,
            "guardian_notification_outbox": 0,
        }

        async with api.transaction():
            await api.execute("SET ROLE memoria_guardian")
            await api.execute(
                """
                SELECT set_config('memoria.guardian_actor_id', 'guardian-a', true),
                       set_config('memoria.guardian_subject_id', 'minor-b', true)
                """
            )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await api.execute(
                    """
                    INSERT INTO guardian_links(
                        link_id, guardian_user_id, minor_user_id, relation, status,
                        verified_via, binding_code_hash, binding_expires_at, created_at
                    ) VALUES (
                        $1, 'guardian-a', 'minor-a', 'parent', 'pending',
                        'wechat_identity', $2, $3, $4
                    )
                    """,
                    uuid.uuid4(),
                    "f" * 64,
                    now + timedelta(minutes=15),
                    now,
                )
    finally:
        if api is not None:
            await api.close()
        if store is not None:
            await store.close()
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
                database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            if maintenance_role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian_maintenance")
            if worker_role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian_worker")
            if role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian")
            await admin.close()

