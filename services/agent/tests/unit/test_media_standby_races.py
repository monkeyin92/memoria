"""Deterministic arbitration of admitted input and terminal conversation close."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import PCMFrame
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import (
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.media_session_types import (
    MediaReplyChunk,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
    asr_result_to_segment,
)
from services.agent.tests.unit.test_media_session import (
    FakeMediaProvider,
    _owner_silence_identity,
    _verified_owner_decision,
)


def _final(identity: SessionIdentity) -> ASRResult:
    return ASRResult(
        task_epoch=1, sentence_id="admitted-final", revision=1,
        capture_start_sample=0, capture_end_sample=320,
        text="请给我讲一个故事", is_final=True, confidence=0.99,
        stream_epoch=identity.stream_epoch,
    )


async def _expire_owner_timer(registry: MediaVoiceCoreRegistry, context: Any) -> None:
    # Drive the real timeout handler without timing the scheduler against sleep.
    old = context.owner_silence_task
    if old is not None:
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
    task = asyncio.create_task(registry._owner_silence_watch(context, 0))
    context.owner_silence_task = task
    await asyncio.wait_for(task, 1)


async def _expire_speech_watchdog(registry: MediaVoiceCoreRegistry, context: Any) -> None:
    old = context.max_user_speech_task
    assert old is not None
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    task = asyncio.create_task(registry._max_user_speech_watch(context, 0))
    context.max_user_speech_task = task
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_admitted_final_is_protected_before_projection_finishes(monkeypatch: Any) -> None:
    identity = _owner_silence_identity("admitted-before-projection")
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(), provider_factory=lambda _: FakeMediaProvider(),
        owner_silence_timeout_s=100,
    )
    context = await registry._get_or_create(identity)
    entered, release = asyncio.Event(), asyncio.Event()
    original = registry._apply_projection_segment

    async def project(self: Any, ctx: Any, segment: Any) -> None:
        entered.set()
        await release.wait()
        await original(ctx, segment)

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_apply_projection_segment", project)
    task = asyncio.create_task(registry.accept_asr_result(identity.session_id, _final(identity)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert context.turn_start_sample is None
        await _expire_owner_timer(registry, context)
        assert not context.standby_requested
        assert context.owner_silence_grace_used
        deadline = context.owner_silence_deadline
        registry._sync_owner_silence_phase(context, "thinking_silent")
        registry._sync_owner_silence_phase(context, "listening")
        assert context.owner_silence_deadline == deadline
        release.set()
        assert await asyncio.wait_for(task, 1)
        assert context.turn_start_sample == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


async def _blocked_commit() -> tuple[Any, ...]:
    identity = _owner_silence_identity("close-during-prepare")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Provider(FakeMediaProvider):
        async def prepare_committed_turn(
            self, _identity: SessionIdentity, text: str,
        ) -> GenerationFence:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = Provider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _: MediaSessionResources(runtime, provider),
        owner_silence_timeout_s=100,
    )
    context = await registry._get_or_create(identity)
    segment = asr_result_to_segment(_final(identity), session_id=identity.session_id)
    assert runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)
    task = asyncio.create_task(registry.commit_user_turn(
        identity.session_id, stream_epoch=identity.stream_epoch, start_sample=0, end_sample=320,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    return registry, context, task, release, cancelled


@pytest.mark.asyncio
async def test_inflight_prepare_gets_one_bounded_grace_then_finishes() -> None:
    registry, context, task, release, _ = await _blocked_commit()
    try:
        await _expire_owner_timer(registry, context)
        assert not context.standby_requested
        assert context.owner_silence_grace_used
        release.set()
        fence, reason = await asyncio.wait_for(task, 1)
        assert fence is not None and reason is None
        assert context.owner_silence_remaining_s == 0  # no owner authority was proved
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)

# ------------------------------------------------------------------ regressions
# The cases below pin the boundaries a terminal close can cross: the tracked
# prepare, the shell that schedules the reply, the generic finalize, and the
# single bounded owner-silence grace.  Every window is driven with an explicit
# event gate, never with a wall-clock sleep.


def _final_at(
    identity: SessionIdentity,
    text: str,
    *,
    start: int = 0,
    end: int = 320,
    epoch: int | None = None,
) -> ASRResult:
    return ASRResult(
        task_epoch=1,
        sentence_id=f"final-{start}-{end}",
        revision=1,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        is_final=True,
        confidence=0.99,
        stream_epoch=identity.stream_epoch if epoch is None else epoch,
    )


async def _device_registry(session_id: str) -> tuple[Any, ...]:
    identity = _owner_silence_identity(session_id)
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    provider = FakeMediaProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _: MediaSessionResources(runtime, provider),
        owner_silence_timeout_s=100,
    )
    context = await registry._get_or_create(identity)
    return registry, context, provider, runtime, identity


async def _ingest_accepted_final(
    registry: MediaVoiceCoreRegistry,
    runtime: Any,
    identity: SessionIdentity,
    *,
    text: str = "请给我讲一个故事",
) -> Any:
    """Put one accepted final on the timeline, as the ingest seam does."""

    context = registry._sessions[identity.session_id]
    segment = asr_result_to_segment(_final_at(identity, text), session_id=identity.session_id)
    assert runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)
    return context


def _arm_endpoint(context: Any, *, end: int = 320) -> None:
    """Mirror the VAD-end endpoint state _commit_pending_turn reads."""

    context.turn_start_sample = 0
    context.turn_end_sample = end
    context.turn_endpoint_sample = end
    context.turn_retire_sample = end


def _user_turns(runtime: Any) -> list[str]:
    return [str(turn.content) for turn in runtime.orchestrator.context.turns if turn.role == "user"]


def _vad(identity: SessionIdentity, *, start: int = 640, final: bool = False) -> SpeechSegment:
    return SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=0,
        segment_id=f"vad-{start}-{final}",
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=start,
        capture_end_sample=start + 1,
        final=final,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("asr_opened_turn", [False, True])
async def test_accepted_vad_retracts_processing_grace_without_refreshing_budget(
    asr_opened_turn: bool,
) -> None:
    registry, context, _, _, identity = await _device_registry("vad-retracts-grace")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    try:
        if asr_opened_turn:
            assert await registry.accept_asr_result(identity.session_id, _final(identity))
            assert context.turn_start_sample == 0
        else:
            # ASR admission may precede its projection/turn-range update.
            context.admitted_input_stream_epoch = identity.stream_epoch
        await _expire_owner_timer(registry, context)
        grace_task = context.owner_silence_task
        assert context.owner_silence_grace_deadline is not None
        await registry.on_speech_segment(session, _vad(identity))
        assert context.owner_silence_grace_deadline is None
        assert context.owner_silence_remaining_s == 0.0
        assert context.owner_silence_grace_used
        assert context.owner_silence_task is None
        assert context.max_user_speech_task is not None
        watchdog = context.max_user_speech_task
        deadline = context.max_user_speech_deadline
        await registry.on_speech_segment(session, _vad(identity, start=960))
        registry._sync_owner_silence_phase(context, "listening")
        assert context.owner_silence_task is None
        assert context.max_user_speech_task is watchdog
        assert context.max_user_speech_deadline == deadline
        assert not context.standby_requested
        if grace_task is not None:
            await asyncio.wait_for(grace_task, 1)
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_accepted_vad_is_protected_before_projection_await(monkeypatch: Any) -> None:
    registry, context, _, _, identity = await _device_registry("vad-before-projection")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    context.admitted_input_stream_epoch = identity.stream_epoch
    await _expire_owner_timer(registry, context)
    entered, release = asyncio.Event(), asyncio.Event()
    original = registry._apply_projection_segment

    async def project(ctx: Any, segment: Any) -> None:
        entered.set()
        await release.wait()
        await original(ctx, segment)

    monkeypatch.setattr(registry, "_apply_projection_segment", project)
    task = asyncio.create_task(registry.on_speech_segment(session, _vad(identity)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert context.turn_start_sample == 640
        assert context.owner_silence_grace_deadline is None
        assert context.owner_silence_task is None
        assert context.max_user_speech_task is not None
        assert not context.standby_requested
        release.set()
        await asyncio.wait_for(task, 1)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_grace", [False, True])
@pytest.mark.parametrize("watchdog_duration", [0, 60])
async def test_accepted_vad_only_invalidates_parked_close_with_a_live_watchdog(
    monkeypatch: Any, with_grace: bool, watchdog_duration: int,
) -> None:
    registry, context, _, _, identity = await _device_registry("vad-close-lock-race")
    registry.max_user_speech_duration_s = watchdog_duration
    session = registry.bridge._open_connection(identity).session
    if with_grace:
        context.admitted_input_stream_epoch = identity.stream_epoch
        await _expire_owner_timer(registry, context)
    entered = asyncio.Event()
    original = registry._request_device_standby

    async def close(ctx: Any, **kwargs: Any) -> bool:
        entered.set()
        return await original(ctx, **kwargs)

    monkeypatch.setattr(registry, "_request_device_standby", close)
    timeout: asyncio.Task[None] | None = None
    try:
        async with context.standby_lock:
            timeout = asyncio.create_task(_expire_owner_timer(registry, context))
            await asyncio.wait_for(entered.wait(), 1)
            assert context.owner_silence_task is None
            await registry.on_speech_segment(session, _vad(identity))
        await asyncio.wait_for(timeout, 1)
        if not watchdog_duration:
            assert context.closed
            assert context.standby_reason == "owner_silence_timeout"
            return
        assert not context.standby_requested
        assert not context.closed
        assert context.owner_silence_remaining_s == 0.0
        assert context.owner_silence_task is None
        assert context.owner_silence_grace_deadline is None
        assert context.max_user_speech_task is not None
        # Vetoing the old close must transfer, not remove, the absolute bound.
        await _expire_speech_watchdog(registry, context)
        assert context.closed
        assert context.standby_reason == "max_user_speech_duration_timeout"
    finally:
        if timeout is not None:
            await asyncio.gather(timeout, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_disabled_watchdog_keeps_processing_grace_bounded() -> None:
    registry, context, _, _, identity = await _device_registry("vad-watchdog-disabled")
    session = registry.bridge._open_connection(identity).session
    try:
        context.admitted_input_stream_epoch = identity.stream_epoch
        await _expire_owner_timer(registry, context)
        deadline = context.owner_silence_grace_deadline
        await registry.on_speech_segment(session, _vad(identity))
        assert context.owner_silence_grace_deadline == deadline
        assert context.max_user_speech_task is None
        await _expire_owner_timer(registry, context)
        assert context.standby_reason == "owner_silence_timeout"
        assert context.closed
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    ["ignore", "clock_pin", "close_pin", "query_pin", "forced", "empty", "stale", "terminal"],
)
async def test_rejected_vad_cannot_retract_grace(monkeypatch: Any, rejection: str) -> None:
    registry, context, _, runtime, identity = await _device_registry(f"vad-reject-{rejection}")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    context.admitted_input_stream_epoch = identity.stream_epoch
    await _expire_owner_timer(registry, context)
    deadline = context.owner_silence_grace_deadline
    revision = context.owner_silence_vad_revision
    segment = _vad(identity)
    if rejection == "ignore":
        monkeypatch.setattr(
            type(runtime), "on_user_voice_started", lambda self: PlaybackInputDecision.IGNORE,
        )
    elif rejection == "clock_pin":
        context.clock_fact_endpoint_pinned = 320
    elif rejection == "close_pin":
        context.conversation_close_endpoint_pinned = 320
    elif rejection == "query_pin":
        context.live_query_endpoint_pinned = 320
    elif rejection == "forced":
        context.live_query_forced_authoritative = True
        context.live_query_forced_text = "未来三天南京天气"
    elif rejection == "empty":
        segment = replace(segment, near_end_rms=0.0)
    elif rejection == "stale":
        segment = replace(segment, stream_epoch=identity.stream_epoch + 1)
    else:
        context.standby_requested = True
    try:
        await registry.on_speech_segment(session, segment)
        assert context.owner_silence_grace_deadline == deadline
        assert context.owner_silence_vad_revision == revision
        assert context.active_vad_stream_epoch is None
        assert context.max_user_speech_task is None
        assert context.turn_start_sample is None
        if rejection not in {"terminal", "empty", "stale"}:
            # Pinned/ignored edges remain observations, preserving projection
            # semantics, but never receive timer or turn admission.
            assert context.projection.provisional is not None
        if rejection != "terminal":
            await _expire_owner_timer(registry, context)
            assert context.closed
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_reason", ["watchdog", "conversation_end"])
async def test_active_vad_cannot_veto_authoritative_close(terminal_reason: str) -> None:
    registry, context, _, _, identity = await _device_registry(f"vad-close-{terminal_reason}")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    try:
        await registry.on_speech_segment(session, _vad(identity))
        if terminal_reason == "watchdog":
            await _expire_speech_watchdog(registry, context)
            assert context.standby_reason == "max_user_speech_duration_timeout"
        else:
            assert await registry._request_device_standby(context, reason=terminal_reason)
            assert context.standby_reason == terminal_reason
        assert context.closed
        assert context.active_vad_stream_epoch is None
        assert context.active_vad_start_sample is None
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_grace", [False, True])
@pytest.mark.parametrize("expires", [False, True])
async def test_vad_finalization_keeps_absolute_watchdog_until_endpoint_handoff(
    monkeypatch: Any, with_grace: bool, expires: bool,
) -> None:
    registry, context, provider, runtime, identity = await _device_registry("vad-end-handoff")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    entered, release = asyncio.Event(), asyncio.Event()

    async def finalize(_identity: SessionIdentity) -> tuple[ASRResult, ...]:
        entered.set()
        await release.wait()
        return ()

    monkeypatch.setattr(provider, "finalize_speech_segment", finalize, raising=False)
    task: asyncio.Task[None] | None = None
    try:
        if with_grace:
            context.admitted_input_stream_epoch = identity.stream_epoch
            await _expire_owner_timer(registry, context)
        await registry.on_speech_segment(session, _vad(identity))
        watchdog = context.max_user_speech_task
        deadline = context.max_user_speech_deadline
        context.asr.last_sent_sample = 960
        task = asyncio.create_task(
            registry.on_speech_segment(session, _vad(identity, start=960, final=True))
        )
        await asyncio.wait_for(entered.wait(), 1)
        assert context.active_vad_stream_epoch == identity.stream_epoch
        assert context.active_vad_start_sample == 640
        assert context.max_user_speech_task is watchdog
        assert watchdog is not None
        assert context.max_user_speech_deadline == deadline
        assert context.owner_silence_task is None
        assert context.turn_endpoint_timeout_handle is None
        registry._sync_owner_silence_phase(context, "listening")
        assert context.owner_silence_task is None
        if expires:
            await _expire_speech_watchdog(registry, context)
            assert context.closed
            assert context.standby_reason == "max_user_speech_duration_timeout"
        release.set()
        await asyncio.wait_for(task, 1)
        assert context.active_vad_stream_epoch is None
        assert context.active_vad_start_sample is None
        assert context.max_user_speech_task is None
        if expires:
            # A late provider return cannot resurrect the expired utterance.
            assert identity.session_id not in registry._sessions
            assert context.turn_endpoint_sample is None
            assert context.turn_endpoint_task is None
            assert _user_turns(runtime) == []
        else:
            assert not context.closed
            assert context.turn_endpoint_sample == 960
            assert context.turn_endpoint_timeout_handle is not None
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", ["clock_fact", "conversation_close", "live_query", "forced"])
async def test_pinned_vad_end_keeps_the_existing_watchdog(pin: str) -> None:
    registry, context, _, _, identity = await _device_registry(f"vad-end-pin-{pin}")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    try:
        await registry.on_speech_segment(session, _vad(identity))
        watchdog = context.max_user_speech_task
        if pin == "forced":
            context.live_query_forced_authoritative = True
            context.live_query_forced_text = "南京天气怎么样"
        else:
            setattr(context, f"{pin}_endpoint_pinned", 800)
        await registry.on_speech_segment(session, _vad(identity, start=960, final=True))
        assert context.max_user_speech_task is watchdog
        assert watchdog is not None
        assert context.active_vad_start_sample == 640
        assert context.turn_endpoint_sample is None
        await _expire_speech_watchdog(registry, context)
        assert context.closed
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_stage", ["projection", "finalization"])
async def test_older_vad_end_cannot_stop_newer_speech(monkeypatch: Any, wait_stage: str) -> None:
    registry, context, _, _, identity = await _device_registry("vad-end-late")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    entered, release = asyncio.Event(), asyncio.Event()
    original = registry._apply_projection_segment

    async def project(ctx: Any, segment: Any) -> None:
        if segment.final and wait_stage == "projection":
            entered.set()
            await release.wait()
        await original(ctx, segment)

    async def finalize(ctx: Any, **kwargs: Any) -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(registry, "_apply_projection_segment", project)
    if wait_stage == "finalization":
        monkeypatch.setattr(registry._audio_ingress, "finalize_speech_segment", finalize)
    task: asyncio.Task[None] | None = None
    try:
        await registry.on_speech_segment(session, _vad(identity))
        task = asyncio.create_task(
            registry.on_speech_segment(session, _vad(identity, start=960, final=True))
        )
        await asyncio.wait_for(entered.wait(), 1)
        await registry.on_speech_segment(session, _vad(identity, start=1280))
        watchdog = context.max_user_speech_task
        release.set()
        await asyncio.wait_for(task, 1)
        assert context.max_user_speech_task is watchdog
        assert watchdog is not None
        assert context.active_vad_start_sample == 1280
        assert context.turn_endpoint_sample is None
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["clear", "finish", "reconnect"])
async def test_turn_cleanup_releases_active_vad_without_resetting_revision(cleanup: str) -> None:
    registry, context, _, _, identity = await _device_registry(f"vad-cleanup-{cleanup}")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    try:
        await registry.on_speech_segment(session, _vad(identity))
        revision = context.owner_silence_vad_revision
        assert revision > 0
        if cleanup == "clear":
            registry._clear_pending_turn_state(context)
        elif cleanup == "finish":
            registry._finish_owner_silence_turn(context, accepted=True)
        else:
            await registry._reuse_session(
                context, replace(identity, stream_epoch=identity.stream_epoch + 1),
            )
        assert context.active_vad_stream_epoch is None
        assert context.active_vad_start_sample is None
        assert context.max_user_speech_task is None
        assert context.owner_silence_vad_revision == revision
        registry._sync_owner_silence_phase(context, "listening")
        assert context.owner_silence_task is not None
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_vad_without_grace_preserves_the_unspent_silence_budget() -> None:
    registry, context, _, _, identity = await _device_registry("vad-unspent-budget")
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    try:
        registry._cancel_owner_silence_timer(context, preserve_remaining=False)
        context.owner_silence_remaining_s = 7.5
        registry._arm_owner_silence_timer(context, reset=False)
        before = context.owner_silence_deadline
        await registry.on_speech_segment(session, _vad(identity))
        assert context.owner_silence_task is None
        assert context.owner_silence_remaining_s <= 7.5
        assert context.owner_silence_remaining_s >= before - asyncio.get_running_loop().time()
        assert not context.owner_silence_grace_used
        remaining = context.owner_silence_remaining_s
        await registry.on_speech_segment(session, _vad(identity, start=960))
        assert context.owner_silence_remaining_s == remaining
        registry._finish_owner_silence_turn(context, accepted=True)
        assert context.owner_silence_remaining_s == remaining
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_close_while_reply_scheduling_is_blocked_never_starts_the_reply(
    monkeypatch: Any,
) -> None:
    """A close that lands after the fence is minted still owns the reply.

    _request_device_standby sets standby_requested and queues CLOSED before
    closed becomes true, and it cannot cancel a prepare that already returned
    its fence.  The seam that schedules the reply therefore has to treat
    standby_requested as terminal on its own.
    """

    registry, context, provider, runtime, identity = await _device_registry("close-blocks-reply")
    await _ingest_accepted_final(registry, runtime, identity)
    _arm_endpoint(context)

    close_queued, release_close = asyncio.Event(), asyncio.Event()
    reply_blocked, release_reply = asyncio.Event(), asyncio.Event()
    original_emit_state = registry.bridge.emit_conversation_state

    async def gated_emit_state(session_id: str, state: int, **kwargs: Any) -> bool:
        emitted = await original_emit_state(session_id, state, **kwargs)
        close_queued.set()
        await release_close.wait()
        return bool(emitted)

    original_cancel_reply = registry._cancel_reply_task

    async def gated_cancel_reply(context_: Any, fence: GenerationFence, **kwargs: Any) -> Any:
        if not reply_blocked.is_set():
            reply_blocked.set()
            await release_reply.wait()
        return await original_cancel_reply(context_, fence, **kwargs)

    dispatched: list[GenerationFence] = []
    original_dispatch = registry._dispatch_reply

    async def recording_dispatch(session_id: str, text: str, fence: GenerationFence) -> Any:
        dispatched.append(fence)
        return await original_dispatch(session_id, text, fence)

    replies: list[str] = []
    original_generate_reply = provider.generate_reply

    def recording_generate_reply(
        identity_: SessionIdentity, text: str, fence: GenerationFence
    ) -> Any:
        replies.append(text)
        return original_generate_reply(identity_, text, fence)

    monkeypatch.setattr(registry.bridge, "emit_conversation_state", gated_emit_state)
    monkeypatch.setattr(registry, "_cancel_reply_task", gated_cancel_reply)
    monkeypatch.setattr(registry, "_dispatch_reply", recording_dispatch)
    monkeypatch.setattr(provider, "generate_reply", recording_generate_reply)

    commit = asyncio.create_task(registry._commit_pending_turn(context))
    standby: asyncio.Task[bool] | None = None
    outcome: Any = None
    try:
        await asyncio.wait_for(reply_blocked.wait(), 1)
        assert not context.standby_requested
        standby = asyncio.create_task(
            registry._request_device_standby(context, reason="owner_silence_timeout")
        )
        await asyncio.wait_for(close_queued.wait(), 1)
        # The window this regression exists for: the terminal flag is set and
        # CLOSED is queued, but resource teardown has not closed the context.
        assert context.standby_requested
        assert not context.closed
        release_reply.set()
        outcome = await asyncio.wait_for(commit, 1)
        release_close.set()
        await asyncio.wait_for(standby, 1)
    finally:
        release_reply.set()
        release_close.set()
        await asyncio.gather(commit, return_exceptions=True)
        if standby is not None:
            await asyncio.gather(standby, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)

    assert dispatched == [], "a reply was scheduled after the terminal close"
    assert replies == [], "provider generation started after the terminal close"
    assert outcome == "session_closed"
    assert context.closed


@pytest.mark.asyncio
async def test_close_during_generation_start_records_a_terminal_minted_fence(
    monkeypatch: Any,
) -> None:
    """The minted fence must not vanish when the close lands on it.

    The user text was accepted and committed before the close, so it may stay on
    the runtime timeline.  The fence that was already announced, however, needs
    an explicit terminal outcome in the delivery ledger or the output results
    instead of being dropped silently.
    """

    registry, context, provider, runtime, identity = await _device_registry(
        "close-during-generation"
    )
    await _ingest_accepted_final(registry, runtime, identity)
    _arm_endpoint(context)

    generation_entered, release_generation = asyncio.Event(), asyncio.Event()
    minted: list[GenerationFence] = []
    original_emit_generation = registry.bridge.emit_generation

    async def gated_emit_generation(session_id: str, fence: GenerationFence, **kwargs: Any) -> bool:
        minted.append(fence)
        generation_entered.set()
        await release_generation.wait()
        return bool(await original_emit_generation(session_id, fence, **kwargs))

    monkeypatch.setattr(registry.bridge, "emit_generation", gated_emit_generation)

    commit = asyncio.create_task(registry._commit_pending_turn(context))
    outcome: Any = None
    try:
        await asyncio.wait_for(generation_entered.wait(), 1)
        assert minted, "the turn has to reach generation START before the close"
        fence = minted[0]
        await asyncio.wait_for(
            registry._request_device_standby(context, reason="owner_silence_timeout"), 1
        )
        release_generation.set()
        outcome = (await asyncio.gather(commit, return_exceptions=True))[0]
    finally:
        release_generation.set()
        await asyncio.gather(commit, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)

    assert outcome == "session_closed"
    assert _user_turns(runtime) == ["请给我讲一个故事"]
    delivery = context.reply_delivery.get(fence)
    terminal = delivery.terminal_event if delivery is not None else None
    recorded = [
        (result.status, result.reason)
        for result in context.output_results
        if result.fence.matches(fence)
    ]
    assert terminal is not None or any(
        status is OutputDispatchStatus.ABORTED or reason == "session_closed"
        for status, reason in recorded
    ), (
        "the minted fence was dropped without a terminal outcome: "
        f"reply_delivery terminal={terminal} output_results={recorded}"
    )


@pytest.mark.asyncio
async def test_generic_finalize_closes_the_gate_while_prepare_is_pending() -> None:
    """_finalize_session must not wait behind a pending provider prepare."""

    registry, context, task, release, cancelled = await _blocked_commit()
    finalized = False
    outcome: Any = None
    try:
        try:
            await asyncio.wait_for(registry._finalize_session(context.identity.session_id), 1)
            finalized = True
        except TimeoutError:
            finalized = False
        release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert finalized, (
            "_finalize_session waited for the pending provider prepare instead of "
            "cancelling it; the close gate has to be set synchronously"
        )
        assert cancelled.is_set(), "the pending provider prepare was never cancelled"
        assert isinstance(outcome, asyncio.CancelledError) or outcome == (None, "session_closed")
        assert not _user_turns(context.runtime), "a new user turn was created after the close"
        assert context.closed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_reply_cancellation_drain_is_bounded_when_old_task_absorbs_cancel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stubborn old reply cannot hold the caller past the drain budget."""

    registry, context, provider, runtime, identity = await _device_registry(
        "bounded-reply-drain"
    )
    fence = await runtime.on_turn_committed("你好")
    started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn_reply() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate a provider adapter that absorbs local cancellation until
            # its remote stream teardown completes.
            await release.wait()
            cleaned.set()

    old_reply = asyncio.create_task(stubborn_reply(), name="test-stubborn-reply")
    context.reply_task = old_reply
    await asyncio.wait_for(started.wait(), 1)

    try:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        with caplog.at_level("WARNING"):
            await registry._cancel_reply_task(context, fence, cancel_timeout_s=0.05)
        elapsed = loop.time() - started_at

        assert elapsed < 0.3
        assert old_reply.done() is False
        assert asyncio.current_task() is not None
        assert asyncio.current_task().cancelling() == 0
        assert "media reply cancellation drain timed out" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.gather(old_reply, return_exceptions=True)
        context.reply_task = None
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_stale_epoch_final_is_refused_before_admission_and_recovery(
    monkeypatch: Any,
) -> None:
    """An old-epoch final is dropped before policy, admission or recovery."""

    registry, context, provider, runtime, identity = await _device_registry("stale-epoch-final")
    recovered: list[str] = []
    original_semantic = registry._recover_rejected_semantic_final
    original_clock_fact = registry._recover_rejected_clock_fact_final

    async def recording_semantic(
        context_: Any, *, session_id: str, result: Any, reason: Any
    ) -> None:
        recovered.append("semantic")
        await original_semantic(context_, session_id=session_id, result=result, reason=reason)

    async def recording_clock_fact(
        context_: Any, *, session_id: str, result: Any, reason: Any
    ) -> None:
        recovered.append("clock_fact")
        await original_clock_fact(context_, session_id=session_id, result=result, reason=reason)

    monkeypatch.setattr(registry, "_recover_rejected_semantic_final", recording_semantic)
    monkeypatch.setattr(registry, "_recover_rejected_clock_fact_final", recording_clock_fact)

    decision = await registry._accept_asr_result_decision(
        identity.session_id,
        _final_at(identity, "请给我讲一个故事", epoch=identity.stream_epoch + 1),
    )
    assert decision.accepted is None
    assert decision.reason is ASRDecisionReason.STALE_STREAM_EPOCH
    assert context.admitted_input_stream_epoch is None
    assert recovered == [], "an old-epoch final must not enter final recovery"
    assert _user_turns(runtime) == []

    assert await registry.accept_asr_result(
        identity.session_id, _final_at(identity, "请给我讲一个故事")
    )
    assert context.admitted_input_stream_epoch == context.stream_epoch
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_repeated_final_and_phase_churn_cannot_extend_the_single_grace() -> None:
    """One spent grace is an absolute deadline, not a renewed listening budget."""

    registry, context, provider, runtime, identity = await _device_registry("grace-single-shot")
    registry._arm_owner_silence_timer(context, reset=True)
    assert await registry.accept_asr_result(
        identity.session_id, _final_at(identity, "请给我讲一个故事")
    )
    await _expire_owner_timer(registry, context)
    grace = context.owner_silence_grace_deadline
    assert grace is not None
    assert not context.standby_requested
    assert context.owner_silence_grace_used

    assert await registry.accept_asr_result(
        identity.session_id, _final_at(identity, "再讲一个", start=320, end=640)
    )
    for _ in range(3):
        registry._sync_owner_silence_phase(context, "thinking_silent")
        registry._sync_owner_silence_phase(context, "listening")
        registry._arm_owner_silence_timer(context, reset=True)
    assert context.owner_silence_grace_deadline == grace
    assert context.owner_silence_deadline == grace
    assert not context.standby_requested

    await _expire_owner_timer(registry, context)
    assert context.standby_requested
    assert context.standby_reason == "owner_silence_timeout"
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_grace_expiry_closes_while_projection_is_pending(monkeypatch: Any) -> None:
    """A pending projection cannot hold the conversation open past the grace."""

    registry, context, provider, runtime, identity = await _device_registry(
        "grace-pending-projection"
    )
    registry._arm_owner_silence_timer(context, reset=True)
    projection_entered, release_projection = asyncio.Event(), asyncio.Event()
    original_project = registry._apply_projection_segment

    async def gated_project(context_: Any, segment: Any) -> None:
        projection_entered.set()
        await release_projection.wait()
        await original_project(context_, segment)

    monkeypatch.setattr(registry, "_apply_projection_segment", gated_project)

    accepted = asyncio.create_task(
        registry.accept_asr_result(identity.session_id, _final_at(identity, "请给我讲一个故事"))
    )
    try:
        await asyncio.wait_for(projection_entered.wait(), 1)
        assert context.turn_start_sample is None
        assert context.admitted_input_stream_epoch == context.stream_epoch
        await _expire_owner_timer(registry, context)
        assert not context.standby_requested
        assert context.owner_silence_grace_deadline is not None
        await _expire_owner_timer(registry, context)
        assert context.standby_requested
        assert context.standby_reason == "owner_silence_timeout"
        release_projection.set()
        assert await asyncio.wait_for(accepted, 1) is False
    finally:
        release_projection.set()
        await asyncio.gather(accepted, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)

    assert _user_turns(runtime) == [], "a late projection committed a closed conversation"


