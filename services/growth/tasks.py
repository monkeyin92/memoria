"""Pure replay of task events stored in the Evidence Ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from services.growth.domain import GrowthTask, TaskConflictError, TaskKind, TaskStatus

TaskEventAction = Literal["create", "active", "paused", "completed", "cancelled", "response"]
_ALLOWED: dict[TaskStatus, frozenset[TaskEventAction]] = {
    "draft": frozenset({"active", "cancelled"}),
    "active": frozenset({"paused", "completed", "cancelled", "response"}),
    "paused": frozenset({"active", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


def apply_task_event(
    task: GrowthTask | None,
    *,
    event_id: str,
    kind: TaskKind,
    action: TaskEventAction,
    expected_revision: int | None = None,
    occurred_at: datetime | None = None,
) -> GrowthTask:
    now = occurred_at or datetime.now(UTC)
    if task is None:
        if action != "create":
            raise TaskConflictError("task_not_found")
        return GrowthTask(event_id.removesuffix(":create"), kind, "draft", 0, (event_id,), created_at=now, updated_at=now)
    if event_id in task.event_ids:
        return task
    if kind != task.kind:
        raise TaskConflictError("invalid_transition")
    if expected_revision != task.revision:
        raise TaskConflictError("revision_conflict")
    if action not in _ALLOWED[task.status]:
        raise TaskConflictError("invalid_transition")
    status = cast(TaskStatus, task.status if action == "response" else action)
    return GrowthTask(
        task.task_id,
        task.kind,
        status,
        task.revision + 1,
        (*task.event_ids, event_id),
        task.prompt_id,
        task.created_at,
        now,
    )
