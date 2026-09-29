"""Atomic identity-state reset for authority-loss transitions (deep module).

DuplexRuntime delegates the physical cache clearing of a degraded identity
epoch here so the runtime method stays a thin seam (P0-1 pattern).  The reset
must run between turns, synchronously, together with the epoch advance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any


def clear_identity_private_state(runtime: Any) -> None:
    """Drop every old-subject field/cache of one DuplexRuntime instance.

    Covers the per-generation records (plans, TTS references, provenance,
    voice, eligibility, modality -- interaction policies are not subject data
    and are epoch-filtered only when the rotation installs a policy), speaker
    PCM/acoustic evidence, pending playback/expression state and the in-flight
    personal voice refresh.  Transcript revisions and the event
    sequence are session-level UI audit and deliberately survive.
    """

    runtime._generation_records.clear()
    runtime._emotion_segments_by_turn.clear()
    runtime._speaker_pcm.clear()
    runtime._speaker_decision = None
    runtime._speaker_class = "uncertain"
    runtime._voice_floor.update(pending_assistant_text="", played_assistant_text="")
    runtime._assistant_expression_fence = None
    runtime._pending_tool_results = 0
    runtime._enroll_fence = None
    runtime._sticky_interrupt_epoch = None
    runtime._sticky_interrupt_route = None
    runtime._sticky_interrupt_text = ""
    runtime._interaction_decision_epoch = None
    runtime._interaction_decision = None
    runtime._interaction_decision_text = ""
    runtime._target_focus_pending_epoch = None
    runtime._paused_reply_available = False
    runtime._paused_reply_binding = None
    runtime._pending_semantic_pause_epoch = None
    runtime._pending_semantic_pause_binding = None
    voice_task = runtime._voice_profile_refresh_task
    if voice_task is not None and not voice_task.done():
        voice_task.cancel()
    runtime._voice_profile_refresh_task = None
    speaker_task = getattr(runtime, "_speaker_classification_task", None)
    if speaker_task is not None and not speaker_task.done():
        speaker_task.cancel()
    runtime._speaker_classification_task = None


def exact_fence_commit_authority(
    runtime: Any,
    fence: Any,
    capability: str | None,
    evidence: Any = None,
) -> bool:
    """TEST-REFERENCE-ONLY exact-fence check for commit-port adapters.

    Not installed by production wiring.  A real transactional commit port
    adapter (Control/Policy baseline) must additionally verify the
    action-specific PolicyReceipt/current evidence; this helper only checks
    the complete current fence, an unexpired evidence marker and the signed
    RuntimeProfile capability.  While the real port is unwired, production
    side-effect registration stays fail closed.
    """

    if not fence.matches(runtime.fence):
        return False
    if evidence is None:
        return False
    expires_at = getattr(evidence, "expires_at", None)
    if expires_at is not None and expires_at <= datetime.now(UTC):
        return False
    permitted = runtime.orchestrator.runtime_profiles.permits(
        fence,
        current_fence=runtime.fence,
        capability=capability,
    )
    return bool(permitted)


_IDENTITY_TASK_FIELDS = (
    "_speaker_classification_task",
    "_context_snapshot_prepare_task",
    "_voice_profile_refresh_task",
)


def capture_identity_tasks(runtime: Any) -> list[Any]:
    """Capture every old identity-owned task BEFORE any state reset.

    The references are kept (never dropped first) so the drain barrier can
    bounded-cancel-and-await each of them; swallowing handlers cannot keep
    running unobserved and late-complete results stay logically void.
    """

    return [
        task for name in _IDENTITY_TASK_FIELDS if (task := getattr(runtime, name, None)) is not None
    ]


def invalidate_identity_epochs(runtime: Any) -> None:
    """Bump identity-private epochs so late results are logically void.

    A higher ``_speaker_epoch`` voids late speaker classifications and every
    speech-epoch-bound control binding; a higher
    ``_context_snapshot_prepare_epoch`` voids late snapshot drafts.
    """

    runtime._speaker_epoch += 1
    runtime._context_snapshot_prepare_epoch += 1
    runtime._interaction_decision_epoch = None
    runtime._interaction_decision = None
    runtime._interaction_decision_text = ""
    runtime._target_focus_epoch = None
    runtime._target_focus_pending_epoch = None
    discard = getattr(runtime._speech_epoch_assembler, "discard_current", None)
    if callable(discard):
        discard()


async def drain_epoch_rotation(runtime: Any, old_fence: Any) -> None:
    """Async drain barrier after a synchronous epoch bump (PR-08).

    Stops/flushes the production LiveKit playback owner, cancels-and-waits
    old LLM/TTS work with a bounded timeout, discards the old TTS provider
    connection and cancels old-fence tool tasks, so no old-subject audio or
    side effect survives into the new epoch.  Any failure raises (the caller
    stays fail-closed with metrics/logs); new outputs fail closed until the
    drain completes (pending-drain flag at the DuplexRuntime sink seams).
    """

    orch = runtime.orchestrator
    timeout_s = orch.epoch_drain_timeout_s
    if orch.playback.playing:
        await _bounded(orch.playback.stop_and_flush(), timeout_s, "playback_flush", orch)
    seam = orch.playback_stop_seam
    if seam is None:
        orch.metrics.inc_runtime_profile_refresh_failure("no_playback_seam")
        raise RuntimeError("epoch rotation drain has no production playback stop seam")
    await _bounded(seam(), timeout_s, "playback_seam", orch)
    orch._tts_cancel.set()
    for label, task in (
        ("llm", orch._active_llm_task),
        ("tts", orch._active_tts_task),
    ):
        if not await _cancel_and_wait(task, timeout_s=timeout_s):
            orch.metrics.inc_runtime_profile_refresh_failure("drain_timeout")
            raise RuntimeError(f"{label} task did not cancel within drain timeout")
    captured = getattr(runtime, "_rotation_captured_tasks", None) or []
    runtime._rotation_captured_tasks = None
    for task in captured:
        if task is None or task.done():
            continue
        if not await _cancel_and_wait(task, timeout_s=timeout_s):
            orch.metrics.inc_runtime_profile_refresh_failure("drain_timeout")
            raise RuntimeError("identity task did not cancel within drain timeout")
    await _bounded(
        orch.tts_pool.discard_active_connection(old_fence),
        timeout_s,
        "tts_discard",
        orch,
    )
    await _bounded(
        orch.task_manager.cancel_cancellable(old_fence),
        timeout_s,
        "tool_cancel",
        orch,
    )


async def _bounded(
    awaitable: Awaitable[Any],
    timeout_s: float,
    label: str,
    orch: Any,
) -> None:
    """Await one drain step under the shared deadline; never hang the loop.

    Production seams may return a coroutine, Task, Future or another Awaitable
    (LiveKit ``AgentSession.interrupt`` currently returns a Future).  Wrap the
    awaitable in our own coroutine before creating the named drain task so all
    valid Awaitable implementations share the same cancellation contract.

    A timeout/cancel also cancels the underlying awaitable and bounded-awaits
    it; a step that swallows
    cancellation is detached (its late result is never adopted) instead of
    leaking a pending task into the loop teardown.
    """

    async def _run() -> None:
        await awaitable

    task = asyncio.create_task(_run(), name=f"drain-{label}")
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
        except (TimeoutError, asyncio.CancelledError):
            pass  # swallow-cancelling step: detached, never adopted
        orch.metrics.inc_runtime_profile_refresh_failure("drain_timeout")
        raise RuntimeError(f"{label} did not finish within the epoch drain deadline") from None


async def _cancel_and_wait(task: Any, *, timeout_s: float) -> bool:
    if task is None or task.done():
        return True
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
    except asyncio.CancelledError:
        # A normal cancellation ends the task as cancelled: success.
        return bool(task.done())
    except TimeoutError:
        return False
    return True


__all__ = [
    "clear_identity_private_state",
    "drain_epoch_rotation",
    "exact_fence_commit_authority",
]
