from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.context_snapshot_manager import (
    MemoryCapsule,
    PersonaCapsule,
)
from services.agent.src.orchestration.delegation_coordinator import OutputIntentAdmission
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.prompts import BRIDGE_PHRASES
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    FunASRServerEvent,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaTextSpan,
    MediaVoiceCoreRegistry,
    MediaVoiceProvider,
    _OutputWork,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
    asr_result_to_segment,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


class FakeMediaProvider(MediaVoiceProvider):
    def __init__(self) -> None:
        self.audio_calls: list[int] = []
        self.cancelled: list[GenerationFence] = []
        self.closed = False

    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        if frame.sequence != 0:
            return ()
        return (
            ASRResult(
                task_epoch=1,
                sentence_id="fake-sentence",
                revision=1,
                capture_start_sample=0,
                capture_end_sample=frame.frame_samples,
                text="你好",
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )

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
                text="你好。",
                first=True,
                final=True,
            )

        return chunks()

    def cancel_generation(self, fence: GenerationFence) -> bool:
        self.cancelled.append(fence)
        return True

    async def close(self, _identity: SessionIdentity) -> None:
        self.closed = True


class MultiFinalProvider(FakeMediaProvider):
    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        return (
            ASRResult(
                task_epoch=1,
                sentence_id=f"sentence-{frame.sequence}",
                revision=1,
                capture_start_sample=frame.capture_start_sample,
                capture_end_sample=frame.capture_end_sample,
                text=("第一句", "第二句")[frame.sequence],
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )


class TimedInterruptProvider(FakeMediaProvider):
    async def interrupted_timed_text_spans(
        self,
        _fence: GenerationFence,
    ) -> tuple[MediaTextSpan, ...]:
        return (MediaTextSpan("你好。", 0, 2),)


async def _requests(queue: asyncio.Queue[media_pb2.MediaToCore | None]):
    while True:
        message = await queue.get()
        if message is None:
            return
        yield message


_EVENT_BACKLOG: dict[int, list[Any]] = {}


async def _next_event(call, kind: str):
    backlog = _EVENT_BACKLOG.setdefault(id(call), [])
    for _ in range(64):
        for index, queued in enumerate(backlog):
            if queued.WhichOneof("event") == kind:
                return backlog.pop(index)
        event = await asyncio.wait_for(call.read(), timeout=1)
        if event is grpc.aio.EOF:
            raise AssertionError(f"bridge ended before {kind}")
        if event.WhichOneof("event") == kind:
            return event
        backlog.append(event)
    raise AssertionError(f"bridge did not emit {kind}")


@pytest.mark.asyncio
async def test_media_registry_builds_one_shared_async_session_resource() -> None:
    identity = SessionIdentity("shared-media-session")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    speaker_pcm: list[bytes] = []
    runtime.feed_speaker_pcm = speaker_pcm.append  # type: ignore[method-assign]
    provider = FakeMediaProvider()
    built: list[SessionIdentity] = []

    async def build_session(requested: SessionIdentity) -> MediaSessionResources:
        built.append(requested)
        return MediaSessionResources(runtime=runtime, provider=provider)

    def unexpected_provider(_identity: SessionIdentity) -> MediaVoiceProvider:
        raise AssertionError("independent provider factory must not run")

    def unexpected_runtime(_session_id: str) -> DuplexRuntime:
        raise AssertionError("independent runtime factory must not run")

    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=unexpected_provider,
        runtime_factory=unexpected_runtime,
        session_factory=build_session,
    )
    registry.install()
    session = bridge.bridge.open(identity)

    await registry.on_audio_frame(
        session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )

    assert built == [identity]
    assert registry.context(identity.session_id) is runtime
    assert provider.audio_calls == [0]
    assert speaker_pcm == [b"\x00\x00\x01\x00"]
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_media_registry_closes_unpublished_factory_resources_on_wiring_failure() -> None:
    identity = SessionIdentity("factory-wiring-failure")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    runtime_closed = asyncio.Event()
    original_runtime_close = runtime.close

    async def close_runtime() -> None:
        runtime_closed.set()
        await original_runtime_close()

    runtime.close = close_runtime  # type: ignore[method-assign]

    class PrewarmProvider(FakeMediaProvider):
        def prewarm(self) -> None:
            return None

    provider = PrewarmProvider()

    def fail_fast_model_wiring(_warmer: Any) -> None:
        raise RuntimeError("prewarm wiring failed")

    runtime.set_fast_model_warmer = fail_fast_model_wiring  # type: ignore[method-assign]

    async def build_session(_identity: SessionIdentity) -> MediaSessionResources:
        return MediaSessionResources(runtime=runtime, provider=provider)

    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=build_session,
    )

    with pytest.raises(RuntimeError, match="prewarm wiring failed"):
        await registry._get_or_create(identity)

    assert runtime_closed.is_set()
    assert provider.closed is True
    assert registry.context(identity.session_id) is None


@pytest.mark.asyncio
async def test_media_asr_dedup_retains_the_most_recent_128_keys() -> None:
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    identity = SessionIdentity("ordered-asr-dedup")
    context = await registry._get_or_create(identity)

    for index in range(130):
        registry._observe_final_asr_result(
            context,
            ASRResult(
                task_epoch=1,
                sentence_id=f"sentence-{index}",
                revision=1,
                capture_start_sample=index * 2,
                capture_end_sample=index * 2 + 2,
                text=str(index),
                is_final=True,
                stream_epoch=1,
            ),
        )

    assert len(context.committed_asr_keys) == 128
    assert next(iter(context.committed_asr_keys))[1] == "sentence-2"
    assert next(reversed(context.committed_asr_keys))[1] == "sentence-129"
    await context.runtime.close()
    await context.provider.close(identity)


