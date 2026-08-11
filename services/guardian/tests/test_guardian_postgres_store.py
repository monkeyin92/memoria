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
                        "guardian_corpus_samples",
                        "tutor_practice_sessions",
                        "tutor_study_progress",
                        "tutor_practice_evidence",
                        "tutor_commit_outbox",
                        "guardian_crisis_events",
                        "guardian_notification_outbox",
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
        assert len(forced_tables) == 9
        assert policy_names == {
            ("guardian_links", "guardian_controller_links"),
            ("guardian_links", "guardian_controller_links_insert"),
            ("guardian_links", "guardian_controller_links_update"),
            ("guardian_consents", "guardian_controller_consents"),
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
        assert exported["tutor_study_progress"] is not None
        deleted = await store.delete_for_account(account_id="actor-a")
        assert deleted["tutor_practice_sessions"] == 1
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
