"""Media session: turn preparation, finalize, commit and watermark lifecycles."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.context_snapshot_manager import (
    MemoryCapsule,
    PersonaCapsule,
)
from services.agent.src.orchestration.conversation_projection import ConversationProjection
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.media_session_types import (
    OutputDispatchResult,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _CapturingGenerationBridge,
    _CapturingMediaBridge,
    _device_identity,
    _seed_pending_media_turn,
    _verified_owner_decision,
    _wait_until,
    open_bridge_connection,
)
from services.speaker.domain import SpeakerDecision


@pytest.mark.asyncio
async def test_media_asr_dedup_retains_the_most_recent_128_keys() -> None:
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    identity = SessionIdentity("ordered-asr-dedup")
    context = await registry.open_session(identity)

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

    assert len(context.pending.committed_asr_keys) == 128
    assert next(iter(context.pending.committed_asr_keys))[1] == "sentence-2"
    assert next(reversed(context.pending.committed_asr_keys))[1] == "sentence-129"
    await context.runtime.close()
    await context.provider.close(identity)


@pytest.mark.asyncio
async def test_commit_accepts_asr_subrange_of_leading_vad_projection() -> None:
    identity = SessionIdentity("leading-vad-subrange")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    provider = FakeMediaProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    context = await registry.open_session(identity)
    segments = (
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="leading-vad",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=1,
            segment_id="trimmed-asr",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=160,
            capture_end_sample=640,
            text="梅莫里亚你好",
            final=True,
        ),
    )
    for segment in segments:
        assert context.runtime.ingest_media_speech_segment(segment)
        await registry._apply_projection_segment(context, segment)

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=160,
        end_sample=640,
    )

    assert fence is not None
    assert reason is None
    assert context.projection.provisional is None
    assert [turn.content for turn in runtime.orchestrator.context.turns if turn.role == "user"] == [
        "梅莫里亚你好"
    ]
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_no_provisional_rejects_before_prepare_or_runtime_commit() -> None:
    identity = SessionIdentity("preflight-no-provisional")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class TrackingPreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = TrackingPreparingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    context = await registry.open_session(identity)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="missing-provisional",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="这句不能成为幽灵话轮",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    context.projection = ConversationProjection(identity.session_id, SpeechTimeline())
    before_fence = runtime.fence
    before_turns = list(runtime.orchestrator.context.turns)

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=320,
    )

    assert fence is None
    assert reason == "no_provisional_turn"
    assert provider.prepare_calls == 0
    assert runtime.fence == before_fence
    assert runtime.orchestrator.context.turns == before_turns
    assert runtime.speech_timeline.committed_sample == 0
    assert context.asr.last_committed_sample == 0
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_endpoint_timeout_waits_for_inflight_turn_prepare() -> None:
    identity = SessionIdentity("tail-timeout-during-prepare")
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

    provider = BlockingPreparingProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    context = await registry.open_session(identity)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="blocking-prepare",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="提交期间不能被超时删除",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)
    context.pending.turn_start_sample = 0
    context.pending.turn_end_sample = 320
    context.pending.turn_endpoint_sample = 320
    context.pending.turn_retire_sample = 320

    committing = asyncio.create_task(registry._commit_pending_turn(context))
    await asyncio.wait_for(prepare_started.wait(), timeout=1)
    expiring = asyncio.create_task(
        registry._expire_endpoint_tail(identity.session_id, identity.stream_epoch, 320)
    )
    await asyncio.sleep(0)

    assert not expiring.done()
    assert context.projection.provisional is not None

    release_prepare.set()
    await asyncio.wait_for(committing, timeout=1)
    await asyncio.wait_for(expiring, timeout=1)

    assert [turn.content for turn in runtime.orchestrator.context.turns if turn.role == "user"] == [
        "提交期间不能被超时删除"
    ]
    assert [
        payload["text"] for event_type, payload in bridge.events if event_type == "turn.committed"
    ] == ["提交期间不能被超时删除"]
    assert not [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
        and payload.get("reason") == "provider_final_missing"
    ]
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_audio_discontinuity_waits_for_inflight_turn_prepare() -> None:
    identity = SessionIdentity("discontinuity-during-prepare")
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

    provider = BlockingPreparingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    context = await registry.open_session(identity)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="discontinuity-blocking-prepare",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="断流不能抢先删除话轮",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)
    context.pending.turn_start_sample = 0
    context.pending.turn_end_sample = 320
    context.pending.turn_endpoint_sample = 320
    context.pending.turn_retire_sample = 320

    committing = asyncio.create_task(registry._commit_pending_turn(context))
    await asyncio.wait_for(prepare_started.wait(), timeout=1)
    resetting = asyncio.create_task(
        registry._audio_ingress._reset_discontinuity(
            context,
            AudioFrame(identity, 1, 320, 320, b"\x00\x00" * 320, discontinuity=True),
        )
    )
    await asyncio.sleep(0)

    assert not resetting.done()
    assert context.projection.provisional is not None

    release_prepare.set()
    await asyncio.wait_for(committing, timeout=1)
    await asyncio.wait_for(resetting, timeout=1)

    assert [turn.content for turn in runtime.orchestrator.context.turns if turn.role == "user"] == [
        "断流不能抢先删除话轮"
    ]
    assert context.projection.provisional is None
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_finalize_failure_waits_for_inflight_turn_prepare() -> None:
    identity = SessionIdentity("finalize-failure-during-prepare")
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

        async def finalize_speech_segment(
            self,
            _identity: SessionIdentity,
        ) -> Sequence[ASRResult]:
            raise RuntimeError("task rotation failed")

    provider = BlockingPreparingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    context = await registry.open_session(identity)
    assert context.asr.record_audio(start_sample=0, frame_samples=320)
    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=1,
        segment_id="finalize-failure-blocking-prepare",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=0,
        capture_end_sample=320,
        text="终结失败不能抢先删除话轮",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)
    context.pending.turn_start_sample = 0
    context.pending.turn_end_sample = 320
    context.pending.turn_endpoint_sample = 320
    context.pending.turn_retire_sample = 320

    committing = asyncio.create_task(registry._commit_pending_turn(context))
    await asyncio.wait_for(prepare_started.wait(), timeout=1)
    finalizing = asyncio.create_task(registry._audio_ingress.finalize_speech_segment(context))
    await asyncio.sleep(0)

    assert not finalizing.done()
    assert context.projection.provisional is not None

    release_prepare.set()
    await asyncio.wait_for(committing, timeout=1)
    assert await asyncio.wait_for(finalizing, timeout=1) is False

    assert [turn.content for turn in runtime.orchestrator.context.turns if turn.role == "user"] == [
        "终结失败不能抢先删除话轮"
    ]
    assert context.projection.provisional is None
    assert context.ingress.provider_failed is True
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
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
async def test_vad_empty_provisional_resyncs_timeline_asr_instead_of_text_mismatch() -> None:
    identity = SessionIdentity("weather-projection-resync", stream_epoch=1)
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    context = await registry.open_session(identity)
    vad = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=1,
        provider_task_epoch=2,
        segment_id="weather-vad",
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=320_000,
        capture_end_sample=663_360,
        text="",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(vad)
    await registry._apply_projection_segment(context, vad)
    assert context.projection.provisional is not None
    assert context.projection.provisional.text == ""
    asr = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=1,
        provider_task_epoch=2,
        segment_id="weather-asr",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=320_000,
        capture_end_sample=648_960,
        text="南京今天天气怎么样",
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(asr)
    assert context.projection.provisional.text == ""

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=1,
        start_sample=320_000,
        end_sample=663_360,
    )

    assert fence is not None
    assert reason is None
    assert context.runtime.orchestrator.context.turns[-1].content == "南京今天天气怎么样"
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_prepare_failure_automatically_retries_and_commits_once() -> None:
    identity = SessionIdentity("prepare-failure-auto-retry")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class FailOncePreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0
            self.reply_calls = 0
            self.reply_started = asyncio.Event()

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            if self.prepare_calls == 1:
                raise RuntimeError("transient preparation failure")
            return await runtime.on_turn_committed(text, input_modality="audio")

        def generate_reply(
            self,
            identity: SessionIdentity,
            user_text: str,
            fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            self.reply_calls += 1
            self.reply_started.set()
            return super().generate_reply(identity, user_text, fence)

    provider = FailOncePreparingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=_CapturingMediaBridge(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
    )
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="自动重试后只提交一次",
    )

    assert await registry._commit_pending_turn(context) == "provider_prepare_failed"
    retry_task = context.pending.turn_commit_retry_task
    assert retry_task is not None

    await asyncio.wait_for(provider.reply_started.wait(), timeout=1)
    await asyncio.wait_for(retry_task, timeout=1)

    assert provider.prepare_calls == 2
    assert provider.reply_calls == 1
    assert [turn.content for turn in runtime.orchestrator.context.turns if turn.role == "user"] == [
        "自动重试后只提交一次"
    ]
    assert context.projection.provisional is None
    assert context.runtime.speech_timeline.committed_sample == 640
    assert context.asr.last_committed_sample == 640
    assert context.pending.turn_commit_retry_task is None
    assert context.pending.turn_commit_retry_attempt == 0
    assert context.pending.turn_commit_retry_stream_epoch is None
    assert context.pending.turn_commit_retry_endpoint_sample is None
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "scheduled"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "attempt"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "succeeded"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "exhausted"}) == 0
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_endpoint_tail_waits_for_matching_prepare_retry() -> None:
    identity = SessionIdentity("endpoint-tail-waits-for-prepare-retry")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class BlockingRetryProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0
            self.retry_started = asyncio.Event()
            self.release_retry = asyncio.Event()

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            if self.prepare_calls == 1:
                raise RuntimeError("transient preparation failure")
            self.retry_started.set()
            await self.release_retry.wait()
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = BlockingRetryProvider()
    bridge = _CapturingMediaBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
    )
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="尾超时必须等同一轮重试",
    )

    assert await registry._commit_pending_turn(context) == "provider_prepare_failed"
    retry_task = context.pending.turn_commit_retry_task
    assert retry_task is not None
    await asyncio.wait_for(provider.retry_started.wait(), timeout=1)

    expiring = asyncio.create_task(
        registry._expire_endpoint_tail(identity.session_id, identity.stream_epoch, 600)
    )
    await asyncio.sleep(0)
    assert not expiring.done()
    assert context.projection.provisional is not None

    provider.release_retry.set()
    await asyncio.wait_for(retry_task, timeout=1)
    await asyncio.wait_for(expiring, timeout=1)

    assert provider.prepare_calls == 2
    assert context.projection.provisional is None
    assert context.asr.last_committed_sample == 640
    assert not [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
        and payload.get("reason") == "provider_final_missing"
    ]
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "succeeded"}) == 1
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_new_vad_supersedes_retry_without_merging_turns() -> None:
    identity = SessionIdentity("new-vad-supersedes-prepare-retry")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class AlwaysFailingPreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            _text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            raise RuntimeError("preparation unavailable")

    provider = AlwaysFailingPreparingProvider()
    bridge = _CapturingMediaBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
    )
    session = bridge.bridge.open(identity)
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="旧话轮不能并入新话轮",
    )

    assert await registry._commit_pending_turn(context) == "provider_prepare_failed"
    first_started = [
        payload for event_type, payload in bridge.events if event_type == "turn.provisional.started"
    ]
    assert len(first_started) == 1

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="next-valid-vad",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=640,
            capture_end_sample=641,
        ),
    )

    started = [
        payload for event_type, payload in bridge.events if event_type == "turn.provisional.started"
    ]
    discarded = [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
    ]
    assert len(started) == 2
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "provider_prepare_retry_superseded_by_new_vad"
    assert discarded[0]["provisional_id"] == first_started[0]["provisional_id"]
    assert started[1]["provisional_id"] != first_started[0]["provisional_id"]
    assert context.projection.provisional is not None
    assert context.projection.provisional.provisional_id == started[1]["provisional_id"]
    assert context.asr.last_committed_sample == 640
    assert context.runtime.speech_timeline.committed_sample == 640
    assert [item.segment_id for item in context.runtime.speech_timeline.pending] == [
        "next-valid-vad"
    ]
    assert context.pending.turn_commit_retry_task is None
    assert context.pending.turn_commit_retry_attempt == 0
    assert context.pending.turn_commit_retry_stream_epoch is None
    assert context.pending.turn_commit_retry_endpoint_sample is None
    assert provider.prepare_calls == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "superseded"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "attempt"}) == 0
    assert not await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="late-hangover-after-supersede",
            revision=1,
            capture_start_sample=600,
            capture_end_sample=640,
            text="尾静音迟到文本",
            is_final=True,
            stream_epoch=identity.stream_epoch,
        ),
    )
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_stale_or_duplicate_vad_does_not_supersede_matching_prepare_retry() -> None:
    identity = SessionIdentity("stale-vad-keeps-prepare-retry")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class FailOncePreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            if self.prepare_calls == 1:
                raise RuntimeError("transient preparation failure")
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = FailOncePreparingProvider()
    bridge = _CapturingMediaBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
    )
    session = bridge.bridge.open(identity)
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="过期 VAD 不能丢掉合法重试",
    )
    replayed_vad = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=0,
        segment_id="replayed-next-vad",
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=640,
        capture_end_sample=641,
    )
    assert context.runtime.ingest_media_speech_segment(replayed_vad)

    assert await registry._commit_pending_turn(context) == "provider_prepare_failed"
    retry_task = context.pending.turn_commit_retry_task
    assert retry_task is not None
    provisional = context.projection.provisional
    assert provisional is not None

    await registry.on_speech_segment(session, replayed_vad)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch + 1,
            provider_task_epoch=0,
            segment_id="stale-next-vad",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=640,
            capture_end_sample=641,
        ),
    )

    assert context.pending.turn_commit_retry_task is retry_task
    assert context.projection.provisional is provisional
    assert not [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
    ]
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "superseded"}) == 0

    await asyncio.wait_for(retry_task, timeout=1)
    assert provider.prepare_calls == 2
    assert context.projection.provisional is None
    assert context.asr.last_committed_sample == 640
    assert context.pending.turn_commit_retry_task is None
    session.close()
    await registry.on_session_closed(session)


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
    context = await registry.open_session(identity)
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
    context = await registry.open_session(identity)
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
    await registry.open_session(reconnected)
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_audio_ingress_serializes_duplicate_finalize_watermark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.voice_core.media_audio_ingress")

    class FinalizeProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_calls = 0
            self.finalize_started = asyncio.Event()
            self.release_finalize = asyncio.Event()

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            return ()

        async def finalize_speech_segment(
            self,
            _identity: SessionIdentity,
        ) -> Sequence[ASRResult]:
            self.finalize_calls += 1
            self.finalize_started.set()
            await self.release_finalize.wait()
            return ()

    provider = FinalizeProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("duplicate-vad-final", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))

    first = asyncio.create_task(registry._audio_ingress.finalize_speech_segment(context))
    await asyncio.wait_for(provider.finalize_started.wait(), timeout=1)
    second = asyncio.create_task(registry._audio_ingress.finalize_speech_segment(context))
    provider.release_finalize.set()

    assert await asyncio.gather(first, second) == [True, True]
    assert provider.finalize_calls == 1
    assert context.ingress.last_finalized_audio_watermark == 2
    boundary_logs = [
        json.loads(record.message.removeprefix("media_asr_boundary "))
        for record in caplog.records
        if record.message.startswith("media_asr_boundary ")
    ]
    assert [entry["result"] for entry in boundary_logs] == ["success", "duplicate"]
    assert boundary_logs[1]["audio_admitted_watermark"] == 2
    assert boundary_logs[1]["previous_finalized_watermark"] == 2
    await context.runtime.close()


@pytest.mark.asyncio
async def test_commit_pauses_provider_asr_when_playback_starts() -> None:
    """Committing a turn starts playback, which must close the ASR task.

    Half-duplex playback stops uplink capture, so leaving the provider task
    open would starve its feed and trip the 23-second idle timeout.
    """

    identity = SessionIdentity(
        "commit-pauses-asr",
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
    open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    context = await _seed_pending_media_turn(registry, identity, text="你好")

    async def classify_owner(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _verified_owner_decision()

    context.runtime.set_speaker_classifier(classify_owner, sample_rate=16_000)
    context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)
    context.runtime.set_target_speaker_focus(True)

    assert provider.pause_asr_calls == []

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=600,
        retire_sample=640,
    )

    assert fence is not None, f"turn did not commit: {reason}"
    assert context.output.playback.current_fence == fence
    assert provider.pause_asr_calls == [identity.stream_epoch]
    await context.runtime.close()


@pytest.mark.asyncio
async def test_stale_asr_final_rejection_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.voice_core.media_session_commit")

    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("stale-final-rejection-log", stream_epoch=2)
    session = bridge.bridge.open(identity)
    await registry.open_session(identity)

    accepted = await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="stale-stream-epoch-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=600,
            text="旧纪元文本",
            is_final=True,
            stream_epoch=1,
        ),
    )

    assert not accepted
    rejected = [
        record
        for record in caplog.records
        if record.message.startswith("media ASR result rejected")
    ]
    assert len(rejected) == 1
    assert rejected[0].levelno == logging.WARNING
    assert "stage=preview" in rejected[0].message
    assert "reason=stale_stream_epoch" in rejected[0].message
    assert "is_final=True" in rejected[0].message
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_audio_ingress_queues_new_audio_after_finalize_boundary() -> None:
    class BlockingFinalizeProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_started = asyncio.Event()
            self.release_finalize = asyncio.Event()

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            return ()

        async def finalize_speech_segment(
            self,
            _identity: SessionIdentity,
        ) -> Sequence[ASRResult]:
            self.finalize_started.set()
            await self.release_finalize.wait()
            return ()

    provider = BlockingFinalizeProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("audio-after-finalize", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))

    finalize = asyncio.create_task(registry._audio_ingress.finalize_speech_segment(context))
    await asyncio.wait_for(provider.finalize_started.wait(), timeout=1)
    next_audio = asyncio.create_task(
        registry.on_audio_frame(
            session,
            AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2),
        )
    )
    await asyncio.sleep(0)

    assert next_audio.done() is False
    assert context.asr.last_sent_sample == 2
    assert provider.audio_calls == [0]

    provider.release_finalize.set()
    assert await asyncio.wait_for(finalize, timeout=1) is True
    await asyncio.wait_for(next_audio, timeout=1)
    for _ in range(20):
        if provider.audio_calls == [0, 1]:
            break
        await asyncio.sleep(0)

    assert context.ingress.last_finalized_audio_watermark == 2
    assert context.asr.last_sent_sample == 4
    assert provider.audio_calls == [0, 1]
    await context.runtime.close()


@pytest.mark.asyncio
async def test_finalize_publishes_watermark_before_rechecking_pending_endpoint() -> None:
    class FinalizeFinalProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            _frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            return ()

        async def finalize_speech_segment(
            self,
            _identity: SessionIdentity,
        ) -> Sequence[ASRResult]:
            return (
                ASRResult(
                    task_epoch=1,
                    sentence_id="finalize-final",
                    revision=1,
                    capture_start_sample=0,
                    capture_end_sample=320,
                    text="终稿覆盖",
                    is_final=True,
                    stream_epoch=1,
                ),
            )

    provider = FinalizeFinalProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0,
        turn_endpoint_absolute_timeout_s=0.03,
    )
    registry.install()
    identity = SessionIdentity("finalize-watermark-recheck")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="finalize-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    context = await registry.open_session(identity)
    vad_end = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=1,
        provider_task_epoch=0,
        segment_id="finalize-end",
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=640,
        capture_end_sample=641,
        final=True,
        voiced_end_sample=640,
    )
    assert context.runtime.ingest_media_speech_segment(vad_end)
    await registry._apply_projection_segment(context, vad_end)
    context.asr.last_sent_sample = 640
    context.pending.turn_start_sample = 0
    context.pending.turn_endpoint_sample = 640
    context.pending.turn_retire_sample = 640

    assert await registry._audio_ingress.finalize_speech_segment(context)
    await asyncio.sleep(0.05)

    assert context.ingress.last_finalized_audio_watermark == 640
    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["终稿覆盖"]
    assert context.asr.last_committed_sample == 640
    await context.runtime.close()


@pytest.mark.asyncio
async def test_vad_finalize_failure_does_not_escape_media_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.voice_core.media_audio_ingress")

    class FailingFinalizeProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            return ()

        async def finalize_speech_segment(
            self,
            _identity: SessionIdentity,
        ) -> Sequence[ASRResult]:
            raise RuntimeError("task rotation failed")

    provider = FailingFinalizeProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("vad-finalize-failure", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="failed-vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=2,
            capture_end_sample=3,
            final=True,
            voiced_end_sample=2,
        ),
    )

    assert context.ingress.provider_failed is True
    assert context.ingress.discontinuity_pending is True
    assert context.pending.turn_endpoint_sample is None
    assert registry.metrics.get("media_sessions_failed_total") >= 1
    boundary_logs = [
        json.loads(record.message.removeprefix("media_asr_boundary "))
        for record in caplog.records
        if record.message.startswith("media_asr_boundary ")
    ]
    assert len(boundary_logs) == 1
    assert boundary_logs[0]["result"] == "failed"
    assert boundary_logs[0]["audio_admitted_watermark"] == 2
    assert boundary_logs[0]["previous_finalized_watermark"] == -1
    assert boundary_logs[0]["provider_pcm_samples"] == 0
    await context.runtime.close()


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
    context = await registry.open_session(identity)

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
async def test_pending_turn_records_skipped_reply_dispatch_reason() -> None:
    identity = SessionIdentity("pending-turn-skipped-output")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=_CapturingMediaBridge(),
        session_factory=lambda _identity: MediaSessionResources(runtime, FakeMediaProvider()),
        metrics=metrics,
    )
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="这轮必须留下跳过原因",
    )

    async def skipped_dispatch(
        _session_id: str,
        _user_text: str,
        fence: GenerationFence,
    ) -> OutputDispatchResult:
        return OutputDispatchResult(
            fence,
            OutputDispatchStatus.SKIPPED,
            "output_intent_inactive",
        )

    registry._dispatch_reply = skipped_dispatch  # type: ignore[method-assign]

    assert await registry._commit_pending_turn(context) is None
    await _wait_until(lambda: bool(context.output.output_results))

    terminal = context.output.output_results[-1]
    assert terminal.status is OutputDispatchStatus.SKIPPED
    assert terminal.reason == "output_intent_inactive"
    assert terminal.emitted_audio is False
    assert (
        metrics.get(
            "voice_output_dispatch_total",
            {"status": "skipped", "reason": "output_intent_inactive"},
        )
        == 1
    )
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)
    await asyncio.sleep(0)

    assert context.output.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.interaction_phase.value == "listening"
    assert "listening" in bridge.states
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_stalled_output_generation_times_out_and_discards_partial_playback() -> None:
    second_read_started = asyncio.Event()
    cancellation_started = asyncio.Event()
    cancellation_finished = asyncio.Event()

    class StalledProvider(FakeMediaProvider):
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
                    text="部分",
                    first=True,
                    final=False,
                )
                second_read_started.set()
                await asyncio.Event().wait()

            return chunks()

        async def cancel_generation(self, fence: GenerationFence) -> None:
            self.cancelled.append(fence)
            cancellation_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancellation_finished.set()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.effects: list[tuple[int, GenerationFence, dict[str, object]]] = []

        async def emit_realtime_effect(
            self,
            _session_id: str,
            effect_kind: int,
            fence: GenerationFence,
            *,
            payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.effects.append((effect_kind, fence, payload))
            return True

        async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
            return True

    provider = StalledProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        output_generation_timeout_s=0.02,
    )
    registry.install()
    identity = SessionIdentity("stalled-output-generation")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)

    reply = asyncio.create_task(
        registry.generate_reply(identity.session_id, "你好", fence),
        name="test-stalled-output-generation",
    )
    await asyncio.wait_for(second_read_started.wait(), timeout=1)
    assert context.output.output_owner is not None
    result = await asyncio.wait_for(reply, timeout=1)

    assert result is False
    assert cancellation_started.is_set()
    await asyncio.wait_for(cancellation_finished.wait(), timeout=1)
    assert provider.cancelled == [fence]
    assert context.output.output_owner is None
    assert context.output.reply_task is None
    assert context.output.output_dispatch_task is None
    assert context.output.output_work == {}
    assert context.output.playback.current_fence is None
    assert context.output.playback.actual_heard_text(fence) == ""
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    cancelled = context.runtime.fence
    assert cancelled.generation_id == fence.generation_id + 1
    cancel_effects = [
        effect
        for effect in bridge.effects
        if effect[0] == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
    ]
    assert cancel_effects == [
        (
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            cancelled,
            {"reason": "output_timeout"},
        )
    ]

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
    assert context.output.playback.actual_heard_text(fence) == ""
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name() == "test-stalled-output-generation"
    ]
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_registry_lets_the_shared_agent_prepare_a_committed_turn() -> None:
    identity = SessionIdentity("prepared-media-turn")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.committed_context_versions: list[int] = []

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
    await registry.open_session(identity)
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
    assert bridge.committed_context_versions == [1]
    await runtime.close()


@pytest.mark.asyncio
async def test_tts_failure_before_first_frame_returns_device_session_to_listening() -> None:
    """Regression for epoch 1375: Doubao handed back a server-expired socket.

    The reply raised before its first frame and nothing released the floor, so
    the runtime stayed in THINKING and dropped every later ``vad.end`` as
    playback echo. The board could not be heard again for the rest of the
    session, and owner-silence never closed it because that timer only runs
    while listening.
    """

    class FailingOutputProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.attempted = asyncio.Event()

        def generate_output(
            self,
            _identity: SessionIdentity,
            intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = (intent, work_id, source_start_sample)

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                self.attempted.set()
                raise RuntimeError("Doubao TTS connection closed before first audio")
                yield  # pragma: no cover - unreachable async-generator marker

            return chunks()

    provider = FailingOutputProvider()
    bridge = _CapturingGenerationBridge()

    def runtime_factory(session_id: str) -> DuplexRuntime:
        return DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)

    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=runtime_factory,
    )
    registry.install()
    identity = _device_identity("device-tts-failed-before-first-frame")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.attempted.wait(), timeout=1)
        await _wait_until(
            lambda: (
                context.runtime.orchestrator.state is ConversationState.LISTENING
                and context.runtime.assistant_speaking is False
            )
        )
        # Without an open input gate the next endpoint is discarded as echo.
        assert context.runtime.playback_overlap_input_blocked() is False
        assert context.runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
    finally:
        await registry.finalize_session(identity.session_id)
