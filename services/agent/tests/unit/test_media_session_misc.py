"""Media session: remaining media-session behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _accept_media_asr_decision,
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _CapturingMediaBridge,
    _device_identity,
    _finish_output_owner_playback,
    _seed_pending_media_turn,
    _verified_owner_decision,
)


@pytest.mark.asyncio
async def test_prepare_retry_exhaustion_discards_once_at_transport_retire_sample() -> None:
    identity = SessionIdentity("prepare-retry-exhaustion")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class AlwaysFailingPreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0
            self.reply_calls = 0

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            _text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            raise RuntimeError("preparation unavailable")

        def generate_reply(
            self,
            identity: SessionIdentity,
            user_text: str,
            fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            self.reply_calls += 1
            return super().generate_reply(identity, user_text, fence)

    provider = AlwaysFailingPreparingProvider()
    bridge = _CapturingMediaBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
    )
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="重试耗尽后确定性丢弃",
    )

    assert await registry._commit_pending_turn(context) == "provider_prepare_failed"
    retry_task = context.pending.turn_commit_retry_task
    assert retry_task is not None
    await asyncio.wait_for(retry_task, timeout=1)

    discarded = [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
    ]
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "provider_prepare_retries_exhausted"
    assert not [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
        and payload.get("reason") == "provider_final_missing"
    ]
    assert provider.prepare_calls == 3
    assert provider.reply_calls == 0
    assert context.projection.provisional is None
    assert context.runtime.speech_timeline.committed_sample == 640
    assert context.asr.last_committed_sample == 640
    assert context.pending.turn_commit_retry_task is None
    assert context.pending.turn_commit_retry_attempt == 0
    assert context.pending.turn_commit_retry_stream_epoch is None
    assert context.pending.turn_commit_retry_endpoint_sample is None
    assert [turn for turn in runtime.orchestrator.context.turns if turn.role == "user"] == []
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "scheduled"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "attempt"}) == 2
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "exhausted"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "succeeded"}) == 0
    assert not await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="late-hangover-after-exhaustion",
            revision=1,
            capture_start_sample=600,
            capture_end_sample=640,
            text="尾静音迟到文本",
            is_final=True,
            stream_epoch=identity.stream_epoch,
        ),
    )
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_conversation_yield_proxy_resolves_only_on_matching_terminal() -> None:
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        metrics=metrics,
    )
    identity = SessionIdentity("conversation-yield-terminal")
    context = await registry.open_session(identity)
    candidate = GenerationFence(identity.session_id, 1, 1, 0)
    unrelated = GenerationFence(identity.session_id, 2, 1, 0)
    context.conversation_yield_candidate_fence = candidate

    registry._record_reply_delivery_event(
        context,
        unrelated,
        ReplyDeliveryEvent.PREEMPTED,
        "unrelated_generation",
    )
    registry._record_reply_delivery_event(
        context,
        candidate,
        ReplyDeliveryEvent.FIRST_FRAME_SENT,
        "still_speaking",
    )

    assert context.conversation_yield_candidate_fence == candidate
    assert metrics.get("voice_conversation_yield_proxy_total", {"status": "confirmed"}) == 0

    registry._record_reply_delivery_event(
        context,
        candidate,
        ReplyDeliveryEvent.PREEMPTED,
        "semantic_overlap",
    )

    assert context.conversation_yield_candidate_fence is None
    assert metrics.get("voice_conversation_yield_proxy_total", {"status": "confirmed"}) == 1
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_missed_hearing_nudge_cooldown_blocks_back_to_back_prompts() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-nudge-cooldown")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        context.pending.turn_endpoint_sample = 16_000
        decision = _verified_owner_decision()
        context.runtime._speaker_decision = decision
        context.runtime._speaker_class = decision.classification

        registry._nudge_missed_hearing(context)
        assert context.missed_hearing_nudge_count == 1
        registry._nudge_missed_hearing(context)
        assert context.missed_hearing_nudge_count == 1
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_assistant_state_emits_typed_floor_effect() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    effects: list[tuple[int, int, GenerationFence, str]] = []

    async def emit_floor_effect(
        _session_id: str,
        floor_state: int,
        *,
        floor_epoch: int,
        fence: GenerationFence,
        source_event_id: str,
        **_kwargs: object,
    ) -> bool:
        effects.append((floor_state, floor_epoch, fence, source_event_id))
        return True

    bridge.emit_floor_effect = emit_floor_effect  # type: ignore[attr-defined,method-assign]
    identity = SessionIdentity("typed-floor-session", stream_epoch=1)
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)

    task = context.runtime.publish_assistant_state("user_speaking")
    assert task is not None
    await task

    assert effects == [
        (
            media_pb2.FLOOR_STATE_SILENCE,
            1,
            GenerationFence(identity.session_id, 0, 0, 0),
            "media_session_ready",
        ),
        (
            media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
            2,
            context.runtime.fence,
            "assistant_state:user_speaking",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("projection", "transcript"))
async def test_device_accept_await_crossing_the_boundary_rejects_the_result(
    device_media_session: Any,
    seam: str,
) -> None:
    """An accepted result whose projection or transcript await crosses the floor fails closed."""

    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    window = await device_media_session(f"device-{seam}-crossing")
    context = window.context
    original_apply = window.registry._apply_projection_segment
    original_emit = window.registry.bridge.emit_transcript

    async def crossing_apply(ctx: Any, segment: Any) -> None:
        # Stands in for the boundary advancing while this await is in flight.
        context.pending.pending_turn_onset_floor = 484_480
        return await original_apply(ctx, segment)

    async def crossing_emit(*args: object, **kwargs: object) -> bool:
        context.pending.pending_turn_onset_floor = 484_480
        return await original_emit(*args, **kwargs)

    if seam == "projection":
        window.registry._apply_projection_segment = crossing_apply  # type: ignore[method-assign]
    else:
        window.registry.bridge.emit_transcript = crossing_emit  # type: ignore[method-assign]

    crossing = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="candidate-1",
        start_sample=258_880,
        end_sample=264_640,
        text="下午一起出发吗",
    )
    assert crossing.accepted is None
    assert crossing.reason is ASRDecisionReason.INTERVAL_CONFLICT
    assert context.pending.turn_start_sample is None
    assert context.pending.turn_end_sample is None


@pytest.mark.asyncio
async def test_typed_device_progress_waits_for_ended_before_completion() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity(
        "device-typed-playback",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="subject",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0, session_epoch=7)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.output.playback.start(fence)
    assert context.output.playback.register_audio(fence, 0, 0, 320)
    assert context.output.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=2,
            audio_start_sample=0,
            audio_end_sample=320,
            text="你好",
            sequence=0,
        )
    )
    context.output.provider_complete = True

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=320,
            client_monotonic_ms=1,
            approximate=False,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.PROGRESS,
        ),
    )

    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.actual_heard is True
    assert delivery.terminal_event is None

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=320,
            client_monotonic_ms=2,
            approximate=False,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )

    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
