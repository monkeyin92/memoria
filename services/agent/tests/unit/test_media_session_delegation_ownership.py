"""Media session: delegation output ownership, provider seams and typed acks."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.delegation_coordinator import OutputIntentAdmission
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
)
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.voice_core import media_session_projection
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
    MediaVoiceCoreRegistry,
    _OutputWork,
)
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
)


@pytest.fixture(autouse=True)
def short_lookup_filler_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fakes model a slow lookup; keep the filler decision near-immediate."""
    monkeypatch.setattr(media_session_projection, "_LIVE_LOOKUP_FILLER_GRACE_S", 0.02)


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
    current = await registry.open_session(identity)
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
        registry.open_session(SessionIdentity(identity.session_id, stream_epoch=2))
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
    context = await registry.open_session(identity)
    context.runtime.orchestrator.delegation.set_output_intent_observer(
        bridge.output_admissions.append
    )
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)
    await asyncio.sleep(0)
    assert bridge.frames
    assert any(
        event_type == "assistant_state" and payload.get("state") == "speaking"
        for event_type, payload in bridge.runtime_events
    )
    assert context.output.output_owner is not None
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

    assert context.output.output_owner is None
    assert bridge.output_admissions[-1].consumed is True
    assert bridge.output_admissions[-1].reason == "playback_completed"
    assert bridge.output_admissions[-1].authoritative_candidates == ()
    assert metrics.get(
        "voice_conversation_participation_proxy_ms_total", {"kind": "assistant"}
    ) == pytest.approx(2 * 1_000 / 24_000)
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
    assert delivery.actual_heard is True
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)
    await asyncio.sleep(0)
    assert context.output.output_owner is not None
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
    assert str(queued_intent.intent_id) in context.output.output_work
    assert context.output.output_owner is not None

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
    assert context.output.output_owner is not None
    second_fence = context.output.output_owner.fence
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
    assert context.output.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    await registry.finalize_session(identity.session_id)


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

    ReplyPipeline(
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
    await registry.open_session(identity)
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
    context = await registry.open_session(identity)

    fence = await context.runtime.on_turn_committed("今天南京天气怎么样")
    for _ in range(5):
        await asyncio.sleep(0)

    assert provider.prewarm_calls == 1
    assert provider.delegations == [("今天南京天气怎么样", fence)]
    assert len(provider.output_intents) == 1
    assert str(provider.output_intents[0].tts_source) == "委派完成。"
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
    context = await registry.open_session(SessionIdentity("media-safety-realtime"))

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
    context = await registry.open_session(identity)
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
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_waits_for_a_semantic_verdict_before_a_conversation_reply() -> None:
    """A no-keyword live query must not start its fallback conversation reply while the verdict runs."""

    class DelegatingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.delegations: list[tuple[str, GenerationFence]] = []
            self.conversation_reply_calls = 0

        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            return "明天从南京到上海可以坐高铁。"

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
    registry = MediaVoiceCoreRegistry(
        bridge=CapturingBridge(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-semantic-live")
    context = await registry.open_session(identity)
    query = "明天从南京去上海，哪种交通方式最快"
    started = asyncio.Event()
    release = asyncio.Event()

    async def live(_text: str) -> bool:
        started.set()
        await release.wait()
        return True

    context.runtime.set_live_lookup_semantic_resolver(live)
    context.runtime.live_lookup_needed(query, start_verdict=True)
    await asyncio.wait_for(started.wait(), timeout=1)
    fence = await context.runtime.on_turn_committed(query)
    try:
        dispatch = asyncio.create_task(registry.generate_reply(identity.session_id, query, fence))
        await asyncio.sleep(0.05)

        assert not dispatch.done(), "dispatch waits for the semantic verdict"
        assert provider.conversation_reply_calls == 0

        release.set()
        assert await asyncio.wait_for(dispatch, timeout=2.0)
        assert provider.delegations == [(query, fence)]
        assert provider.conversation_reply_calls == 0
    finally:
        release.set()
        await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    await context.runtime.on_turn_committed("今天南京天气怎么样")
    try:
        await asyncio.wait_for(ack_started.wait(), timeout=1)
        ack_owner = context.output.output_owner
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
                for work in context.output.output_work.values()
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
        deep_owner = context.output.output_owner
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
        assert context.output.output_owner is None
    finally:
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        await registry.finalize_session(identity.session_id)