@pytest.mark.asyncio
async def test_explicit_conversation_end_stands_by_without_lockup() -> None:
    """The in-body farewell close must finish, not deadlock on its own lock."""

    registry, context, provider, runtime, identity = await _device_registry("farewell-close")
    await _ingest_accepted_final(registry, runtime, identity, text="好的，再见")

    result = await asyncio.wait_for(
        registry.commit_user_turn(
            identity.session_id,
            stream_epoch=context.stream_epoch,
            start_sample=0,
            end_sample=320,
        ),
        1,
    )
    assert result == (None, "conversation_end_explicit")
    assert context.standby_requested
    assert context.standby_reason == "conversation_end_explicit"
    assert context.closed
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("verified", "closes_after_playback"), [(True, False), (False, True)])
async def test_post_playback_budget_after_a_spent_grace_follows_owner_authority(
    monkeypatch: Any,
    verified: bool,
    closes_after_playback: bool,
) -> None:
    """Only a verified owner restores the window once the grace was spent."""

    registry, context, provider, runtime, identity = await _device_registry("post-playback-budget")
    registry._arm_owner_silence_timer(context, reset=True)
    assert await registry.accept_asr_result(
        identity.session_id, _final_at(identity, "请给我讲一个故事")
    )
    await _expire_owner_timer(registry, context)
    assert context.owner_silence_grace_used

    if verified:
        decision = _verified_owner_decision()
        monkeypatch.setattr(runtime, "_speaker_decision", decision)
        monkeypatch.setattr(runtime, "_speaker_class", decision.classification)
    else:
        monkeypatch.setattr(runtime, "_speaker_decision", None)
        monkeypatch.setattr(runtime, "_speaker_class", "uncertain")

    registry._finish_owner_silence_turn(context, accepted=True)
    assert context.owner_silence_grace_deadline is None
    if verified:
        assert context.owner_silence_remaining_s == registry.owner_silence_timeout_s
        assert context.owner_silence_grace_used is False
    else:
        assert context.owner_silence_remaining_s == 0.0

    registry._sync_owner_silence_phase(context, "listening")
    if closes_after_playback:
        await asyncio.wait_for(context.owner_silence_task, 1)
        assert context.standby_requested
        assert context.standby_reason == "owner_silence_timeout"
    else:
        assert context.owner_silence_task is not None
        assert not context.standby_requested
    await registry._finalize_session(identity.session_id)



