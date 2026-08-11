"""Listener-cue scheduling owner (deep module).

The micro-pause -> observe_partial -> play chain runs as one identity-owned
background task so the epoch rotation can capture and bounded-cancel it; a
late cue can never be tagged with a newer subject.  Kept out of
``DuplexRuntime`` to hold the module line budget while preserving the exact
owner semantics (P0-1 pattern).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime
from typing import Any

from services.agent.src.orchestration.cue_scheduler import ListenerCue
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)


def publish_listener_cue(runtime: Any, cue: ListenerCue, state: str) -> None:
    """Publish one cue lifecycle event under the cue's own identity fence."""

    if cue.fence.session_epoch != runtime.fence.session_epoch:
        # Old identity epoch: drop, never tag with the current subject.
        return
    runtime._publish(
        {
            "type": "listener_cue",
            "session_id": runtime.session_id,
            "cue_id": cue.cue_id,
            "cue_epoch": cue.cue_epoch,
            "user_turn_id": cue.user_turn_id,
            "state": state,
            "phase": runtime.interaction_phase.value,
            "at": datetime.now(UTC).isoformat(),
        },
        fence=cue.fence,
    )


async def stop_cue_handle(handle: Any) -> None:
    """Stop a physical cue handle, awaiting an async stop/flush."""

    stop = getattr(handle, "stop", None)
    if callable(stop):
        result = stop()
        if inspect.isawaitable(result):
            await result


def cancel_listener_cue(runtime: Any) -> None:
    """Sync cancel seam (interrupt/close): stop the active cue and its
    candidate; the epoch drain owns awaiting a playing cue task."""

    cancel_listener_cue_candidate(runtime)
    cue = runtime._active_listener_cue
    runtime.cue_scheduler.cancel_turn()
    handle = runtime._active_listener_cue_handle
    stop = getattr(handle, "stop", None)
    if callable(stop):
        result = stop()
        if inspect.isawaitable(result):
            # Sync seam: run the async stop detached; the drain awaits the
            # captured cue owner task.
            asyncio.ensure_future(result)
    runtime._active_listener_cue = None
    runtime._active_listener_cue_handle = None
    if cue is not None:
        publish_listener_cue(runtime, cue, "cancelled")
    if runtime.interaction_phase is InteractionPhase.BACKCHANNEL:
        runtime.set_interaction_phase(
            InteractionPhase.USER_SPEAKING,
            cause="listener_cue_cancelled",
            publish=False,
        )


def cancel_listener_cue_candidate(runtime: Any) -> None:
    task = runtime._listener_cue_candidate_task
    if task is not None and not task.done():
        task.cancel()
    runtime._listener_cue_candidate_task = None


def schedule_listener_cue(
    runtime: Any,
    text: str,
    *,
    now_ns: int | None,
) -> None:
    """Schedule one cue after the micro pause; the whole chain stays under
    ``_listener_cue_candidate_task`` so rotation/close own it."""

    runtime._cancel_listener_cue_candidate()
    observed_at_ns = now_ns if now_ns is not None else time.monotonic_ns()
    frozen_fence = runtime.fence

    async def _after_micro_pause() -> None:
        try:
            await asyncio.sleep(runtime.cue_scheduler.pause_ms / 1000)
            cue = runtime.cue_scheduler.observe_partial(
                text,
                fence=frozen_fence,
                now_ns=observed_at_ns + runtime.cue_scheduler.pause_ms * 1_000_000,
                aec_healthy=runtime._listener_cue_aec_healthy,
                main_response_active=runtime.orchestrator.state
                in {
                    ConversationState.THINKING,
                    ConversationState.SPEAKING,
                    ConversationState.TOOL_WAITING,
                },
            )
            if cue is not None:
                await runtime._play_listener_cue(cue)
        finally:
            # The loop may already be closed during teardown; never raise
            # from a finally on that path (the reference is also cleared by
            # the rotation/close seams).
            try:
                still_owner = (
                    runtime._listener_cue_candidate_task
                    is asyncio.current_task()
                )
            except RuntimeError:
                still_owner = False
            if still_owner:
                runtime._listener_cue_candidate_task = None

    runtime._listener_cue_candidate_task = runtime._spawn(
        _after_micro_pause(),
        name="listener-cue-micro-pause",
    )


__all__ = [
    "ListenerCue",
    "cancel_listener_cue",
    "cancel_listener_cue_candidate",
    "publish_listener_cue",
    "schedule_listener_cue",
    "stop_cue_handle",
]