@pytest.mark.asyncio
async def test_old_epoch_provider_callback_cannot_update_speaker_or_shadow() -> None:
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

    class ShadowCapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.shadow_segments: list[SpeechSegment] = []

        async def emit_speech_segment_decision(
            self,
            _session_id: str,
            segment: SpeechSegment,
            **_kwargs: Any,
        ) -> bool:
            self.shadow_segments.append(segment)
            return True

    identity = SessionIdentity("old-epoch-callback", stream_epoch=1)
    provider = BlockingProvider()
    bridge = ShadowCapturingBridge()
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
    await registry._get_or_create(SessionIdentity(identity.session_id, stream_epoch=2))
    release.set()
    await asyncio.wait_for(callback, timeout=1)

    assert speaker_pcm == []
    assert bridge.shadow_segments == []
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_prepare_failure_leaves_media_turn_retryable() -> None:
    identity = SessionIdentity("prepare-failure-leaves-turn-retryable")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class FailOncePreparingProvider(FakeMediaProvider):
        prepare_calls = 0

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            if self.prepare_calls == 1:
                raise RuntimeError("turn preparation failed")
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = FailOncePreparingProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    context = await registry._get_or_create(identity)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="prepare-failure",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="准备失败也要保留",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=320,
    )

    assert fence is None
    assert reason == "provider_prepare_failed"
    assert context.projection.provisional is not None
    assert context.projection.provisional.text == "准备失败也要保留"
    assert context.runtime.speech_timeline.committed_sample == 0
    assert context.asr.last_committed_sample == 0

    retried_fence, retry_reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=320,
    )

    assert retried_fence is not None
    assert retry_reason is None
    assert provider.prepare_calls == 2
    assert context.projection.provisional is None
    assert context.runtime.speech_timeline.committed_sample == 320
    assert context.asr.last_committed_sample == 320
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_prepare_failure_after_runtime_commit_publishes_the_committed_projection() -> None:
    identity = SessionIdentity("prepare-failure-after-runtime-commit")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class PartiallyFailingProvider(FakeMediaProvider):
        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            await runtime.on_turn_committed(text, input_modality="audio")
            raise RuntimeError("post-commit preparation failed")

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.committed: list[dict[str, object]] = []

        async def emit_event(
            _self,
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            if event_type == "turn.committed":
                _self.committed.append(payload)
            return True

    provider = PartiallyFailingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    context = await registry._get_or_create(identity)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="post-commit-prepare-failure",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="提交后失败仍需可见",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=320,
    )

    assert fence is None
    assert reason == "provider_prepare_failed"
    assert context.projection.provisional is None
    assert [payload["text"] for payload in bridge.committed] == ["提交后失败仍需可见"]
    assert [turn.content for turn in runtime.orchestrator.context.turns if turn.role == "user"] == [
        "提交后失败仍需可见"
    ]
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_reconnect_during_prepare_cannot_publish_old_epoch_commit() -> None:
    identity = SessionIdentity("prepare-reconnect-fence", stream_epoch=1)
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    class BlockingPreparingProvider(FakeMediaProvider):
        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            prepare_started.set()
            await release_prepare.wait()
            return await runtime.on_turn_committed(text, input_modality="audio")

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            _payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.events.append(event_type)
            return True

    provider = BlockingPreparingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="prepare-reconnect",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="重连期间不能提交旧话轮",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)

    committing = asyncio.create_task(
        registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=320,
        )
    )
    await asyncio.wait_for(prepare_started.wait(), timeout=1)
    reconnected = SessionIdentity(identity.session_id, stream_epoch=2)
    assert session.reconnect(reconnected)
    release_prepare.set()
    fence, reason = await asyncio.wait_for(committing, timeout=1)

    assert fence is None
    assert reason == "stale_stream_epoch"
    assert "turn.committed" not in bridge.events
    assert context.projection.provisional is not None
    await registry._get_or_create(reconnected)
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
    await registry._get_or_create(existing_identity)

    slow_task = asyncio.create_task(registry._get_or_create(SessionIdentity("slow-new-session")))
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
async def test_different_new_sessions_build_in_parallel_with_per_session_singleflight() -> None:
    bridge = MediaBridgeGrpcServer()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    builds: list[str] = []
    resources: list[MediaSessionResources] = []

    async def build_session(identity: SessionIdentity) -> MediaSessionResources:
        builds.append(identity.session_id)
        if identity.session_id == "first-new-session":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        resource = MediaSessionResources(
            DuplexRuntime.create(session_id=identity.session_id),
            FakeMediaProvider(),
        )
        resources.append(resource)
        return resource

    registry = MediaVoiceCoreRegistry(bridge=bridge, session_factory=build_session)
    first = asyncio.create_task(registry._get_or_create(SessionIdentity("first-new-session")))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    duplicate = asyncio.create_task(registry._get_or_create(SessionIdentity("first-new-session")))
    second = asyncio.create_task(registry._get_or_create(SessionIdentity("second-new-session")))

    await asyncio.wait_for(second_started.wait(), timeout=0.1)
    release_first.set()
    first_context, duplicate_context, _ = await asyncio.gather(first, duplicate, second)

    assert first_context is duplicate_context
    assert builds.count("first-new-session") == 1
    assert builds.count("second-new-session") == 1
    for resource in resources:
        await resource.runtime.close()
        await resource.provider.close(SessionIdentity(resource.runtime.session_id))


@pytest.mark.asyncio
async def test_audio_ingress_pump_drops_stale_backlog_and_marks_discontinuity() -> None:
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
        if provider.audio_calls == [0, 3]:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0, 3]
    assert provider.reset_samples == [6]
    assert registry.metrics.get("media_pcm_overflow_total") >= 1
    assert registry.metrics.get("media_discontinuity_total") >= 1
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_loss_concealed_audio_marks_timeline_and_lowers_asr_confidence() -> None:
    class LossProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            return (
                ASRResult(
                    task_epoch=1,
                    sentence_id="lossy-sentence",
                    revision=1,
                    capture_start_sample=frame.capture_start_sample,
                    capture_end_sample=frame.capture_start_sample + frame.frame_samples,
                    text="你好",
                    is_final=True,
                    confidence=0.8,
                    stream_epoch=frame.identity.stream_epoch,
                ),
            )

    bridge = MediaBridgeGrpcServer()
    provider = LossProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("loss-concealed-timeline")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)

    await registry.on_audio_frame(
        session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
            loss_concealed=True,
        ),
    )
    for _ in range(20):
        if context.runtime.speech_timeline.pending:
            break
        await asyncio.sleep(0)

    pending = context.runtime.speech_timeline.pending
    assert len(pending) == 1
    assert pending[0].loss_concealed is True
    assert pending[0].confidence == pytest.approx(0.6)
    assert registry.metrics.get("media_loss_concealed_frames_total") >= 1
    await context.runtime.close()


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
async def test_reconnect_preserves_current_output_intent_owner() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    identity = SessionIdentity("resume-output-owner", stream_epoch=1)
    context = await registry._get_or_create(identity)
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
    context.output_owner = lease  # type: ignore[assignment]

    await registry._reuse_session(
        context,
        SessionIdentity(identity.session_id, stream_epoch=2),
    )

    assert context.output_owner is lease
    assert coordinator.output_intent_is_selected(
        intent,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
    )
    await context.runtime.close()
    await context.provider.close(context.identity)


@pytest.mark.asyncio
async def test_registry_observes_real_runtime_output_intent_admission() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.output_admissions: list[OutputIntentAdmission] = []

        def emit_output_intent_decision(
            self,
            _session_id: str,
            admission: OutputIntentAdmission,
        ) -> bool:
            self.output_admissions.append(admission)
            return True

    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = SessionIdentity("runtime-output-observer")
    context = await registry._get_or_create(identity)
    fence = context.runtime.fence
    intent = context.runtime.orchestrator.delegation.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=fence,
        context_version=0,
        expires_at_ms=2_000,
        now_ms=1_000,
    )

    assert context.runtime.orchestrator.delegation.admit_output_intent(
        intent,
        current_fence=fence,
        current_context_version=0,
        floor_allows_output=True,
        now_ms=1_500,
    )
    assert len(bridge.output_admissions) == 1
    assert bridge.output_admissions[0].accepted is True
    await context.runtime.close()
    await context.provider.close(identity)


