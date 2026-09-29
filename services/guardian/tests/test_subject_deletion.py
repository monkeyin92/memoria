"""Subject-scoped deletion (``SubjectGuardianPort``) in the PostgreSQL guardian store.

A bound subject (a child or elder with no account) is served by a device whose
binding owner is ``owner-o``.  Deleting the subject removes their crisis
events, the notifications about them and their tutor rows inside the owner's
account, and nothing of the owner's or another subject's.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.archive.object_store import ObjectRef
from services.governance.subject_ports import SubjectGuardianPort
from services.guardian.corpus import CorpusSample
from services.guardian.domain import ConsentRecord, PersonConsentRecord
from services.guardian.postgres_store import PostgresGuardianStore
from services.tutor.domain import PracticeSession, StudyProgress
from testing.guardian_seed import declare_binding_guardian
from testing.postgres_harness import TestDatabase

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
OWNER = "owner-o"
SUBJECT = "subject-s"
OTHER_SUBJECT = "subject-t"
OTHER_OWNER = "owner-p"
SUBJECT_CRISIS = "7d1f9c1e-0000-4000-8000-000000000001"
OTHER_CRISIS = "7d1f9c1e-0000-4000-8000-000000000002"
# Practice session ids are UUIDs in PostgreSQL; the label names the events.
SESSION_IDS = {
    "s-session": "11111111-0000-4000-8000-000000000001",
    "o-session": "11111111-0000-4000-8000-000000000002",
    "t-session": "11111111-0000-4000-8000-000000000003",
}


def _session(label: str, *, subject_id: str, actor_id: str) -> PracticeSession:
    return PracticeSession(
        session_id=SESSION_IDS[label],
        subject_id=subject_id,
        actor_id=actor_id,
        voice_session_id=f"voice-{label}",
        focus="tutor_english",
        task_id="english-past-story",
        status="draft",
        revision=0,
        event_ids=(f"{label}:create", f"evt-{label}"),
        practiced_seconds=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _progress(*, subject_id: str, actor_id: str, source: tuple[str, ...]) -> StudyProgress:
    return StudyProgress(
        subject_id=subject_id,
        actor_id=actor_id,
        practiced_seconds=60,
        active_days=(NOW.date(),),
        current_streak_days=1,
        weak_points=(),
        mastered_skills=(),
        source_event_ids=source,
        last_practiced_at=NOW,
    )


async def _insert_evidence(
    connection: asyncpg.Connection, rows: tuple[tuple[str, str, str], ...]
) -> None:
    for event_id, subject_id, actor_id in rows:
        await connection.execute(
            """
            INSERT INTO tutor_practice_evidence(
                event_id, assessment_id, kind, subject_id, actor_id,
                envelope_json, envelope_sha256, commit_sha256,
                outcome, skill_key, session_id, session_revision, created_at
            ) VALUES (
                $1, NULL, 'tutor.practice_turn_recorded', $2, $3, '{}'::jsonb, $4, $4,
                NULL, NULL, 's', 0, $5
            )
            """,
            event_id,
            subject_id,
            actor_id,
            "a" * 64,
            NOW,
        )
        await connection.execute(
            """
            INSERT INTO tutor_commit_outbox(
                event_id, kind, subject_id, actor_id, archive_payload_json,
                status, created_at
            ) VALUES (
                $1, 'tutor.practice_turn_recorded', $2, $3,
                jsonb_build_object('event_id', $4::text, 'account_id', $3::text),
                'delivered', $5
            )
            """,
            f"outbox-{event_id}",
            subject_id,
            actor_id,
            f"archived-{event_id}",
            NOW,
        )


async def _seed(store: PostgresGuardianStore, database: TestDatabase) -> None:
    await store.save_practice_session(
        _session("s-session", subject_id=SUBJECT, actor_id=OWNER)
    )
    await store.save_practice_session(
        _session("o-session", subject_id=OWNER, actor_id=OWNER)
    )
    await store.save_practice_session(
        _session("t-session", subject_id=OTHER_SUBJECT, actor_id=OWNER)
    )
    await store.save_study_progress(
        _progress(subject_id=SUBJECT, actor_id=OWNER, source=("evt-s-progress",)),
        rebuilt_at=NOW,
    )
    # ``tutor_study_progress`` is keyed by account, so the other subject's
    # progress lives under another owner here.
    await store.save_study_progress(
        _progress(subject_id=OTHER_SUBJECT, actor_id=OTHER_OWNER, source=("evt-t",)),
        rebuilt_at=NOW,
    )
    # Tutor evidence/outbox rows are written only by the receipt-verified
    # ``commit_aggregate`` and Identity has no API here: seed them as the owner.
    admin = await asyncpg.connect(database.owner_dsn())
    try:
        await _insert_evidence(
            admin,
            (
                ("evt-s-turn", SUBJECT, OWNER),
                ("evt-o-turn", OWNER, OWNER),
                ("evt-t-turn", OTHER_SUBJECT, OWNER),
            ),
        )
        for minor in (SUBJECT, OTHER_SUBJECT):
            await declare_binding_guardian(admin, guardian_id=OWNER, subject_id=minor, at=NOW)
    finally:
        await admin.close()
    for crisis_event_id, minor in ((SUBJECT_CRISIS, SUBJECT), (OTHER_CRISIS, OTHER_SUBJECT)):
        receipt = await store.enqueue_crisis_event(
            crisis_event_id=crisis_event_id,
            evidence_event_id=f"crisis-evidence-{minor}",
            minor_user_id=minor,
            occurred_at=NOW,
            script_version="crisis-transfer-draft-v1",
            declared_guardian_ids=(OWNER,),
        )
        assert receipt.notification_count == 1
    await store.grant_person_consent(
        PersonConsentRecord(
            consent_id="5c0b7a52-0000-4000-8000-000000000001",
            subject_person_id=SUBJECT,
            grantor_person_id=OWNER,
            consent_kind="memory_retention",
            policy_version="minor-memory-v1",
            granted_at=NOW,
            evidence_event_id="consent-evidence-s",
        ),
        actor_person_id=OWNER,
    )
    await store.record_push_subscription(
        guardian_user_id=OWNER,
        template_id="crisis-template-01",
        result="accept",
        openid="owner-openid",
        now=NOW,
    )


def _ids(rows: object, column: str) -> set[str]:
    assert isinstance(rows, list)
    return {str(row[column]) for row in rows}


@pytest.mark.asyncio
async def test_subject_deletion_removes_only_the_subjects_guardian_rows(
    guardian_postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await _seed(store, guardian_postgres_database)
    port: SubjectGuardianPort = store

    # The ids come back before the rows go: afterwards nothing can find them.
    assert await port.subject_tutor_event_ids(account_id=OWNER, subject_id=SUBJECT) == (
        "archived-evt-s-turn",
        "evt-s-progress",
        "evt-s-session",
        "evt-s-turn",
        "outbox-evt-s-turn",
        "s-session:create",
    )
    assert await port.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {
        "guardian_notifications": 1,
        "crisis_events": 1,
        "tutor_practice_sessions": 1,
        "tutor_study_progress": 1,
        "tutor_practice_evidence": 1,
        "tutor_commit_outbox": 1,
    }

    deleted = await port.delete_subject_rows(account_id=OWNER, subject_id=SUBJECT)
    assert deleted == {
        "guardian_notifications": 1,
        "crisis_events": 1,
        "tutor_practice_sessions": 1,
        "tutor_study_progress": 1,
        "tutor_practice_evidence": 1,
        "tutor_commit_outbox": 1,
    }
    assert await port.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {}
    assert await port.subject_tutor_event_ids(account_id=OWNER, subject_id=SUBJECT) == ()

    # The owner's own practice and the other subject's rows stay.
    owner_export = await store.export_for_account(account_id=OWNER)
    assert _ids(owner_export["tutor_practice_sessions"], "session_id") == {
        SESSION_IDS["o-session"],
        SESSION_IDS["t-session"],
    }
    assert _ids(owner_export["tutor_practice_evidence"], "event_id") == {
        "evt-o-turn",
        "evt-t-turn",
    }
    assert _ids(owner_export["tutor_commit_outbox"], "event_id") == {
        "outbox-evt-o-turn",
        "outbox-evt-t-turn",
    }
    assert await store.study_progress(subject_id=SUBJECT) is None
    assert await store.study_progress(subject_id=OTHER_SUBJECT) is not None
    subject_export = await store.export_for_account(account_id=SUBJECT)
    assert subject_export["crisis_events"] == []
    other_export = await store.export_for_account(account_id=OTHER_SUBJECT)
    assert _ids(other_export["crisis_events"], "minor_user_id") == {OTHER_SUBJECT}
    notifications = await store.guardian_notifications(guardian_user_id=OWNER)
    assert [item.minor_user_id for item in notifications] == [OTHER_SUBJECT]
    # The consent audit and the guardian's subscribe-message ledger stay.
    assert [
        record.subject_person_id
        for record in await store.list_person_consents(
            subject_person_id=SUBJECT, actor_person_id=OWNER
        )
    ] == [SUBJECT]
    assert await store.push_subscription(
        guardian_user_id=OWNER, template_id="crisis-template-01"
    ) is not None

    again = await port.delete_subject_rows(account_id=OWNER, subject_id=SUBJECT)
    assert set(again.values()) == {0}
    assert await port.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {}


@pytest.mark.asyncio
async def test_remaining_subject_rows_reports_live_corpus_samples(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    # The corpus fence checks consent expiry against the database clock.
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"corpus-binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id=OWNER,
        minor_user_id=SUBJECT,
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id=SUBJECT,
        binding_code_hash=digest,
        now=now,
    )
    consent = await store.grant_consent(
        ConsentRecord(
            consent_id="c0a5e47a-0000-4000-8000-000000000003",
            link_id=link.link_id,
            consent_kind="corpus_recording",
            policy_version="authorized-child-corpus-v1",
            granted_at=now,
            expires_at=now + timedelta(days=2),
            evidence_event_id="corpus-consent-s",
        ),
        actor_user_id=OWNER,
    )
    sample_id = "5a3b1e00-0000-4000-8000-000000000003"
    await store.record_corpus_sample(
        CorpusSample(
            sample_id=sample_id,
            minor_user_id=SUBJECT,
            consent_id=consent.consent_id,
            source_event_id="source-s",
            reference=ObjectRef(
                account_id=SUBJECT,
                object_key="hash/authorized-child-corpus/sample-s.fernet",
                media_type="audio/wav",
                byte_count=1,
                content_sha256="b" * 64,
                encryption_key_version="v1",
                backend="local",
            ),
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    assert await store.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {
        "corpus_samples": 1
    }
    # Deletion leaves the purge to the corpus retention service.
    await store.delete_subject_rows(account_id=OWNER, subject_id=SUBJECT)
    assert await store.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {
        "corpus_samples": 1
    }
    await store.mark_corpus_sample_deleted(sample_id=sample_id, deleted_at=now)
    assert await store.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {}


@pytest.mark.asyncio
async def test_subject_scope_never_targets_the_account_itself(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    with pytest.raises(ValueError, match="never targets the account"):
        await store.delete_subject_rows(account_id=OWNER, subject_id=OWNER)
    with pytest.raises(ValueError, match="bounded non-empty"):
        await store.subject_tutor_event_ids(account_id=OWNER, subject_id=" ")
