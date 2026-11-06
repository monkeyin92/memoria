"""Guardian PostgreSQL privacy lifecycle contract tests.

Covers person consent grant/read/revoke and subject deletion through the
maintenance role."""

from __future__ import annotations

import os
import uuid
from datetime import (
    UTC,
    datetime,
    timedelta,
)

import asyncpg
import pytest
from services.guardian.domain import (
    GuardianConflictError,
    GuardianNotFoundError,
    PersonConsentRecord,
)
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.tests.guardian_postgres_support import (
    _ensure_roles,
    _postgres_dsn,
    _role_dsn,
)
from services.tutor.domain import (
    PracticeSession,
    StudyProgress,
)


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

