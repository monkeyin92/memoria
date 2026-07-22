from datetime import UTC, datetime

import pytest
from services.growth.domain import GrowthTask, TaskConflictError
from services.growth.tasks import apply_task_event


def test_task_events_are_idempotent_and_use_compare_and_set() -> None:
    created = apply_task_event(
        None,
        event_id="task-1:create",
        kind="life_interview",
        action="create",
        occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    duplicate = apply_task_event(
        created,
        event_id="task-1:create",
        kind="life_interview",
        action="create",
        occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    assert duplicate == created
    active = apply_task_event(
        created,
        event_id="task-1:active",
        kind="life_interview",
        action="active",
        expected_revision=0,
        occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    assert active.status == "active"
    assert active.revision == 1
    with pytest.raises(TaskConflictError, match="revision_conflict"):
        apply_task_event(
            active,
            event_id="task-1:stale",
            kind="life_interview",
            action="paused",
            expected_revision=0,
            occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
        )


def test_task_state_machine_allows_pause_resume_complete_and_cancel() -> None:
    task = GrowthTask("t", "scenario_choice", "draft", 0, ())
    active = apply_task_event(task, event_id="1", kind=task.kind, action="active", expected_revision=0)
    paused = apply_task_event(active, event_id="2", kind=task.kind, action="paused", expected_revision=1)
    resumed = apply_task_event(paused, event_id="3", kind=task.kind, action="active", expected_revision=2)
    assert apply_task_event(resumed, event_id="4", kind=task.kind, action="completed", expected_revision=3).status == "completed"
    assert apply_task_event(task, event_id="5", kind=task.kind, action="cancelled", expected_revision=0).status == "cancelled"