@pytest.mark.asyncio
async def test_main_reply_holds_output_owner_until_playback_ack() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.output_admissions: list[OutputIntentAdmission] = []
            self.frames: list[object] = []
            self.runtime_events: list[tuple[str, dict[str, object]]] = []

        def emit_output_intent_decision(
            self,
            _session_id: str,
            admission: OutputIntentAdmission,
        ) -> bool:
            self.output_admissions.append(admission)
            return True

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
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("main-output-owner")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
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
        ),
    )

    assert context.output_owner is None
    assert bridge.output_admissions[-1].consumed is True
    assert bridge.output_admissions[-1].reason == "playback_completed"
    assert bridge.output_admissions[-1].authoritative_candidates == ()
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
    assert await registry._enqueue_output_work(context, _OutputWork(queued_intent))
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
        ),
    )
    await asyncio.sleep(0)

    assert len(bridge.frames) == 2
    second = bridge.frames[1]
    assert second.generation_id == fence.generation_id
    assert second.sequence == 1
    assert second.source_start_sample == 2
    assert context.output_owner is not None

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=1,
            rendered_sample_end=482,
            client_monotonic_ms=2,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
        ),
    )
    assert context.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_reserved_output_kind_is_rejected_at_streamcore_execution_boundary() -> None:
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = SessionIdentity("reserved-output-kind")
    context = await registry._get_or_create(identity)
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

    assert not await registry._enqueue_output_work(context, _OutputWork(reserved))
    assert str(reserved.intent_id) not in context.output_work
    assert not coordinator.output_intent_is_active(
        reserved,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 1,
    )
    assert context.output_owner is None
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_empty_provider_reply_returns_the_current_session_to_listening() -> None:
    class EmptyProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                if False:  # pragma: no cover - keeps this an async generator
                    yield MediaReplyChunk(b"\x00\x00", 0)

            return chunks()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.states: list[str] = []

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            if event_type == "assistant_state":
                self.states.append(str(payload.get("state", "")))
            return True

    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: EmptyProvider(),
    )
    registry.install()
    identity = SessionIdentity("empty-provider-reply")
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)
    await asyncio.sleep(0)

    assert context.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.interaction_phase.value == "listening"
    assert "listening" in bridge.states
    await registry._finalize_session(identity.session_id)


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
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("superseded-output-owner")
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("帮我查一下")
    context.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "帮我查一下", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert context.output_owner is not None

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
    assert context.output_owner is None
    coordinator.complete_output_intent(
        acknowledgement,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 2,
    )
    await registry._finalize_session(identity.session_id)


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
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.playback.start(fence)
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
    assert await registry._enqueue_output_work(context, _OutputWork(urgent))
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
    assert context.output_owner is None
    await registry._finalize_session(identity.session_id)


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
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("帮我查一下")
    context.playback.start(fence)
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
    assert context.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.THINKING
    assert context.runtime._pending_assistant_text == ""
    assert context.runtime._was_speaking is False
    assert context.runtime._assistant_expression_fence is None
    coordinator.complete_output_intent(
        acknowledgement,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 2,
    )
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_reply_owner_and_task_are_revoked_before_slow_provider_cancel() -> None:
    reply_started = asyncio.Event()
    reply_drained = asyncio.Event()
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    class SlowCancelProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                try:
                    reply_started.set()
                    await asyncio.Event().wait()
                    yield MediaReplyChunk(b"\x00\x00", 0)
                finally:
                    reply_drained.set()

            return chunks()

        async def cancel_generation(self, _fence: GenerationFence) -> None:
            cancel_started.set()
            await release_cancel.wait()

    provider = SlowCancelProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("slow-provider-cancel")
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "你好", fence))
    await asyncio.wait_for(reply_started.wait(), timeout=1)
    assert context.output_owner is not None

    cancellation = asyncio.create_task(registry._cancel_reply_task(context, fence))
    await asyncio.wait_for(cancel_started.wait(), timeout=1)
    try:
        assert context.output_owner is None
        await asyncio.wait_for(reply_drained.wait(), timeout=1)
        assert reply.done()
    finally:
        release_cancel.set()
        await asyncio.gather(cancellation, return_exceptions=True)
        if not reply.done():
            reply.cancel()
        await asyncio.gather(reply, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_provider_cancel_failure_is_contained_and_reply_task_is_drained() -> None:
    reply_started = asyncio.Event()
    reply_drained = asyncio.Event()

    class FailingCancelProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                try:
                    reply_started.set()
                    await asyncio.Event().wait()
                    yield MediaReplyChunk(b"\x00\x00", 0)
                finally:
                    reply_drained.set()

            return chunks()

        async def cancel_generation(self, _fence: GenerationFence) -> None:
            raise RuntimeError("provider cancel failed")

    provider = FailingCancelProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("failing-provider-cancel")
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "你好", fence))
    await asyncio.wait_for(reply_started.wait(), timeout=1)

    try:
        await registry._cancel_reply_task(context, fence)
        assert context.output_owner is None
        assert reply_drained.is_set()
        assert reply.done()
    finally:
        if not reply.done():
            reply.cancel()
        await asyncio.gather(reply, return_exceptions=True)
        await context.runtime.close()
        await provider.close(identity)


@pytest.mark.asyncio
async def test_media_registry_lets_the_shared_agent_prepare_a_committed_turn() -> None:
    identity = SessionIdentity("prepared-media-turn")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.context_versions: list[int] = []
            self.committed_context_versions: list[int] = []

        async def emit_context_activated(self, _session_id: str, context_version: int) -> bool:
            self.context_versions.append(context_version)
            return True

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            if event_type == "turn.committed":
                self.committed_context_versions.append(int(payload["context_version"]))
            return True

        async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
            return True

    class PreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepared: list[str] = []

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            self.prepared.append(text)
            fence = await runtime.on_turn_committed(text, input_modality="audio")
            snapshot = await runtime.freeze_context_capsules_for_generation(
                fence,
                memory_capsule=MemoryCapsule(),
                persona_capsule=PersonaCapsule(),
            )
            assert snapshot is not None and snapshot.version == 1
            return fence

    provider = PreparingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    bridge.bridge.open(identity)
    await registry._get_or_create(identity)
    assert runtime.ingest_media_speech_segment(
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="prepared-turn",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=0,
            capture_end_sample=320,
            text="帮我制定计划",
            final=True,
        )
    )

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=1,
        start_sample=0,
        end_sample=320,
    )

    assert reason is None
    assert fence is not None and fence.matches(runtime.fence)
    assert provider.prepared == ["帮我制定计划"]
    assert runtime.orchestrator.context_version_for_fence(fence) == 1
    assert bridge.context_versions == [1]
    assert bridge.committed_context_versions == [1]
    await runtime.close()


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
            ),
        )
        assert context.output_owner is None
    finally:
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
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
async def test_media_vad_classifies_the_speaker_before_committing_the_turn() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.committed: list[dict[str, object]] = []
            self.committed_event = asyncio.Event()

        async def emit_event(
            self,
            session_id: str,
            event_type: str,
            payload: dict[str, object],
            *,
            turn_id: int = 0,
            generation_id: int = 0,
            tool_epoch: int = 0,
            task_epoch: int = 0,
            context_version: int = 0,
        ) -> bool:
            _ = session_id, turn_id, generation_id, tool_epoch, task_epoch, context_version
            if event_type == "turn.committed":
                self.committed.append(payload)
                self.committed_event.set()
            return True

    identity = SessionIdentity("classified-media-turn")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    bind_owner_policy(
        runtime,
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )
    classified_pcm: list[bytes] = []

    async def classify(pcm: bytes, sample_rate: int) -> SpeakerDecision:
        assert sample_rate == 16_000
        classified_pcm.append(pcm)
        return SpeakerDecision(
            classification="owner",
            score=0.92,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="speaker-test-v1",
            template_version=1,
            profile_id="owner-profile",
            permissions=permissions_for_speaker("owner"),
        )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, FakeMediaProvider()),
        turn_endpoint_grace_s=0,
    )
    registry.install()
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    pcm = b"\x01\x00" * 320
    await registry.on_audio_frame(
        session,
        AudioFrame(identity, 0, 0, 320, pcm),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=320,
            capture_end_sample=321,
            final=True,
            voiced_end_sample=320,
        ),
    )
    await asyncio.wait_for(bridge.committed_event.wait(), timeout=1)

    assert classified_pcm == [pcm]
    assert runtime.current_speaker_class == "owner"
    assert bridge.committed[0]["speaker_evidence"] == {
        "speaker_class": "owner",
        "reason_code": "owner_match",
        "authority_verified": True,
    }
    assert bridge.committed[0]["history_eligible"] is True
    await runtime.close()


