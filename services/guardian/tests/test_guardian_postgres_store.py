from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.object_store import ObjectRef
from services.guardian.corpus import CorpusConsentInactiveError, CorpusSample
from services.guardian.domain import (
    ConsentRecord,
    GuardianConflictError,
    GuardianNotFoundError,
    PersonConsentRecord,
)
from services.guardian.postgres_store import PostgresGuardianStore
from services.tutor.domain import PracticeConflictError, PracticeSession, StudyProgress


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
            "guardian_push_subscriptions",
            "guardian_controller_push_subscriptions",
        ),
    ):
        policy_start = schema.index(f"CREATE POLICY {policy_name}")
        policy_end = schema.find(";", policy_start)
        policy = schema[policy_start:policy_end]
        assert "USING (true)" not in policy
        assert "WITH CHECK (true)" not in policy
        assert "memoria.guardian_actor_id" in policy
        assert "memoria.guardian_subject_id" in policy


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


def _role_dsn(
    dsn: str,
    *,
    database: str,
    role: str,
    password: str,
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{role}:{password}@{host}",
            f"/{database}",
            "",
            "",
        )
    )


async def _ensure_roles(
    admin: asyncpg.Connection,
    *,
    database: str,
) -> tuple[bool, bool, bool]:
    """Create the three dedicated roles; returns created flags."""

    created = []
    for role, password in (
        ("memoria_guardian", "api-role-password"),
        ("memoria_guardian_maintenance", "maintenance-role-password"),
        ("memoria_guardian_worker", "worker-role-password"),
    ):
        exists = bool(
            await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        )
        if not exists:
            await admin.execute(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"
            )
        created.append(not exists)
    return tuple(created)  # type: ignore[return-value]


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
                        "guardian_push_subscriptions",
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
        assert len(forced_tables) == 11
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
                "guardian_push_subscriptions",
                "guardian_controller_push_subscriptions",
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


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
)
async def test_postgres_tutor_rows_are_subject_scoped_and_rls_enforced() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_tutor_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    admin_db: asyncpg.Connection | None = None
    role_created, maintenance_role_created, worker_role_created = False, False, False
    store: PostgresGuardianStore | None = None
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
        admin_db = await asyncpg.connect(dsn)

        session = PracticeSession(
            session_id="11111111-2222-3333-4444-555555555555",
            subject_id="subject-a",
            actor_id="actor-a",
            voice_session_id="voice-pg",
            focus="tutor_english",
            task_id="english-past-story",
            status="draft",
            revision=0,
            event_ids=("11111111-2222-3333-4444-555555555555:create",),
            practiced_seconds=0,
            created_at=now,
            updated_at=now,
        )
        await store.save_practice_session(session)
        await store.save_study_progress(
            StudyProgress(
                subject_id="subject-a",
                actor_id="actor-a",
                practiced_seconds=600,
                active_days=(now.date(),),
                current_streak_days=1,
                weak_points=(("past-story", 1),),
                mastered_skills=(),
                source_event_ids=("turn-1",),
                last_practiced_at=now,
            ),
            rebuilt_at=now,
        )

        # Idempotent replay of the exact current row succeeds; a stale CAS
        # revision fails closed.
        assert await store.save_practice_session(session) == session
        with pytest.raises(PracticeConflictError, match="revision_conflict"):
            await store.save_practice_session(
                PracticeSession(
                    session_id="11111111-2222-3333-4444-555555555555",
                    subject_id="subject-a",
                    actor_id="actor-a",
                    voice_session_id="voice-pg",
                    focus="tutor_english",
                    task_id="english-past-story",
                    status="active",
                    revision=2,
                    event_ids=(
                        "11111111-2222-3333-4444-555555555555:create",
                        "11111111-2222-3333-4444-555555555555:active",
                    ),
                    practiced_seconds=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        assert await store.practice_session(
            subject_id="subject-a",
            session_id="11111111-2222-3333-4444-555555555555",
        ) is not None
        assert await store.practice_session(
            subject_id="subject-other",
            session_id="11111111-2222-3333-4444-555555555555",
        ) is None

        # RLS negative test: as the NOBYPASSRLS guardian role, a session-local
        # subject scope hides every other subject's rows, including legacy
        # rows with unknown ownership (quarantined).
        connection = await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                await connection.execute(
                    "SET LOCAL memoria.subject_id = 'subject-a'"
                )
                visible = await connection.fetchval(
                    "SELECT count(*) FROM tutor_practice_sessions"
                )
                progress_visible = await connection.fetchval(
                    "SELECT count(*) FROM tutor_study_progress"
                )
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                await connection.execute(
                    "SET LOCAL memoria.subject_id = 'subject-b'"
                )
                hidden = await connection.fetchval(
                    "SELECT count(*) FROM tutor_practice_sessions"
                )
                progress_hidden = await connection.fetchval(
                    "SELECT count(*) FROM tutor_study_progress"
                )
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                blocked = await connection.fetchval(
                    """
                    SELECT count(*) FROM tutor_practice_sessions
                    WHERE subject_id = 'subject-a'
                    """
                )
            # With neither subject scope nor maintenance role, the API role
            # sees zero rows: fail closed, not fail open.
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                unset = await connection.fetchval(
                    "SELECT count(*) FROM tutor_practice_sessions"
                )
            # The API role cannot self-open the broad account scope: the
            # policy requires current_user to BE the maintenance role.
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                await connection.execute(
                    "SELECT set_config('memoria.allow_account_scope', '1', true)"
                )
                spoofed = await connection.fetchval(
                    "SELECT count(*) FROM tutor_practice_sessions"
                )
            # Cross-subject writes are rejected by WITH CHECK under RLS.
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                await connection.execute(
                    "SELECT set_config('memoria.subject_id', 'subject-b', true)"
                )
                with pytest.raises(asyncpg.exceptions.PostgresError):
                    await connection.execute(
                        """
                        INSERT INTO tutor_practice_sessions(
                            session_id, account_id, subject_id, actor_id,
                            voice_session_id, focus, task_id, status, revision,
                            event_ids_json, practiced_seconds, created_at, updated_at
                        ) VALUES (
                            'tutor-pg-cross', 'actor-b', 'subject-a', 'actor-b',
                            'voice-pg', 'tutor_english', 'english-past-story',
                            'draft', 0, '["x"]'::jsonb, 0, now(), now()
                        )
                        """
                    )
            # The authority-column guard rejects null/blank new writes.
            async with connection.transaction():
                await connection.execute("SET ROLE memoria_guardian")
                await connection.execute(
                    "SELECT set_config('memoria.subject_id', 'subject-b', true)"
                )
                with pytest.raises(asyncpg.exceptions.PostgresError):
                    await connection.execute(
                        """
                        INSERT INTO tutor_practice_sessions(
                            session_id, account_id, subject_id, actor_id,
                            voice_session_id, focus, task_id, status, revision,
                            event_ids_json, practiced_seconds, created_at, updated_at
                        ) VALUES (
                            'tutor-pg-null', 'actor-b', NULL, 'actor-b',
                            'voice-pg', 'tutor_english', 'english-past-story',
                            'draft', 0, '["x"]'::jsonb, 0, now(), now()
                        )
                        """
                    )
        finally:
            await connection.close()
        assert visible == 1
        assert progress_visible == 1
        assert hidden == 0
        assert progress_hidden == 0
        assert blocked == 0
        assert unset == 0
        assert spoofed == 0

        # Seed one outbox row, then prove the maintenance-role worker
        # function claims it exactly once (SKIP LOCKED) and delivers it.
        assert admin_db is not None
        await admin_db.execute(
            """
            INSERT INTO tutor_commit_outbox(
                event_id, kind, subject_id, actor_id,
                archive_payload_json, status, created_at
            ) VALUES (
                'tutor-pg-evict', 'tutor.practice_turn_recorded',
                'subject-a', 'actor-a', '{"k":1}'::jsonb, 'pending', now()
            )
            """
        )
        # Evidence rows and a bystander account's rows: the account-scoped
        # governance functions must cover both tutor tables under either key
        # column and must never reach another account's rows.
        await admin_db.execute(
            """
            INSERT INTO tutor_practice_evidence(
                event_id, assessment_id, kind, subject_id, actor_id,
                envelope_json, envelope_sha256, commit_sha256,
                outcome, skill_key, session_id, session_revision, created_at
            ) VALUES
                (
                    'tutor-pg-evidence-a', NULL, 'tutor.practice_turn_recorded',
                    'subject-a', 'actor-a', '{}'::jsonb, $1, $1,
                    NULL, NULL, 'session-pg-a', 0, now()
                ),
                (
                    'tutor-pg-evidence-subject', NULL,
                    'tutor.practice_turn_recorded',
                    'actor-a', 'actor-other', '{}'::jsonb, $1, $1,
                    NULL, NULL, 'session-pg-subject', 0, now()
                ),
                (
                    'tutor-pg-evidence-b', NULL, 'tutor.practice_turn_recorded',
                    'subject-b', 'actor-b', '{}'::jsonb, $1, $1,
                    NULL, NULL, 'session-pg-b', 0, now()
                )
            """,
            "a" * 64,
        )
        await admin_db.execute(
            """
            INSERT INTO tutor_commit_outbox(
                event_id, kind, subject_id, actor_id,
                archive_payload_json, status, created_at
            ) VALUES
                (
                    'tutor-pg-outbox-subject', 'tutor.practice_turn_recorded',
                    'actor-a', 'actor-other', '{"k":3}'::jsonb, 'delivered', now()
                ),
                (
                    'tutor-pg-outbox-b', 'tutor.practice_turn_recorded',
                    'subject-b', 'actor-b', '{"k":2}'::jsonb, 'delivered', now()
                )
            """
        )
        worker = await asyncpg.connect(
            _role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_worker",
                password="worker-role-password",
            )
        )
        maintenance = await asyncpg.connect(
            _role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_maintenance",
                password="maintenance-role-password",
            )
        )
        api_role = await asyncpg.connect(
            _role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian",
                password="api-role-password",
            )
        )
        try:
            # The worker role owns the outbox functions: claim works, and a
            # concurrent worker cannot double-claim (SKIP LOCKED lease).
            claimed = await worker.fetch(
                "SELECT * FROM guardian_tutor_outbox_claim('worker-1', NULL, 10, 60)"
            )
            again = await worker.fetch(
                "SELECT * FROM guardian_tutor_outbox_claim('worker-2', NULL, 10, 60)"
            )
            await worker.execute(
                "SELECT guardian_tutor_outbox_mark_delivered($1)",
                claimed[0]["event_id"],
            )
            assert len(claimed) == 1
            assert again == []
            # The worker cannot open the account scope: no EXECUTE grant.
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await worker.fetchval(
                    "SELECT guardian_tutor_account_scope_export('actor-a')"
                )
            # The maintenance role cannot act as the outbox worker.
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await maintenance.fetch(
                    "SELECT * FROM guardian_tutor_outbox_claim('m', NULL, 10, 60)"
                )
            # The API role has no EXECUTE on either governance surface.
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await api_role.fetchval(
                    "SELECT guardian_tutor_account_scope_export('actor-a')"
                )
            with pytest.raises(asyncpg.exceptions.PostgresError):
                await api_role.fetch(
                    "SELECT * FROM guardian_tutor_outbox_claim('api', NULL, 10, 60)"
                )
            # The maintenance role's direct business reads stay subject
            # scoped: without the flag nothing is visible.
            direct = await maintenance.fetchval(
                "SELECT count(*) FROM tutor_practice_sessions"
            )
            assert direct == 0
            # The new account-scope reads/grants do not widen direct access:
            # without the scope flag the maintenance role sees nothing.
            evidence_direct = await maintenance.fetchval(
                "SELECT count(*) FROM tutor_practice_evidence"
            )
            outbox_direct = await maintenance.fetchval(
                "SELECT count(*) FROM tutor_commit_outbox"
            )
            assert evidence_direct == 0
            assert outbox_direct == 0
            worker_direct = await worker.fetchval(
                "SELECT count(*) FROM tutor_commit_outbox"
            )
            assert worker_direct == 0
            exported = await maintenance.fetchval(
                "SELECT guardian_tutor_account_scope_export('actor-a')"
            )
            assert exported is not None
        finally:
            await worker.close()
            await maintenance.close()
            await api_role.close()

        # Account-scoped export still sees the subject's rows (actor match).
        exported = await store.export_for_account(account_id="actor-a")
        assert len(exported["tutor_practice_sessions"]) == 1
        assert len(exported["tutor_study_progress"]) == 1
        assert "person_consents" in exported
        assert {row["event_id"] for row in exported["tutor_practice_evidence"]} == {
            "tutor-pg-evidence-a",
            "tutor-pg-evidence-subject",
        }
        assert {row["event_id"] for row in exported["tutor_commit_outbox"]} == {
            "tutor-pg-evict",
            "tutor-pg-outbox-subject",
        }
        deleted = await store.delete_for_account(account_id="actor-a")
        assert deleted["tutor_practice_sessions"] == 1
        assert deleted["tutor_practice_evidence"] == 2
        assert deleted["tutor_commit_outbox"] == 2
        assert "person_consents" in deleted
        assert await store.remaining_account_rows(account_id="actor-a") == {}
        # The deletion is physical, account scoped, and idempotent: the
        # bystander account keeps its own evidence and outbox rows.
        assert await store.remaining_account_rows(account_id="actor-b") == {
            "tutor_practice_evidence": 1,
            "tutor_commit_outbox": 1,
        }
        again = await store.delete_for_account(account_id="actor-a")
        assert again["tutor_practice_evidence"] == 0
        assert again["tutor_commit_outbox"] == 0
        assert await store.remaining_account_rows(account_id="actor-a") == {}
    finally:
        if store is not None:
            await store.close()
        if admin_db is not None:
            await admin_db.close()
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


