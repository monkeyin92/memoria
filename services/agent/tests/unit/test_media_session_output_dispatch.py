"""Media session: output intent dispatch, ownership and preemption."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.delegation_coordinator import OutputIntentAdmission
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
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
    _OutputWork,
)
from services.agent.src.voice_core.media_session_types import (
    OutputDispatchResult,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _seed_pending_media_turn,
    _verified_owner_decision,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor
from services.speaker.domain import SpeakerDecision


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
