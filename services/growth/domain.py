"""Small records for replaying S4 learning tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

TaskKind = Literal["natural_chat", "life_interview", "scenario_choice", "decision_review"]
TaskStatus = Literal["draft", "active", "paused", "completed", "cancelled"]


class TaskConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GrowthTask:
    task_id: str
    kind: TaskKind
    status: TaskStatus
    revision: int
    event_ids: tuple[str, ...]
    prompt_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
