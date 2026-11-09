"""Media session: VAD boundaries, endpoint tails and clock-skew commits."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
    LIVE_LOOKUP_FILLER,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
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
    ProviderAudioTaskSnapshot,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    MultiFinalProvider,
    _AckCapturingProvider,
    _bind_verified_owner_classifier,
    _CapturingGenerationBridge,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _seed_pending_media_turn,
    _verified_owner_decision,
    _wait_until,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


@pytest.mark.asyncio
async def test_duplicate_media_turn_is_skipped_while_its_reply_is_in_flight() -> None:
    """epoch 1900: 同一句的重复 final 又开一轮，把正在播的提示音掐断。

    The second commit covered a contiguous extension of the same question while
    its acknowledgement was still audible.  Opening that turn released the first
    delegation and cancelled the cue, so the user heard a truncated cue then
    2.26s of silence.  Identical text while the reply is in flight is a repeat.
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

    question = "今天南京天气怎么样"
    provider = CueProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("duplicate-media-turn-skipped")
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
        assert registry._reply_in_flight(context)
        duplicate_start = context.runtime.speech_timeline.committed_sample
        duplicate_segment = SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=2,
            segment_id="duplicate-final",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=duplicate_start,
            capture_end_sample=1200,
            text=question,
            final=True,
        )
        assert context.runtime.ingest_media_speech_segment(duplicate_segment)
        await registry._apply_projection_segment(context, duplicate_segment)
        context.pending.turn_start_sample = duplicate_start
        context.pending.turn_end_sample = 1200
        context.pending.turn_endpoint_sample = 1200
        context.pending.turn_retire_sample = 1240
        repeat_fence, repeat_reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=duplicate_start,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_fence is None
        assert repeat_reason == "duplicate_media_turn"
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_scheduled_empty_output_returns_to_listening() -> None:
    """An output task must not mistake itself for a queued successor."""

    class EmptyProvider(_AckCapturingProvider):
        def generate_output(self, *args: Any, **kwargs: Any) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                for chunk in ():
                    yield chunk

            return chunks()

    provider = EmptyProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("scheduled-empty-output")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    try:
        fence = await context.runtime.on_turn_committed("你好")
        context.output.playback.start(fence)
        coordinator = context.runtime.orchestrator.delegation
        now_ms = int(time.time() * 1_000)
        intent = coordinator.bridge_acknowledgement(
            BRIDGE_PHRASES[0], fence=fence,
            context_version=coordinator.current_context_version(identity.session_id),
            expires_at_ms=now_ms + 10_000, now_ms=now_ms,
        )
        coordinator.admit_output_intent(
            intent, current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True, now_ms=now_ms,
        )
        assert await registry._enqueue_output_work(context, _OutputWork(intent, fence))
        await _wait_until(lambda: bool(context.output.output_results))
        assert context.output.output_results[-1].reason == "provider_completed_without_audio"
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert context.output.output_owner is None
        assert context.output.output_dispatch_task is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_answer_still_delivered_after_same_turn_generation_bump() -> None:
    """同 turn 辅助输出 bump 代际后，答案不得被判过期丢掉。

    Regression for the 2026-09-12 weather stall (formerly triggered by the
    second cue). The second cue is gone, but same-turn generation tolerance
    must still re-fence the deep result when an auxiliary bump advances the
    runtime without a new user turn.
    """

    class AnswerProvider(_LateOwnedDelegationProvider):
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

    provider = AnswerProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("same-turn-generation-bump-answer")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        fence = await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        bumped = await context.runtime.begin_media_auxiliary_output(fence)
        assert bumped is not None
        assert bumped.generation_id == fence.generation_id + 1
        assert bumped.turn_id == fence.turn_id
        provider.release.set()
        await asyncio.sleep(0.3)
        if context.output.output_owner is not None and not provider.deep_started.is_set():
            await _finish_output_owner_playback(registry, identity, bridge, session)
        await asyncio.wait_for(provider.deep_started.wait(), timeout=4)
        assert provider.output_kinds == [
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
            media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
        ]
        assert LIVE_LOOKUP_FILLER in provider.output_texts
        claim = context.output.delegation_output_claims.get(fence)
        assert claim is not None
        assert claim.state is DelegationOutputState.COMPLETED
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


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
async def test_vad_boundary_drains_audio_before_rotating_provider_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.voice_core.media_audio_ingress")

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

        @property
        def current_asr_audio_task_snapshot(self) -> ProviderAudioTaskSnapshot:
            return ProviderAudioTaskSnapshot(
                task_epoch=self.task_epoch,
                task_sample_origin=0,
                audio_start_sample=0,
                audio_end_sample=320,
                send_count=1,
            )

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

    context = registry.session_state(identity.session_id)
    assert provider.finalize_called is True
    assert context.pending.turn_end_sample == 320
    assert context.asr.latest_authoritative_task_epoch == 2
    assert any(segment.text == "今天星期几" for segment in context.runtime.speech_timeline.pending)
    boundary_logs = [
        record.message.removeprefix("media_asr_boundary ")
        for record in caplog.records
        if record.message.startswith("media_asr_boundary ")
    ]
    assert len(boundary_logs) == 1
    boundary = json.loads(boundary_logs[0])
    assert boundary == {
        "audio_admitted_watermark": 320,
        "finalize_reason": "vad_end",
        "next_task_pending": False,
        "previous_finalized_watermark": -1,
        "provider_pcm_all_zero": None,
        "provider_pcm_channels": 1,
        "provider_pcm_clipping_detected": None,
        "provider_pcm_encoding": "pcm_s16le",
        "provider_pcm_end_sample": 320,
        "provider_pcm_observed_samples": 0,
        "provider_pcm_peak_abs": None,
        "provider_pcm_rms": None,
        "provider_pcm_sample_rate_hz": 16000,
        "provider_pcm_samples": 320,
        "provider_pcm_send_count": 1,
        "provider_pcm_start_sample": 0,
        "provider_task_epoch_after": 2,
        "provider_task_epoch_before": 1,
        "provider_task_origin_sample": 0,
        "result": "success",
        "rotation_observed": True,
        "segment_samples": 320,
        "session_id": identity.session_id,
        "stream_epoch": 1,
        "vad_event_sample": 320,
        "vad_start_sample": 0,
        "voiced_end_sample": 320,
    }
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

    context = registry.session_state(identity.session_id)
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
        registry.session_state(identity.session_id),
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

    context = registry.session_state(identity.session_id)
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

    context = registry.session_state(identity.session_id)
    assert context.pending.turn_endpoint_sample is None
    assert context.pending.turn_start_sample is None
    assert context.projection.provisional is None
    assert context.asr.last_committed_sample == 640
    assert context.runtime.speech_timeline.committed_sample == 640
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []


