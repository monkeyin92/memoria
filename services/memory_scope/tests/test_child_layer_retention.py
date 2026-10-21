from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ChildMemoryKind,
)
from services.memory_scope.child_layer_retention import (
    ChildLayerRetentionWorker,
    ExpirySummary,
    IdleActivityUnavailable,
    expire_idle_archives,
    idle_archive_subjects,
)

NOW = datetime(2026, 10, 15, tzinfo=UTC)
_CHILD_LOGGER = "services.memory_scope.child_layer_retention"


@pytest.fixture(autouse=True)
def _capture_child_logs_from_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_app installs a non-propagating handler; point caplog back at root.
    monkeypatch.setattr(logging.getLogger(_CHILD_LOGGER), "propagate", True)


def _erased(records: int, events: int) -> dict[str, Any]:
    return {
        "erased": {"memory_records": records, "memory_status_events": events},
        "remaining": {"memory_records": 0},
    }


class _Layers:
    def __init__(
        self,
        *,
        due: Sequence[str] = (),
        archived: Sequence[str] = (),
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self._due = tuple(due)
        self._archived = tuple(archived)
        self._broken = broken

    async def kind_subjects(
        self, kind: ChildMemoryKind, *, only_expired: bool = False
    ) -> tuple[str, ...]:
        if kind == KIND_DIALOGUE_SUMMARY:
            assert only_expired is True
            return self._due
        return self._archived

    async def expire_due_summaries(self, *, subject_id: str) -> Mapping[str, Any]:
        return _erased(2, 1)

    async def purge_archive(self, *, subject_id: str) -> Mapping[str, Any]:
        if subject_id in self._broken:
            raise RuntimeError("child_archive remaining after purge")
        return _erased(3, 0)


class _Activity:
    def __init__(self, last_active: Mapping[str, datetime] | None) -> None:
        self._last_active = last_active

    async def last_active_for(self, subjects: Sequence[str]) -> dict[str, datetime]:
        if self._last_active is None:
            raise asyncpg.UndefinedTableError('relation "guardian_subject_activity" does not exist')
        return {key: value for key, value in self._last_active.items() if key in subjects}


async def test_idle_archive_subjects_keeps_idle_and_never_active_subjects() -> None:
    layers = _Layers(archived=("recent", "boundary", "old", "missing"))
    activity = _Activity(
        {
            "recent": NOW - timedelta(days=1),
            "boundary": NOW - timedelta(days=180),
            "old": NOW - timedelta(days=200),
        }
    )

    idle = await idle_archive_subjects(layers, activity, now=NOW)

    assert idle == ["boundary", "old", "missing"]


@pytest.mark.parametrize("activity", [None, _Activity(None)])
async def test_idle_archive_subjects_fails_closed_without_activity(
    activity: _Activity | None,
) -> None:
    layers = _Layers(archived=("old",))

    with pytest.raises(IdleActivityUnavailable):
        await idle_archive_subjects(layers, activity, now=NOW)


async def test_one_subject_that_cannot_be_purged_does_not_stop_the_rest() -> None:
    layers = _Layers(broken=frozenset({"b"}))

    results = await expire_idle_archives(layers, ["a", "b", "c"])

    assert [result["subject_id"] for result in results] == ["a", "b", "c"]
    assert results[1]["error"] == "RuntimeError: child_archive remaining after purge"
    assert ExpirySummary.of(KIND_CHILD_ARCHIVE, ["a", "b", "c"], results) == ExpirySummary(
        kind=KIND_CHILD_ARCHIVE,
        subjects=3,
        failed=1,
        memory_records=6,
        memory_status_events=0,
    )


async def test_worker_logs_counts_and_never_subject_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    layers = _Layers(due=("child-due",), archived=("child-idle", "child-active"))
    activity = _Activity({"child-active": NOW, "child-idle": NOW - timedelta(days=365)})
    worker = ChildLayerRetentionWorker(layers, activity)

    with caplog.at_level(logging.INFO, logger=_CHILD_LOGGER):
        summaries = await worker.run_once(now=NOW)

    assert summaries == [
        ExpirySummary(KIND_DIALOGUE_SUMMARY, 1, 0, 2, 1),
        ExpirySummary(KIND_CHILD_ARCHIVE, 1, 0, 3, 0),
    ]
    assert caplog.messages == [
        "child layer retention kind=dialogue_summary status=ok subjects=1 failed=0"
        " memory_records=2 memory_status_events=1",
        "child layer retention kind=child_archive status=ok subjects=1 failed=0"
        " memory_records=3 memory_status_events=0",
        "child layer retention round"
        " kind=dialogue_summary status=ok subjects=1 failed=0"
        " memory_records=2 memory_status_events=1"
        " kind=child_archive status=ok subjects=1 failed=0"
        " memory_records=3 memory_status_events=0",
    ]
    joined = " ".join(caplog.messages)
    for subject_id in ("child-due", "child-idle", "child-active"):
        assert subject_id not in joined


async def test_worker_skips_archives_but_still_expires_summaries_without_activity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    layers = _Layers(due=("child-due",), archived=("child-idle",))
    worker = ChildLayerRetentionWorker(layers, _Activity(None))

    with caplog.at_level(logging.INFO, logger=_CHILD_LOGGER):
        summaries = await worker.run_once(now=NOW)

    assert summaries == [ExpirySummary(KIND_DIALOGUE_SUMMARY, 1, 0, 2, 1)]
    assert caplog.messages == [
        "child layer retention kind=dialogue_summary status=ok subjects=1 failed=0"
        " memory_records=2 memory_status_events=1",
        "child layer retention kind=child_archive status=skipped subjects=0 failed=0"
        " memory_records=0 memory_status_events=0 reason=IdleActivityUnavailable",
        "child layer retention round"
        " kind=dialogue_summary status=ok subjects=1 failed=0"
        " memory_records=2 memory_status_events=1"
        " kind=child_archive status=skipped subjects=0 failed=0"
        " memory_records=0 memory_status_events=0",
    ]
    joined = " ".join(caplog.messages)
    assert "guardian_subject_activity" not in joined
    assert "child-due" not in joined


async def test_worker_logs_only_exception_type_when_a_pass_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _BrokenArchive(_Layers):
        async def kind_subjects(
            self, kind: ChildMemoryKind, *, only_expired: bool = False
        ) -> tuple[str, ...]:
            if kind == KIND_CHILD_ARCHIVE:
                raise RuntimeError("child-secret-id cannot be read")
            return await super().kind_subjects(kind, only_expired=only_expired)

    worker = ChildLayerRetentionWorker(_BrokenArchive(due=("child-due",)), None)

    with caplog.at_level(logging.INFO, logger=_CHILD_LOGGER):
        summaries = await worker.run_once(now=NOW)

    assert summaries == [ExpirySummary(KIND_DIALOGUE_SUMMARY, 1, 0, 2, 1)]
    assert caplog.messages == [
        "child layer retention kind=dialogue_summary status=ok subjects=1 failed=0"
        " memory_records=2 memory_status_events=1",
        "child layer retention kind=child_archive status=failed subjects=0 failed=0"
        " memory_records=0 memory_status_events=0 error_type=RuntimeError",
        "child layer retention round"
        " kind=dialogue_summary status=ok subjects=1 failed=0"
        " memory_records=2 memory_status_events=1"
        " kind=child_archive status=failed subjects=0 failed=0"
        " memory_records=0 memory_status_events=0",
    ]
    joined = " ".join(caplog.messages)
    assert "child-secret-id" not in joined
    assert "RuntimeError:" not in joined
    assert "Traceback" not in joined
    assert all(record.exc_info is None for record in caplog.records)


async def test_worker_logs_partial_purge_failures_without_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    layers = _Layers(archived=("child-idle",), broken=frozenset({"child-idle"}))
    activity = _Activity({"child-idle": NOW - timedelta(days=365)})
    worker = ChildLayerRetentionWorker(layers, activity)

    with caplog.at_level(logging.INFO, logger=_CHILD_LOGGER):
        await worker.run_once(now=NOW)

    assert caplog.messages[1] == (
        "child layer retention kind=child_archive status=ok subjects=1 failed=1"
        " memory_records=0 memory_status_events=0"
    )
    assert "child_archive remaining after purge" not in " ".join(caplog.messages)


async def test_worker_rejects_a_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        ChildLayerRetentionWorker(_Layers(), None, interval_s=0)
