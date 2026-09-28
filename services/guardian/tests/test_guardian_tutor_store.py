"""PR-13: subject-scoped tutor persistence in the PostgreSQL guardian store.

Runs on ``guardian_postgres_store`` (production roles, RLS applies).  The
account-scoped governance of tutor evidence and the commit outbox, and the
authority-column guard, are covered in ``test_guardian_postgres_store.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.guardian.postgres_store import PostgresGuardianStore
from services.tutor.domain import PracticeConflictError, PracticeSession, StudyProgress

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
# The PostgreSQL store keys practice sessions by UUID.
SESSION_ID = "00000000-0000-0000-0000-00000000a001"


def _session(
    *,
    session_id: str = SESSION_ID,
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
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store

    saved = await store.save_practice_session(_session())
    assert saved.subject_id == "subject-1"
    await store.save_study_progress(_progress(), rebuilt_at=NOW)

    own = await store.practice_session(subject_id="subject-1", session_id=SESSION_ID)
    assert own is not None
    assert own.actor_id == "actor-1"
    assert own.voice_session_id == "voice-session-1"
    other = await store.practice_session(subject_id="subject-other", session_id=SESSION_ID)
    assert other is None
    own_progress = await store.study_progress(subject_id="subject-1")
    assert own_progress is not None
    assert own_progress.actor_id == "actor-1"
    assert await store.study_progress(subject_id="subject-other") is None


@pytest.mark.asyncio
async def test_save_rejects_cross_subject_and_stale_revision_cas(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
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
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await store.save_study_progress(_progress(), rebuilt_at=NOW)
    updated = _progress(practiced_seconds=1200, source_event_ids=("turn-1", "turn-2"))
    await store.save_study_progress(updated, rebuilt_at=NOW)

    progress = await store.study_progress(subject_id="subject-1")
    assert progress is not None
    assert progress.practiced_seconds == 1200
    assert progress.source_event_ids == ("turn-1", "turn-2")

    exported = await store.export_for_account(account_id="actor-1")
    progress_rows = exported["tutor_study_progress"]
    session_rows = exported["tutor_practice_sessions"]
    assert isinstance(progress_rows, list) and isinstance(session_rows, list)
    assert len(progress_rows) == 1
    assert len(session_rows) == 0


@pytest.mark.asyncio
async def test_one_owner_holds_progress_for_two_subjects(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await store.save_study_progress(_progress(subject_id="subject-1"), rebuilt_at=NOW)
    await store.save_study_progress(
        _progress(subject_id="subject-2", practiced_seconds=300),
        rebuilt_at=NOW,
    )
    # Rebuilding one subject updates only that subject's row.
    await store.save_study_progress(
        _progress(subject_id="subject-1", practiced_seconds=900),
        rebuilt_at=NOW,
    )

    first = await store.study_progress(subject_id="subject-1")
    second = await store.study_progress(subject_id="subject-2")
    assert first is not None and first.practiced_seconds == 900
    assert second is not None and second.practiced_seconds == 300
    assert first.actor_id == second.actor_id == "actor-1"

    exported = await store.export_for_account(account_id="actor-1")
    progress_rows = exported["tutor_study_progress"]
    assert isinstance(progress_rows, list)
    assert [row["subject_id"] for row in progress_rows] == [
        "subject-1",
        "subject-2",
    ]
    assert await store.remaining_account_rows(account_id="actor-1") == {
        "tutor_study_progress": 2
    }
    deleted = await store.delete_for_account(account_id="actor-1")
    assert deleted["tutor_study_progress"] == 2
    assert await store.remaining_account_rows(account_id="actor-1") == {}