@pytest.mark.asyncio
async def test_tail_timeout_fences_late_final_and_rotates_projection_identity() -> None:
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

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60,
        turn_endpoint_absolute_timeout_s=60,
    )
    identity = SessionIdentity("late-final-after-tail-timeout")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="timed-out-start",
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
            provider_task_epoch=0,
            segment_id="timed-out-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=640,
            capture_end_sample=641,
            final=True,
            voiced_end_sample=600,
        ),
    )
    context = registry.session_state(identity.session_id)
    endpoint_task = context.turn_endpoint_task
    assert endpoint_task is not None
    endpoint_task.cancel()
    await asyncio.gather(endpoint_task, return_exceptions=True)
    timeout_handle = context.pending.turn_endpoint_timeout_handle
    assert timeout_handle is not None
    timeout_handle.cancel()
    context.pending.turn_endpoint_timeout_handle = None
    first_started = [
        payload for event_type, payload in bridge.events if event_type == "turn.provisional.started"
    ]
    assert len(first_started) == 1

    await registry._expire_endpoint_tail(
        identity.session_id,
        identity.stream_epoch,
        600,
    )

    discarded = [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
    ]
    assert len(discarded) == 1
    assert discarded[0]["provisional_id"] == first_started[0]["provisional_id"]
    assert discarded[0]["reason"] == "provider_final_missing"
    before_late_fence = context.runtime.fence
    assert not await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="late-final",
            revision=1,
            capture_start_sample=600,
            capture_end_sample=640,
            text="尾静音迟到终稿不能提交",
            is_final=True,
            stream_epoch=identity.stream_epoch,
        ),
    )
    assert context.runtime.fence == before_late_fence
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="next-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=641,
            capture_end_sample=642,
        ),
    )
    started = [
        payload for event_type, payload in bridge.events if event_type == "turn.provisional.started"
    ]
    assert len(started) == 2
    assert started[1]["provisional_id"] != started[0]["provisional_id"]
    assert int(started[1]["projection_revision"]) > int(discarded[0]["projection_revision"])
    await context.runtime.close()
    await provider.close(identity)


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

    context = registry.session_state(identity.session_id)
    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["南京天气"]
    committed = [
        payload for event_type, payload in bridge.client_events if event_type == "turn.committed"
    ]
    assert committed and committed[-1]["provider_final_missing"] is True
    assert context.pending.pending_partial is None


