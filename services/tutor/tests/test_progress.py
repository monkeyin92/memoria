from datetime import UTC, datetime, timedelta

import pytest
from services.archive.domain import EvidenceEvent
from services.tutor.domain import PracticeConflictError
from services.tutor.progress import (
    PRACTICE_TURN_EVENT,
    apply_practice_event,
    practice_turn_from_evidence,
    project_study_progress,
)

NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)


def _event(
    event_id: str,
    *,
    outcome: str = "struggled",
    skill_key: str = "past-tense",
    day: int = 0,
    speaker_class: str = "owner",
    history_eligible: bool = True,
) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=event_id,
        account_id="student-1",
        event_type=PRACTICE_TURN_EVENT,
        occurred_at=NOW + timedelta(days=day),
        speaker_class=speaker_class,  # type: ignore[arg-type]
        source="tutor",
        session_id="practice-1",
        payload={
            "session_focus": "tutor_english",
            "task_id": "english-past-story",
            "skill_key": skill_key,
            "outcome": outcome,
            "duration_seconds": 600,
            "history_eligible": history_eligible,
        },
    )


def test_practice_session_reuses_draft_active_paused_completed_cas_pattern() -> None:
    created = apply_practice_event(
        None,
        event_id="practice-1:create",
        account_id="student-1",
        focus="tutor_english",
        task_id="english-past-story",
        action="create",
        occurred_at=NOW,
    )
    active = apply_practice_event(
        created,
        event_id="practice-1:active",
        account_id="student-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="active",
        expected_revision=0,
        occurred_at=NOW,
    )
    practiced = apply_practice_event(
        active,
        event_id="practice-1:tick",
        account_id="student-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="practice",
        expected_revision=1,
        duration_seconds=25 * 60,
        occurred_at=NOW,
    )
    paused = apply_practice_event(
        practiced,
        event_id="practice-1:paused",
        account_id="student-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="paused",
        expected_revision=2,
        occurred_at=NOW,
    )
    completed = apply_practice_event(
        paused,
        event_id="practice-1:completed",
        account_id="student-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="completed",
        expected_revision=3,
        occurred_at=NOW,
    )

    assert completed.status == "completed"
    assert completed.practiced_seconds == 25 * 60
    assert apply_practice_event(
        active,
        event_id="practice-1:active",
        account_id="student-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="active",
        expected_revision=999,
    ) == active
    with pytest.raises(PracticeConflictError, match="revision_conflict"):
        apply_practice_event(
            active,
            event_id="practice-1:stale",
            account_id="student-1",
            focus="tutor_english",
            task_id=created.task_id,
            action="paused",
            expected_revision=0,
        )


def test_projection_is_rebuildable_idempotent_and_clears_mastered_weak_points() -> None:
    struggled = _event("turn-1")
    second_day = _event("turn-2", outcome="struggled", day=1)
    mastered = _event("turn-3", outcome="mastered", day=2)
    current_weak = _event("turn-4", outcome="gave_up", skill_key="pronunciation", day=2)

    progress = project_study_progress(
        account_id="student-1",
        events=(struggled, second_day, struggled, mastered, current_weak),
    )

    assert progress.practiced_seconds == 4 * 600
    assert progress.current_streak_days == 3
    assert progress.weak_points == (("pronunciation", 2),)
    assert progress.mastered_skills == ("past-tense",)
    assert progress.source_event_ids == ("turn-1", "turn-2", "turn-3", "turn-4")


def test_projection_fails_closed_for_guest_or_history_ineligible_events() -> None:
    guest = _event("guest", speaker_class="guest")
    ineligible = _event("ineligible", history_eligible=False)

    assert practice_turn_from_evidence(guest) is None
    assert practice_turn_from_evidence(ineligible) is None
    assert project_study_progress(account_id="student-1", events=(guest, ineligible)).source_event_ids == ()

