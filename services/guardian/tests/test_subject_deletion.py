"""Subject-scoped deletion (``SubjectGuardianPort``) in the SQLite guardian store.

A bound subject (a child or elder with no account) is served by a device whose
binding owner is ``owner-o``.  Deleting the subject removes their crisis
events, the notifications about them and their tutor rows inside the owner's
account, and nothing of the owner's or another subject's.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.governance.subject_ports import SubjectGuardianPort
from services.guardian.domain import PersonConsentRecord
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.sqlite_store import SqliteGuardianStore
from services.tutor.domain import PracticeSession, StudyProgress

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
OWNER = "owner-o"
SUBJECT = "subject-s"
OTHER_SUBJECT = "subject-t"
OTHER_OWNER = "owner-p"
SUBJECT_CRISIS = "7d1f9c1e-0000-4000-8000-000000000001"
OTHER_CRISIS = "7d1f9c1e-0000-4000-8000-000000000002"


def _postgres_store_is_a_subject_guardian_port(
    store: PostgresGuardianStore,
) -> SubjectGuardianPort:
    # Type-checked only: the PostgreSQL contract runs in
    # test_guardian_postgres_store.py when MEMORIA_TEST_POSTGRES_DSN is set.
    return store


def _session(session_id: str, *, subject_id: str, actor_id: str) -> PracticeSession:
    return PracticeSession(
        session_id=session_id,
        subject_id=subject_id,
        actor_id=actor_id,
        voice_session_id=f"voice-{session_id}",
        focus="tutor_english",
        task_id="english-past-story",
        status="draft",
        revision=0,
        event_ids=(f"{session_id}:create", f"evt-{session_id}"),
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


def _insert_evidence(path: Path, rows: tuple[tuple[str, str, str], ...]) -> None:
    with sqlite3.connect(path) as connection:
        for event_id, subject_id, actor_id in rows:
            connection.execute(
                """
                INSERT INTO tutor_practice_evidence(
                    event_id, assessment_id, kind, subject_id, actor_id,
                    envelope_json, envelope_sha256, commit_sha256,
                    outcome, skill_key, session_id, session_revision, created_at
                ) VALUES (
                    ?, NULL, 'tutor.practice_turn_recorded', ?, ?, '{}', ?, ?,
                    NULL, NULL, ?, 0, ?
                )
                """,
                (event_id, subject_id, actor_id, "a" * 64, "a" * 64, "s", NOW.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO tutor_commit_outbox(
                    event_id, kind, subject_id, actor_id, archive_payload_json,
                    status, created_at
                ) VALUES (?, 'tutor.practice_turn_recorded', ?, ?, ?, 'delivered', ?)
                """,
                (
                    f"outbox-{event_id}",
                    subject_id,
                    actor_id,
                    json.dumps({"event_id": f"archived-{event_id}", "account_id": actor_id}),
                    NOW.isoformat(),
                ),
            )


async def _seed(path: Path) -> SqliteGuardianStore:
    store = SqliteGuardianStore(path)
    store.initialize()
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
    _insert_evidence(
        path,
        (
            ("evt-s-turn", SUBJECT, OWNER),
            ("evt-o-turn", OWNER, OWNER),
            ("evt-t-turn", OTHER_SUBJECT, OWNER),
        ),
    )
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
    return store


def _surviving(path: Path, table: str, column: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(f"SELECT {column} FROM {table}")  # noqa: S608
        }


@pytest.mark.asyncio
async def test_subject_deletion_removes_only_the_subjects_guardian_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "subject.sqlite3"
    store = await _seed(path)
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
    assert _surviving(path, "tutor_practice_sessions", "session_id") == {
        "o-session",
        "t-session",
    }
    assert _surviving(path, "tutor_study_progress", "subject_id") == {OTHER_SUBJECT}
    assert _surviving(path, "tutor_practice_evidence", "event_id") == {
        "evt-o-turn",
        "evt-t-turn",
    }
    assert _surviving(path, "tutor_commit_outbox", "event_id") == {
        "outbox-evt-o-turn",
        "outbox-evt-t-turn",
    }
    assert _surviving(path, "guardian_crisis_events", "minor_user_id") == {OTHER_SUBJECT}
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
async def test_remaining_subject_rows_reports_live_corpus_samples(tmp_path: Path) -> None:
    path = tmp_path / "corpus.sqlite3"
    store = SqliteGuardianStore(path)
    store.initialize()
    # Foreign keys are off on this raw connection: the sample stands alone.
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO guardian_corpus_samples(
                sample_id, minor_user_id, consent_id, source_event_id, object_key,
                media_type, byte_count, content_sha256, encryption_key_version,
                object_backend, created_at, expires_at
            ) VALUES ('sample-s', ?, 'consent-s', 'source-s', 'key-s', 'audio/wav',
                      1, ?, 'v1', 'local', ?, ?)
            """,
            (SUBJECT, "b" * 64, NOW.isoformat(), (NOW + timedelta(days=1)).isoformat()),
        )
    assert await store.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {
        "corpus_samples": 1
    }
    # Deletion leaves the purge to the corpus retention service.
    await store.delete_subject_rows(account_id=OWNER, subject_id=SUBJECT)
    await store.mark_corpus_sample_deleted(sample_id="sample-s", deleted_at=NOW)
    assert await store.remaining_subject_rows(account_id=OWNER, subject_id=SUBJECT) == {}


@pytest.mark.asyncio
async def test_subject_scope_never_targets_the_account_itself(tmp_path: Path) -> None:
    store = SqliteGuardianStore(tmp_path / "scope.sqlite3")
    store.initialize()
    with pytest.raises(ValueError, match="never targets the account"):
        await store.delete_subject_rows(account_id=OWNER, subject_id=OWNER)
    with pytest.raises(ValueError, match="bounded non-empty"):
        await store.subject_tutor_event_ids(account_id=OWNER, subject_id=" ")