async def _turn_with_partial_then_vad_end(
    registry: MediaVoiceCoreRegistry, session: Any, identity: SessionIdentity
) -> None:
    """The partial ends 4 s before the device VAD end (t023: the device ended 4.2 s after the child), so
    the partial does not cover the endpoint and the commit waits for a covering final."""

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="late-final-start",
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
            sentence_id="late-final-sentence",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=600,
            text="有同学总是欺负我",
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
            segment_id="late-final-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=64_600,
            capture_end_sample=64_601,
            final=True,
            voiced_end_sample=64_600,
        ),
    )


def _late_final_registry(fallback_s: float) -> MediaVoiceCoreRegistry:
    return MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
        turn_endpoint_grace_s=0.001,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=60,
        turn_final_missing_fallback_s=fallback_s,
    )


@pytest.mark.asyncio
async def test_a_partial_short_of_a_late_vad_end_commits_at_the_soft_tail() -> None:
    """Device soak 2026-11-01 t023: the device VAD ended 4.2 s after the child, ASR never covered it, the turn
    waited for the 6 s hard tail, its commit started there, and the session closed with the sentence
    unanswered."""

    registry = _late_final_registry(0.02)
    identity = SessionIdentity("late-provider-final")
    session = registry.bridge.bridge.open(identity)
    await _turn_with_partial_then_vad_end(registry, session, identity)

    await asyncio.sleep(0.06)

    context = registry.session_state(identity.session_id)
    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["有同学总是欺负我"]
    assert context.pending.pending_partial is None


@pytest.mark.asyncio
async def test_a_final_short_of_a_late_vad_end_commits_at_the_soft_tail_with_its_own_text() -> None:
    registry = _late_final_registry(0.05)
    identity = SessionIdentity("final-in-time")
    session = registry.bridge.bridge.open(identity)
    await _turn_with_partial_then_vad_end(registry, session, identity)
    final = ASRResult(
        task_epoch=1,
        sentence_id="late-final-sentence",
        revision=2,
        capture_start_sample=0,
        capture_end_sample=600,
        text="有同学总是欺负我，我不想去学校了",
        is_final=True,
        confidence=0.9,
        stream_epoch=1,
    )
    context = registry.session_state(identity.session_id)
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)

    await asyncio.sleep(0.1)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["有同学总是欺负我，我不想去学校了"]


@pytest.mark.asyncio
async def test_words_after_a_noise_endpoint_commit_without_waiting_for_its_tail() -> None:
    """Device soak 2026-11-01 t007: 0.2 s of noise right after a reply pinned the endpoint; the child's
    question came after it with no new device VAD.  The endpoint commit found no words, and the turn sat
    until the noise's 6 s tail, where the session closed under the question's commit."""

    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
        turn_endpoint_grace_s=0.001,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=60,
        turn_final_missing_fallback_s=0,
    )
    identity = SessionIdentity("words-after-noise-endpoint")
    session = registry.bridge.bridge.open(identity)
    for segment_id, start, final in (("noise-start", 0, False), ("noise-end", 3_200, True)):
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=start,
                capture_end_sample=start + 1,
                final=final,
                voiced_end_sample=start if final else None,
            ),
        )
    question = ASRResult(
        task_epoch=1,
        sentence_id="question-after-noise",
        revision=1,
        capture_start_sample=36_000,
        capture_end_sample=69_600,
        text="恐龙都吃什么东西",
        is_final=True,
        confidence=0.9,
        stream_epoch=1,
    )
    context = registry.session_state(identity.session_id)
    assert await registry.accept_asr_result(identity.session_id, question)
    registry._observe_final_asr_result(context, question)

    await asyncio.sleep(0.1)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["恐龙都吃什么东西"]


