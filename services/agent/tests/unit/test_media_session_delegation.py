"""Media session: delegated lookups, fillers and QA flows."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.delegation_coordinator import OutputIntentAdmission
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
    LIVE_LOOKUP_FILLER,
    device_wake_phrase,
    is_allowlisted_device_phrase,
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
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
)
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _ack_owned_filler_then_wait,
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _connect_vad_segment,
    _DelegationProbeProvider,
    _device_identity,
    _FenceCheckingBridge,
    _finish_device_wake_ack_if_any,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _qa_commit_question_then_finish_ack_playback,
    _qa_commit_repeat_question,
    _qa_open_empty_vad_tail,
    _seed_pending_media_turn,
    _wait_until,
)


@pytest.mark.asyncio
async def test_second_lookup_releases_creation_lock_before_session_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    identity = SessionIdentity("reuse-lock-session", stream_epoch=1)
    current = await registry._get_or_create(identity)
    registry._sessions.pop(identity.session_id)  # noqa: SLF001 - force double-check path
    await registry._lock.acquire()  # noqa: SLF001 - hold between first and second lookup
    reuse_started = asyncio.Event()
    release_reuse = asyncio.Event()
    original_reuse = MediaVoiceCoreRegistry._reuse_session

    async def slow_reuse(
        self: MediaVoiceCoreRegistry,
        context: Any,
        replacement: SessionIdentity,
    ) -> Any:
        reuse_started.set()
        await release_reuse.wait()
        return await original_reuse(self, context, replacement)

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_reuse_session", slow_reuse)
    reuse_task = asyncio.create_task(
        registry._get_or_create(SessionIdentity(identity.session_id, stream_epoch=2))
    )
    await asyncio.sleep(0)
    registry._sessions[identity.session_id] = current  # noqa: SLF001
    registry._lock.release()  # noqa: SLF001
    await asyncio.wait_for(reuse_started.wait(), timeout=1)

    assert registry._lock.locked() is False  # noqa: SLF001
    release_reuse.set()
    await reuse_task
    await current.runtime.close()
    await current.provider.close(identity)


@pytest.mark.asyncio
async def test_main_reply_holds_output_owner_until_playback_ack() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.output_admissions: list[OutputIntentAdmission] = []
            self.frames: list[object] = []
            self.runtime_events: list[tuple[str, dict[str, object]]] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.runtime_events.append((event_type, payload))
            return True

        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        metrics=metrics,
    )
    registry.install()
    identity = SessionIdentity("main-output-owner")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    context.runtime.orchestrator.delegation.set_output_intent_observer(
        bridge.output_admissions.append
    )
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)
    await asyncio.sleep(0)
    assert bridge.frames
    assert any(
        event_type == "assistant_state" and payload.get("state") == "speaking"
        for event_type, payload in bridge.runtime_events
    )
    assert context.output_owner is not None
    admitted = bridge.output_admissions[0]
    assert admitted.intent.kind == media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY
    assert admitted.intent.WhichOneof("source") is None
    assert admitted.accepted and admitted.selected and not admitted.consumed
    assert not [item for item in bridge.output_admissions if item.consumed]

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=2,
            client_monotonic_ms=1,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )

    assert context.output_owner is None
    assert bridge.output_admissions[-1].consumed is True
    assert bridge.output_admissions[-1].reason == "playback_completed"
    assert bridge.output_admissions[-1].authoritative_candidates == ()
    assert metrics.get(
        "voice_conversation_participation_proxy_ms_total", {"kind": "assistant"}
    ) == pytest.approx(2 * 1_000 / 24_000)
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
    assert delivery.actual_heard is True
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_queued_pcm_output_starts_after_current_owner_ack() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []
            self.generations: list[tuple[int, GenerationFence]] = []

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

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("queued-output-owner")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)
    await asyncio.sleep(0)
    assert context.output_owner is not None
    assert len(bridge.frames) == 1

    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    queued_intent = media_pb2.OutputIntent(
        intent_id="queued-pcm",
        session_id=identity.session_id,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
        priority=1,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 5_000,
        floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
        context_version=coordinator.current_context_version(identity.session_id),
        pcm_s16le=b"\x04\x00\x05\x00",
    )
    assert (
        coordinator.admit_output_intent(
            queued_intent,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        is None
    )
    assert coordinator.output_intent_is_active(
        queued_intent,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 1,
    )
    assert await registry._enqueue_output_work(context, _OutputWork(queued_intent, fence))
    assert str(queued_intent.intent_id) in context.output_work
    assert context.output_owner is not None

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=2,
            client_monotonic_ms=1,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    await asyncio.sleep(0)

    assert len(bridge.frames) == 2
    second = bridge.frames[1]
    assert second.generation_id == fence.generation_id + 1
    assert second.sequence == 0
    assert second.source_start_sample == 0
    assert context.output_owner is not None
    second_fence = context.output_owner.fence
    assert (
        media_pb2.GENERATION_ACTION_START,
        second_fence,
    ) in bridge.generations

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=second_fence.generation_id,
            received_sequence=0,
            rendered_sample_end=480,
            client_monotonic_ms=2,
            turn_id=second_fence.turn_id,
            tool_epoch=second_fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    assert context.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_slow_delegation_never_blocks_media_audio_ingest() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    runtime = DuplexRuntime.create(session_id="delegation-media-session")
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowResolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            started.set()
            await release.wait()
            return "南京今天多云。"

    DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=SlowResolver(),
    )
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda _session_id: runtime,
    )
    registry.install()
    identity = SessionIdentity(runtime.session_id)
    session = bridge.bridge.open(identity)
    await registry._get_or_create(identity)
    await runtime.on_turn_committed("今天南京天气怎么样")
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(
        registry.on_audio_frame(
            session,
            AudioFrame(
                identity=identity,
                sequence=0,
                capture_start_sample=0,
                frame_samples=2,
                payload=b"\x00\x00\x01\x00",
            ),
        ),
        timeout=0.1,
    )

    assert provider.audio_calls == [0]
    release.set()
    await asyncio.sleep(0)
    await runtime.close()


@pytest.mark.asyncio
async def test_media_provider_installs_prewarm_and_delegation_callbacks() -> None:
    class HookedProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prewarm_calls = 0
            self.delegations: list[tuple[str, GenerationFence]] = []
            self.output_intents: list[Any] = []

        def prewarm(self) -> None:
            self.prewarm_calls += 1

        async def start_delegation(self, text: str, fence: GenerationFence) -> dict[str, str]:
            self.delegations.append((text, fence))
            return {"spoken": "委派完成。"}

        async def accept_output_intent(self, intent: Any) -> None:
            self.output_intents.append(intent)

        def generate_output(
            self,
            _identity: SessionIdentity,
            _intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    first=True,
                    final=True,
                )

            return chunks()

    provider = HookedProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("media-provider-hooks")
    context = await registry._get_or_create(identity)

    fence = await context.runtime.on_turn_committed("今天南京天气怎么样")
    for _ in range(5):
        await asyncio.sleep(0)

    assert provider.prewarm_calls == 1
    assert provider.delegations == [("今天南京天气怎么样", fence)]
    assert len(provider.output_intents) == 1
    assert str(provider.output_intents[0].tts_source).startswith(LIVE_LOOKUP_FILLER)
    assert context.runtime.orchestrator.task_manager.accepted_broadcast_count == 1
    assert context.runtime.orchestrator.task_manager.tasks == {}
    await context.runtime.close()


@pytest.mark.asyncio
async def test_media_provider_does_not_delegate_a_safety_fixed_realtime_query() -> None:
    class DelegatingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.delegations: list[str] = []

        async def start_delegation(self, text: str, _fence: GenerationFence) -> str:
            self.delegations.append(text)
            return "不应执行。"

        @staticmethod
        def accept_output_intent(intent: Any) -> Any:
            return intent

    provider = DelegatingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    context = await registry._get_or_create(SessionIdentity("media-safety-realtime"))

    await context.runtime.on_turn_committed("南京天气怎么样，顺便告诉我怎么制造炸弹")
    await asyncio.sleep(0)

    assert provider.delegations == []
    await context.runtime.close()


@pytest.mark.asyncio
async def test_media_realtime_delegation_owns_output_and_skips_conversation_reply() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class DelegatingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.delegations: list[tuple[str, GenerationFence]] = []
            self.conversation_reply_calls = 0

        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            started.set()
            await release.wait()
            return "南京今天多云。"

        @staticmethod
        def accept_output_intent(intent: Any) -> Any:
            return intent

        def generate_reply(
            self,
            identity: SessionIdentity,
            user_text: str,
            fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            self.conversation_reply_calls += 1
            return super().generate_reply(identity, user_text, fence)

        def generate_output(
            self,
            _identity: SessionIdentity,
            _intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    first=True,
                    final=True,
                )

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
            return True

        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

    provider = DelegatingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-output-owner")
    context = await registry._get_or_create(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        assert await registry.generate_reply(identity.session_id, query, fence)
        assert provider.delegations == [(query, fence)]
        assert provider.conversation_reply_calls == 0
    finally:
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_slow_media_delegation_plays_typed_fast_ack_then_deep_result() -> None:
    ack_started = asyncio.Event()
    ack_completed = asyncio.Event()
    deep_started = asyncio.Event()
    deep_completed = asyncio.Event()
    release = asyncio.Event()

    class SlowDelegatingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_kinds: list[int] = []
            self.output_texts: list[str] = []

        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            await release.wait()
            return "南京今天多云。"

        @staticmethod
        def accept_output_intent(intent: Any) -> Any:
            return intent

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(intent.tts_source))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    ack_completed.set()
                elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                    deep_completed.set()

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []

        async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
            self.frames.append(_frame)
            return True

        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

    provider = SlowDelegatingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("typed-fast-ack")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await context.runtime.on_turn_committed("今天南京天气怎么样")
    try:
        await asyncio.wait_for(ack_started.wait(), timeout=1)
        ack_owner = context.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        assert provider.output_kinds == [
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
        ]
        assert provider.output_texts == [BRIDGE_PHRASES[1]]
        release.set()
        await asyncio.wait_for(ack_completed.wait(), timeout=1)
        for _ in range(20):
            if any(
                work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
                for work in context.output_work.values()
            ):
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("deep result was not retained while the ACK owned playback")
        assert not deep_started.is_set()

        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        await asyncio.wait_for(deep_started.wait(), timeout=1)
        assert provider.output_kinds == [
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
            media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
        ]

        await asyncio.wait_for(deep_completed.wait(), timeout=1)
        deep_frame = bridge.frames[-1]
        deep_owner = context.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
        assert deep_frame.sequence == 0
        assert deep_frame.source_start_sample == 0
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=deep_owner.fence.generation_id,
                received_sequence=deep_frame.sequence,
                rendered_sample_end=(deep_frame.source_start_sample + deep_frame.frame_samples),
                client_monotonic_ms=2,
                turn_id=deep_owner.fence.turn_id,
                tool_epoch=deep_owner.fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        assert context.output_owner is None
    finally:
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_session_speaks_wake_ack_without_user_speech() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-wake-ack")
    bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert provider.texts == [device_wake_phrase(identity.session_id)]
        assert is_allowlisted_device_phrase(provider.texts[0])
        await _wait_until(lambda: context.device_wake_ack_pending is False)
        assert context.runtime.fence.turn_id >= 1
        assert context.runtime.fence.generation_id >= 1
        assert bridge.generation_starts
        wake_fence = bridge.generation_starts[0]
        assert wake_fence.turn_id >= 1
        assert wake_fence.generation_id >= 1
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_energy_connect_vad_after_sample_zero_skips_device_wake_ack() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-energy-later-connect-vad-wake")
    session = bridge.bridge.open(identity)
    original = registry._speak_device_wake_ack

    async def _inject_then_speak(context: Any) -> None:
        await registry.on_speech_segment(
            session,
            _connect_vad_segment(
                identity,
                sample=3200,
                rms=400.0,
                segment_id="energy-later-start",
            ),
        )
        await original(context)

    registry._speak_device_wake_ack = _inject_then_speak  # type: ignore[method-assign]
    try:
        context = await registry._get_or_create(identity)
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
        assert context.turn_start_sample == 3200
        assert context.device_wake_ack_pending is False
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_live_lookup_final_commits_before_vad_end() -> None:
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
    identity = _device_identity("device-early-live-lookup")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="weather-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="weather-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="明天南宁天气怎么样",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.turn_endpoint_sample == 16_000
        assert context.live_query_endpoint_pinned == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_weather_final_recovered_after_straddling_committed_range() -> None:
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
    identity = _device_identity("device-weather-straddle")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        context.asr.mark_committed(64_000)
        context.runtime.commit_media_speech_range(
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=64_000,
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
        from services.agent.src.voice_core.speech_timeline import ASRResult

        weather = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="weather-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=187_520,
            text="今天南京的天气怎么样",
            is_final=True,
            confidence=0.9,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            weather,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
        assert context.live_query_forced_text == "今天南京的天气怎么样"
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=2,
                segment_id="weather-vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=113_280,
                capture_end_sample=113_281,
            ),
        )
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=2,
                segment_id="weather-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=187_520,
                capture_end_sample=187_521,
                final=True,
                voiced_end_sample=173_120,
            ),
        )
        context.turn_start_sample = 113_280
        context.turn_end_sample = 173_120
        context.turn_endpoint_sample = 173_120
        context.turn_retire_sample = 187_520
        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=113_280,
            end_sample=173_120,
            retire_sample=187_520,
        )
        assert fence is not None
        assert reason is None
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天南京的天气怎么样"]
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_weather_final_recovered_after_cross_sentence_overlap() -> None:
    """Playback echo must not starve the first real user turn.

    Regression for the 2026-09-03 field failure: the welcome-message echo was
    transcribed into a final interval, the weather rescue final overlapped it
    and was rejected as cross_sentence_overlap, and the turn starved with no
    commit. The recovered forced text must win commit-time resolution even
    though the echo text on the in-range timeline is longer.
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
    identity = _device_identity("device-weather-overlap")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
        from services.agent.src.voice_core.speech_timeline import ASRResult

        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=62_080,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        echo_decision = await registry._accept_asr_result_decision(
            identity.session_id,
            echo,
        )
        assert echo_decision.accepted is not None
        weather = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="weather-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=148_160,
            text="今天南京的天气怎么样",
            is_final=True,
            confidence=0.9,
            rescue_synthesized=True,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            weather,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.live_query_forced_text == "今天南京的天气怎么样"
        assert context.live_query_forced_authoritative
        # Recovery must arm turn bounds itself; do not depend on a later
        # timeline inject or a test harness manually setting samples.
        assert context.turn_start_sample is not None
        assert context.turn_end_sample == 148_160
        assert context.turn_endpoint_sample == 148_160
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=2,
                segment_id="weather-vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=2,
                segment_id="weather-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=133_760,
                capture_end_sample=133_761,
                final=True,
                voiced_end_sample=133_760,
            ),
        )
        # Authoritative recovery ignores later VAD and keeps the armed endpoint.
        assert context.turn_endpoint_sample == 148_160
        await asyncio.sleep(0.05)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        # The longer echo text must not win; the authoritative forced text does.
        assert user_turns == ["今天南京的天气怎么样"]
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_weather_recovery_commits_despite_post_reject_vad_jitter() -> None:
    """Reject-then-VAD-jitter must not starve an authoritative live-query recovery.

    Regression for 2026-09-03 epoch 1361: weather final was rejected as
    cross_sentence_overlap, recovery set forced text, then post-reject VAD
    jitter kept moving the endpoint past turn_end so grace commit deferred
    forever until tail timeout cleared the forced text. The recovered turn
    must commit through the automatic grace path even when more VAD arrives.
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
        turn_endpoint_grace_s=0,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=2.0,
    )
    registry.install()
    identity = _device_identity("device-weather-vad-jitter")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
        from services.agent.src.voice_core.speech_timeline import ASRResult

        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=62_080,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        echo_decision = await registry._accept_asr_result_decision(
            identity.session_id,
            echo,
        )
        assert echo_decision.accepted is not None

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=2,
                segment_id="weather-vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )

        weather = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="weather-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=85_120,
            text="今天南京的天气怎么样",
            is_final=True,
            confidence=0.9,
            rescue_synthesized=True,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            weather,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.live_query_forced_text == "今天南京的天气怎么样"
        assert context.live_query_forced_authoritative
        assert context.turn_end_sample == 85_120
        assert context.turn_endpoint_sample == 85_120

        # Field order: rejection arrived before vad_end was processed. The
        # late vad_end and later jitter must not clear the forced window.
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=2,
                segment_id="weather-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=85_120,
                capture_end_sample=85_121,
                final=True,
                voiced_end_sample=70_720,
            ),
        )
        assert context.turn_endpoint_sample == 85_120

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=3,
                segment_id="jitter-vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=90_000,
                capture_end_sample=90_001,
            ),
        )
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=3,
                segment_id="jitter-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=130_880,
                capture_end_sample=130_881,
                final=True,
                voiced_end_sample=116_480,
            ),
        )
        assert context.turn_endpoint_sample == 85_120
        assert context.live_query_forced_text == "今天南京的天气怎么样"

        await asyncio.sleep(0.05)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天南京的天气怎么样"]
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_h5_session_does_not_speak_device_wake_ack() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("h5-no-wake")
    try:
        context = await registry._get_or_create(identity)
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
        assert context.device_wake_ack_pending is False
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_owned_delegation_filler_playback_is_not_turn_terminal() -> None:
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("owned-filler-not-terminal")
    context = None
    try:
        context, ack_fence = await _ack_owned_filler_then_wait(
            registry,
            identity,
            provider,
            bridge,
        )
        claim = context.delegation_output_claims[ack_fence]
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=1)
        assert media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT in provider.output_kinds
        deep_owner = context.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
    finally:
        provider.release.set()
        if context is not None:
            await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_half_duplex_owned_wait_ignores_user_speech_and_keeps_weather() -> None:
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
    identity = SessionIdentity("half-duplex-owned-hold")
    context = None
    try:
        context, ack_fence = await _ack_owned_filler_then_wait(
            registry,
            identity,
            provider,
            bridge,
        )
        assert context.runtime.barge_in_enabled is False
        decision = context.runtime.on_user_voice_started()
        assert decision is PlaybackInputDecision.IGNORE
        assert context.runtime.output_floor_allows_assistant
        assert context.runtime.orchestrator.state is ConversationState.TOOL_WAITING
        claim = context.delegation_output_claims[ack_fence]
        assert claim.state is DelegationOutputState.OWNED
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=1)
        assert claim.state is not DelegationOutputState.RELEASED
        deep_owner = context.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
    finally:
        provider.release.set()
        if context is not None:
            await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_half_duplex_media_vad_does_not_preempt_owned_weather_successor() -> None:
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
    identity = SessionIdentity(
        "half-duplex-vad-hold",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )
    context = None
    try:
        context, ack_fence = await _ack_owned_filler_then_wait(
            registry,
            identity,
            provider,
            bridge,
        )
        session = bridge.bridge.get(identity.session_id)
        assert session is not None
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="owned-wait-vad",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=32_000,
                capture_end_sample=32_160,
                final=False,
            ),
        )
        assert context.turn_start_sample is None
        assert context.runtime.orchestrator.state is ConversationState.TOOL_WAITING
        claim = context.delegation_output_claims[ack_fence]
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=1)
        assert claim.state is not DelegationOutputState.RELEASED
        deep_owner = context.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
    finally:
        provider.release.set()
        if context is not None:
            await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_provider_does_not_install_an_unavailable_delegation_seam() -> None:
    class UnsupportedProvider(FakeMediaProvider):
        supports_delegation = False

        async def start_delegation(self, _text: str, _fence: GenerationFence) -> None:
            raise AssertionError("unavailable delegation seam was installed")

        async def accept_output_intent(self, _intent: Any) -> None:
            raise AssertionError("unavailable output intent seam was installed")

    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: UnsupportedProvider(),
    )
    registry.install()
    context = await registry._get_or_create(SessionIdentity("no-delegation-seam"))

    assert context.runtime._delegation_starter is None
    await context.runtime.close()


@pytest.mark.asyncio
async def test_media_registry_replaces_factory_legacy_delegation_starter() -> None:
    legacy_calls: list[tuple[str, GenerationFence]] = []

    class DirectProvider(_DelegationProbeProvider):
        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            return "南京今天多云。"

    identity = SessionIdentity("factory-delegation-override")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    async def legacy_starter(text: str, fence: GenerationFence) -> None:
        legacy_calls.append((text, fence))

    runtime.set_delegation_starter(legacy_starter)
    provider = DirectProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    context = await registry._get_or_create(identity)

    assert context.runtime._delegation_starter is not legacy_starter
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)
    await _wait_until(lambda: provider.output_kinds == [media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT])

    assert legacy_calls == []
    assert provider.delegations == [(query, fence)]
    assert context.delegation_output_claims[fence].state is DelegationOutputState.COMPLETED
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_claim_is_not_created_for_non_realtime_or_unstarted_turns() -> None:
    class NonRealtimeProvider(_DelegationProbeProvider):
        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            return "不应委派。"

    provider = NonRealtimeProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("non-realtime-claim")
    context = await registry._get_or_create(identity)

    # A realtime-shaped chat turn that resolves locally must never create a
    # claim: the starter guard rejects it before any claim is registered.
    fence = await context.runtime.on_turn_committed("现在几点开会")
    assert context.delegation_output_claims == {}
    assert provider.delegations == []
    context.playback.start(fence)
    # The provider audio path does not complete end-to-end in this harness;
    # the claim contract is that exactly one local reply is produced.
    await registry.generate_reply(identity.session_id, "现在几点开会", fence)
    assert provider.reply_calls == 1

    # A control utterance whose interaction decision never starts delegation
    # must likewise leave no claim behind.
    await context.runtime.on_turn_committed("停一下")
    assert context.delegation_output_claims == {}
    assert provider.delegations == []
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_provider_failure_falls_back_to_one_local_reply() -> None:
    class FailingProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            raise RuntimeError("deep provider exploded")

    provider = FailingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-provider-failure")
    context = await registry._get_or_create(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_coordinator_rejection_falls_back_to_one_local_reply() -> None:
    class RejectedProvider(_DelegationProbeProvider):
        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            return "不应执行。"

    provider = RejectedProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-coordinator-rejection")
    context = await registry._get_or_create(identity)
    # Remove the registered tool authority so ``coordinator.delegate`` itself
    # rejects the request before any provider work is started.
    context.runtime.orchestrator.task_manager.specs.pop("media_deep_response", None)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.delegations == []
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_none_result_falls_back_to_one_local_reply() -> None:
    class NoneResultProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> None:
            return None

    provider = NoneResultProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-none-result")
    context = await registry._get_or_create(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_error_result_falls_back_to_one_local_reply() -> None:
    class ErrorResultProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> dict[str, str]:
            return {"error": "search backend failed"}

    provider = ErrorResultProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-error-result")
    context = await registry._get_or_create(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_fast_media_delegation_prefixes_lookup_filler() -> None:
    class FastProvider(_DelegationProbeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            return "南京今天多云。"

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    first=True,
                    final=True,
                )

            return chunks()

    provider = FastProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("fast-delegation-filler-prefix")
    context = await registry._get_or_create(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)

    await _wait_until(
        lambda: provider.output_kinds == [media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT],
    )
    claim = context.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.COMPLETED
    assert media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT not in provider.output_kinds
    assert provider.output_texts == [f"{LIVE_LOOKUP_FILLER}南京今天多云。"]
    assert provider.reply_calls == 0
    assert await registry.generate_reply(identity.session_id, query, fence)
    assert provider.reply_calls == 0
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_live_lookup_ack_starts_lagging_transport_generation_before_pcm() -> None:
    provider = _LateOwnedDelegationProvider()
    bridge = _FenceCheckingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("lagging-commit-start-ack")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await _finish_device_wake_ack_if_any(registry, identity, provider, bridge, session)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.accepted_pcm), timeout=2.0)
        owner = context.output_owner
        assert owner is not None
        ack_frame = bridge.accepted_pcm[-1]
        assert ack_frame.turn_id == owner.fence.turn_id
        assert ack_frame.generation_id == owner.fence.generation_id
        assert not any(
            getattr(frame, "generation_id", None) == owner.fence.generation_id
            for frame in bridge.rejected_pcm
        )
        assert "output_generation_start" in bridge.start_reasons
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_unheard_live_lookup_ack_prefixes_deep_result() -> None:
    class UnheardProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(intent.tts_source))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                self.deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    self.ack_completed.set()

            return chunks()

    class RejectingBridge(MediaBridgeGrpcServer):
        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
            return False

    provider = UnheardProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=RejectingBridge(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("unheard-lookup-ack-prefix")
    try:
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: context.output_owner is None, timeout=2.0)
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT in provider.output_kinds
        assert media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT in provider.output_kinds
        assert provider.output_texts[-1] == f"{BRIDGE_PHRASES[1]}南京今天多云，气温二十二度。"
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_started_live_lookup_ack_is_not_preempted_by_deep_result() -> None:
    class HoldingAckProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.ack_hold = asyncio.Event()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                self.deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=intent.kind != media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    await self.ack_hold.wait()
                    yield MediaReplyChunk(
                        pcm_s16le=b"\x04\x00\x05\x00",
                        source_start_sample=source_start_sample + 2,
                        text="",
                        first=False,
                        final=True,
                    )
                    self.ack_completed.set()

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[object] = []

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

    provider = HoldingAckProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("started-ack-not-preempted")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        context.runtime.orchestrator.delegation.reset_output_intent_state(identity.session_id)
        provider.release.set()
        await asyncio.sleep(0.05)
        assert not provider.deep_started.is_set()
        assert context.output_owner is ack_owner
        provider.ack_hold.set()
        await asyncio.wait_for(provider.ack_completed.wait(), timeout=2)
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert provider.output_kinds == [
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
            media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
        ]
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
    finally:
        provider.ack_hold.set()
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_heard_ack_then_duplicate_turn_commit_does_not_repeat_filler() -> None:
    """epoch 1897: 同一句被重复 final 后，已听到的 filler 被再补一遍。

    The device transcribed 「今天南京天气怎么样」 twice and committed the same
    question again while the live-lookup ACK was already audible.  The ACK was
    heard, yet the deep result still carried the filler prefix, so the user
    heard 「稍等，我查询一下。」 twice before the weather answer.
    """

    class HeardAckProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                self.deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    self.ack_completed.set()

            return chunks()

    provider = HeardAckProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("heard-ack-duplicate-turn")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        # The board re-transcribes the same question and commits it again.
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT in provider.output_kinds
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_duplicate_turn_commit_does_not_emit_a_second_lookup_ack() -> None:
    """epoch 1899: 两次提交各开一次委派，各自播了一遍 filler。

    The board transcribed one weather question twice, so two sibling
    delegations each admitted their own FAST_ACKNOWLEDGEMENT and the user
    heard 「稍等，我查询一下。」 twice before the answer.  The epoch-1897 fix
    only guards the deep-result prefix; the second ACK was never gated.
    """

    class HeardAckProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                self.deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    self.ack_completed.set()

            return chunks()

    provider = HeardAckProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("duplicate-commit-single-lookup-ack")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        # The board re-transcribes the same question and commits it again
        # while the lookup is still running.
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.sleep(0.3)
        assert (
            provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT) == 1
        )
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_later_live_lookup_still_announces_itself() -> None:
    """epoch 1900: 突发记忆窗口过长，会吞掉下一次提问的提示音。

    The acknowledgement gate reused the 30s burst window that exists for
    stripping a result prefix.  A question asked well after the previous burst
    must still announce itself, so the gate needs its own short window.
    """

    class TwoQuestionProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                self.deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    self.ack_completed.set()

            return chunks()

    provider = TwoQuestionProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("later-lookup-announces-itself")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        # The next question arrives long after that burst, so its own
        # acknowledgement must play even though the session remembers the cue.
        context.live_lookup_filler_admitted_at = time.monotonic() - 10.0
        await context.runtime.on_turn_committed("上海明天天气怎么样")
        await _wait_until(
            lambda: provider.output_kinds.count(
                media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
            )
            == 2,
            timeout=2.0,
        )
        assert (
            provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT) == 2
        )
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_duplicate_turn_commit_is_skipped_after_ack_playback_completed() -> None:
    """epoch 1912: ACK 播完后重复 final 从闸门空窗进入并抢占待交付答案。

    The live-lookup acknowledgement finished playing, but the deep answer was
    still running inside an OWNED delegation claim.  ``_reply_in_flight`` only
    saw the (now finished) reply task, so the re-transcribed same question
    opened a fresh turn, superseded the pending answer, and its own cue/answer
    were then dropped as ``output_intent_inactive``.  A duplicate of the
    committed text must be skipped whenever the delegated answer has not yet
    been delivered.
    """

    question = "今天南京天气怎么样"

    class RecordingProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            return super().generate_output(
                _identity,
                intent,
                _fence,
                work_id="",
                source_start_sample=source_start_sample,
            )

    provider = RecordingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("duplicate-after-ack-completed")
    session = bridge.bridge.open(identity)
    try:
        context = await _seed_pending_media_turn(
            registry,
            identity,
            text=question,
            endpoint_sample=600,
            retire_sample=640,
        )
        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=600,
            retire_sample=640,
        )
        assert fence is not None, reason
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        # The cue has already been heard, and the delegated lookup is still
        # running, so no answer has been delivered yet.
        assert not registry._reply_in_flight(context)
        assert any(
            claim.state is DelegationOutputState.OWNED
            for claim in context.delegation_output_claims.values()
        )
        # The board re-transcribes the same question over the contiguous range.
        duplicate_segment = SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=2,
            segment_id="duplicate-final",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=600,
            capture_end_sample=1200,
            text=question,
            final=True,
        )
        assert context.runtime.ingest_media_speech_segment(duplicate_segment)
        await registry._apply_projection_segment(context, duplicate_segment)
        context.turn_start_sample = 600
        context.turn_end_sample = 1200
        context.turn_endpoint_sample = 1200
        context.turn_retire_sample = 1240
        repeat_fence, repeat_reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_fence is None
        assert repeat_reason == "duplicate_media_turn"
        # The single surviving delegation then delivers the answer once.
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert (
            provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT) == 1
        )
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_blocked_duplicate_still_delivers_the_first_answer() -> None:
    """QA item 2(b): a skipped duplicate must not evaporate the live answer.

    The gate now blocks a same-text re-commit while the delegation is OWNED.
    Red line 1 still demands the surviving turn deliver its answer audibly and
    that the filler is not replayed.
    """

    question = "今天南京天气怎么样"

    class RecordingProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            return super().generate_output(
                _identity,
                intent,
                _fence,
                work_id="",
                source_start_sample=source_start_sample,
            )

    provider = RecordingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-blocked-duplicate-delivers")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            question,
        )
        claim = context.delegation_output_claims[fence]
        assert claim.state is DelegationOutputState.OWNED
        assert not registry._reply_in_flight(context)
        frames_before = len(bridge.frames)
        ack_count_before = provider.output_kinds.count(
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
        )
        repeat_fence, repeat_reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text=question,
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_fence is None
        assert repeat_reason == "duplicate_media_turn"
        # Red line 1: the single surviving delegation still emits its answer,
        # under its own generation (not a leftover ACK frame).
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        await _wait_until(
            lambda: context.output_owner is not None
            and context.output_owner.fence.generation_id != fence.generation_id,
            timeout=2.0,
        )
        deep_owner = context.output_owner
        assert deep_owner is not None
        deep_generation = deep_owner.fence.generation_id
        assert deep_generation != fence.generation_id
        await _wait_until(
            lambda: any(frame.generation_id == deep_generation for frame in bridge.frames),
            timeout=2.0,
        )
        assert len(bridge.frames) > frames_before
        assert claim.state is DelegationOutputState.COMPLETED
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
        # Red line 3: the skipped duplicate did not replay the filler.
        assert (
            provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT)
            == ack_count_before
        )
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("result_before_tail", [True, False])
async def test_qa_weather_result_survives_empty_vad_tail(result_before_tail: bool) -> None:
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60,
        turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-weather-empty-vad-{result_before_tail}")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样"
        )
        claim = context.delegation_output_claims[fence]
        endpoint = await _qa_open_empty_vad_tail(registry, context, session, identity)
        assert context.runtime.fence.matches(fence)
        assert await registry.generate_reply(identity.session_id, "今天南京天气怎么样", fence)
        assert claim.normal_reply_observed
        speaker_class = context.runtime._speaker_class
        frame_count = len(bridge.frames)
        if result_before_tail:
            provider.release.set()
            await _wait_until(lambda: claim.state is not DelegationOutputState.OWNED)
            assert claim.state is DelegationOutputState.COMPLETED
            assert any(
                work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
                for work in context.output_work.values()
            )
            assert not provider.deep_started.is_set()
            assert len(bridge.frames) == frame_count
            assert context.output_retry_task is None
        await registry._expire_endpoint_tail(
            identity.session_id, identity.stream_epoch, endpoint
        )
        assert context.runtime.output_floor_allows_assistant
        assert context.runtime._speaker_class == speaker_class
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        await _wait_until(lambda: len(bridge.frames) > frame_count)
        deep_fence = context.output_owner.fence
        await _finish_output_owner_playback(registry, identity, bridge, session)
        delivery = context.reply_delivery.get(deep_fence)
        assert delivery.actual_heard and delivery.playback_ended
        await _wait_until(lambda: context.output_dispatch_task is None)
        assert not context.output_work
        assert context.output_retry_task is None
        assert provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT) == 1
        assert provider.output_kinds.count(
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
        ) == 1
        assert context.turn_start_sample is None
        assert context.runtime.fence.turn_id == fence.turn_id
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("text_evidence", [False, True], ids=["room-noise", "owner-partial"])
async def test_qa_evidence_less_vad_cannot_hold_weather_result_past_cap(
    monkeypatch: pytest.MonkeyPatch,
    text_evidence: bool,
) -> None:
    """Room-noise VAD bursts must not starve a ready tool answer.

    Run 2026-09-24 session b18fede9: the weather answer was ready 2 s after
    the filler, but three noise VAD bursts (FunASR and rescue both empty)
    kept one empty pending turn open for ~8 s; each vad.start reset the
    2.5 s tail.  The cap retires such an evidence-less turn and resumes the
    answer, while any text evidence leaves the user's turn in charge.
    """

    from services.agent.src.voice_core import media_session_turns

    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_BASE_S", 0.3)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S", 0.2)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_MAX_S", 0.6)
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        # Only the cap may release the floor inside this test's window.
        turn_endpoint_grace_s=60,
        turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-evidence-less-hold-{text_evidence}")
    session = bridge.bridge.open(identity)

    async def vad(name: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id=f"noise-{name}",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样"
        )
        claim = context.delegation_output_claims[fence]
        registry._clear_pending_turn_state(context)
        await vad("start-1", 640, final=False)
        assert not context.runtime.output_floor_allows_assistant
        if text_evidence:
            context.pending_partial = ASRResult(
                stream_epoch=identity.stream_epoch,
                task_epoch=1,
                sentence_id="owner-partial",
                revision=1,
                capture_start_sample=640,
                capture_end_sample=1_600,
                text="我还想问",
                is_final=False,
            )
        assert await registry.generate_reply(identity.session_id, "今天南京天气怎么样", fence)
        frame_count = len(bridge.frames)
        provider.release.set()
        await _wait_until(lambda: claim.state is not DelegationOutputState.OWNED)
        assert context.evidence_less_hold_since is not None
        # Noise keeps re-opening the same empty turn faster than any tail.
        for index, sample in enumerate((1_280, 1_920, 2_560, 3_200, 3_840), start=1):
            await asyncio.sleep(0.1)
            await vad(f"end-{index}", sample, final=True)
            await asyncio.sleep(0.05)
            await vad(f"start-{index + 1}", sample + 160, final=False)

        if text_evidence:
            await asyncio.sleep(0.5)
            assert not context.runtime.output_floor_allows_assistant
            assert context.turn_start_sample is not None
            assert not provider.deep_started.is_set()
            assert len(bridge.frames) == frame_count
            assert context.evidence_less_hold_since is None
        else:
            await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
            await _wait_until(lambda: len(bridge.frames) > frame_count)
            assert context.evidence_less_hold_since is None
            assert provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT) == 1
            assert context.runtime.fence.turn_id == fence.turn_id
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidator",
    [
        "new_vad", "stop", "new_turn", "identity", "close", "standby",
        "expiry", "context", "partial", "stream", "enrollment",
    ],
)
async def test_qa_empty_tail_does_not_revive_invalid_weather_output(invalidator: str) -> None:
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60, turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-weather-no-revive-{invalidator}")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样"
        )
        endpoint = await _qa_open_empty_vad_tail(registry, context, session, identity)
        provider.release.set()
        claim = context.delegation_output_claims[fence]
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
        coordinator = context.runtime.orchestrator.delegation
        if invalidator == "new_vad":
            await registry.on_speech_segment(
                session,
                SpeechSegment(
                    session_id=identity.session_id, stream_epoch=identity.stream_epoch,
                    provider_task_epoch=0, segment_id="newer-vad", revision=1,
                    kind=SegmentKind.VAD, capture_start_sample=1280, capture_end_sample=1281,
                ),
            )
            assert context.turn_endpoint_sample is None
        elif invalidator == "stop":
            await context.runtime.accept_media_generation(fence.bump_generation(), cause="stop")
        elif invalidator == "new_turn":
            await context.runtime.on_turn_committed("给我讲个故事")
        elif invalidator == "identity":
            context.runtime.orchestrator.bump_session_epoch(fence.session_epoch + 1)
        elif invalidator == "close":
            await registry._finalize_session(identity.session_id)
        elif invalidator == "standby":
            context.standby_requested = True
        elif invalidator == "stream":
            context.stream_epoch += 1
        elif invalidator == "enrollment":
            context.runtime.begin_formal_speaker_enrollment()
        elif invalidator == "expiry":
            for intent in coordinator._shadow_output_by_session[identity.session_id].values():
                intent.expires_at_ms = int(time.time() * 1_000) - 1
        elif invalidator == "context":
            coordinator.activate_context_version(
                identity.session_id, coordinator.current_context_version(identity.session_id) + 1
            )
        elif invalidator == "partial":
            context.pending_partial = ASRResult(
                task_epoch=1, sentence_id="real-speech", revision=1,
                capture_start_sample=640, capture_end_sample=650, text="等等",
                is_final=False, stream_epoch=identity.stream_epoch,
            )
        await registry._expire_endpoint_tail(identity.session_id, identity.stream_epoch, endpoint)
        assert not await registry._start_selected_output(context)
        assert not provider.deep_started.is_set()
        assert len(bridge.frames) == 1
        assert context.output_retry_task is None
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_lookup", [True, False])
async def test_qa_conversation_or_failed_lookup_queues_during_empty_vad(
    failed_lookup: bool,
) -> None:
    class FailingProvider(_LateOwnedDelegationProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            await self.release.wait()
            raise RuntimeError("lookup failed")

    provider = FailingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60, turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-empty-vad-conversation-{failed_lookup}")
    session = bridge.bridge.open(identity)
    try:
        if failed_lookup:
            context, fence = await _qa_commit_question_then_finish_ack_playback(
                registry, identity, bridge, provider, session, "今天南京天气怎么样"
            )
        else:
            context = await registry._get_or_create(identity)
            fence = await context.runtime.on_turn_committed("给我讲个故事")
            context.playback.start(fence)
        endpoint = await _qa_open_empty_vad_tail(registry, context, session, identity)
        frame_count = len(bridge.frames)
        assert await registry.generate_reply(identity.session_id, "给我讲个故事", fence)
        if failed_lookup:
            provider.release.set()
        await _wait_until(lambda: any(
            work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY
            for work in context.output_work.values()
        ))
        assert len(bridge.frames) == frame_count
        assert context.output_retry_task is None
        await registry._expire_endpoint_tail(identity.session_id, identity.stream_epoch, endpoint)
        await _wait_until(lambda: len(bridge.frames) > frame_count)
        assert len(bridge.frames) == frame_count + 1
        await _finish_output_owner_playback(registry, identity, bridge, session)
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("preempt_owner", [False, True])
async def test_queued_ack_and_deep_result_survive_internal_output_handoffs(
    preempt_owner: bool,
) -> None:
    class Bridge(_CapturingGenerationBridge):
        async def emit_realtime_effect(self, *_args: object, **_kwargs: object) -> bool:
            return True

    provider = FakeMediaProvider()
    bridge = Bridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _: provider)
    registry.install()
    identity = SessionIdentity(f"queued-ack-deep-handoff-{preempt_owner}")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    try:
        fence = await context.runtime.on_turn_committed("你好")
        context.playback.start(fence)
        assert await registry.generate_reply(identity.session_id, "你好", fence)
        assert len(bridge.frames) == 1
        if not preempt_owner:
            await _finish_output_owner_playback(registry, identity, bridge, session)
            assert context.output_owner is None
        coordinator = context.runtime.orchestrator.delegation
        context.runtime.on_user_voice_started()
        now = int(time.time() * 1_000)
        works = []
        for name, kind, pcm in (
            ("answer", media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT, b"\x04\x00\x05\x00"),
            ("ack", media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT, b"\x06\x00\x07\x00"),
        ):
            intent = media_pb2.OutputIntent(
                intent_id=name, session_id=identity.session_id, turn_id=fence.turn_id,
                generation_id=fence.generation_id, tool_epoch=fence.tool_epoch,
                kind=kind, priority=100, created_at_ms=now, expires_at_ms=now + 20_000,
                context_version=coordinator.current_context_version(identity.session_id),
                floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
                pcm_s16le=pcm,
            )
            coordinator.admit_output_intent(
                intent, current_fence=fence,
                current_context_version=coordinator.current_context_version(identity.session_id),
                floor_allows_output=False, now_ms=now,
            )
            work = _OutputWork(intent, fence)
            works.append(work)
            assert await registry._enqueue_output_work(context, work)
        assert len(bridge.frames) == 1
        context.runtime.open_assistant_floor(cause="empty_input_retired")
        if preempt_owner:
            assert await registry._enqueue_output_work(context, works[-1])
        else:
            registry._schedule_output_retry(context)
        await _wait_until(lambda: len(bridge.frames) == 2)
        assert len(context.output_work) == 2
        assert all(work.fence.matches(context.runtime.fence) for work in context.output_work.values())
        assert not {"answer", "ack"}.intersection(context.output_work)
        assert context.output_owner.intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
        await _finish_output_owner_playback(registry, identity, bridge, session)
        await _wait_until(lambda: len(bridge.frames) == 3)
        assert context.output_owner.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
        deep_fence = context.output_owner.fence
        await _finish_output_owner_playback(registry, identity, bridge, session)
        assert context.reply_delivery.get(deep_fence).playback_ended
        assert not context.output_work
        assert len(bridge.frames) == 3
        assert context.runtime.fence.turn_id == fence.turn_id
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_same_question_after_delivered_answer_is_not_blocked() -> None:
    """QA item 2(a): once the answer is delivered, re-asking must be answered.

    The COMPLETED claim has resolved and playback is over, so the widened gate
    must not treat the finished delegation as still in flight.
    """

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-same-question-after-delivered")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            question,
        )
        claim = context.delegation_output_claims[fence]
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED, timeout=2.0)
        deep_owner = context.output_owner
        assert deep_owner is not None
        deep_fence = deep_owner.fence
        await _wait_until(lambda: len(bridge.frames) >= 2, timeout=2.0)
        deep_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=deep_fence.generation_id,
                received_sequence=deep_frame.sequence,
                rendered_sample_end=(deep_frame.source_start_sample + deep_frame.frame_samples),
                client_monotonic_ms=2,
                turn_id=deep_fence.turn_id,
                tool_epoch=deep_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        await _wait_until(lambda: context.output_owner is None, timeout=2.0)
        assert not registry._reply_or_delegation_pending(context)
        repeat_fence, repeat_reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text=question,
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_reason != "duplicate_media_turn"
        assert repeat_fence is not None
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_different_question_in_delegation_window_is_not_blocked() -> None:
    """QA item 2(d): the gate is text-scoped; a new question must go through."""

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-different-question")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            question,
        )
        assert context.delegation_output_claims[fence].state is DelegationOutputState.OWNED
        repeat_fence, repeat_reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text="北京明天天气怎么样",
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_reason != "duplicate_media_turn"
        assert repeat_fence is not None
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_different_question_delivers_after_superseding_old_delegation() -> None:
    """A real topic switch must cancel the old lookup and render the new one."""

    class TopicSwitchProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.delegation_started: dict[str, asyncio.Event] = {}
            self.release_by_text: dict[str, asyncio.Event] = {}
            self.output_texts: list[str] = []

        async def start_delegation(self, text: str, _fence: GenerationFence) -> str:
            started = self.delegation_started.setdefault(text, asyncio.Event())
            release = self.release_by_text.setdefault(text, asyncio.Event())
            started.set()
            await release.wait()
            return f"答案：{text}。"

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            return super().generate_output(
                _identity,
                intent,
                _fence,
                work_id=work_id,
                source_start_sample=source_start_sample,
            )

    first_question = "今天南京天气怎么样"
    second_question = "北京明天天气怎么样"
    provider = TopicSwitchProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-topic-switch-delivers-new")
    session = bridge.bridge.open(identity)
    try:
        context, first_fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            first_question,
        )
        await asyncio.wait_for(
            provider.delegation_started[first_question].wait(),
            timeout=2,
        )
        first_claim = context.delegation_output_claims[first_fence]
        assert first_claim.state is DelegationOutputState.OWNED

        second_fence, reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text=second_question,
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert second_fence is not None, reason
        assert second_fence.turn_id > first_fence.turn_id
        await _wait_until(
            lambda: second_fence in context.delegation_output_claims,
            timeout=2,
        )
        await _wait_until(
            lambda: second_question in provider.delegation_started,
            timeout=2,
        )
        assert set(provider.delegation_started) == {first_question, second_question}, provider.delegation_started

        provider.release_by_text[second_question].set()
        await _wait_until(
            lambda: any(
                text.startswith("答案：北京明天天气怎么样")
                for text in provider.output_texts
            ),
            timeout=2,
        )
        assert any(
            text.startswith("答案：北京明天天气怎么样")
            for text in provider.output_texts
        )
        assert not any(
            text.startswith("答案：今天南京天气怎么样")
            for text in provider.output_texts
        )
        assert first_claim.state is DelegationOutputState.RELEASED
        assert not any(
            result.reason == "output_intent_inactive"
            for result in context.output_results
        )
        await _wait_until(
            lambda: not context.runtime.orchestrator.task_manager.tasks,
            timeout=2,
        )
    finally:
        provider.release_by_text.setdefault(first_question, asyncio.Event()).set()
        provider.release_by_text.setdefault(second_question, asyncio.Event()).set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_released_delegation_local_fallback_emits_audio() -> None:
    """QA item 2(c): the RELEASED local fallback is not a dead end."""

    class FailingProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            raise RuntimeError("deep provider exploded")

    provider = FailingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-released-fallback-audio")
    bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        query = "今天南京天气怎么样"
        fence = await context.runtime.on_turn_committed(query)
        context.playback.start(fence)
        assert await registry.generate_reply(identity.session_id, query, fence)
        await _wait_until(lambda: provider.reply_calls == 1, timeout=2.0)
        claim = context.delegation_output_claims[fence]
        assert claim.state is DelegationOutputState.RELEASED
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        assert bridge.frames, "local fallback emitted no audio frame"
        assert context.output_owner is not None
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_slow_lookup_uses_a_single_cue() -> None:
    """慢查询只播一句稍长提示，不再追加同质第二句稍等。

    epoch 1900 用 THINKING_FILLER 盖静音；真机反馈两句同质稍等体验差，
    改为更长的 LIVE_LOOKUP_FILLER 单句，并关掉第二句 cue。
    """

    class CueProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id
            self.output_kinds.append(int(intent.kind))
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_started.set()
            elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
                self.deep_started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                    self.ack_completed.set()

            return chunks()

    provider = CueProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("slow-lookup-single-cue")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        # Slow lookup remains pending; the old second cue fired after ~2.5s.
        await asyncio.sleep(0.6)
        assert provider.output_texts == [LIVE_LOOKUP_FILLER]
        assert BRIDGE_PHRASES[4] not in provider.output_texts
        assert provider.output_kinds == [
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
        ]
    finally:
        provider.release.set()
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_stale_generation_media_delegation_produces_no_output() -> None:
    started = asyncio.Event()
    deep_gate = asyncio.Event()

    class StaleProvider(_DelegationProbeProvider):
        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            started.set()
            await deep_gate.wait()
            return "南京今天多云。"

    provider = StaleProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("stale-delegation")
    context = await registry._get_or_create(identity)
    first_fence = await context.runtime.on_turn_committed("今天南京天气怎么样")
    claim = context.delegation_output_claims[first_fence]

    # Wait until the deep provider is actually working so the supersede
    # happens mid-flight rather than before the delegation started.
    await asyncio.wait_for(started.wait(), timeout=1)

    # Supersede the generation while the provider is still working; the old
    # delegation must never admit an ACK, a deep result or a local reply.
    await context.runtime.on_turn_committed("你好")
    await asyncio.sleep(0.05)
    assert provider.output_kinds == []
    assert provider.reply_calls == 0

    deep_gate.set()
    await _wait_until(lambda: claim.state is DelegationOutputState.RELEASED)
    await asyncio.sleep(0.02)
    assert provider.output_kinds == []
    assert provider.reply_calls == 0
    assert provider.delegations == [("今天南京天气怎么样", first_fence)]
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_concurrent_media_normal_replies_produce_only_one_local_output_after_delegation_failure() -> (
    None
):
    class FailingProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            raise RuntimeError("deep provider exploded")

    provider = FailingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-duplicate-guard")
    context = await registry._get_or_create(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.playback.start(fence)

    first = asyncio.create_task(registry.generate_reply(identity.session_id, query, fence))
    second = asyncio.create_task(registry.generate_reply(identity.session_id, query, fence))
    assert await asyncio.wait_for(first, timeout=1)
    assert await asyncio.wait_for(second, timeout=1)
    await _wait_until(lambda: provider.reply_calls == 1)

    await asyncio.sleep(0.02)
    claim = context.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_playback_ack_without_text_spans_still_completes_speaking() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("no-span-ack-session")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好。")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.playback.start(fence)
    # Audio is delivered and fully rendered, but the provider never supplied a
    # timed text span: the ledger has a received watermark without any span.
    assert context.playback.register_audio(fence, 0, 0, 2)
    context.provider_complete = True
    completed: list[tuple[GenerationFence, str]] = []
    original = context.runtime.on_media_playback_done

    async def spy(
        done_fence: GenerationFence,
        heard_text: str,
        *,
        tools_active: bool = False,
    ) -> bool:
        completed.append((done_fence, heard_text))
        return await original(done_fence, heard_text, tools_active=tools_active)

    context.runtime.on_media_playback_done = spy  # type: ignore[method-assign]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=2,
            client_monotonic_ms=1,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
        ),
    )

    # The final ACK advances only the audio watermark and yields no new text
    # span; the registry must still finalize playback instead of staying
    # SPEAKING forever.
    assert completed == [(fence, "")]
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
