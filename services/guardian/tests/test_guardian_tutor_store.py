"""PR-13: subject-scoped tutor persistence in the SQLite guardian store."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.guardian.sqlite_store import SqliteGuardianStore
from services.tutor.domain import PracticeConflictError, PracticeSession, StudyProgress

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _session(
    *,
    session_id: str = "session-1",
    subject_id: str = "subject-1",
    actor_id: str = "actor-1",
    voice_session_id: str = "voice-session-1",
    revision: int = 0,
    event_ids: tuple[str, ...] = ("session-1:create",),
) -> PracticeSession:
    return PracticeSession(
        session_id=session_id,
        subject_id=subject_id,
        actor_id=actor_id,
        voice_session_id=voice_session_id,
        focus="tutor_english",
        task_id="english-past-story",
        status="draft",
        revision=revision,
        event_ids=event_ids,
        practiced_seconds=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _progress(
    *,
    subject_id: str = "subject-1",
    actor_id: str = "actor-1",
    practiced_seconds: int = 600,
    source_event_ids: tuple[str, ...] = ("turn-1",),
) -> StudyProgress:
    return StudyProgress(
        subject_id=subject_id,
        actor_id=actor_id,
        practiced_seconds=practiced_seconds,
        active_days=(NOW.date(),),
        current_streak_days=1,
        weak_points=(("past-story", 1),),
        mastered_skills=(),
        source_event_ids=source_event_ids,
        last_practiced_at=NOW,
    )


@pytest.mark.asyncio
async def test_tutor_rows_are_subject_scoped(
    tmp_path: Path,
) -> None:
    store = SqliteGuardianStore(tmp_path / "tutor.sqlite3")
    store.initialize()

    saved = await store.save_practice_session(_session())
    assert saved.subject_id == "subject-1"
    await store.save_study_progress(_progress(), rebuilt_at=NOW)

    own = await store.practice_session(subject_id="subject-1", session_id="session-1")
    assert own is not None
    assert own.actor_id == "actor-1"
    assert own.voice_session_id == "voice-session-1"
    other = await store.practice_session(subject_id="subject-other", session_id="session-1")
    assert other is None
    own_progress = await store.study_progress(subject_id="subject-1")
    assert own_progress is not None
    assert own_progress.actor_id == "actor-1"
    assert await store.study_progress(subject_id="subject-other") is None


@pytest.mark.asyncio
async def test_save_rejects_cross_subject_and_stale_revision_cas(
    tmp_path: Path,
) -> None:
    store = SqliteGuardianStore(tmp_path / "tutor-cas.sqlite3")
    store.initialize()
    await store.save_practice_session(_session())

    with pytest.raises(PracticeConflictError, match="revision_conflict"):
        await store.save_practice_session(
            _session(
                revision=2,
                event_ids=("session-1:create", "session-1:active", "session-1:stale"),
            )
        )
    # A save outside the subject scope cannot even see the session: it fails
    # closed as not found rather than leaking the row.
    with pytest.raises(PracticeConflictError, match="practice_session_not_found"):
        await store.save_practice_session(
            _session(subject_id="subject-other", revision=1)
        )
    with pytest.raises(PracticeConflictError, match="revision_conflict"):
        await store.save_practice_session(
            _session(actor_id="actor-other", revision=1)
        )
    with pytest.raises(PracticeConflictError, match="revision_conflict"):
        await store.save_practice_session(
            _session(voice_session_id="voice-other", revision=1)
        )

    # The idempotent replay of the exact current row is a no-op success.
    same = await store.save_practice_session(_session())
    assert same.revision == 0


@pytest.mark.asyncio
async def test_progress_upserts_by_subject_and_keeps_actor_account(
    tmp_path: Path,
) -> None:
    store = SqliteGuardianStore(tmp_path / "tutor-progress.sqlite3")
    store.initialize()
    await store.save_study_progress(_progress(), rebuilt_at=NOW)
    updated = _progress(practiced_seconds=1200, source_event_ids=("turn-1", "turn-2"))
    await store.save_study_progress(updated, rebuilt_at=NOW)

    progress = await store.study_progress(subject_id="subject-1")
    assert progress is not None
    assert progress.practiced_seconds == 1200
    assert progress.source_event_ids == ("turn-1", "turn-2")

    exported = await store.export_for_account(account_id="actor-1")
    assert exported["tutor_study_progress"] is not None
    assert len(exported["tutor_practice_sessions"]) == 0


@pytest.mark.asyncio
async def test_legacy_account_rows_are_quarantined_and_still_account_exportable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tutor_practice_sessions (
            session_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            focus TEXT NOT NULL CHECK (focus IN ('tutor_english', 'tutor_homework')),
            task_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused', 'completed')),
            revision INTEGER NOT NULL CHECK (revision >= 0),
            event_ids_json TEXT NOT NULL,
            practiced_seconds INTEGER NOT NULL DEFAULT 0 CHECK (practiced_seconds >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE tutor_study_progress (
            account_id TEXT PRIMARY KEY,
            practiced_seconds INTEGER NOT NULL DEFAULT 0 CHECK (practiced_seconds >= 0),
            active_days_json TEXT NOT NULL,
            current_streak_days INTEGER NOT NULL DEFAULT 0 CHECK (current_streak_days >= 0),
            weak_points_json TEXT NOT NULL,
            mastered_skills_json TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            last_practiced_at TEXT,
            rebuilt_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO tutor_practice_sessions(
            session_id, account_id, focus, task_id, status, revision,
            event_ids_json, practiced_seconds, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-session-1",
            "legacy-account",
            "tutor_english",
            "english-past-story",
            "draft",
            0,
            json.dumps(["legacy-session-1:create"]),
            0,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    connection.commit()
    connection.close()

    store = SqliteGuardianStore(path)
    store.initialize()

    # The legacy row has unknown subject ownership: quarantined from every
    # subject-scoped read and never projectable.
    assert await store.practice_session(
        subject_id="legacy-account",
        session_id="legacy-session-1",
    ) is None
    # It remains visible and deletable on the account-scoped paths.
    exported = await store.export_for_account(account_id="legacy-account")
    assert len(exported["tutor_practice_sessions"]) == 1
    remaining = await store.remaining_account_rows(account_id="legacy-account")
    assert remaining["tutor_practice_sessions"] == 1
    deleted = await store.delete_for_account(account_id="legacy-account")
    assert deleted["tutor_practice_sessions"] == 1
    assert await store.remaining_account_rows(account_id="legacy-account") == {}


def test_authority_column_triggers_reject_null_or_blank_new_rows(
    tmp_path: Path,
) -> None:
    store = SqliteGuardianStore(tmp_path / "triggers.sqlite3")
    store.initialize()

    connection = sqlite3.connect(tmp_path / "triggers.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="tutor authority columns"):
            connection.execute(
                """
                INSERT INTO tutor_practice_sessions(
                    session_id, account_id, subject_id, actor_id,
                    voice_session_id, focus, task_id, status, revision,
                    event_ids_json, practiced_seconds, created_at, updated_at
                ) VALUES ('s-1', 'a-1', NULL, 'a-1', 'v-1',
                          'tutor_english', 'english-past-story', 'draft',
                          0, '[]', 0, '2026-08-09T00:00:00+00:00',
                          '2026-08-09T00:00:00+00:00')
                """
            )
        connection.execute(
            """
            INSERT INTO tutor_practice_sessions(
                session_id, account_id, subject_id, actor_id,
                voice_session_id, focus, task_id, status, revision,
                event_ids_json, practiced_seconds, created_at, updated_at
            ) VALUES ('s-1', 'a-1', 'subject-1', 'a-1', 'v-1',
                      'tutor_english', 'english-past-story', 'draft',
                      0, '[]', 0, '2026-08-09T00:00:00+00:00',
                      '2026-08-09T00:00:00+00:00')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="tutor authority columns"):
            connection.execute(
                """
                UPDATE tutor_practice_sessions
                SET actor_id = NULL WHERE session_id = 's-1'
                """
            )
        connection.commit()
    finally:
        connection.close()