async def _noise_endpoint_then_words(
    words_end: int,
) -> tuple[MediaVoiceCoreRegistry, Any, float]:
    """Noise pins the endpoint at sample 3200; 60 ms later ASR reports words ending at ``words_end``."""

    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
        turn_endpoint_grace_s=60,
        turn_endpoint_min_grace_s=60,
        turn_endpoint_max_grace_s=60,
        turn_endpoint_absolute_timeout_s=61,
        turn_final_missing_fallback_s=30,
    )
    identity = SessionIdentity(f"words-past-noise-endpoint-{words_end}")
    session = registry.bridge.bridge.open(identity)
    for segment_id, start, final in (("noise-start", 0, False), ("noise-end", 3_200, True)):
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=start,
                capture_end_sample=start + 1,
                final=final,
                voiced_end_sample=start if final else None,
            ),
        )
    context = registry.session_state(identity.session_id)
    first_deadline = context.pending.turn_endpoint_tail_deadline
    assert first_deadline is not None
    await asyncio.sleep(0.06)
    assert await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="still-talking",
            revision=1,
            capture_start_sample=8_000,
            capture_end_sample=words_end,
            text="有同学总是欺负我",
            is_final=False,
            confidence=0.9,
            stream_epoch=1,
        ),
    )
    return registry, context, first_deadline


@pytest.mark.asyncio
async def test_words_past_a_noise_endpoint_restart_its_absolute_tail() -> None:
    """Device soak 2026-11-01 t023: echo pinned the endpoint, the child talked on with no new device VAD,
    and the 6 s tail of the echo's endpoint put the device on standby under the child's own commit."""

    registry, context, first_deadline = await _noise_endpoint_then_words(3_200 + 32_000)

    restarted = context.pending.turn_endpoint_tail_deadline or 0
    assert restarted >= first_deadline + 0.05, "the tail now counts from the child's words"
    soft = context.pending.final_missing_fallback_handle
    assert soft is not None and soft.when() >= asyncio.get_running_loop().time() + 29.9, (
        "the soft tail counts from the child's words too"
    )
    assert context.pending.turn_endpoint_sample == 3_200
    assert not context.standby_requested
    await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_words_inside_the_coverage_tolerance_keep_the_tail() -> None:
    registry, context, first_deadline = await _noise_endpoint_then_words(3_200 + 8_000)

    assert context.pending.turn_endpoint_tail_deadline == first_deadline
    await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_endpoint_tail_rescues_partial_when_device_vad_end_trails_the_last_word() -> None:
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
    identity = SessionIdentity("late-device-vad-end")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="late-end-start",
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
            sentence_id="late-end-sentence",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=30_000,
            text="讲一个很短的笑话吧",
            is_final=False,
            confidence=0.9,
            stream_epoch=1,
        ),
    )
    # soak-150810 t014: the device VAD end lands 55 968 samples (3.5 s) after the last
    # transcribed word, so no final ever covers the endpoint and the partial is the only text.
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="late-end-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=85_968,
            capture_end_sample=85_969,
            final=True,
            voiced_end_sample=85_968,
        ),
    )

    await asyncio.sleep(0.04)

    context = registry.session_state(identity.session_id)
    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["讲一个很短的笑话吧"]
    committed = [
        payload for event_type, payload in bridge.client_events if event_type == "turn.committed"
    ]
    assert committed and committed[-1]["provider_final_missing"] is True
    assert context.pending.pending_partial is None
    await context.runtime.close()


@pytest.mark.asyncio
async def test_endpoint_tail_keeps_farther_partial_when_final_timing_shrinks() -> None:
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
    identity = SessionIdentity("shrinking-final-timing")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="shrinking-final-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    partial = ASRResult(
        task_epoch=1,
        sentence_id="shrinking-final-sentence",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=40_000,
        text="今天星期几",
        is_final=False,
        confidence=0.9,
        stream_epoch=1,
    )
    final = replace(
        partial,
        revision=2,
        capture_end_sample=10_000,
        is_final=True,
    )
    assert await registry.accept_asr_result(identity.session_id, partial)
    assert await registry.accept_asr_result(identity.session_id, final)
    context = registry.session_state(identity.session_id)
    assert context.pending.pending_partial is not None
    assert context.pending.pending_partial.capture_end_sample == 40_000
    assert context.pending.pending_partial.revision == 2

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="shrinking-final-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=40_000,
            capture_end_sample=40_001,
            final=True,
            voiced_end_sample=40_000,
        ),
    )
    await asyncio.sleep(0.04)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["今天星期几"]
    committed = [
        payload for event_type, payload in bridge.client_events if event_type == "turn.committed"
    ]
    assert committed and committed[-1]["provider_final_missing"] is True
    assert context.pending.pending_partial is None
    await context.runtime.close()