@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["conversation_end_explicit", "owner_silence_timeout"])
async def test_close_cancels_prepare_before_any_new_turn(reason: str) -> None:
    registry, context, task, release, cancelled = await _blocked_commit()
    before = context.runtime.fence
    try:
        await asyncio.wait_for(registry._request_device_standby(context, reason=reason), 1)
        assert cancelled.is_set()
        release.set()
        fence, outcome = await asyncio.wait_for(task, 1)
        assert fence is None
        assert outcome == "session_closed"
        assert context.runtime.fence == before
        assert not [t for t in context.runtime.orchestrator.context.turns if t.role == "user"]
        assert context.provider.closed
        assert context.closed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_grace_expiry_cancels_prepare_without_waiting_for_provider() -> None:
    registry, context, task, release, cancelled = await _blocked_commit()
    try:
        await _expire_owner_timer(registry, context)
        assert not context.standby_requested
        await _expire_owner_timer(registry, context)
        assert cancelled.is_set()
        assert context.standby_reason == "owner_silence_timeout"
        assert await task == (None, "session_closed")
        assert not [t for t in context.runtime.orchestrator.context.turns if t.role == "user"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_finalize_that_lands_inside_an_inflight_pcm_emit_aborts_the_stream() -> None:
    """A terminal close owns the frame the transport already accepted.

    The first provider chunk is accepted by the real bridge transport before
    the close is observed, so only the post-await terminal gate can stop the
    output stream.  The stream must not pull another chunk, accept another
    PCM frame, publish a transcript or record provider completion, and the
    generation must terminate as ``session_closed``.
    """

    identity = _owner_silence_identity("finalize-during-pcm-emit")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    accepted_first_frame, release_first_frame = asyncio.Event(), asyncio.Event()
    close_reached_cancel, release_close = asyncio.Event(), asyncio.Event()
    pulled: list[int] = []
    published: list[str] = []

    class GatedTwoChunkProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> Any:
            async def chunks() -> Any:
                for index, sample in enumerate((0, 480)):
                    pulled.append(index)
                    yield MediaReplyChunk(
                        pcm_s16le=b"\x01\x00" * 480,
                        source_start_sample=sample,
                        first=index == 0,
                        final=index == 1,
                    )

            return chunks()

    provider = GatedTwoChunkProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _: MediaSessionResources(runtime, provider),
        owner_silence_timeout_s=100,
    )
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    context = await registry._get_or_create(identity)

    accepted: list[int] = []
    real_emit = bridge.emit_pcm_when_connected

    async def gated_emit_pcm_when_connected(
        session_id: str,
        frame: PCMFrame,
        *,
        timeout_s: float,
    ) -> bool:
        emitted = await real_emit(session_id, frame, timeout_s=timeout_s)
        if not emitted:
            return False
        # The frame is already accepted by the real transport here; hold the
        # stream inside its await until the close has been observed.
        accepted.append(frame.sequence)
        if len(accepted) == 1:
            accepted_first_frame.set()
            await release_first_frame.wait()
        return True

    original_cancel_reply = registry._cancel_reply_task

    async def gated_cancel_reply(
        context_: Any,
        fence: GenerationFence,
        *,
        reason: str = "cancelled",
        cancel_timeout_s: float = 5.0,
    ) -> None:
        # Hold the finalizer after the terminal flag is set and before the reply
        # task is cancelled, so the in-flight emit window cannot be dissolved by
        # a plain cancellation of the stream under test.
        if not close_reached_cancel.is_set():
            close_reached_cancel.set()
            await release_close.wait()
        await original_cancel_reply(
            context_,
            fence,
            reason=reason,
            cancel_timeout_s=cancel_timeout_s,
        )

    bridge.emit_pcm_when_connected = gated_emit_pcm_when_connected  # type: ignore[method-assign]
    registry._cancel_reply_task = gated_cancel_reply  # type: ignore[method-assign]
    real_publish_transcript = context.runtime.publish_transcript

    def recording_publish_transcript(**kwargs: Any) -> Any:
        published.append(str(kwargs.get("speaker", "")))
        return real_publish_transcript(**kwargs)

    context.runtime.publish_transcript = recording_publish_transcript  # type: ignore[method-assign]

    fence = await runtime.on_turn_committed("你好")
    context.playback.start(fence)
    assert await bridge.emit_generation(
        identity.session_id,
        fence,
        action=media_pb2.GENERATION_ACTION_START,
    )

    reply = asyncio.create_task(registry._dispatch_reply(identity.session_id, "你好", fence))
    finalize: asyncio.Task[None] | None = None
    result: Any = None
    try:
        await asyncio.wait_for(accepted_first_frame.wait(), 1)
        assert pulled == [0]
        assert not context.closed
        assert not context.standby_requested
        finalize = asyncio.create_task(registry._finalize_session(identity.session_id))
        await asyncio.wait_for(close_reached_cancel.wait(), 1)
        # The terminal state is already installed while the stream is still
        # inside the emit its transport accepted.
        assert context.standby_requested
        assert context.closed
        release_first_frame.set()
        result = await asyncio.wait_for(reply, 1)
        release_close.set()
        await asyncio.wait_for(finalize, 1)
    finally:
        release_first_frame.set()
        release_close.set()
        await asyncio.gather(reply, return_exceptions=True)
        if finalize is not None:
            await asyncio.gather(finalize, return_exceptions=True)
        await registry._finalize_session(identity.session_id)

    assert result.status is OutputDispatchStatus.ABORTED
    assert result.reason == "session_closed"
    assert result.emitted_audio is False
    assert pulled == [0], "the provider iterator was pulled past the terminal close"
    assert accepted == [0], "a second PCM frame reached the transport"
    # emit_pcm_when_connected pops/acks the downlink deque on success, so the
    # acceptance watermark (not the deque) is the transport proof.
    assert connection.session.last_downlink_sequence == 0
    assert connection.session.last_downlink_ack_sequence == 0
    assert published == [], "a transcript was published after the terminal close"
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert ReplyDeliveryEvent.PROVIDER_COMPLETED not in delivery.events
    assert delivery.provider_completed is False
    assert delivery.terminal_event is ReplyDeliveryEvent.ERROR
    assert delivery.terminal_reason == "session_closed"
    assert context.output_owner is None
    assert context.provider_complete is False
    assert finalize is not None and finalize.done() and not finalize.cancelled()
    assert context.closed