@pytest.mark.asyncio
async def test_vad_boundary_drains_audio_before_rotating_provider_task() -> None:
    class BoundaryProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.ingest_started = asyncio.Event()
            self.release_ingest = asyncio.Event()
            self.audio_drained = False
            self.finalize_called = False
            self.task_epoch = 1

        @property
        def current_asr_task_epoch(self) -> int:
            return self.task_epoch

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            self.ingest_started.set()
            await self.release_ingest.wait()
            self.audio_drained = True
            return ()

        async def finalize_speech_segment(
            self,
            identity: SessionIdentity,
        ) -> Sequence[ASRResult]:
            assert self.audio_drained
            self.finalize_called = True
            self.task_epoch = 2
            return (
                ASRResult(
                    task_epoch=1,
                    sentence_id="vad-tail",
                    revision=1,
                    capture_start_sample=0,
                    capture_end_sample=320,
                    text="今天星期几",
                    is_final=True,
                    stream_epoch=identity.stream_epoch,
                ),
            )

    provider = BoundaryProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60,
    )
    registry.install()
    identity = SessionIdentity("vad-provider-boundary", stream_epoch=1)
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    await registry.on_audio_frame(
        session,
        AudioFrame(identity, 0, 0, 320, b"\x01\x00" * 320),
    )
    await asyncio.wait_for(provider.ingest_started.wait(), timeout=1)

    boundary_task = asyncio.create_task(
        registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id="vad-end",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=320,
                capture_end_sample=321,
                final=True,
                voiced_end_sample=320,
            ),
        )
    )
    await asyncio.sleep(0)
    assert provider.finalize_called is False
    provider.release_ingest.set()
    await asyncio.wait_for(boundary_task, timeout=1)

    context = registry._sessions[identity.session_id]
    assert provider.finalize_called is True
    assert context.turn_end_sample == 320
    assert context.asr.latest_authoritative_task_epoch == 2
    assert any(segment.text == "今天星期几" for segment in context.runtime.speech_timeline.pending)
    if context.turn_endpoint_task is not None:
        context.turn_endpoint_task.cancel()
        await asyncio.gather(context.turn_endpoint_task, return_exceptions=True)
    await context.runtime.close()


@pytest.mark.asyncio
async def test_multiple_asr_finals_wait_for_vad_and_commit_one_logical_turn() -> None:
    provider = MultiFinalProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("multi-final-session")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    await registry.on_audio_frame(
        session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )
    # A short VAD pause is not a logical turn boundary: the following speech
    # start cancels the pending endpoint and both provider finals stay in one
    # sample-clock turn.
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-pause",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=2,
            capture_end_sample=3,
            final=True,
        ),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-resume",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=2,
            capture_end_sample=3,
        ),
    )
    await registry.on_audio_frame(
        session,
        AudioFrame(
            identity=identity,
            sequence=1,
            capture_start_sample=2,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )

    context = registry._sessions[identity.session_id]
    assert context.asr.last_sent_sample == 4
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=4,
            capture_end_sample=5,
            final=True,
        ),
    )
    await asyncio.sleep(0.03)

    user_turns = [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ]
    assert user_turns == ["第一句 第二句"]
    assert context.asr.last_committed_sample == 4


@pytest.mark.asyncio
async def test_vad_endpoint_waits_for_late_asr_coverage_before_committing() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("late-final-session")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    assert await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="first",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=320,
            text="第一句",
            is_final=True,
            stream_epoch=1,
        ),
    )
    registry._observe_final_asr_result(
        registry._sessions[identity.session_id],
        ASRResult(
            task_epoch=1,
            sentence_id="first",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=320,
            text="第一句",
            is_final=True,
            stream_epoch=1,
        ),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=640,
            capture_end_sample=641,
            final=True,
            voiced_end_sample=640,
        ),
    )
    await asyncio.sleep(0.03)

    context = registry._sessions[identity.session_id]
    assert context.asr.last_committed_sample == 0
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []

    late = ASRResult(
        task_epoch=1,
        sentence_id="second",
        revision=1,
        capture_start_sample=320,
        capture_end_sample=640,
        text="第二句",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, late)
    registry._observe_final_asr_result(context, late)
    await asyncio.sleep(0.03)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["第一句 第二句"]
    assert context.asr.last_committed_sample == 640


@pytest.mark.asyncio
async def test_absolute_endpoint_tail_discards_turn_when_provider_final_never_arrives() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        turn_endpoint_grace_s=0.001,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=0.02,
    )
    identity = SessionIdentity("missing-provider-final")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="missing-final-start",
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
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="missing-final-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=640,
            capture_end_sample=641,
            final=True,
            voiced_end_sample=600,
        ),
    )

    await asyncio.sleep(0.04)

    context = registry._sessions[identity.session_id]
    assert context.turn_endpoint_sample is None
    assert context.turn_start_sample is None
    assert context.projection.provisional is None
    assert context.asr.last_committed_sample == 600
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []


@pytest.mark.asyncio
async def test_absolute_endpoint_tail_commits_stable_partial_with_missing_final_marker() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.client_events: list[tuple[str, dict[str, Any]]] = []

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            payload: dict[str, Any],
            **_kwargs: Any,
        ) -> bool:
            self.client_events.append((event_type, payload))
            return True

    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        turn_endpoint_grace_s=0.001,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=0.02,
    )
    identity = SessionIdentity("partial-provider-final")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="partial-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    assert await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="partial-sentence",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=600,
            text="南京天气",
            is_final=False,
            confidence=0.9,
            stream_epoch=1,
        ),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="partial-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=600,
            capture_end_sample=601,
            final=True,
            voiced_end_sample=600,
        ),
    )

    await asyncio.sleep(0.04)

    context = registry._sessions[identity.session_id]
    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["南京天气"]
    committed = [
        payload for event_type, payload in bridge.client_events if event_type == "turn.committed"
    ]
    assert committed and committed[-1]["provider_final_missing"] is True
    assert context.pending_partial is None


@pytest.mark.asyncio
async def test_vad_tail_silence_tolerance_does_not_leave_turn_pending() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("tail-silence-session")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    final = ASRResult(
        task_epoch=1,
        sentence_id="tail-final",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=16_000,
        text="尾音结束",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-tail-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=24_000,
            capture_end_sample=24_001,
            final=True,
            voiced_end_sample=16_000,
        ),
    )
    await asyncio.sleep(0.03)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["尾音结束"]
    assert context.asr.last_committed_sample == 24_000

    late_tail = ASRResult(
        task_epoch=1,
        sentence_id="late-tail",
        revision=1,
        capture_start_sample=16_000,
        capture_end_sample=20_000,
        text="不应串入",
        is_final=True,
        stream_epoch=1,
    )
    assert not await registry.accept_asr_result(identity.session_id, late_tail)

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="next-vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=24_000,
            capture_end_sample=24_001,
        ),
    )
    next_final = ASRResult(
        task_epoch=1,
        sentence_id="next-final",
        revision=1,
        capture_start_sample=24_000,
        capture_end_sample=25_000,
        text="新一轮",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, next_final)
    registry._observe_final_asr_result(context, next_final)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="next-vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=26_000,
            capture_end_sample=26_001,
            final=True,
            voiced_end_sample=25_000,
        ),
    )
    await asyncio.sleep(0.03)
    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["尾音结束", "新一轮"]


