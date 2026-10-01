"""Media session: playback, output streaming and generation delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.delegation_coordinator import OutputIntentAdmission
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
    _OutputWork,
)
from services.agent.src.voice_core.media_session_output_stream import _next_pcm_send_slot
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
    OutputDispatchResult,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _ack_owned_filler_then_wait,
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _commit_pending_turn_from_device_endpoint,
    _device_identity,
    _feed_playback_window_finals,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _open_retained_window,
    _seed_pending_media_turn,
    _start_retained_utterance,
    _verified_owner_decision,
    _wait_until,
    open_bridge_connection,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor
from services.speaker.domain import SpeakerDecision


def test_pcm_output_pacer_spaces_frames_and_does_not_burst_after_a_stall() -> None:
    delay, next_send_at = _next_pcm_send_slot(
        now=10.0,
        next_send_at=10.0,
        frame_samples=480,
    )
    assert delay == 0
    assert next_send_at == pytest.approx(10.02)

    delay, next_send_at = _next_pcm_send_slot(
        now=10.005,
        next_send_at=next_send_at,
        frame_samples=480,
    )
    assert delay == pytest.approx(0.015)
    assert next_send_at == pytest.approx(10.04)

    delay, next_send_at = _next_pcm_send_slot(
        now=11.0,
        next_send_at=next_send_at,
        frame_samples=480,
    )
    assert delay == 0
    assert next_send_at == pytest.approx(11.02)


@pytest.mark.asyncio
async def test_registry_rejects_late_audio_after_terminal_cleanup() -> None:
    identity = SessionIdentity(
        "terminal-registry-input",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    connection = open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    await registry.open_session(identity)
    await registry.finalize_session(identity.session_id)

    assert bridge.bridge.is_terminal(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
    )
    await registry.on_audio_frame(
        connection.session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )
    assert registry.context(identity.session_id) is None
    with pytest.raises(ValueError, match="media session is terminal"):
        await registry.open_session(identity)


@pytest.mark.asyncio
async def test_old_epoch_provider_callback_cannot_update_speaker() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            started.set()
            await release.wait()
            return (
                ASRResult(
                    task_epoch=1,
                    sentence_id="old-epoch",
                    revision=1,
                    capture_start_sample=frame.capture_start_sample,
                    capture_end_sample=frame.capture_end_sample,
                    text="旧连接结果",
                    is_final=True,
                    stream_epoch=frame.identity.stream_epoch,
                ),
            )

    identity = SessionIdentity("old-epoch-callback", stream_epoch=1)
    provider = BlockingProvider()
    bridge = MediaBridgeGrpcServer()
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    speaker_pcm: list[bytes] = []
    runtime.feed_speaker_pcm = speaker_pcm.append  # type: ignore[method-assign]
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    session = bridge.bridge.open(identity)

    callback = asyncio.create_task(
        registry.on_audio_frame(
            session,
            AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await registry.open_session(SessionIdentity(identity.session_id, stream_epoch=2))
    release.set()
    await asyncio.wait_for(callback, timeout=1)

    assert speaker_pcm == []
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_slow_new_session_does_not_block_existing_session_audio() -> None:
    bridge = MediaBridgeGrpcServer()
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    resources: dict[str, MediaSessionResources] = {}

    async def build_session(identity: SessionIdentity) -> MediaSessionResources:
        if identity.session_id == "slow-new-session":
            slow_started.set()
            await release_slow.wait()
        resources[identity.session_id] = MediaSessionResources(
            DuplexRuntime.create(session_id=identity.session_id),
            FakeMediaProvider(),
        )
        return resources[identity.session_id]

    registry = MediaVoiceCoreRegistry(bridge=bridge, session_factory=build_session)
    registry.install()
    existing_identity = SessionIdentity("existing-session")
    existing_session = bridge.bridge.open(existing_identity)
    await registry.open_session(existing_identity)

    slow_task = asyncio.create_task(registry.open_session(SessionIdentity("slow-new-session")))
    await asyncio.wait_for(slow_started.wait(), timeout=1)
    audio_task = asyncio.create_task(
        registry.on_audio_frame(
            existing_session,
            AudioFrame(
                identity=existing_identity,
                sequence=0,
                capture_start_sample=0,
                frame_samples=2,
                payload=b"\x00\x00\x01\x00",
            ),
        )
    )
    completed, _ = await asyncio.wait({audio_task}, timeout=0.05)
    release_slow.set()
    await slow_task
    if audio_task not in completed:
        audio_task.cancel()
        await asyncio.gather(audio_task, return_exceptions=True)
    for resource in resources.values():
        await resource.runtime.close()
        await resource.provider.close(existing_identity)

    assert audio_task in completed
    assert resources[existing_identity.session_id].provider.audio_calls == [0]


@pytest.mark.asyncio
async def test_audio_ingress_overflow_drops_oldest_and_marks_discontinuity() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 0:
                started.set()
                await release.wait()
            return ()

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    bridge = MediaBridgeGrpcServer()
    provider = SlowProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=2,
    )
    identity = SessionIdentity("bounded-audio-pump")
    session = bridge.bridge.open(identity)

    for sequence in range(4):
        await asyncio.wait_for(
            registry.on_audio_frame(
                session,
                AudioFrame(
                    identity=identity,
                    sequence=sequence,
                    capture_start_sample=sequence * 2,
                    frame_samples=2,
                    payload=b"\x00\x00\x01\x00",
                ),
            ),
            timeout=0.05,
        )
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    for _ in range(20):
        if provider.audio_calls == [0, 2, 3]:
            break
        await asyncio.sleep(0)

    # Overflow drops only the oldest buffered frame (sequence 1) so the newest
    # speech still reaches the provider; both gap boundaries reset the task.
    assert provider.audio_calls == [0, 2, 3]
    assert provider.reset_samples == [4, 6]
    assert registry.metrics.get("media_pcm_overflow_total") >= 1
    assert registry.metrics.get("media_discontinuity_total") >= 1
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_audio_ingress_provider_failure_drains_backlog_and_recovers_next_frame() -> None:
    class RecoveringProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.recoveries = 0
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 0:
                self.first_started.set()
                await self.release_first.wait()
                raise RuntimeError("provider send failed")
            return ()

        async def recover_after_failure(self, _identity: SessionIdentity) -> None:
            self.recoveries += 1

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    provider = RecoveringProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=4,
    )
    identity = SessionIdentity("provider-send-recovery", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)

    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await registry.on_audio_frame(session, AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2))
    await registry.on_audio_frame(session, AudioFrame(identity, 2, 4, 2, b"\x02\x00" * 2))
    provider.release_first.set()
    for _ in range(50):
        if context.ingress.provider_failed:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0]
    assert context.ingress.queue.empty()
    assert context.ingress.provider_failed is True

    await registry.on_audio_frame(session, AudioFrame(identity, 3, 6, 2, b"\x03\x00" * 2))
    for _ in range(50):
        if provider.audio_calls == [0, 3]:
            break
        await asyncio.sleep(0)

    assert provider.recoveries == 1
    assert provider.reset_samples == [6]
    assert provider.audio_calls == [0, 3]
    assert context.ingress.provider_failed is False
    await context.runtime.close()


@pytest.mark.asyncio
async def test_audio_ingress_lazy_task_start_failure_drains_backlog_and_resets_at_recovery_frame() -> (
    None
):
    class LazyStartFailureProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.failure_started = asyncio.Event()
            self.release_failure = asyncio.Event()
            self.recoveries = 0
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 1:
                self.failure_started.set()
                await self.release_failure.wait()
                raise RuntimeError("lazy task start failed")
            return ()

        async def recover_after_failure(self, _identity: SessionIdentity) -> None:
            self.recoveries += 1

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    provider = LazyStartFailureProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=4,
    )
    identity = SessionIdentity("lazy-task-start-recovery", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)

    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))
    await registry.on_audio_frame(session, AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2))
    await asyncio.wait_for(provider.failure_started.wait(), timeout=1)
    await registry.on_audio_frame(session, AudioFrame(identity, 2, 4, 2, b"\x02\x00" * 2))
    provider.release_failure.set()

    for _ in range(50):
        if context.ingress.provider_failed:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0, 1]
    assert context.ingress.queue.empty()
    assert context.ingress.provider_failed is True

    await registry.on_audio_frame(session, AudioFrame(identity, 3, 6, 2, b"\x03\x00" * 2))
    for _ in range(50):
        if provider.audio_calls == [0, 1, 3]:
            break
        await asyncio.sleep(0)

    assert provider.recoveries == 1
    assert provider.reset_samples == [6]
    assert provider.audio_calls == [0, 1, 3]
    assert context.ingress.provider_failed is False
    await context.runtime.close()


@pytest.mark.asyncio
async def test_direct_device_reply_forwards_assistant_expression() -> None:
    identity = SessionIdentity(
        "direct-expression",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[str, dict[str, object]]] = []

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.events.append((event_type, payload))
            return True

    class HappyProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=0,
                    text="太好了，这真值得庆祝！",
                    first=True,
                    final=True,
                )

            return chunks()

    provider = HappyProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    context = await _seed_pending_media_turn(registry, identity, text="今天有个好消息")

    async def classify_owner(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _verified_owner_decision()

    context.runtime.set_speaker_classifier(classify_owner, sample_rate=16_000)
    context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)
    context.runtime.set_target_speaker_focus(True)
    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=600,
        retire_sample=640,
    )
    assert fence is not None, f"turn did not commit: {reason}"
    await registry.generate_reply(identity.session_id, "今天有个好消息", fence)
    await asyncio.sleep(0)
    expressions = [
        payload for event_type, payload in bridge.events if event_type == "assistant_expression"
    ]
    assert expressions
    assert expressions[0]["expression"] == "happy"
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_reconnect_preserves_current_output_intent_owner() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    identity = SessionIdentity("resume-output-owner", stream_epoch=1)
    context = await registry.open_session(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    await context.runtime.accept_media_generation(fence, cause="test")
    intent = context.runtime.orchestrator.delegation.conversation_reply(
        fence=fence,
        context_version=context.runtime.orchestrator.context_version_for_fence(fence),
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        now_ms=int(time.time() * 1_000),
    )
    coordinator = context.runtime.orchestrator.delegation
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
        )
        == ""
    )
    lease = SimpleNamespace(intent=intent, fence=fence, task=asyncio.current_task())
    context.output.output_owner = lease  # type: ignore[assignment]

    await registry._reuse_session(
        context,
        SessionIdentity(identity.session_id, stream_epoch=2),
    )

    assert context.output.output_owner is lease
    assert coordinator.output_intent_is_selected(
        intent,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
    )
    await context.runtime.close()
    await context.provider.close(context.identity)


@pytest.mark.asyncio
async def test_unheard_output_waits_through_transient_half_duplex_floor_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient floor loss must not kill an output before its first PCM."""

    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = SessionIdentity("unheard-floor-replay")
    context = await registry.open_session(identity)
    context.runtime.barge_in_enabled = False
    fence = await context.runtime.on_turn_committed("你好")
    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    intent = coordinator.conversation_reply(
        fence=fence,
        context_version=coordinator.current_context_version(identity.session_id),
        expires_at_ms=now_ms + 5_000,
        now_ms=now_ms,
    )
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms,
        )
        == ""
    )

    selected = True
    original_is_selected = coordinator.output_intent_is_selected

    def is_selected(
        _self: Any,
        candidate: Any,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
    ) -> bool:
        if not selected:
            return False
        return original_is_selected(
            candidate,
            current_fence=current_fence,
            current_context_version=current_context_version,
            floor_allows_output=floor_allows_output,
            now_ms=now_ms,
        )

    monkeypatch.setattr(type(coordinator), "output_intent_is_selected", is_selected)
    ready = asyncio.Event()
    lease_holder: list[Any] = []

    async def wait_for_first_frame() -> bool:
        nonlocal selected
        lease = SimpleNamespace(
            intent=intent,
            fence=fence,
            task=asyncio.current_task(),
        )
        context.output.output_owner = lease  # type: ignore[assignment]
        lease_holder.append(lease)
        set_floor(context.runtime, fresh_user_speech=True)  # simulate floor flip
        selected = False
        ready.set()
        return await registry._wait_for_unheard_output_floor(
            context,
            lease,
            emitted_audio=False,
        )

    wait_task = asyncio.create_task(wait_for_first_frame())
    await asyncio.wait_for(ready.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert not wait_task.done()
    assert context.output.output_owner is lease_holder[0]

    selected = True
    assert await asyncio.wait_for(wait_task, timeout=1)
    assert context.output.output_owner is lease_holder[0]
    set_floor(context.runtime, fresh_user_speech=False)
    registry._release_output_owner(context, fence, reason="test_complete")
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_nonzero_session_epoch_reply_reaches_provider_and_first_pcm() -> None:
    class ProbeProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.reply_fences: list[GenerationFence] = []

        def generate_reply(
            self,
            identity: SessionIdentity,
            user_text: str,
            fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            self.reply_fences.append(fence)
            return super().generate_reply(identity, user_text, fence)

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.output_admissions: list[OutputIntentAdmission] = []
            self.frames: list[object] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

    identity = SessionIdentity("nonzero-session-epoch-output")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    runtime.orchestrator.bump_session_epoch(7)
    provider = ProbeProvider()
    bridge = CapturingBridge()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=metrics,
    )
    registry.install()
    context = await registry.open_session(identity)
    context.runtime.orchestrator.delegation.set_output_intent_observer(
        bridge.output_admissions.append
    )
    fence = await context.runtime.on_turn_committed("你好")
    assert fence.session_epoch == 7
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)

    assert bridge.output_admissions
    assert bridge.output_admissions[0].accepted is True
    assert bridge.output_admissions[0].selected is True
    assert provider.reply_fences == [fence]
    assert len(bridge.frames) == 1
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.delivery_id == (
        "nonzero-session-epoch-output/epoch-7/turn-1/generation-1/tool-0"
    )
    assert delivery.events == (
        ReplyDeliveryEvent.FIRST_FRAME_SENT,
        ReplyDeliveryEvent.PROVIDER_COMPLETED,
    )
    assert delivery.terminal is False
    assert context.output.output_results == [
        OutputDispatchResult(
            fence,
            OutputDispatchStatus.COMPLETED,
            "provider_stream_complete",
            emitted_audio=True,
        )
    ]
    assert (
        metrics.get(
            "voice_output_dispatch_total",
            {"status": "completed", "reason": "provider_stream_complete"},
        )
        == 1
    )
    assert metrics.latency_samples["tts_first_frame"]
    assert metrics.latency_samples["tts_first_frame"][-1] >= 0
    assert metrics.get("voice_first_frame_preempted_total") == 0
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_reserved_output_kind_is_rejected_at_streamcore_execution_boundary() -> None:
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = SessionIdentity("reserved-output-kind")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    reserved = media_pb2.OutputIntent(
        intent_id="reserved-notification",
        session_id=identity.session_id,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        kind=media_pb2.OUTPUT_INTENT_KIND_NOTIFICATION,
        priority=1,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 5_000,
        floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
        context_version=coordinator.current_context_version(identity.session_id),
        tts_source="这条预留通知不能播放。",
    )
    assert (
        coordinator.admit_output_intent(
            reserved,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        == "这条预留通知不能播放。"
    )

    assert not await registry._enqueue_output_work(context, _OutputWork(reserved, fence))
    assert str(reserved.intent_id) not in context.output.output_work
    assert not coordinator.output_intent_is_active(
        reserved,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 1,
    )
    assert context.output.output_owner is None
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_higher_priority_intent_supersedes_main_reply_before_pcm() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                started.set()
                await release.wait()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=0,
                    first=True,
                    final=True,
                )

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

    provider = BlockingProvider()
    bridge = CapturingBridge()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        metrics=metrics,
    )
    registry.install()
    identity = SessionIdentity("superseded-output-owner")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("帮我查一下")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "帮我查一下", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert context.output.output_owner is not None

    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    acknowledgement = coordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=fence,
        context_version=coordinator.current_context_version(identity.session_id),
        expires_at_ms=now_ms + 2_000,
        now_ms=now_ms,
    )
    assert (
        coordinator.admit_output_intent(
            acknowledgement,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        == BRIDGE_PHRASES[0]
    )
    release.set()

    assert not await asyncio.wait_for(reply, timeout=1)
    assert bridge.frames == []
    assert provider.cancelled == [fence]
    assert context.output.output_owner is None
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PREEMPTED
    assert delivery.first_frame_sent is False
    assert metrics.get("voice_first_frame_preempted_total") == 1
    coordinator.complete_output_intent(
        acknowledgement,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 2,
    )
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_higher_priority_pcm_preempts_after_audio_and_restarts_from_zero() -> None:
    started = asyncio.Event()

    class BlockingProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                started.set()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=0,
                    text="旧回复",
                    first=True,
                    final=False,
                )
                await asyncio.Event().wait()

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []
            self.generations: list[tuple[int, GenerationFence]] = []
            self.effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

        async def emit_generation(
            self,
            _session_id: str,
            fence: GenerationFence,
            *,
            action: int,
            **_kwargs: object,
        ) -> bool:
            self.generations.append((action, fence))
            return True

        async def emit_realtime_effect(
            self,
            _session_id: str,
            effect_kind: int,
            fence: GenerationFence,
            *,
            source_event_id: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.effects.append((effect_kind, fence, source_event_id, payload))
            return True

    provider = BlockingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("preempt-after-audio")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "查一下", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(bridge.frames) == 1
    assert bridge.frames[0].generation_id == fence.generation_id

    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    urgent = media_pb2.OutputIntent(
        intent_id="urgent-pcm",
        session_id=identity.session_id,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        kind=media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
        priority=100,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 5_000,
        floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
        context_version=coordinator.current_context_version(identity.session_id),
        pcm_s16le=b"\x06\x00\x07\x00",
    )
    assert (
        coordinator.admit_output_intent(
            urgent,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        == ""
    )
    assert await registry._enqueue_output_work(context, _OutputWork(urgent, fence))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reply, timeout=1)
    await asyncio.sleep(0)

    assert provider.cancelled == [fence]
    assert len(bridge.frames) == 2
    replacement = bridge.frames[1]
    assert replacement.generation_id > fence.generation_id
    assert replacement.sequence == 0
    assert replacement.source_start_sample == 0
    replacement_fence = GenerationFence(
        identity.session_id,
        replacement.turn_id,
        replacement.generation_id,
        replacement.tool_epoch,
    )
    assert any(
        effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
        and cancelled.generation_id > fence.generation_id
        and source_event_id == "output_preempted"
        and payload == {"reason": "output_preempted"}
        for effect_kind, cancelled, source_event_id, payload in bridge.effects
    )
    assert any(
        action == media_pb2.GENERATION_ACTION_START and new.matches(replacement_fence)
        for action, new in bridge.generations
    )

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=replacement_fence.generation_id,
            received_sequence=0,
            rendered_sample_end=replacement.frame_samples,
            client_monotonic_ms=1,
            turn_id=replacement_fence.turn_id,
            tool_epoch=replacement_fence.tool_epoch,
        ),
    )
    assert context.output.output_owner is None
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_unheard_preempt_does_not_emit_playback_flush() -> None:
    started = asyncio.Event()

    class BlockingProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                started.set()
                await asyncio.Event().wait()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=0,
                    text="旧回复",
                    first=True,
                    final=False,
                )

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []
            self.generations: list[tuple[int, GenerationFence]] = []
            self.effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

        async def emit_generation(
            self,
            _session_id: str,
            fence: GenerationFence,
            *,
            action: int,
            **_kwargs: object,
        ) -> bool:
            self.generations.append((action, fence))
            return True

        async def emit_realtime_effect(
            self,
            _session_id: str,
            effect_kind: int,
            fence: GenerationFence,
            *,
            source_event_id: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.effects.append((effect_kind, fence, source_event_id, payload))
            return True

    provider = BlockingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity(
        "device-unheard-preempt",
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
    fence = await context.runtime.on_turn_committed("查一下")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "查一下", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert context.output.output_owner is not None
    assert bridge.frames == []

    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    urgent = media_pb2.OutputIntent(
        intent_id="urgent-pcm",
        session_id=identity.session_id,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        context_version=coordinator.current_context_version(identity.session_id),
        kind=media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
        priority=100,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 5_000,
        floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
        pcm_s16le=b"\x06\x00\x07\x00",
    )
    assert (
        coordinator.admit_output_intent(
            urgent,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        == ""
    )
    assert await registry._enqueue_output_work(context, _OutputWork(urgent, fence))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reply, timeout=1)
    await asyncio.sleep(0)

    assert provider.cancelled == [fence]
    assert not any(
        effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
        for effect_kind, _cancelled, _source, _payload in bridge.effects
    )
    assert len(bridge.frames) == 1
    replacement = bridge.frames[0]
    assert replacement.generation_id > fence.generation_id
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=replacement.generation_id,
            received_sequence=0,
            rendered_sample_end=replacement.frame_samples,
            client_monotonic_ms=1,
            turn_id=replacement.turn_id,
            tool_epoch=replacement.tool_epoch,
        ),
    )
    assert context.output.output_owner is None
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_expired_emitted_output_restores_listen_and_flushes_device() -> None:
    first_frame_sent = asyncio.Event()
    release = asyncio.Event()

    class ExpiringProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=0,
                    text="旧回复",
                    first=True,
                    final=False,
                )
                await release.wait()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x04\x00\x05\x00",
                    source_start_sample=2,
                    text="尾句",
                    final=True,
                )

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []
            self.effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            first_frame_sent.set()
            return True

        async def emit_realtime_effect(
            self,
            _session_id: str,
            effect_kind: int,
            fence: GenerationFence,
            *,
            source_event_id: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.effects.append((effect_kind, fence, source_event_id, payload))
            return True

    provider = ExpiringProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity(
        "device-expired-emitted-output",
        account_id="account",
        device_id="device",
        subject_id="subject",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        client_type="device",
    )
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "查一下", fence))
    await asyncio.wait_for(first_frame_sent.wait(), timeout=1)

    coordinator = context.runtime.orchestrator.delegation
    current = coordinator.current_output_intent(
        identity.session_id,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
    )
    assert current is not None
    current.expires_at_ms = int(time.time() * 1_000) - 1
    release.set()

    assert await asyncio.wait_for(reply, timeout=1) is False
    assert provider.cancelled == [fence]
    assert context.output.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.interaction_phase is InteractionPhase.LISTENING
    assert context.output.playback.current_fence is None
    assert any(
        effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
        and cancelled.generation_id == fence.generation_id + 1
        and source_event_id == "output_superseded"
        and payload == {"reason": "superseded"}
        for effect_kind, cancelled, source_event_id, payload in bridge.effects
    )
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.first_frame_sent is True
    assert delivery.terminal_event is ReplyDeliveryEvent.PREEMPTED
    await registry.finalize_session(identity.session_id)


