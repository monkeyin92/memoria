"""FunASR task-failure bookkeeping: one sanitized failure per provider task.

``FunASRTaskFailureMixin`` is composed into ``FunASRSession``; the session still
owns the websocket loops, PCM ring and segment evidence, and the rotation and
finish flow, while the shared state this mixin reads is declared here only for
type checking.  The failure record, its session event context and the
fail-closed task fence live together because every caller that reacts to a
provider task failure needs the same three answers: what failed, which task
epoch it belonged to, and whether that task is still the current one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from websockets.asyncio.client import ClientConnection

from services.agent.src.providers.funasr_protocol import (
    FunASRServerEvent,
    sanitize_diagnostic_text,
    sanitize_error_code,
    sanitize_error_message,
)
from services.agent.src.providers.provider_errors import APIConnectionError

logger = logging.getLogger(__name__)


def _is_empty_audio_error(error_code: object) -> bool:
    """Return whether FunASR rejected one task because it found no usable audio."""

    return str(error_code or "").strip().casefold() == "emptyaudio"


@dataclass(frozen=True, slots=True)
class FunASRTaskFailure:
    task_id: str
    task_epoch: int
    segment_epoch: int
    error_code: str | None
    error_message: str


@dataclass(frozen=True, slots=True)
class FunASRTaskEventContext:
    """Immutable sample-clock context for one provider task."""

    task_epoch: int
    segment_epoch: int
    sample_origin: int
    audio_end_sample: int | None
    boundary_observed: bool


class FunASRTaskFailureMixin:
    """Record and classify FunASR provider task failures."""

    if TYPE_CHECKING:
        # Shared state owned by FunASRSession.__init__.
        task_id: str | None
        events: asyncio.Queue[FunASRServerEvent]
        _failed: bool
        _closed: bool
        _ws: ClientConnection | None
        _ready: asyncio.Event
        _rotation_pending: bool
        _task_epoch: int
        _segment_epoch: int
        _finished_task_id: str | None
        _replaceable_provider_failure: bool
        _last_task_failure: FunASRTaskFailure | None
        _task_sample_origin: int
        _task_audio_end_sample: int | None
        _task_event_contexts: dict[str, FunASRTaskEventContext]
        _task_event_order: deque[str]
        _task_failed_event: asyncio.Event
        _task_started_event: asyncio.Event

        # Core session members consumed by this mixin.
        def _clear_pcm_ring(self) -> None: ...

        def _mark_ws_disconnected(self) -> None: ...

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def last_task_failure(self) -> FunASRTaskFailure | None:
        return self._last_task_failure

    def _failure_matches_current_task(self) -> bool:
        failure = self._last_task_failure
        if failure is None:
            return False
        return failure.task_epoch == max(1, self._task_epoch) and (
            not self.task_id or failure.task_id == self.task_id
        )

    def _has_replaceable_empty_audio_failure(self) -> bool:
        failure = self._last_task_failure
        return bool(
            self._replaceable_provider_failure
            and failure is not None
            and _is_empty_audio_error(failure.error_code)
            and self._failure_matches_current_task()
        )

    def _record_task_failure(
        self,
        *,
        task_id: str | None = None,
        task_epoch: int | None = None,
        segment_epoch: int | None = None,
        error_code: Any = None,
        error_message: Any = None,
    ) -> FunASRTaskFailure:
        failure = FunASRTaskFailure(
            task_id=sanitize_diagnostic_text(
                str(task_id or self.task_id or "unknown"),
                fallback="unknown",
                limit=128,
            ),
            task_epoch=max(1, task_epoch if task_epoch is not None else self._task_epoch),
            segment_epoch=max(
                1,
                segment_epoch if segment_epoch is not None else self._segment_epoch,
            ),
            error_code=sanitize_error_code(error_code),
            error_message=sanitize_error_message(
                error_message,
                fallback="FunASR task failed",
            ),
        )
        self._last_task_failure = failure
        logger.warning(
            "FunASR task failure task_id=%s task_epoch=%s segment_epoch=%s "
            "error_type=FunASRTaskFailure error_code=%s",
            failure.task_id,
            failure.task_epoch,
            failure.segment_epoch,
            failure.error_code or "unknown",
        )
        return failure

    def _failure_summary(self, prefix: str) -> str:
        failure = self._last_task_failure
        if failure is None:
            return (
                f"{prefix} task_id={self.task_id or 'unknown'} "
                f"task_epoch={max(1, self._task_epoch)} "
                f"segment_epoch={max(1, self._segment_epoch)} "
                "error_code=unknown"
            )
        return (
            f"{prefix} task_id={failure.task_id} "
            f"task_epoch={failure.task_epoch} "
            f"segment_epoch={failure.segment_epoch} "
            f"error_code={failure.error_code or 'unknown'}"
        )

    def _failure_exception(self, prefix: str) -> APIConnectionError:
        return APIConnectionError(self._failure_summary(prefix))

    def _remember_current_task_event_context(self) -> None:
        task_id = self.task_id or ""
        if not task_id:
            return
        if task_id not in self._task_event_contexts:
            self._task_event_order.append(task_id)
        self._task_event_contexts[task_id] = FunASRTaskEventContext(
            task_epoch=max(1, self._task_epoch),
            segment_epoch=max(1, self._segment_epoch),
            sample_origin=self._task_sample_origin,
            audio_end_sample=self._task_audio_end_sample,
            boundary_observed=self._finished_task_id == task_id,
        )
        while len(self._task_event_order) > 8:
            expired = self._task_event_order.popleft()
            self._task_event_contexts.pop(expired, None)

    def task_event_context(self, task_id: str) -> FunASRTaskEventContext | None:
        """Return the preserved epoch/origin for a queued provider event."""

        if task_id and task_id == self.task_id:
            self._remember_current_task_event_context()
        return self._task_event_contexts.get(task_id)

    def _task_context_matches(
        self,
        *,
        ws: ClientConnection,
        task_id: str,
        task_epoch: int,
    ) -> bool:
        """Return whether a PCM send still targets the same live task."""

        return (
            not self._closed
            and not self._failed
            and self._ws is ws
            and (self.task_id == task_id or (not task_id and self.task_id is None))
            and self._task_epoch == task_epoch
            and (not task_id or self._finished_task_id != task_id)
            and self._ready.is_set()
        )

    async def _fail_task_fence(
        self,
        *,
        task_id: str | None = None,
        task_epoch: int | None = None,
        error_code: str = "task_fence",
        error_message: str = "FunASR task state changed during PCM send",
        failed_ws: ClientConnection | None = None,
        enqueue_event: bool = True,
    ) -> FunASRTaskFailure:
        """Fail closed after an ambiguous provider/task transition.

        Once a PCM frame may have crossed a provider boundary, the frame is not
        replayed and the session cannot be made ready again by a late event.
        """

        failure = self._record_task_failure(
            task_id=task_id,
            task_epoch=task_epoch,
            error_code=error_code,
            error_message=error_message,
        )
        self._failed = True
        self._replaceable_provider_failure = False
        self._ready.clear()
        self._rotation_pending = False
        self._task_failed_event.set()
        self._task_started_event.set()
        self._clear_pcm_ring()
        ws = failed_ws if failed_ws is not None else self._ws
        if ws is not None and self._ws is ws:
            self._ws = None
            self._mark_ws_disconnected()
            with contextlib.suppress(Exception):
                await ws.close()
        if enqueue_event:
            self.events.put_nowait(
                FunASRServerEvent(
                    event="task-failed",
                    task_id=failure.task_id,
                    error_code=failure.error_code,
                    error_message=failure.error_message,
                )
            )
        return failure