@pytest.mark.asyncio
async def test_eight_hundred_ms_within_turn_pause_does_not_split_child_speech() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("child-pause-session")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    await vad("start-1", 0, final=False)
    first = ASRResult(1, "first", 1, 0, 320, "我想说", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, first)
    registry._observe_final_asr_result(context, first)
    await vad("pause", 320, final=True)
    await asyncio.sleep(0.82)
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []

    await vad("resume", 320, final=False)
    second = ASRResult(1, "second", 1, 320, 640, "一个故事", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, second)
    registry._observe_final_asr_result(context, second)
    await vad("end", 640, final=True)
    endpoint_task = context.turn_endpoint_task
    assert endpoint_task is not None
    endpoint_task.cancel()
    await asyncio.gather(endpoint_task, return_exceptions=True)
    await registry._commit_pending_turn(context)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["我想说 一个故事"]


@pytest.mark.asyncio
async def test_kws_hard_stop_uses_the_authoritative_wire_flag() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("kws-session")
    session = bridge.bridge.open(identity)
    effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

    async def emit_realtime_effect(
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

    bridge.emit_realtime_effect = emit_realtime_effect  # type: ignore[attr-defined,method-assign]

    async def keyword(segment_id: str, confidence: float, hard_stop: bool) -> None:
        start = len(segment_id) * 10
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.KWS,
                capture_start_sample=start,
                capture_end_sample=start + 1,
                text="停一下",
                final=True,
                confidence=confidence,
                hard_stop=hard_stop,
            ),
        )

    await keyword("not-hard", 1.0, False)
    await keyword("low-confidence", 0.79, True)
    assert session.fence.generation_id == 1
    await keyword("accepted-hard-stop", 0.8, True)
    assert session.fence.generation_id == 2
    assert [
        (effect_kind, fence.generation_id, source_event_id, payload)
        for effect_kind, fence, source_event_id, payload in effects
    ] == [
        (
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            1,
            "keyword_interrupt",
            {"reason": "keyword_interrupt"},
        ),
        (
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            2,
            "keyword_interrupt",
            {"reason": "keyword_interrupt"},
        ),
    ]
    assert registry.metrics.latency_samples["interrupt_core_stop"]


@pytest.mark.asyncio
async def test_media_vad_start_ducks_before_any_asr_result() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    published: list[tuple[int, GenerationFence, dict[str, object]]] = []
    client_events: list[str] = []

    async def emit_event(
        _session_id: str,
        event_type: str,
        _payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        client_events.append(event_type)
        return True

    async def emit_realtime_effect(
        _session_id: str,
        effect_kind: int,
        fence: GenerationFence,
        *,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        published.append((effect_kind, fence, payload))
        return True

    bridge.emit_event = emit_event  # type: ignore[method-assign]
    bridge.emit_realtime_effect = emit_realtime_effect  # type: ignore[attr-defined,method-assign]
    identity = SessionIdentity("vad-duck-session", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    await asyncio.sleep(0)

    assert len(published) == 1
    effect_kind, effect_fence, payload = published[0]
    assert effect_kind == media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT
    assert effect_fence == context.runtime.fence
    assert payload["session_id"] == identity.session_id
    assert payload["action"] == "duck"
    assert payload["gain"] == 0.0
    assert payload["turn_id"] == context.runtime.fence.turn_id
    assert payload["generation_id"] == context.runtime.fence.generation_id
    assert payload["tool_epoch"] == context.runtime.fence.tool_epoch
    assert isinstance(payload["at"], str)
    assert "assistant_audio" not in client_events


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
    context = await registry._get_or_create(identity)

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
async def test_media_backchannel_restores_without_persisting_a_turn() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    effects: list[tuple[int, dict[str, object]]] = []

    async def emit_realtime_effect(
        _session_id: str,
        effect_kind: int,
        _fence: GenerationFence,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        effects.append((effect_kind, payload))
        return True

    bridge.emit_realtime_effect = emit_realtime_effect  # type: ignore[attr-defined,method-assign]
    identity = SessionIdentity("backchannel-session", stream_epoch=1)
    bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    assert context.runtime.ingest_media_speech_segment(
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="backchannel",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=0,
            capture_end_sample=320,
            text="嗯嗯",
            final=True,
        )
    )

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=1,
        start_sample=0,
        end_sample=320,
    )
    await asyncio.sleep(0)

    assert fence is None and reason == "backchannel"
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]
    assert any(
        effect_kind == media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT
        and payload["action"] == "restore"
        for effect_kind, payload in effects
    )


@pytest.mark.asyncio
async def test_media_sustained_barge_in_restores_gain_and_commits() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    effects: list[tuple[int, dict[str, object]]] = []

    async def emit_realtime_effect(
        _session_id: str,
        effect_kind: int,
        _fence: GenerationFence,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        effects.append((effect_kind, payload))
        return True

    bridge.emit_realtime_effect = emit_realtime_effect  # type: ignore[attr-defined,method-assign]
    identity = SessionIdentity("barge-in-session", stream_epoch=1)
    bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    assert context.runtime.ingest_media_speech_segment(
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="barge-in",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=0,
            capture_end_sample=8_000,
            text="我想问一下天气",
            final=True,
        )
    )

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=1,
        start_sample=0,
        end_sample=8_000,
    )
    await asyncio.sleep(0)

    assert fence is not None and reason is None
    assert context.runtime.orchestrator.context.turns[-1].content == "我想问一下天气"
    assert any(
        effect_kind == media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT
        and payload["action"] == "restore"
        for effect_kind, payload in effects
    )


@pytest.mark.asyncio
async def test_media_echo_restores_without_cancelling_or_committing() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = SessionIdentity("echo-session", stream_epoch=1)
    bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await context.runtime.on_assistant_speaking("正在播放的同一句话")
    before = context.runtime.fence
    assert context.runtime.ingest_media_speech_segment(
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="assistant-echo",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=0,
            capture_end_sample=8_000,
            text="正在播放的同一句话",
            final=True,
        )
    )

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=1,
        start_sample=0,
        end_sample=8_000,
    )

    assert fence is None and reason == "assistant_echo"
    assert context.runtime.fence.matches(before)
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]


