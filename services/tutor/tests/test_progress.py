"""Subject-scoped practice replay and study projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.archive.domain import EvidenceEvent
from services.tutor.domain import PracticeConflictError, PracticeTurn
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
    subject_id: str = "subject-1",
    actor_id: str = "actor-1",
    voice_session_id: str = "voice-session-1",
    outcome: str = "struggled",
    skill_key: str = "past-story",
    task_id: str = "english-past-story",
    day: int = 0,
    speaker_class: str = "owner",
    history_eligible: bool = True,
    envelope: bool = True,
) -> EvidenceEvent:
    payload: dict[str, object] = {
        "session_focus": "tutor_english",
        "task_id": task_id,
        "skill_key": skill_key,
        "outcome": outcome,
        "duration_seconds": 600,
        "history_eligible": history_eligible,
    }
    if envelope:
        payload.update(
            {
                "subject_id": subject_id,
                "actor_id": actor_id,
                "voice_session_id": voice_session_id,
                "device_id": "device-1",
                "binding_id": "binding-1",
                "binding_version": 3,
                "subject_revision": 2,
                "session_epoch": 4,
                "runtime_profile_id": "rp_1",
                "generation_id": 7,
                "turn_id": 3,
                "tool_epoch": 11,
                "policy_receipt_id": "receipt-tutor",
                "criterion_ids": [
                    "homework-self-guided.criterion.v1"
                    if task_id == "homework-self-guided"
                    else f"{task_id}.criterion.v1"
                ],
            }
        )
    return EvidenceEvent(
        event_id=event_id,
        account_id=actor_id,
        event_type=PRACTICE_TURN_EVENT,
        occurred_at=NOW + timedelta(days=day),
        speaker_class=speaker_class,  # type: ignore[arg-type]
        source="tutor",
        session_id="practice-1",
        payload=payload,
    )


def test_practice_session_reuses_draft_active_paused_completed_cas_pattern() -> None:
    created = apply_practice_event(
        None,
        event_id="practice-1:create",
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
        focus="tutor_english",
        task_id="english-past-story",
        action="create",
        occurred_at=NOW,
    )
    active = apply_practice_event(
        created,
        event_id="practice-1:active",
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="active",
        expected_revision=0,
        occurred_at=NOW,
    )
    practiced = apply_practice_event(
        active,
        event_id="practice-1:tick",
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
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
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="paused",
        expected_revision=2,
        occurred_at=NOW,
    )
    completed = apply_practice_event(
        paused,
        event_id="practice-1:completed",
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
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
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
        focus="tutor_english",
        task_id=created.task_id,
        action="active",
        expected_revision=999,
    ) == active
    with pytest.raises(PracticeConflictError, match="revision_conflict"):
        apply_practice_event(
            active,
            event_id="practice-1:stale",
            subject_id="subject-1",
            actor_id="actor-1",
            voice_session_id="voice-session-1",
            focus="tutor_english",
            task_id=created.task_id,
            action="paused",
            expected_revision=0,
        )


def test_practice_session_scope_is_bound_to_subject_actor_and_voice_session() -> None:
    created = apply_practice_event(
        None,
        event_id="practice-1:create",
        subject_id="subject-1",
        actor_id="actor-1",
        voice_session_id="voice-session-1",
        focus="tutor_english",
        task_id="english-past-story",
        action="create",
        occurred_at=NOW,
    )
    for overrides in (
        {"subject_id": "subject-other"},
        {"actor_id": "actor-other"},
        {"voice_session_id": "voice-session-other"},
    ):
        base = {
            "subject_id": "subject-1",
            "actor_id": "actor-1",
            "voice_session_id": "voice-session-1",
        }
        base.update(overrides)
        with pytest.raises(PracticeConflictError, match="practice_session_scope_mismatch"):
            apply_practice_event(
                created,
                event_id="practice-1:stale",
                focus="tutor_english",
                task_id=created.task_id,
                action="active",
                expected_revision=0,
                **base,
            )


def test_turn_from_evidence_requires_the_subject_bound_envelope() -> None:
    turn = practice_turn_from_evidence(_event("turn-1"), subject_id="subject-1")
    assert turn is not None
    assert turn.subject_id == "subject-1"
    assert turn.actor_id == "actor-1"
    assert turn.voice_session_id == "voice-session-1"
    assert turn.session_epoch == 4
    assert turn.runtime_profile_id == "rp_1"
    assert turn.device_id == "device-1"
    assert turn.binding_id == "binding-1"
    assert turn.binding_version == 3
    assert turn.subject_revision == 2
    assert turn.generation_id == 7
    assert turn.turn_id == 3
    assert turn.tool_epoch == 11
    assert turn.policy_receipt_id == "receipt-tutor"

    assert practice_turn_from_evidence(_event("turn-2"), subject_id="subject-other") is None
    assert practice_turn_from_evidence(_event("turn-3", envelope=False), subject_id="subject-1") is None
    assert practice_turn_from_evidence(_event("turn-4", skill_key="not-in-catalog"), subject_id="subject-1") is None
    assert practice_turn_from_evidence(_event("turn-5", speaker_class="guest"), subject_id="subject-1") is None
    assert practice_turn_from_evidence(_event("turn-6", history_eligible=False), subject_id="subject-1") is None


def test_projection_is_subject_scoped_and_rebuildable() -> None:
    struggled = _event("turn-1")
    second_day = _event("turn-2", outcome="struggled", day=1)
    mastered = _event("turn-3", outcome="mastered", day=2)
    current_weak = _event(
        "turn-4",
        outcome="gave_up",
        task_id="english-weekly-recap",
        skill_key="weekly-recap",
        day=2,
    )
    other_subject = _event("turn-5", subject_id="subject-other", day=2)
    quarantined = _event("turn-6", envelope=False, day=2)

    progress = project_study_progress(
        subject_id="subject-1",
        events=(struggled, second_day, struggled, mastered, current_weak, other_subject, quarantined),
    )

    assert progress.practiced_seconds == 4 * 600
    assert progress.current_streak_days == 3
    assert progress.weak_points == (("weekly-recap", 2),)
    assert progress.mastered_skills == ("past-story",)
    assert progress.source_event_ids == ("turn-1", "turn-2", "turn-3", "turn-4")
    assert progress.subject_id == "subject-1"


def test_projection_fails_closed_for_guest_or_history_ineligible_events() -> None:
    guest = _event("guest", speaker_class="guest")
    ineligible = _event("ineligible", history_eligible=False)

    assert practice_turn_from_evidence(guest, subject_id="subject-1") is None
    assert practice_turn_from_evidence(ineligible, subject_id="subject-1") is None
    assert (
        project_study_progress(
            subject_id="subject-1",
            events=(guest, ineligible),
        ).source_event_ids
        == ()
    )


def test_turn_carries_the_fence_for_history_eligibility() -> None:
    turn = practice_turn_from_evidence(_event("turn-fenced"), subject_id="subject-1")
    assert isinstance(turn, PracticeTurn)
    assert turn.history_eligible is True