def _two_subject_progress(
    subject_id: str,
    *,
    practiced_seconds: int,
    now: datetime,
) -> StudyProgress:
    return StudyProgress(
        subject_id=subject_id,
        actor_id="actor-a",
        practiced_seconds=practiced_seconds,
        active_days=(now.date(),),
        current_streak_days=1,
        weak_points=(("past-story", 1),),
        mastered_skills=(),
        source_event_ids=(f"{subject_id}:turn-1",),
        last_practiced_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
)
@pytest.mark.parametrize("account_keyed_install", [False, True])
async def test_postgres_one_owner_holds_progress_for_two_subjects(
    account_keyed_install: bool,
) -> None:
    """Progress is per subject; the legacy account key is dropped in place."""

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_tutor_rekey_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    admin_db: asyncpg.Connection | None = None
    role_created, maintenance_role_created, worker_role_created = False, False, False
    store: PostgresGuardianStore | None = None
    now = datetime.now(UTC)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        (
            role_created,
            maintenance_role_created,
            worker_role_created,
        ) = await _ensure_roles(admin, database=database)
        admin_db = await asyncpg.connect(dsn)
        if account_keyed_install:
            # The shape every install had before the rekey: one row per
            # acting account, plus a quarantined pre-subject-contract row.
            await admin_db.execute(
                """
                CREATE TABLE tutor_study_progress (
                    account_id TEXT PRIMARY KEY,
                    subject_id TEXT,
                    actor_id TEXT,
                    practiced_seconds INTEGER NOT NULL DEFAULT 0,
                    active_days_json JSONB NOT NULL,
                    current_streak_days INTEGER NOT NULL DEFAULT 0,
                    weak_points_json JSONB NOT NULL,
                    mastered_skills_json JSONB NOT NULL,
                    source_event_ids_json JSONB NOT NULL,
                    last_practiced_at TIMESTAMPTZ,
                    rebuilt_at TIMESTAMPTZ NOT NULL
                );
                CREATE UNIQUE INDEX idx_tutor_progress_subject
                ON tutor_study_progress(subject_id) WHERE subject_id IS NOT NULL;
                """
            )
            for account_id, subject_id, actor_id, seconds in (
                ("actor-a", "subject-a", "actor-a", 600),
                ("legacy-account", None, None, 120),
            ):
                await admin_db.execute(
                    """
                    INSERT INTO tutor_study_progress(
                        account_id, subject_id, actor_id, practiced_seconds,
                        active_days_json, current_streak_days, weak_points_json,
                        mastered_skills_json, source_event_ids_json, rebuilt_at
                    ) VALUES ($1, $2, $3, $4, '[]', 0, '[]', '[]', '[]', $5)
                    """,
                    account_id,
                    subject_id,
                    actor_id,
                    seconds,
                    now,
                )
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
        # The schema is re-applied on every deploy; a second apply on the
        # rekeyed table must be a no-op.
        await admin_db.execute(
            Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(
                encoding="utf-8"
            )
        )

        primary_keys = await admin_db.fetchval(
            """
            SELECT count(*) FROM pg_constraint
            WHERE conrelid = 'public.tutor_study_progress'::regclass
              AND contype = 'p'
            """
        )
        account_not_null = await admin_db.fetchval(
            """
            SELECT attnotnull FROM pg_attribute
            WHERE attrelid = 'public.tutor_study_progress'::regclass
              AND attname = 'account_id'
            """
        )
        assert primary_keys == 0
        assert account_not_null is True
        if account_keyed_install:
            legacy_rows = await admin_db.fetch(
                """
                SELECT account_id, subject_id, actor_id, practiced_seconds
                FROM tutor_study_progress ORDER BY account_id
                """
            )
            assert [tuple(row) for row in legacy_rows] == [
                ("actor-a", "subject-a", "actor-a", 600),
                ("legacy-account", None, "legacy-account", 120),
            ]

        await store.save_study_progress(
            _two_subject_progress("subject-b", practiced_seconds=300, now=now),
            rebuilt_at=now,
        )
        await store.save_study_progress(
            _two_subject_progress("subject-a", practiced_seconds=900, now=now),
            rebuilt_at=now,
        )
        first = await store.study_progress(subject_id="subject-a")
        second = await store.study_progress(subject_id="subject-b")
        assert first is not None and first.practiced_seconds == 900
        assert second is not None and second.practiced_seconds == 300
        assert first.actor_id == second.actor_id == "actor-a"

        # The subject RLS scope still isolates each subject's row, and the
        # authority guard still rejects a row without a subject.
        async with admin_db.transaction():
            await admin_db.execute("SET LOCAL ROLE memoria_guardian")
            await admin_db.execute("SET LOCAL memoria.subject_id = 'subject-b'")
            scoped = await admin_db.fetch(
                "SELECT subject_id FROM tutor_study_progress"
            )
        assert [row["subject_id"] for row in scoped] == ["subject-b"]
        with pytest.raises(asyncpg.RaiseError, match="tutor authority columns"):
            await admin_db.execute(
                """
                INSERT INTO tutor_study_progress(
                    account_id, subject_id, actor_id, active_days_json,
                    weak_points_json, mastered_skills_json,
                    source_event_ids_json, rebuilt_at
                ) VALUES ('actor-a', NULL, 'actor-a', '[]', '[]', '[]', '[]', now())
                """
            )

        exported = await store.export_for_account(account_id="actor-a")
        assert [row["subject_id"] for row in exported["tutor_study_progress"]] == [
            "subject-a",
            "subject-b",
        ]
        assert await store.remaining_account_rows(account_id="actor-a") == {
            "tutor_study_progress": 2
        }
        deleted = await store.delete_for_account(account_id="actor-a")
        assert deleted["tutor_study_progress"] == 2
        assert await store.remaining_account_rows(account_id="actor-a") == {}
        if account_keyed_install:
            # The quarantined row stays reachable only on the account paths.
            legacy_export = await store.export_for_account(account_id="legacy-account")
            assert [
                row["subject_id"] for row in legacy_export["tutor_study_progress"]
            ] == [None]
            assert await store.remaining_account_rows(
                account_id="legacy-account"
            ) == {"tutor_study_progress": 1}
    finally:
        if store is not None:
            await store.close()
        if admin_db is not None:
            await admin_db.close()
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
@pytest.mark.parametrize(
    ("relation_type", "relationship_status", "relationship_evidence", "declared_mode"),
    [
        # The pre-2026-09-25 binding wrote a pending one-sided declaration...
        ("guardian_of", "pending", "guardian_declaration_v1:device_binding", "parent_for_child"),
        # ...every binding since attests the guardian active (P1-11); a
        # pending-only check refused these until 2026-09-26.
        ("guardian_of", "active", "guardian_attestation_v1:device_binding", "parent_for_child"),
        # An elder's crisis reaches the child who bound the device (P0-04 D7).
        ("delegate_for", "active", "delegate_attestation_v1:device_binding", "child_for_parent"),
    ],
)
async def test_declared_guardian_notification_requires_the_identity_declaration(
    relation_type: str, relationship_status: str, relationship_evidence: str, declared_mode: str
) -> None:
    """A declared guardian is a distinct, database-validated notification basis.

    An account-less subject can never confirm a guardian link, so the only
    authorized recipient is the guardian whose ``guardian_of`` relationship is
    a one-sided declaration.  SQLite inserts that row directly, so the
    production path has to be proven here: the PostgreSQL port must accept a
    real declaration and still refuse an id that has none.
    """

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_guardian_declared_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    store: PostgresGuardianStore | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        await _ensure_roles(admin, database=database)
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

        # The declared-guardian port validates against the Identity authority,
        # so that schema has to be installed in the same database.
        bootstrap = await asyncpg.connect(dsn)
        now = datetime.now(UTC)
        try:
            await bootstrap.execute(
                (
                    Path(__file__).resolve().parents[2]
                    / "identity"
                    / "postgres_schema.sql"
                ).read_text(encoding="utf-8")
            )
            # The schema only allows 'adult' with verified age evidence, so the
            # fixture has to declare the same rows the identity authority would.
            for person_id, category, band, evidence in (
                ("declared-guardian", "adult", "adult", "verified"),
                ("undeclared-guardian", "adult", "adult", "verified"),
                ("self-declared-outsider", "adult", "adult", "verified"),
                ("declared-ward", "minor", "under_14", "unverified"),
            ):
                await bootstrap.execute(
                    """
                    INSERT INTO identity_persons(
                        person_id, display_name, subject_category, age_band,
                        age_evidence_status, locale, timezone, status,
                        created_at, updated_at
                    ) VALUES ($1,$2,$3,$4,$6,'zh-CN','Asia/Shanghai',
                              'active',$5,$5)
                    """,
                    person_id,
                    person_id,
                    category,
                    band,
                    now,
                    evidence,
                )
            await bootstrap.execute(
                """
                INSERT INTO identity_relationships(
                    relationship_id, source_person_id, target_person_id,
                    relation_type, status, valid_from, established_evidence_id,
                    confirmed_by_source_at, confirmed_by_target_at,
                    requires_confirmation, can_delegate, delegation_depth,
                    permissions_json, auto_suspended, created_at, updated_at
                ) VALUES (
                    'declaration-1', 'declared-guardian', 'declared-ward',
                    $4, $2, $1, $3,
                    $1, NULL, TRUE, FALSE, 0, '[]'::jsonb, FALSE, $1, $1
                )
                """,
                now,
                relationship_status,
                relationship_evidence,
                relation_type,
            )
            # P0-04: the declaration only authorizes a recipient when it is
            # binding-scoped — the declarant owns an ACTIVE binding naming the
            # subject as primary subject.  Seed exactly that binding.
            await bootstrap.execute(
                """
                INSERT INTO identity_device_bindings(
                    binding_id, device_id, declared_mode, family_space_id,
                    account_owner_person_id, binding_version, status, reason,
                    valid_from, valid_until, supersedes_binding_id,
                    service_profile_version, policy_bundle_version,
                    consent_snapshot_id, persona_assignment_id, created_at
                ) VALUES (
                    'declaration-binding', 'declaration-device',
                    $2, NULL, 'declared-guardian', 1, 'active',
                    'create', $1, NULL, NULL, $2 || '-v1',
                    'multi-subject-v1', NULL, 'starlight:v1', $1
                )
                """,
                now,
                declared_mode,
            )
            await bootstrap.execute(
                """
                INSERT INTO identity_device_binding_roles(
                    binding_id, person_id, role, status, permissions_json,
                    granted_at, ended_at
                ) VALUES
                    ('declaration-binding', 'declared-guardian', 'guardian',
                     'active', '[]'::jsonb, $1, NULL),
                    ('declaration-binding', 'declared-ward',
                     'primary_subject', 'active', '[]'::jsonb, $1, NULL)
                """,
                now,
            )
            # P0-04 negative fixture: this person self-declared the SAME
            # subject with arbitrary evidence and holds no binding at all.
            # Their declaration is a real pending one-sided row, so the only
            # thing that can refuse them is the binding-scope check.
            await bootstrap.execute(
                """
                INSERT INTO identity_relationships(
                    relationship_id, source_person_id, target_person_id,
                    relation_type, status, valid_from, established_evidence_id,
                    confirmed_by_source_at, confirmed_by_target_at,
                    requires_confirmation, can_delegate, delegation_depth,
                    permissions_json, auto_suspended, created_at, updated_at
                ) VALUES (
                    'declaration-outside', 'self-declared-outsider',
                    'declared-ward', 'guardian_of', 'pending', $1,
                    'self-asserted-evidence', $1, NULL, TRUE, FALSE, 0,
                    '[]'::jsonb, FALSE, $1, $1
                )
                """,
                now,
            )
        finally:
            await bootstrap.close()

        receipt = await store.enqueue_crisis_event(
            crisis_event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "declared-crisis")),
            evidence_event_id="declared-crisis-evidence",
            minor_user_id="declared-ward",
            occurred_at=now,
            script_version="crisis-transfer-v1",
            declared_guardian_ids=("declared-guardian",),
        )
        assert receipt.notification_count == 1
        notifications = await store.guardian_notifications(
            guardian_user_id="declared-guardian"
        )
        assert len(notifications) == 1
        assert notifications[0].minor_user_id == "declared-ward"
        assert (
            await store.active_link(
                guardian_user_id="declared-guardian",
                minor_user_id="declared-ward",
            )
            is None
        )

        with pytest.raises(asyncpg.PostgresError, match="not a declared guardian"):
            await store.enqueue_crisis_event(
                crisis_event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "undeclared-crisis")),
                evidence_event_id="undeclared-crisis-evidence",
                minor_user_id="declared-ward",
                occurred_at=now,
                script_version="crisis-transfer-v1",
                declared_guardian_ids=("undeclared-guardian",),
            )

        # P0-04: a real one-sided self-declaration with NO binding is also
        # refused, so learning the subject's person id is not enough to
        # become a crisis-notification recipient.
        with pytest.raises(
            asyncpg.PostgresError, match="no binding-scoped declaration"
        ):
            await store.enqueue_crisis_event(
                crisis_event_id=str(
                    uuid.uuid5(uuid.NAMESPACE_URL, "outsider-crisis")
                ),
                evidence_event_id="outsider-crisis-evidence",
                minor_user_id="declared-ward",
                occurred_at=now,
                script_version="crisis-transfer-v1",
                declared_guardian_ids=("self-declared-outsider",),
            )
    finally:
        if store is not None:
            await store.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()