async def _speaking_session_with_effects(
    name: str,
    *,
    speak_first: bool,
) -> tuple[Any, Any, Any, GenerationFence, list[tuple[int, GenerationFence, str, dict[str, object]]], asyncio.Task[bool], asyncio.Event]:
    """A device session whose first output of ``fence`` puts one frame on the wire (or none)."""

    first_frame_sent = asyncio.Event()
    release = asyncio.Event()

    class AckProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                if speak_first:
                    yield MediaReplyChunk(
                        pcm_s16le=b"\x02\x00\x03\x00",
                        source_start_sample=0,
                        text="我查一下",
                        first=True,
                        final=False,
                    )
                await release.wait()

            return chunks()

    effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

    class CapturingBridge(MediaBridgeGrpcServer):
        async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
            first_frame_sent.set()
            return True

        async def emit_realtime_effect(
            self,
            _session_id: str,
            effect_kind: int,
            fence: GenerationFence,
            *,
            source_event_id: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            effects.append((effect_kind, fence, source_event_id, payload))
            return True

    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _identity: AckProvider())
    registry.install()
    identity = _device_identity(name)
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "查一下", fence))
    if speak_first:
        await asyncio.wait_for(first_frame_sent.wait(), timeout=1)
    else:
        await asyncio.sleep(0.05)
    return registry, identity, context, fence, effects, reply, release