@pytest.mark.asyncio
async def test_media_vad_without_asr_restores_duck_after_endpoint_grace() -> None:
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    effects: list[tuple[int, dict[str, object]]] = []
    restored = asyncio.Event()

    async def emit_realtime_effect(
        _session_id: str,
        effect_kind: int,
        _fence: GenerationFence,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        effects.append((effect_kind, payload))
        if effect_kind == media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT:
            restored.set()
        return True

    bridge.emit_realtime_effect = emit_realtime_effect  # type: ignore[attr-defined,method-assign]
    identity = SessionIdentity("no-asr-session", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    for segment_id, sample, final in (
        ("vad-start", 0, False),
        ("vad-end", 320, True),
    ):
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )
    await asyncio.wait_for(restored.wait(), timeout=0.2)

    assert [effect_kind for effect_kind, _payload in effects] == [
        media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
        media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
    ]
    assert [payload["action"] for _effect_kind, payload in effects] == ["duck", "restore"]


async def _start_speaking_reply(
    registry: MediaVoiceCoreRegistry,
    session: object,
    identity: SessionIdentity,
) -> tuple[object, GenerationFence]:
    """Drive one VAD+ASR turn and a provider reply, returning its context."""

    context = await registry._get_or_create(identity)

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,  # type: ignore[arg-type]
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    await vad("start", 0, final=False)
    final = ASRResult(1, "turn-final", 1, 0, 320, "你好", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await vad("end", 320, final=True)
    await asyncio.sleep(0.03)
    fence = context.runtime.fence
    if context.reply_task is not None:
        assert await asyncio.wait_for(context.reply_task, timeout=1)
    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    # Keep the bridge-side authoritative gate in sync with the runtime fence
    # so a subsequent client stop/KWS cancel derives the expected fence.
    session.generation.advance(fence)  # type: ignore[attr-defined]
    session.reset_downlink_generation(fence)  # type: ignore[attr-defined]
    session.generation_active = True  # type: ignore[attr-defined]
    return context, fence


@pytest.mark.asyncio
async def test_interruption_records_only_provider_timed_prefix_before_fence_change() -> None:
    provider = TimedInterruptProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("timed-interrupt-session")
    context = await registry._get_or_create(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    context.playback.start(fence)
    assert context.playback.register_audio(fence, 0, 0, 2)
    assert context.playback.acknowledge(fence, 2, received_sequence=0) == ()

    await registry._record_interrupted_timed_spans(context, fence)

    assert context.playback.actual_heard_text(fence) == "你好。"


@pytest.mark.asyncio
async def test_client_stop_during_interruption_pending_finalizes_heard_prefix() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("pending-stop-session")
    session = bridge.bridge.open(identity)
    context, fence = await _start_speaking_reply(registry, session, identity)
    assert context.playback.register_audio(fence, 0, 0, 2)
    assert context.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=3,
            audio_start_sample=0,
            audio_end_sample=2,
            text="你好。",
        )
    )
    assert context.playback.acknowledge(fence, 2)
    assert context.playback.actual_heard_text(fence) == "你好。"
    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    # VAD has already moved the runtime into INTERRUPTION_PENDING; the client
    # stop must still run the interrupted-playback finalize with the heard
    # prefix instead of switching fences without history.
    context.runtime.orchestrator.state_machine.state = ConversationState.INTERRUPTION_PENDING
    interrupted: list[tuple[GenerationFence, str]] = []
    original = context.runtime.on_media_playback_interrupted

    async def spy(*, interrupted_from: GenerationFence, synchronized_transcript: str) -> None:
        interrupted.append((interrupted_from, synchronized_transcript))
        await original(
            interrupted_from=interrupted_from,
            synchronized_transcript=synchronized_transcript,
        )

    context.runtime.on_media_playback_interrupted = spy  # type: ignore[method-assign]
    stop = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="stop-pending-1",
        session_id=identity.session_id,
        stream_epoch=1,
        sequence=1,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        payload={"idempotency_key": "stop-pending-1", "reason": "test"},
    )
    assert session.accept_client_stop(stop)
    await registry.on_client_event(session, stop)
    assert interrupted == [(fence, "你好。")]
    assert registry.metrics.latency_samples["interrupt_core_stop"]


@pytest.mark.asyncio
async def test_kws_stop_during_interruption_pending_finalizes_heard_prefix() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("kws-pending-stop-session")
    session = bridge.bridge.open(identity)
    context, fence = await _start_speaking_reply(registry, session, identity)
    assert context.playback.register_audio(fence, 0, 0, 2)
    assert context.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=3,
            audio_start_sample=0,
            audio_end_sample=2,
            text="你好。",
        )
    )
    assert context.playback.acknowledge(fence, 2)
    context.runtime.orchestrator.state_machine.state = ConversationState.INTERRUPTION_PENDING
    interrupted: list[str] = []
    original = context.runtime.on_media_playback_interrupted

    async def spy(*, interrupted_from: GenerationFence, synchronized_transcript: str) -> None:
        interrupted.append(synchronized_transcript)
        await original(
            interrupted_from=interrupted_from,
            synchronized_transcript=synchronized_transcript,
        )

    context.runtime.on_media_playback_interrupted = spy  # type: ignore[method-assign]
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="kws-pending-stop",
            revision=1,
            kind=SegmentKind.KWS,
            capture_start_sample=400,
            capture_end_sample=402,
            text="停一下",
            final=True,
            confidence=0.95,
            hard_stop=True,
        ),
    )
    assert interrupted == ["你好。"]
    assert registry.metrics.latency_samples["interrupt_core_stop"]


@pytest.mark.asyncio
async def test_interrupt_metric_uses_core_monotonic_clock() -> None:
    import time as time_module

    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("slo-session")
    session = bridge.bridge.open(identity)
    context, fence = await _start_speaking_reply(registry, session, identity)
    future_edge_wall_clock_ms = int(time_module.time() * 1000) + 86_400_000
    stop = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="slo-stop-1",
        session_id=identity.session_id,
        stream_epoch=1,
        sequence=1,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        payload={"idempotency_key": "slo-stop-1", "reason": "test"},
    )
    assert session.accept_client_stop(stop)
    await registry.on_client_event(session, stop, future_edge_wall_clock_ms)
    latency = registry.metrics.latency_samples["interrupt_core_stop"][-1]
    assert 0.0 <= latency < 1.0


@pytest.mark.asyncio
async def test_downlink_queue_overflow_cancels_runtime_and_provider() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer(max_pending_messages=1)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("overflow-registry-session")
    connection = bridge._open_connection(identity)
    context = await registry._get_or_create(identity)
    baseline = connection.outgoing.get_nowait()
    assert baseline is not None and baseline.WhichOneof("event") == "floor_effect"
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好。")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    connection.session.generation.advance(fence)
    assert connection.session.reset_downlink_generation(fence)
    connection.session.generation_active = True
    assert context.runtime.orchestrator.state is ConversationState.SPEAKING

    assert await bridge.emit_generation(
        identity.session_id,
        fence,
        action=media_pb2.GENERATION_ACTION_START,
    )
    assert not await bridge._enqueue(
        connection,
        media_pb2.CoreToMedia(error=media_pb2.CoreError(code="full", message="full")),
    )

    assert connection.session.generation_active is False
    assert context.runtime.fence.matches(connection.session.fence)
    assert provider.cancelled == [fence]


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

    async def spy(done_fence: GenerationFence, heard_text: str) -> bool:
        completed.append((done_fence, heard_text))
        return await original(done_fence, heard_text)

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


@pytest.mark.asyncio
async def test_approximate_device_progress_completes_without_actual_heard() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
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
    context = await registry._get_or_create(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.playback.start(fence)
    assert context.playback.register_audio(fence, 0, 0, 320)
    assert context.playback.add_span(
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
    context.provider_complete = True

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
        ),
    )

    assert context.playback.actual_heard_text(fence) == ""
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert all(turn.content != "你好" for turn in context.runtime.orchestrator.context.turns)