@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
)
async def test_postgres_person_consent_grant_read_revoke_is_subject_scoped() -> None:
    """P0-04: the person-consent lifecycle must close under FORCE RLS.

    The API role is a real non-superuser/NOBYPASSRLS role.  The grantor's
    single-record read and revoke must use the trusted subject context; the
    old implementation set the subject to the grantor and therefore saw no
    row.  Isolation, replay and the policy read gate are asserted here.
    """

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_person_consent_{uuid.uuid4().hex[:10]}"
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
        role_attributes = await admin.fetchrow(
            """
            SELECT rolsuper, rolbypassrls FROM pg_roles
            WHERE rolname = 'memoria_guardian'
            """
        )
        assert role_attributes is not None
        assert bool(role_attributes["rolsuper"]) is False
        assert bool(role_attributes["rolbypassrls"]) is False

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
        consent_a = PersonConsentRecord(
            consent_id=str(uuid.uuid4()),
            subject_person_id="person-child-a",
            grantor_person_id="person-parent-a",
            consent_kind="memory_retention",
            policy_version="minor-retention-v1",
            granted_at=now,
            evidence_event_id="person-consent-a",
        )
        consent_b = PersonConsentRecord(
            consent_id=str(uuid.uuid4()),
            subject_person_id="person-child-b",
            grantor_person_id="person-parent-b",
            consent_kind="memory_retention",
            policy_version="minor-retention-v1",
            granted_at=now,
            evidence_event_id="person-consent-b",
        )
        await store.grant_person_consent(consent_a, actor_person_id="person-parent-a")
        await store.grant_person_consent(consent_b, actor_person_id="person-parent-b")

        # The grantor reads its own single record with the trusted subject
        # context (the old subject=grantor lookup returned NotFound here).
        fetched = await store.get_person_consent(
            consent_id=consent_a.consent_id,
            actor_person_id="person-parent-a",
            subject_person_id="person-child-a",
        )
        assert fetched == consent_a
        # The subject itself can read its own row.
        subject_view = await store.get_person_consent(
            consent_id=consent_a.consent_id,
            actor_person_id="person-child-a",
            subject_person_id="person-child-a",
        )
        assert subject_view == consent_a

        # Cross-parent and cross-subject reads stay closed under RLS.
        with pytest.raises(GuardianNotFoundError):
            await store.get_person_consent(
                consent_id=consent_a.consent_id,
                actor_person_id="person-parent-b",
                subject_person_id="person-child-a",
            )
        with pytest.raises(GuardianNotFoundError):
            await store.get_person_consent(
                consent_id=consent_a.consent_id,
                actor_person_id="person-parent-a",
                subject_person_id="person-child-b",
            )
        assert await store.list_person_consents(
            subject_person_id="person-child-a",
            actor_person_id="person-parent-b",
        ) == ()
        listed = await store.list_person_consents(
            subject_person_id="person-child-a",
            actor_person_id="person-parent-a",
        )
        assert [item.consent_id for item in listed] == [consent_a.consent_id]

        # The unioned read gate sees each subject's own consent only.
        active_a = await store.active_consent(
            minor_user_id="person-child-a", consent_kind="memory_retention"
        )
        assert isinstance(active_a, PersonConsentRecord)
        assert active_a.consent_id == consent_a.consent_id
        assert (
            await store.active_consent(
                minor_user_id="person-child-a",
                consent_kind="minor_voice_session",
            )
            is None
        )
        assert (
            await store.active_consent(
                minor_user_id="person-child-b", consent_kind="memory_retention"
            )
        ).consent_id == consent_b.consent_id  # type: ignore[union-attr]

        # Wrong grantor/subject revokes are refused before any write.
        with pytest.raises(GuardianNotFoundError):
            await store.revoke_person_consent(
                consent_id=consent_a.consent_id,
                grantor_person_id="person-parent-b",
                subject_person_id="person-child-a",
                revoked_at=now + timedelta(minutes=1),
                revocation_evidence_event_id="person-consent-a-revoked-by-other",
            )
        with pytest.raises(GuardianNotFoundError):
            await store.revoke_person_consent(
                consent_id=consent_a.consent_id,
                grantor_person_id="person-parent-a",
                subject_person_id="person-child-b",
                revoked_at=now + timedelta(minutes=1),
                revocation_evidence_event_id="person-consent-a-wrong-subject",
            )

        revoked = await store.revoke_person_consent(
            consent_id=consent_a.consent_id,
            grantor_person_id="person-parent-a",
            subject_person_id="person-child-a",
            revoked_at=now + timedelta(minutes=2),
            revocation_evidence_event_id="person-consent-a-revoked",
        )
        assert revoked.revoked_at == now + timedelta(minutes=2)
        # The read gate tightened for A and B is untouched.
        assert (
            await store.active_consent(
                minor_user_id="person-child-a", consent_kind="memory_retention"
            )
            is None
        )
        still_active_b = await store.active_consent(
            minor_user_id="person-child-b", consent_kind="memory_retention"
        )
        assert isinstance(still_active_b, PersonConsentRecord)

        # Same-evidence revoke replays; a different evidence conflicts.
        replay = await store.revoke_person_consent(
            consent_id=consent_a.consent_id,
            grantor_person_id="person-parent-a",
            subject_person_id="person-child-a",
            revoked_at=now + timedelta(minutes=5),
            revocation_evidence_event_id="person-consent-a-revoked",
        )
        assert replay.revoked_at == revoked.revoked_at
        with pytest.raises(GuardianConflictError):
            await store.revoke_person_consent(
                consent_id=consent_a.consent_id,
                grantor_person_id="person-parent-a",
                subject_person_id="person-child-a",
                revoked_at=now + timedelta(minutes=6),
                revocation_evidence_event_id="person-consent-a-revoked-again",
            )

        # Export/delete/remaining must see real person-consent rows for both
        # the grantor and the subject, and must not leak across families.
        exported_grantor_a = await store.export_for_account(
            account_id="person-parent-a"
        )
        assert [
            str(row["consent_id"])
            for row in exported_grantor_a["person_consents"]  # type: ignore[union-attr]
        ] == [consent_a.consent_id]
        exported_subject_a = await store.export_for_account(
            account_id="person-child-a"
        )
        assert [
            str(row["consent_id"])
            for row in exported_subject_a["person_consents"]  # type: ignore[union-attr]
        ] == [consent_a.consent_id]
        exported_family_b = await store.export_for_account(
            account_id="person-parent-b"
        )
        assert [
            str(row["consent_id"])
            for row in exported_family_b["person_consents"]  # type: ignore[union-attr]
        ] == [consent_b.consent_id]

        assert await store.remaining_account_rows(account_id="person-parent-a") == {
            "person_consents": 1
        }
        assert await store.remaining_account_rows(account_id="person-child-a") == {
            "person_consents": 1
        }
        deleted_a = await store.delete_for_account(account_id="person-parent-a")
        assert deleted_a["person_consents"] == 1
        assert await store.remaining_account_rows(account_id="person-parent-a") == {}
        assert await store.remaining_account_rows(account_id="person-child-a") == {}
        # The other family is untouched.
        assert await store.remaining_account_rows(account_id="person-parent-b") == {
            "person_consents": 1
        }
        assert await store.remaining_account_rows(account_id="person-child-b") == {
            "person_consents": 1
        }
        deleted_b = await store.delete_for_account(account_id="person-parent-b")
        assert deleted_b["person_consents"] == 1
        assert await store.remaining_account_rows(account_id="person-parent-b") == {}
        assert await store.remaining_account_rows(account_id="person-child-b") == {}
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
async def test_postgres_subject_deletion_removes_only_the_subjects_rows() -> None:
    """One bound subject inside the owner's account, through the maintenance role."""

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_subject_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    admin_db: asyncpg.Connection | None = None
    role_created, maintenance_role_created, worker_role_created = False, False, False
    store: PostgresGuardianStore | None = None
    now = datetime.now(UTC)
    owner, subject, other_subject = "owner-o", "subject-s", "subject-t"
    subject_crisis, other_crisis = uuid.uuid4(), uuid.uuid4()
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
        )
        await store.initialize()
        admin_db = await asyncpg.connect(dsn)

        for session_id, session_subject in (
            ("11111111-0000-4000-8000-000000000001", subject),
            ("11111111-0000-4000-8000-000000000002", owner),
            ("11111111-0000-4000-8000-000000000003", other_subject),
        ):
            await store.save_practice_session(
                PracticeSession(
                    session_id=session_id,
                    subject_id=session_subject,
                    actor_id=owner,
                    voice_session_id=f"voice-{session_subject}",
                    focus="tutor_english",
                    task_id="english-past-story",
                    status="draft",
                    revision=0,
                    event_ids=(f"{session_id}:create", f"evt-session-{session_subject}"),
                    practiced_seconds=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        # ``tutor_study_progress`` is keyed by account: the other subject's
        # progress lives under another owner.
        for progress_subject, progress_actor in (
            (subject, owner),
            (other_subject, "owner-p"),
        ):
            await store.save_study_progress(
                StudyProgress(
                    subject_id=progress_subject,
                    actor_id=progress_actor,
                    practiced_seconds=60,
                    active_days=(now.date(),),
                    current_streak_days=1,
                    weak_points=(),
                    mastered_skills=(),
                    source_event_ids=(f"evt-progress-{progress_subject}",),
                    last_practiced_at=now,
                ),
                rebuilt_at=now,
            )
        for event_id, row_subject in (
            ("evt-s-turn", subject),
            ("evt-o-turn", owner),
            ("evt-t-turn", other_subject),
        ):
            await admin_db.execute(
                """
                INSERT INTO tutor_practice_evidence(
                    event_id, assessment_id, kind, subject_id, actor_id,
                    envelope_json, envelope_sha256, commit_sha256,
                    outcome, skill_key, session_id, session_revision, created_at
                ) VALUES (
                    $1, NULL, 'tutor.practice_turn_recorded', $2, $3,
                    '{}'::jsonb, $4, $4, NULL, NULL, 'session', 0, now()
                )
                """,
                event_id,
                row_subject,
                owner,
                "a" * 64,
            )
            await admin_db.execute(
                """
                INSERT INTO tutor_commit_outbox(
                    event_id, kind, subject_id, actor_id,
                    archive_payload_json, status, created_at
                ) VALUES (
                    $1, 'tutor.practice_turn_recorded', $2, $3,
                    jsonb_build_object('event_id', $4::text), 'delivered', now()
                )
                """,
                f"outbox-{event_id}",
                row_subject,
                owner,
                f"archived-{event_id}",
            )
        for crisis_event_id, minor in ((subject_crisis, subject), (other_crisis, other_subject)):
            await admin_db.execute(
                """
                INSERT INTO guardian_crisis_events(
                    crisis_event_id, evidence_event_id, minor_user_id,
                    occurred_at, script_version
                ) VALUES ($1, $2, $3, now(), 'crisis-transfer-draft-v1')
                """,
                crisis_event_id,
                f"crisis-evidence-{minor}",
                minor,
            )
            await admin_db.execute(
                """
                INSERT INTO guardian_notification_outbox(
                    notification_id, crisis_event_id, guardian_user_id,
                    channel, status, attempts, created_at
                ) VALUES ($1, $2, $3, 'wechat_subscription', 'pending', 0, now())
                """,
                uuid.uuid4(),
                crisis_event_id,
                owner,
            )
        await admin_db.execute(
            """
            INSERT INTO guardian_person_consents(
                consent_id, subject_person_id, grantor_person_id, consent_kind,
                policy_version, granted_at, evidence_event_id
            ) VALUES ($1, $2, $3, 'memory_retention', 'minor-memory-v1', now(),
                      'consent-evidence-s')
            """,
            uuid.uuid4(),
            subject,
            owner,
        )
        await admin_db.execute(
            """
            INSERT INTO guardian_push_subscriptions(
                guardian_user_id, template_id, openid, remaining, last_result,
                created_at, updated_at
            ) VALUES ($1, 'crisis-template-01', 'owner-openid', 1, 'accept', now(), now())
            """,
            owner,
        )
        # A live corpus sample is reported, never deleted, by the subject
        # scope (the corpus retention service purges it).  Replica mode skips
        # the consent FK for this stand-alone row.
        await admin_db.execute("SET session_replication_role = replica")
        await admin_db.execute(
            """
            INSERT INTO guardian_corpus_samples(
                sample_id, minor_user_id, consent_id, source_event_id, object_key,
                media_type, byte_count, content_sha256, encryption_key_version,
                object_backend, created_at, expires_at
            ) VALUES ($1, $2, $3, 'source-s', 'key-s', 'audio/wav', 1, $4, 'v1',
                      'local', now(), now() + interval '1 day')
            """,
            uuid.uuid4(),
            subject,
            uuid.uuid4(),
            "b" * 64,
        )
        await admin_db.execute("SET session_replication_role = origin")

        api_role = await asyncpg.connect(
            _role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian",
                password="api-role-password",
            )
        )
        try:
            # The API role has no EXECUTE on the subject-scope functions.
            for function in (
                "guardian_subject_scope_tutor_event_ids",
                "guardian_subject_scope_delete",
                "guardian_subject_scope_remaining",
            ):
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await api_role.fetchval(
                        f"SELECT {function}($1, $2)",  # noqa: S608
                        owner,
                        subject,
                    )
        finally:
            await api_role.close()
        # The SQL functions re-check the scope themselves.
        maintenance = await asyncpg.connect(
            _role_dsn(
                admin_dsn,
                database=database,
                role="memoria_guardian_maintenance",
                password="maintenance-role-password",
            )
        )
        try:
            with pytest.raises(asyncpg.exceptions.RaiseError, match="distinct owner"):
                await maintenance.fetchval(
                    "SELECT guardian_subject_scope_delete($1, $1)", owner
                )
        finally:
            await maintenance.close()

        assert await store.subject_tutor_event_ids(
            account_id=owner, subject_id=subject
        ) == (
            "11111111-0000-4000-8000-000000000001:create",
            "archived-evt-s-turn",
            "evt-progress-subject-s",
            "evt-s-turn",
            "evt-session-subject-s",
            "outbox-evt-s-turn",
        )
        expected = {
            "crisis_events": 1,
            "guardian_notifications": 1,
            "tutor_practice_sessions": 1,
            "tutor_study_progress": 1,
            "tutor_practice_evidence": 1,
            "tutor_commit_outbox": 1,
        }
        assert await store.remaining_subject_rows(
            account_id=owner, subject_id=subject
        ) == {**expected, "corpus_samples": 1}
        assert await store.delete_subject_rows(
            account_id=owner, subject_id=subject
        ) == expected
        assert await store.remaining_subject_rows(
            account_id=owner, subject_id=subject
        ) == {"corpus_samples": 1}
        assert await store.subject_tutor_event_ids(
            account_id=owner, subject_id=subject
        ) == ()

        surviving = {
            table: {
                str(row[0])
                for row in await admin_db.fetch(f"SELECT {column} FROM {table}")  # noqa: S608
            }
            for table, column in (
                ("tutor_practice_sessions", "subject_id"),
                ("tutor_study_progress", "subject_id"),
                ("tutor_practice_evidence", "event_id"),
                ("tutor_commit_outbox", "event_id"),
                ("guardian_crisis_events", "minor_user_id"),
                ("guardian_notification_outbox", "crisis_event_id"),
                ("guardian_person_consents", "subject_person_id"),
                ("guardian_push_subscriptions", "guardian_user_id"),
            )
        }
        assert surviving == {
            "tutor_practice_sessions": {owner, other_subject},
            "tutor_study_progress": {other_subject},
            "tutor_practice_evidence": {"evt-o-turn", "evt-t-turn"},
            "tutor_commit_outbox": {"outbox-evt-o-turn", "outbox-evt-t-turn"},
            "guardian_crisis_events": {other_subject},
            "guardian_notification_outbox": {str(other_crisis)},
            "guardian_person_consents": {subject},
            "guardian_push_subscriptions": {owner},
        }

        again = await store.delete_subject_rows(account_id=owner, subject_id=subject)
        assert set(again.values()) == {0}
        # The subject's corpus row stayed live throughout; the rest is gone.
        assert await store.remaining_subject_rows(
            account_id=owner, subject_id=subject
        ) == {"corpus_samples": 1}
    finally:
        if store is not None:
            await store.close()
        if admin_db is not None:
            await admin_db.close()
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