@pytest.mark.asyncio
async def test_a_failed_follow_on_output_still_closes_a_fence_that_already_spoke() -> None:
    """The dispatch that fails sent nothing (its first frame was rejected as a sequence gap), but the
    acknowledgement before it did: without a terminal fence the device stays in SPEAKING."""

    registry, identity, context, fence, effects, reply, release = await _speaking_session_with_effects(
        "follow-on-failure-after-ack", speak_first=True
    )
    try:
        assert context.output.audio_sent_for(fence) is True
        await registry._abort_unheard_stream(
            context, fence, reason="transport_rejected", emitted_audio=False
        )
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert any(
            effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
            and cancelled.generation_id == fence.generation_id + 1
            for effect_kind, cancelled, _source, _payload in effects
        )
    finally:
        release.set()
        await asyncio.gather(reply, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_a_failed_output_on_a_fence_that_never_spoke_does_not_cancel_it() -> None:
    """The control: nothing reached the device, so there is no audible generation to close."""

    registry, identity, context, fence, effects, reply, release = await _speaking_session_with_effects(
        "follow-on-failure-silent", speak_first=False
    )
    try:
        assert context.output.audio_sent_for(fence) is False
        await registry._abort_unheard_stream(
            context, fence, reason="transport_rejected", emitted_audio=False
        )
        assert not any(
            effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
            for effect_kind, _cancelled, _source, _payload in effects
        )
    finally:
        release.set()
        await asyncio.gather(reply, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_owner_is_rechecked_after_speaking_transition_before_output() -> None:
    speaking_started = asyncio.Event()
    release_speaking = asyncio.Event()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.event_types: list[str] = []
            self.frames: list[object] = []

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            _payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.event_types.append(event_type)
            return True

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("owner-recheck-after-await")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("帮我查一下")
    context.output.playback.start(fence)
    original = context.runtime.on_assistant_speaking

    async def blocked_speaking(text: str, **kwargs: object) -> bool:
        speaking_started.set()
        await release_speaking.wait()
        return await original(text, **kwargs)

    context.runtime.on_assistant_speaking = blocked_speaking  # type: ignore[method-assign]
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "帮我查一下", fence))
    await asyncio.wait_for(speaking_started.wait(), timeout=1)

    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    acknowledgement = coordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=fence,
        context_version=coordinator.current_context_version(identity.session_id),
        expires_at_ms=now_ms + 2_000,
        now_ms=now_ms,
    )
    assert (
        coordinator.admit_output_intent(
            acknowledgement,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        == BRIDGE_PHRASES[0]
    )
    release_speaking.set()

    assert not await asyncio.wait_for(reply, timeout=1)
    await asyncio.sleep(0)
    assert "transcript_delta" not in bridge.event_types
    assert "assistant_expression" not in bridge.event_types
    assert bridge.frames == []
    assert provider.cancelled == [fence]
    assert context.output.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.THINKING
    assert context.runtime._voice_floor.pending_assistant_text == ""
    assert context.runtime.assistant_speaking is False
    assert context.runtime._assistant_expression_fence is None
    coordinator.complete_output_intent(
        acknowledgement,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 2,
    )
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_progressing_output_generation_is_not_killed_by_wall_clock_timeout() -> None:
    """Long TTS is paced to realtime; the deadline is a stall watchdog."""

    class SlowProgressProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                for index in range(4):
                    if index:
                        await asyncio.sleep(0.04)
                    yield MediaReplyChunk(
                        pcm_s16le=b"\x02\x00\x03\x00",
                        source_start_sample=index * 2,
                        text="今" if index == 0 else "",
                        first=index == 0,
                        final=index == 3,
                    )

            return chunks()

    bridge = MediaBridgeGrpcServer()
    provider = SlowProgressProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        output_generation_timeout_s=0.08,
    )
    registry.install()
    identity = SessionIdentity("progressing-output-generation")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("今天天气怎么样")
    context.output.playback.start(fence)

    result = await asyncio.wait_for(
        registry.generate_reply(identity.session_id, "今天天气怎么样", fence),
        timeout=1,
    )

    assert result is True
    assert context.output.output_owner is None
    assert provider.cancelled == []
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_reclassified_as_generation_deadline() -> None:
    class ProviderTimeout(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                raise TimeoutError("provider timeout")
                yield MediaReplyChunk(b"\x00\x00", 0)  # pragma: no cover

            return chunks()

    bridge = MediaBridgeGrpcServer()
    provider = ProviderTimeout()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        output_generation_timeout_s=1.0,
    )
    registry.install()
    identity = SessionIdentity("provider-timeout-error")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)

    with pytest.raises(TimeoutError, match="provider timeout"):
        await registry.generate_reply(identity.session_id, "你好", fence)

    assert context.output.output_owner is None
    assert context.output.playback.current_fence is fence
    assert context.runtime.orchestrator.state is ConversationState.THINKING
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_unheard_wake_pcm_restores_half_duplex_listen() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()

    def runtime_factory(session_id: str) -> DuplexRuntime:
        runtime = DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)

        def drop_pcm(_cancellation: object, _pcm: bytes) -> bytes | None:
            return None

        runtime.gate_tts_audio = drop_pcm  # type: ignore[method-assign]
        return runtime

    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=runtime_factory,
    )
    registry.install()
    identity = _device_identity("device-unheard-wake")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await _wait_until(
            lambda: (
                context.output.output_owner is None
                and context.runtime.assistant_speaking is False
                and context.runtime.orchestrator.state is ConversationState.LISTENING
                and bool(provider.texts)
            )
        )
        assert context.runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_late_clock_fact_recovery_does_not_preempt_in_flight_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Late overlap-rejected clock-fact must not cancel an in-flight reply.

    Regression for stage-7 epoch 1363: weekday early-committed turn3 and
    started Doubao TTS, then a longer-range offline final was rejected as
    cross_sentence_overlap, recovery early-committed turn4, and cancel_reply
    killed turn3 (reply_task_exception). While a reply owns the session,
    recovery may still ingest timeline text but must not re-arm/commit.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-late-clock-fact-inflight")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        first = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=178_240,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, first)

        async def _reply_owns_session() -> bool:
            return bool(
                context.output.output_owner is not None
                and any(
                    turn.role == "user" and turn.content == "今天星期几"
                    for turn in context.runtime.orchestrator.context.turns
                )
            )

        for _ in range(40):
            if await _reply_owns_session():
                break
            await asyncio.sleep(0.05)
        assert await _reply_owns_session()
        assert MediaVoiceCoreRegistry._reply_in_flight(context)
        in_flight_fence = context.output.output_owner.fence
        in_flight_task = context.output.reply_task
        generation_count_before = len(bridge.generation_starts)
        user_turns_before = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns_before == ["今天星期几"]
        committed = context.asr.last_committed_sample
        assert committed >= 178_240

        # Blocking interval after the committed watermark (playback echo shape).
        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="playback-echo",
            revision=1,
            capture_start_sample=committed,
            capture_end_sample=committed + 62_080,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, echo)

        # Longer overlapping rescue final cannot supersede a real provider
        # interval → cross_sentence_overlap → recovery must skip re-commit.
        late = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-late-offline",
            revision=1,
            capture_start_sample=committed,
            capture_end_sample=committed + 120_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.95,
            rescue_synthesized=True,
        )
        with caplog.at_level(logging.WARNING):
            decision = await registry._accept_asr_result_decision(
                identity.session_id,
                late,
            )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.pending.turn_endpoint_sample is None
        assert context.output.output_owner is not None
        assert context.output.output_owner.fence == in_flight_fence
        if in_flight_task is not None:
            assert context.output.reply_task is in_flight_task
            assert not in_flight_task.cancelled()
        assert len(bridge.generation_starts) == generation_count_before
        user_turns_after = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns_after == ["今天星期几"]
        assert any(
            "clock-fact recovery commit skipped: reply in flight" in record.message
            for record in caplog.records
        )
        await asyncio.sleep(0.05)
        assert not any(
            "reply_task_exception" in record.message or "reply_task_cancelled" in record.message
            for record in caplog.records
        )
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_half_duplex_owned_result_respects_a_closed_output_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late result must not clear fresh speech and reopen the floor."""

    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = SessionIdentity("half-duplex-closed-floor")
    context = None
    try:
        context, fence = await _ack_owned_filler_then_wait(
            registry,
            identity,
            provider,
            bridge,
        )
        claim = context.output.delegation_output_claims[fence]
        set_floor(context.runtime, fresh_user_speech=True)
        context.runtime.set_interaction_phase(
            InteractionPhase.USER_SPEAKING,
            cause="test_fresh_user_speech",
        )

        provider.release.set()
        with caplog.at_level(logging.INFO):
            await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED, timeout=1)
        await asyncio.sleep(0)

        assert not provider.deep_started.is_set()
        assert context.runtime._voice_floor.fresh_user_speech is True
        assert any(
            work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
            for work in context.output.output_work.values()
        )
        assert context.output.output_retry_task is None
        assert any(
            "media output deferred" in record.getMessage()
            for record in caplog.records
        )
    finally:
        provider.release.set()
        if context is not None:
            await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_inactive_output_intent_drops_deep_result_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """epoch 1912: 已完成的深查答案不得因输出意图非活动而零错误静默丢弃。

    The finished deep answer was released with ``reason=output_intent_inactive``
    and only an INFO line, so losing a completed answer left no trace.  The drop
    must now surface an explicit warning on the delegation seam.
    """

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("deep-result-dropped-observable")
    try:
        context = await registry.open_session(identity)
        delegation = context.runtime.orchestrator.delegation
        # Force the exact seam that dropped the epoch-1912 answer: the admitted
        # output intent is judged inactive even though the deep result finished.
        monkeypatch.setattr(
            type(delegation),
            "output_intent_is_active",
            lambda *_args, **_kwargs: False,
        )
        provider.release.set()
        with caplog.at_level(logging.WARNING):
            await context.runtime.on_turn_committed(question)
            await _wait_until(
                lambda: any(
                    claim.state is DelegationOutputState.RELEASED
                    for claim in context.output.delegation_output_claims.values()
                ),
                timeout=2.0,
            )
        assert any(
            "deep result dropped" in record.getMessage() for record in caplog.records
        )
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_blocked_output_work_is_bounded_with_its_admission_queue() -> None:
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _: FakeMediaProvider())
    identity = SessionIdentity("bounded-blocked-output")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    try:
        fence = await context.runtime.on_turn_committed("你好")
        context.runtime.on_user_voice_started()
        coordinator = context.runtime.orchestrator.delegation
        now = int(time.time() * 1_000)
        for index in range(12):
            intent = coordinator.bridge_acknowledgement(
                BRIDGE_PHRASES[0], fence=fence,
                context_version=coordinator.current_context_version(identity.session_id),
                expires_at_ms=now + 20_000, now_ms=now,
            )
            intent.intent_id = f"bounded-{index:02d}"
            coordinator.admit_output_intent(
                intent, current_fence=fence,
                current_context_version=coordinator.current_context_version(identity.session_id),
                floor_allows_output=False, now_ms=now,
            )
            assert await registry._enqueue_output_work(context, _OutputWork(intent, fence))
            assert len(context.output.output_work) <= 4
            assert context.output.output_retry_task is None
        assert not bridge.frames
        await context.runtime.accept_media_generation(fence.bump_turn(), cause="new_turn")
        registry._schedule_output_retry(context)
        assert not context.output.output_work
        assert context.output.output_retry_task is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_playback_overlap_split_is_blocked_by_forced_text(
    device_media_session: Any,
) -> None:
    """An authoritative forced text keeps the candidate; releasing it splits."""

    window = await device_media_session("split-forced-text")
    await _feed_playback_window_finals(window)
    _start_retained_utterance(window)
    context = window.context
    assert context.pending.pending_turn_playback_overlap is True

    context.pending.live_query_forced_text = "明天南京天气"
    forced = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-1",
        start_sample=400_000,
        end_sample=410_000,
        text="下午一起出发吗",
    )
    assert forced.accepted is not None
    assert context.pending.turn_start_sample == 158_560

    context.pending.live_query_forced_text = None
    split = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-2",
        start_sample=470_000,
        end_sample=480_000,
        text="下午一起出发吗",
        revision=2,
    )
    assert split.accepted is not None
    assert context.pending.turn_start_sample == 470_000


@pytest.mark.asyncio
async def test_device_retained_window_drops_playback_overlap_after_the_split(
    device_media_session: Any,
) -> None:
    """The retained window is no longer overlap: its own long pause still merges."""

    window = await _open_retained_window(
        device_media_session,
        "device-retained-flag",
        retained_end=490_000,
    )
    context = window.context
    assert context.pending.pending_turn_playback_overlap is False
    assert context.pending.pending_turn_onset_floor == 484_480
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="clause-2",
        start_sample=532_000,
        end_sample=540_000,
        text="还有明天呢",
        revision=2,
    )
    assert context.pending.turn_start_sample == 484_480

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=540_000)
    text = window.provider.prepared[-1]
    assert "下午一起出发吗" in text and "还有明天呢" in text
    assert "AAA" not in text


@pytest.mark.asyncio
async def test_approximate_device_progress_completes_without_actual_heard() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
        metrics=metrics,
    )
    registry.install()
    identity = SessionIdentity(
        "device-approximate-playback",
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
    fence = GenerationFence(identity.session_id, 1, 1, 0)
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
            approximate=True,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )

    assert context.output.playback.actual_heard_text(fence) == ""
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert all(turn.content != "你好" for turn in context.runtime.orchestrator.context.turns)
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
    assert delivery.actual_heard is False
    assert (
        metrics.get("voice_conversation_participation_proxy_ms_total", {"kind": "assistant"}) == 0
    )


@pytest.mark.asyncio
async def test_typed_device_playback_error_is_not_successful_completion() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.controls: list[tuple[int, GenerationFence, str]] = []

        async def emit_generation(
            self,
            _session_id: str,
            fence: GenerationFence,
            *,
            action: int,
            reason: str = "",
            **_kwargs: object,
        ) -> bool:
            self.controls.append((action, fence, reason))
            return True

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity(
        "device-playback-error",
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
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.output.playback.start(fence)
    assert context.output.playback.register_audio(fence, 0, 0, 320)
    context.output.provider_complete = True

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=0,
            client_monotonic_ms=1,
            approximate=True,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ERROR,
        ),
    )

    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.ERROR
    assert delivery.playback_ended is False
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.fence.generation_id == fence.generation_id + 1
    assert bridge.controls == [
        (
            media_pb2.GENERATION_ACTION_CANCEL,
            context.runtime.fence,
            "device_playback_error",
        )
    ]
