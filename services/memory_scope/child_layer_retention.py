"""Daily physical expiry of the two child memory layers.

``dialogue_summary`` rows are deleted once ``retention_expires_at`` passes (30
days). ``child_archive`` rows are deleted for subjects idle past
``ARCHIVE_IDLE_TTL`` (180 days). The 30-day and 7-day parent reminders before
an archive purge are not sent yet; the purge runs anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from services.guardian.child_memory import (
    ARCHIVE_IDLE_TTL,
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ChildMemoryKind,
)
from services.guardian.subject_activity import SubjectActivityStore
from services.memory_scope.child_layers import ChildLayerStore

logger = logging.getLogger(__name__)

CHILD_LAYER_RETENTION_INTERVAL_S = 86400.0


class IdleActivityUnavailable(RuntimeError):
    """Subject activity cannot be read, so no archive subject counts as idle."""


@dataclass(frozen=True, slots=True)
class ExpirySummary:
    """Log-safe aggregate of one expiry pass: counts only, never subject ids."""

    kind: ChildMemoryKind
    subjects: int
    failed: int
    memory_records: int
    memory_status_events: int

    @classmethod
    def of(
        cls,
        kind: ChildMemoryKind,
        subjects: Sequence[str],
        results: Sequence[Mapping[str, Any]],
    ) -> ExpirySummary:
        erased = [result["erased"] for result in results if "erased" in result]
        return cls(
            kind=kind,
            subjects=len(subjects),
            failed=sum(1 for result in results if "error" in result),
            memory_records=sum(int(item.get("memory_records", 0)) for item in erased),
            memory_status_events=sum(int(item.get("memory_status_events", 0)) for item in erased),
        )


async def due_summary_subjects(layers: ChildLayerStore) -> tuple[str, ...]:
    return await layers.kind_subjects(KIND_DIALOGUE_SUMMARY, only_expired=True)


async def idle_archive_subjects(
    layers: ChildLayerStore,
    activity: SubjectActivityStore | None,
    *,
    now: datetime,
) -> list[str]:
    """Archive subjects whose last activity is older than ARCHIVE_IDLE_TTL or missing."""

    subjects = await layers.kind_subjects(KIND_CHILD_ARCHIVE)
    if not subjects:
        return []
    if activity is None:
        raise IdleActivityUnavailable("no subject activity store is configured")
    try:
        last_active = await activity.last_active_for(subjects)
    except asyncpg.UndefinedTableError as exc:
        raise IdleActivityUnavailable("guardian_subject_activity does not exist") from exc
    cutoff = now - ARCHIVE_IDLE_TTL
    return [
        subject_id
        for subject_id in subjects
        if (last := last_active.get(subject_id)) is None or last <= cutoff
    ]


async def expire_due_summaries(
    layers: ChildLayerStore, subjects: Sequence[str]
) -> list[dict[str, Any]]:
    return await _each(
        subjects, lambda subject_id: layers.expire_due_summaries(subject_id=subject_id)
    )


async def expire_idle_archives(
    layers: ChildLayerStore, subjects: Sequence[str]
) -> list[dict[str, Any]]:
    return await _each(subjects, lambda subject_id: layers.purge_archive(subject_id=subject_id))


async def _each(
    subjects: Sequence[str],
    erase: Callable[[str], Awaitable[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    # One subject that cannot be erased must not starve every subject after it.
    results: list[dict[str, Any]] = []
    for subject_id in subjects:
        try:
            results.append({"subject_id": subject_id, **await erase(subject_id)})
        except (RuntimeError, asyncpg.PostgresError) as exc:
            results.append({"subject_id": subject_id, "error": f"{type(exc).__name__}: {exc}"})
    return results


class ChildLayerRetentionWorker:
    """Run both expiry jobs at startup, then once per interval."""

    def __init__(
        self,
        layers: ChildLayerStore,
        activity: SubjectActivityStore | None,
        *,
        interval_s: float = CHILD_LAYER_RETENTION_INTERVAL_S,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("child layer retention interval must be positive")
        self._layers = layers
        self._activity = activity
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="child-layer-retention")

    async def stop(self) -> None:
        # Cancelling mid-pass is safe: each subject erase is one transaction and
        # the next pass deletes whatever is still due.
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self, *, now: datetime | None = None) -> list[ExpirySummary]:
        """Expire due summaries, then idle archives. A job that cannot run is logged and skipped."""

        stamped = now or datetime.now(UTC)
        passes = (
            await _logged(KIND_DIALOGUE_SUMMARY, self._expire_summaries()),
            await _logged(KIND_CHILD_ARCHIVE, self._expire_archives(stamped)),
        )
        return [summary for summary in passes if summary is not None]

    async def _expire_summaries(self) -> ExpirySummary:
        subjects = await due_summary_subjects(self._layers)
        results = await expire_due_summaries(self._layers, subjects)
        return ExpirySummary.of(KIND_DIALOGUE_SUMMARY, subjects, results)

    async def _expire_archives(self, now: datetime) -> ExpirySummary:
        subjects = await idle_archive_subjects(self._layers, self._activity, now=now)
        results = await expire_idle_archives(self._layers, subjects)
        return ExpirySummary.of(KIND_CHILD_ARCHIVE, subjects, results)

    async def _run(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval_s)


async def _logged(kind: ChildMemoryKind, job: Awaitable[ExpirySummary]) -> ExpirySummary | None:
    try:
        summary = await job
    except IdleActivityUnavailable as exc:
        logger.warning("child layer retention kind=%s skipped reason=%s", kind, exc)
        return None
    except Exception:
        logger.exception("child layer retention kind=%s failed", kind)
        return None
    logger.info(
        "child layer retention kind=%s subjects=%s failed=%s"
        " memory_records=%s memory_status_events=%s",
        summary.kind,
        summary.subjects,
        summary.failed,
        summary.memory_records,
        summary.memory_status_events,
    )
    return summary


__all__ = [
    "CHILD_LAYER_RETENTION_INTERVAL_S",
    "ChildLayerRetentionWorker",
    "ExpirySummary",
    "IdleActivityUnavailable",
    "due_summary_subjects",
    "expire_due_summaries",
    "expire_idle_archives",
    "idle_archive_subjects",
]