@pytest.mark.asyncio
async def test_vad_endpoint_accepts_final_within_bounded_clock_skew() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.001,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=0.03,
    )
    registry.install()
    identity = SessionIdentity("bounded-endpoint-clock-skew")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="bounded-skew-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    final = ASRResult(
        task_epoch=1,
        sentence_id="bounded-skew-final",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=16_000,
        text="今天星期几",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    # A bounded skew is only safe after the provider task-finished boundary;
    # otherwise a later sentence from the same task may still arrive.
    # Production epoch 921 observed an 18,240-sample (1.14 s) gap between
    # FunASR's final word and the firmware-adjusted voiced end.
    context.ingress.last_finalized_audio_watermark = 34_240
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="bounded-skew-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=34_240,
            capture_end_sample=34_241,
            final=True,
            voiced_end_sample=34_240,
        ),
    )

    await asyncio.sleep(0.02)

    assert [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == ["今天星期几"]
    assert context.asr.last_committed_sample == 34_240
    await context.runtime.close()


@pytest.mark.asyncio
async def test_vad_endpoint_does_not_commit_a_final_beyond_bounded_clock_skew() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.001,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=0.03,
    )
    registry.install()
    identity = SessionIdentity("excessive-endpoint-clock-skew")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="excessive-skew-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    final = ASRResult(
        task_epoch=1,
        sentence_id="excessive-skew-final",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=16_000,
        text="过早终稿",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    context.ingress.last_finalized_audio_watermark = 40_001
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="excessive-skew-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=40_001,
            capture_end_sample=40_002,
            final=True,
            voiced_end_sample=40_001,
        ),
    )

    assert context.pending.turn_endpoint_sample == 40_001
    await asyncio.sleep(0.005)
    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []

    await asyncio.sleep(0.05)

    assert [
        turn.content
        for turn in context.runtime.orchestrator.context.turns
        if turn.role == "user"
    ] == ["过早终稿"]
    assert context.pending.turn_endpoint_sample is None
    assert context.asr.last_committed_sample == 40_001
    await context.runtime.close()


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
    context = await registry.open_session(identity)
    _bind_verified_owner_classifier(context.runtime)
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
    # The reply to the first turn is still in flight, so this overlap turn is
    # an ordinary barge-in and must carry verified owner authority.
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
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
    context = await registry.open_session(identity)

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
async def test_new_utterance_never_inherits_previous_owner_authority() -> None:
    """Owner authority is scoped to one utterance, never to the whole session.

    A verified owner in the previous turn must not authorize the next
    utterance: the runtime clears the decision when the new turn opens, and
    the VAD seam projects that cleared state instead of the stale authority.
    """

    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = SessionIdentity("authority-freshness", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    bind_owner_policy(context.runtime)

    # The previous utterance closed as a verified owner.
    context.runtime._speaker_decision = _verified_owner_decision()
    context.runtime._speaker_class = "owner"
    assert context.runtime.current_speaker_authority_verified is True

    # The next utterance opens through the real VAD seam.
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="next-utterance-vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
            final=False,
        ),
    )

    assert context.runtime.current_speaker_class == "uncertain"
    assert context.runtime.current_speaker_authority_verified is False
    assert context.runtime.current_speaker_reason_code == "classification_pending"

    # No classifier is wired, so this utterance can only resolve to
    # unclassified authority: its text must not cancel the reply.
    assert context.runtime.ingest_media_speech_segment(
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="next-utterance-asr",
            revision=1,
            kind=SegmentKind.ASR_FINAL,
            capture_start_sample=0,
            capture_end_sample=8_000,
            text="我们下午几点出发",
            final=True,
        )
    )
    before = context.runtime.fence

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=1,
        start_sample=0,
        end_sample=8_000,
    )

    assert fence is None and reason == "speaker_authority_unverified"
    assert context.runtime.fence.matches(before)
    assert context.runtime.assistant_speaking
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]
