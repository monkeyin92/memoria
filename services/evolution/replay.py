"""Offline trajectory replay runner.

The runner deliberately reads the authoritative archive again at evaluation
time.  It never accepts a client-supplied transcript, speaker classification,
or generation fence, and it never treats ``actual_heard`` as a task result.
An evaluator returns bounded task/process/quality evidence; the runner binds
that evidence back to the canonical archive pair before persisting it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Protocol

from services.archive.domain import LifeArchivePort
from services.evolution.account_fence import AccountReadGuard, AccountWriteBlockedError
from services.evolution.curation import EvolutionControlPlane
from services.evolution.trajectory import (
    CanonicalTrajectory,
    CanonicalTrajectoryError,
    TrajectoryAssessment,
    TrajectoryReplayInput,
)


@dataclass(frozen=True, slots=True)
class OfflineTrajectoryReplayRequest:
    """A durable logical evaluation identity and its server-owned pair refs."""

    evaluation_id: str
    account_id: str
    user_event_id: str
    assistant_event_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("evaluation_id", self.evaluation_id),
            ("account_id", self.account_id),
            ("user_event_id", self.user_event_id),
            ("assistant_event_id", self.assistant_event_id),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a non-empty string <= 128 characters")


@dataclass(frozen=True, slots=True)
class OfflineTrajectoryReplayResult:
    evaluation_id: str
    signal_id: str | None
    skipped: bool


class OfflineTrajectoryEvaluator(Protocol):
    """Independent evaluator contract used only outside the request path."""

    evaluator_version: str

    async def evaluate(self, trajectory: TrajectoryReplayInput) -> TrajectoryAssessment | None: ...


class OfflineTrajectoryReplayWorker:
    """Replay canonical pairs through a distinct evaluator and append signals.

    A retry with the same ``evaluation_id`` produces the exact same signal id.
    ``EvolutionStore.append_signal`` then makes equal retries no-ops and turns
    inconsistent retry output into an immutable conflict instead of silently
    replacing evidence.  A re-keyed retry is also rejected when its canonical
    pair and evaluator version already have a signal; different evaluator
    versions remain independent reviews.
    """

    def __init__(
        self,
        archive: LifeArchivePort,
        control_plane: EvolutionControlPlane,
        evaluator: OfflineTrajectoryEvaluator,
        *,
        account_read_guard: AccountReadGuard | None = None,
    ) -> None:
        self._archive = archive
        self._control_plane = control_plane
        self._evaluator = evaluator
        self._account_read_guard = account_read_guard or _unguarded_read

    async def replay(
        self,
        request: OfflineTrajectoryReplayRequest,
    ) -> OfflineTrajectoryReplayResult:
        # Snapshot the canonical pair under a short lease.  The evaluator is
        # intentionally outside the lease so a slow external judge cannot
        # indefinitely block account deletion.
        try:
            with self._account_read_guard(request.account_id):
                if self._control_plane.store.is_account_deleting(request.account_id):
                    return _skipped_replay(request.evaluation_id)
                user_event, assistant_event = await asyncio.gather(
                    self._archive.event(
                        account_id=request.account_id,
                        event_id=request.user_event_id,
                    ),
                    self._archive.event(
                        account_id=request.account_id,
                        event_id=request.assistant_event_id,
                    ),
                )
                if user_event is None or assistant_event is None:
                    raise CanonicalTrajectoryError("canonical trajectory pair was not found")
                trajectory = CanonicalTrajectory.from_events(user_event, assistant_event)
                if self._control_plane.store.is_account_deleting(request.account_id):
                    return _skipped_replay(request.evaluation_id)
        except AccountWriteBlockedError:
            return _skipped_replay(request.evaluation_id)

        assessment = await self._evaluator.evaluate(trajectory.replay_input)
        if assessment is None:
            return _skipped_replay(request.evaluation_id)
        if assessment.evaluator_version != self._evaluator.evaluator_version:
            raise CanonicalTrajectoryError("evaluator returned an unexpected evaluator_version")
        try:
            with self._account_read_guard(request.account_id):
                if self._control_plane.store.is_account_deleting(request.account_id):
                    return _skipped_replay(request.evaluation_id)
                report = self._control_plane.observe(
                    assessment.observation(trajectory, evaluation_id=request.evaluation_id)
                )
        except AccountWriteBlockedError:
            # The durable fence may win between the final read and the
            # owner-private append. Drop the evaluation rather than exposing a
            # partial success or turning a deletion race into a worker crash.
            return _skipped_replay(request.evaluation_id)
        return OfflineTrajectoryReplayResult(
            evaluation_id=request.evaluation_id,
            signal_id=report.signal_id,
            skipped=False,
        )

    async def replay_all(
        self,
        requests: Iterable[OfflineTrajectoryReplayRequest],
    ) -> tuple[OfflineTrajectoryReplayResult, ...]:
        """Run in input order so evaluator side effects remain auditable."""

        return tuple([await self.replay(request) for request in requests])


__all__ = [
    "OfflineTrajectoryEvaluator",
    "OfflineTrajectoryReplayRequest",
    "OfflineTrajectoryReplayResult",
    "OfflineTrajectoryReplayWorker",
]


def _unguarded_read(account_id: str) -> AbstractContextManager[None]:
    del account_id
    return nullcontext()


def _skipped_replay(evaluation_id: str) -> OfflineTrajectoryReplayResult:
    return OfflineTrajectoryReplayResult(
        evaluation_id=evaluation_id,
        signal_id=None,
        skipped=True,
    )