@pytest.mark.asyncio
async def test_media_registry_runs_fake_asr_llm_tts_through_both_fences() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer(allow_go_shadow=True)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    port = await bridge.start("127.0.0.1:0")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    rpc = channel.stream_stream(
        "/memoria.media.v1.VoiceMediaBridge/Connect",
        request_serializer=media_pb2.MediaToCore.SerializeToString,
        response_deserializer=media_pb2.CoreToMedia.FromString,
    )
    requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    call = rpc(_requests(requests))
    identity = media_pb2.SessionIdentity(
        session_id="media-registry-session",
        account_id="account",
        participant_id="participant",
        device_id="h5",
        client_type="h5",
        stream_epoch=1,
    )
    session_identity = SessionIdentity(
        "media-registry-session",
        account_id="account",
        participant_id="participant",
        device_id="h5",
    )
    try:
        await requests.put(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(
                    identity=identity,
                    interaction_authority=media_pb2.INTERACTION_AUTHORITY_GO_SHADOW,
                )
            )
        )
        accepted = await asyncio.wait_for(call.read(), timeout=1)
        assert accepted.accepted.identity.session_id == session_identity.session_id
        await requests.put(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=identity,
                    type=media_pb2.VAD_EVENT_SPEECH_START,
                    sample_position=0,
                    probability=0.99,
                )
            )
        )
        await requests.put(
            media_pb2.MediaToCore(
                audio=media_pb2.AudioFrame(
                    identity=identity,
                    sequence=0,
                    capture_start_sample=0,
                    frame_samples=2,
                    payload=b"\x00\x00\x01\x00",
                )
            )
        )
        floor = await _next_event(call, "floor_effect")
        assert floor.floor_effect.floor_state == media_pb2.FLOOR_STATE_SILENCE
        assert floor.floor_effect.floor_epoch == 1
        assert floor.floor_effect.candidate_only is False
        assert floor.floor_effect.expires_at_ms > 0
        transcript = await _next_event(call, "transcript")
        assert transcript.transcript.text == "你好"
        for _ in range(4):
            shadow = await _next_event(call, "shadow_observation")
            if shadow.shadow_observation.WhichOneof("input") == "speech_segment":
                break
        else:
            raise AssertionError("bridge did not emit the ASR speech segment shadow")
        assert shadow.shadow_observation.authoritative_timeline.latest_task_epoch == 1
        assert shadow.shadow_observation.speech_segment.segment_id == "fake-sentence"
        assert len(shadow.shadow_observation.speech_segment.text_sha256) == 32
        assert provider.audio_calls == [0]

        await requests.put(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=identity,
                    type=media_pb2.VAD_EVENT_SPEECH_END,
                    sample_position=2,
                    probability=0.99,
                    voiced_end_sample=2,
                )
            )
        )
        committed_timeline = await _next_event(call, "shadow_observation")
        assert committed_timeline.shadow_observation.speech_commit.committed_sample == 2
        assert committed_timeline.shadow_observation.authoritative_timeline.segments == []
        context_observation = await _next_event(call, "shadow_observation")
        assert context_observation.shadow_observation.WhichOneof("input") == "context_activated"
        assert (
            context_observation.shadow_observation.context_activated.context_version
            == context_observation.shadow_observation.authoritative_context_version
        )
        started = await _next_event(call, "generation")
        assert started.generation.generation_id == 1
        fence = GenerationFence(session_identity.session_id, 1, 1, 0)
        audio = await _next_event(call, "audio")
        assert audio.audio.generation_id == fence.generation_id
        completed = await _next_event(call, "generation")
        assert completed.generation.action == media_pb2.GENERATION_ACTION_COMPLETE
        context = registry._sessions[session_identity.session_id]
        assert context.playback.current_fence == fence
        assert context.playback._spans[fence]
        assert context.runtime.orchestrator.state is ConversationState.SPEAKING

        await requests.put(
            media_pb2.MediaToCore(
                playback=media_pb2.PlaybackProgress(
                    identity=identity,
                    generation_id=fence.generation_id,
                    received_sequence=audio.audio.sequence,
                    rendered_sample_end=2,
                    client_monotonic_ms=1,
                    approximate=False,
                    turn_id=fence.turn_id,
                    tool_epoch=fence.tool_epoch,
                )
            )
        )
        heard_payload: dict[str, object] = {}
        for _ in range(32):
            heard_event = await _next_event(call, "client")
            heard_payload = json.loads(bytes(heard_event.client.json_payload))
            payload = heard_payload.get("payload")
            if (
                heard_payload.get("type") == "transcript_delta"
                and isinstance(payload, dict)
                and payload.get("heard") is True
            ):
                break
        assert heard_payload["type"] == "transcript_delta"
        assert heard_payload["v"] == 1
        assert heard_payload["protocol"] == "media-v1"
        assert heard_payload["event_id"]
        assert heard_payload["server_monotonic_ms"] > 0
        assert heard_payload["payload"]["heard"] is True  # type: ignore[index]
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert context.runtime.orchestrator.context.turns[-1].content == "你好。"

        stop = MediaEnvelope.create(
            type="client.stop_assistant",
            event_id="stop-1",
            session_id=session_identity.session_id,
            stream_epoch=1,
            sequence=1,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            payload={"idempotency_key": "stop-1", "reason": "test"},
        )
        await requests.put(
            media_pb2.MediaToCore(
                device=media_pb2.DeviceEvent(
                    identity=identity,
                    event_type="client.stop_assistant",
                    json_payload=stop.encode(),
                )
            )
        )
        cancelled = await _next_event(call, "generation")
        assert cancelled.generation.action == media_pb2.GENERATION_ACTION_CANCEL
        assert cancelled.generation.generation_id == 2
        assert not await registry.generate_reply(session_identity.session_id, "旧回复", fence)
        assert registry.context(session_identity.session_id) is not None
    finally:
        await requests.put(None)
        await call.read()
        await channel.close()
        await bridge.stop()
    assert provider.closed is True
    assert bridge.bridge.get(session_identity.session_id) is None


@pytest.mark.asyncio
async def test_registry_rolls_back_supervisor_when_runtime_timeline_rejects() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("registry-transaction")
    context = await registry._get_or_create(identity)

    runtime_newer = ASRResult(2, "same-sentence", 1, 0, 320, "新结果", True)
    assert context.runtime.ingest_media_speech_segment(
        asr_result_to_segment(runtime_newer, session_id=identity.session_id)
    )
    before = context.asr.timeline.pending

    stale = ASRResult(1, "same-sentence", 1, 0, 320, "旧结果", True)
    decision = await registry._accept_asr_result_decision(identity.session_id, stale)

    assert decision.accepted is None
    assert decision.reason.value == "interval_conflict"
    assert context.asr.timeline.pending == before
    assert context.asr.latest_authoritative_task_epoch == 2

    another_old = ASRResult(1, "different-sentence", 1, 320, 640, "仍是旧任务", True)
    stale_decision = await registry._accept_asr_result_decision(
        identity.session_id,
        another_old,
    )
    assert stale_decision.reason.value == "stale_task_epoch"


@pytest.mark.asyncio
async def test_registry_fences_old_result_as_soon_as_provider_switches_tasks() -> None:
    class ReconnectingASR:
        def __init__(self) -> None:
            self.task_id = "task-1"
            self.task_epoch = 1
            self.task_sample_origin = 0
            self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()

        async def connect(self) -> None:
            return None

        async def reconnect_with_replay(self) -> None:
            raise AssertionError("stream epoch did not change")

        async def send_pcm(self, _pcm: bytes, *, capture_start_sample: int) -> None:
            assert capture_start_sample == 0
            self.task_id = "task-2"
            self.task_epoch = 2
            self.task_sample_origin = 320
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id="task-1",
                    sentence=FunASRSentence(
                        sentence_id=9,
                        text="旧任务迟到结果",
                        begin_ms=0,
                        end_ms=20,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )

        async def aclose(self) -> None:
            return None

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.transcripts: list[SpeechSegment] = []
            self.task_starts: list[int] = []
            self.segment_decisions: list[tuple[int, bool, str]] = []

        async def emit_transcript(
            self,
            _session_id: str,
            segment: SpeechSegment,
            *,
            turn_id: int | None = None,
            speaker_class: str = "",
            task_epoch: int = 0,
            context_version: int = 0,
        ) -> bool:
            _ = turn_id, speaker_class, task_epoch, context_version
            self.transcripts.append(segment)
            return True

        async def emit_speech_task_started(
            self,
            _session_id: str,
            task_epoch: int,
            _timeline: Any,
        ) -> bool:
            self.task_starts.append(task_epoch)
            return True

        async def emit_speech_segment_decision(
            self,
            _session_id: str,
            segment: SpeechSegment,
            *,
            authoritative_accepted: bool,
            authoritative_reason: str,
            timeline: Any,
            latest_task_epoch: int,
        ) -> bool:
            _ = timeline, latest_task_epoch
            self.segment_decisions.append(
                (segment.provider_task_epoch, authoritative_accepted, authoritative_reason)
            )
            return True

    asr = ReconnectingASR()
    provider = ExistingVoiceProviderAdapter(
        asr_session_factory=lambda: asr,  # type: ignore[arg-type]
        language_model=object(),  # type: ignore[arg-type]
        speech_synthesis=object(),  # type: ignore[arg-type]
    )
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("task-start-fence")
    session = bridge.bridge.open(identity)

    await registry.on_audio_frame(
        session,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )

    context = registry._sessions[identity.session_id]
    assert context.asr.latest_authoritative_task_epoch == 2
    assert bridge.transcripts == []
    assert bridge.task_starts == [2]
    assert bridge.segment_decisions == [(1, False, "stale_task_epoch")]
    assert context.runtime.speech_timeline.pending == ()
    await context.runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_registry_forwards_only_normalized_watermark_tail() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("registry-normalized-tail")
    context = await registry._get_or_create(identity)
    first = ASRResult(1, "sentence", 1, 0, 320, "你好", True)
    assert await registry.accept_asr_result(identity.session_id, first)
    context.asr.mark_committed(320)
    extension = ASRResult(
        2,
        "sentence",
        1,
        0,
        800,
        "你好世界",
        True,
        word_timings=(
            # Complete evidence lets the supervisor drop the committed prefix
            # without guessing a character boundary.
            ASRWordTiming("你", 0, 160),
            ASRWordTiming("好", 160, 320),
            ASRWordTiming("世", 320, 560),
            ASRWordTiming("界", 560, 800),
        ),
    )

    decision = await registry._accept_asr_result_decision(identity.session_id, extension)
    assert decision.accepted is not None
    assert decision.accepted.text == "世界"
    assert (
        context.runtime.speech_timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=800,
        )
        == "你好 世界"
    )


@pytest.mark.asyncio
async def test_registry_shadows_the_normalized_watermark_tail() -> None:
    first = ASRResult(1, "sentence", 1, 0, 320, "你好", True)
    extension = ASRResult(
        2,
        "sentence",
        1,
        0,
        800,
        "你好世界",
        True,
        word_timings=(
            ASRWordTiming("你", 0, 160),
            ASRWordTiming("好", 160, 320),
            ASRWordTiming("世", 320, 560),
            ASRWordTiming("界", 560, 800),
        ),
    )

    class WatermarkProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            return (first,) if frame.sequence == 0 else (extension,)

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.shadow_segments: list[SpeechSegment] = []

        async def emit_speech_segment_decision(
            self,
            _session_id: str,
            segment: SpeechSegment,
            **_kwargs: Any,
        ) -> bool:
            self.shadow_segments.append(segment)
            return True

    identity = SessionIdentity("registry-shadow-normalized-tail")
    provider = WatermarkProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    session = bridge.bridge.open(identity)
    await registry.on_audio_frame(
        session,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    context = registry._sessions[identity.session_id]
    context.asr.mark_committed(320)
    await registry.on_audio_frame(
        session,
        AudioFrame(identity, 1, 320, 480, b"\x00\x00" * 480),
    )

    assert [
        (item.capture_start_sample, item.capture_end_sample, item.text)
        for item in bridge.shadow_segments
    ] == [
        (0, 320, "你好"),
        (320, 800, "世界"),
    ]
    await context.runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_registry_chain_normalizes_tail_and_keeps_rejected_replay_out_of_next_turn() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.transcripts: list[SpeechSegment] = []
            self.client_events: list[tuple[str, dict[str, object]]] = []

        async def emit_transcript(
            self,
            session_id: str,
            segment: SpeechSegment,
            *,
            turn_id: int | None = None,
            speaker_class: str = "",
            task_epoch: int = 0,
            context_version: int = 0,
        ) -> bool:
            _ = session_id, turn_id, speaker_class, task_epoch, context_version
            self.transcripts.append(segment)
            return True

        async def emit_event(
            self,
            session_id: str,
            event_type: str,
            payload: dict[str, object],
            *,
            turn_id: int = 0,
            generation_id: int = 0,
            tool_epoch: int = 0,
            task_epoch: int = 0,
            context_version: int = 0,
        ) -> bool:
            _ = session_id, turn_id, generation_id, tool_epoch, task_epoch, context_version
            self.client_events.append((event_type, payload))
            return True

    class WatermarkProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            results = (
                ASRResult(1, "sentence", 1, 0, 320, "你好", True),
                ASRResult(
                    2,
                    "sentence",
                    1,
                    0,
                    640,
                    "你好世界",
                    True,
                    word_timings=(
                        ASRWordTiming("你", 0, 160),
                        ASRWordTiming("好", 160, 320),
                        ASRWordTiming("世", 320, 480),
                        ASRWordTiming("界", 480, 640),
                    ),
                ),
                ASRResult(3, "replay", 1, 0, 960, "旧前缀污染", True),
                ASRResult(2, "next", 1, 960, 1280, "下一轮", True),
            )
            return (results[frame.sequence],)

    provider = WatermarkProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60,
    )
    registry.install()
    identity = SessionIdentity("registry-full-chain")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    async def audio(sequence: int, start_sample: int) -> None:
        await registry.on_audio_frame(
            session,
            AudioFrame(identity, sequence, start_sample, 320, b"\x00\x00" * 320),
        )

    async def commit(segment_id: str, sample: int) -> None:
        await vad(segment_id, sample, final=True)
        endpoint_task = context.turn_endpoint_task
        assert endpoint_task is not None
        endpoint_task.cancel()
        await asyncio.gather(endpoint_task, return_exceptions=True)
        await registry._commit_pending_turn(context)

    await vad("start-1", 0, final=False)
    await audio(0, 0)
    assert context.projection.provisional is not None
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]
    await commit("end-1", 320)
    assert context.projection.provisional is None

    await vad("start-2", 320, final=False)
    await audio(1, 320)
    await commit("end-2", 640)
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界"]

    await audio(2, 640)
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界"]
    assert context.turn_start_sample is None
    assert context.turn_end_sample is None

    await vad("start-3", 960, final=False)
    await audio(3, 960)
    await commit("end-3", 1280)

    user_turns = [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ]
    assert user_turns == ["你好", "世界", "下一轮"]
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界", "下一轮"]
    provisional_started = [
        payload
        for event_type, payload in bridge.client_events
        if event_type == "turn.provisional.started"
    ]
    committed = [
        payload for event_type, payload in bridge.client_events if event_type == "turn.committed"
    ]
    assert len(provisional_started) == 3
    assert len({payload["provisional_id"] for payload in provisional_started}) == 3
    assert [payload["text"] for payload in committed] == ["你好", "世界", "下一轮"]
    assert all(payload["history_eligible"] is False for payload in committed)
