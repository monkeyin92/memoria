"""Media session: session lifecycle, identity, reconnects and speaker gates."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    FunASRServerEvent,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaSessionResources,
    MediaVoiceCoreRegistry,
    MediaVoiceProvider,
)
from services.agent.src.voice_core.media_session_types import (
    ProviderAudioTaskSnapshot,
)
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
    asr_result_to_segment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _accept_media_asr_final,
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _device_identity,
    _finish_output_owner_playback,
    _queued_event,
    _seed_pending_media_turn,
    _verified_owner_decision,
    open_bridge_connection,
)
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


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
async def test_device_close_phrase_works_without_formal_owner_authority() -> None:
    identity = SessionIdentity(
        "device-trusted-close",
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
    context = await _seed_pending_media_turn(registry, identity, text="再见")
    context.runtime.set_device_conversation_controls(True)

    async def classify_policy_denied(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=None,
            quality_score=0.0,
            reason_code="authority_policy_denied",
            model_version="unavailable",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    context.runtime.set_speaker_classifier(classify_policy_denied, sample_rate=16_000)
    context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)
    context.runtime.set_target_speaker_focus(True)

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=600,
        retire_sample=640,
    )

    assert fence is None
    assert reason == "conversation_end_explicit"
    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "conversation_end_explicit"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


@pytest.mark.asyncio
async def test_guest_close_phrase_is_rejected_before_device_standby() -> None:
    identity = SessionIdentity(
        "guest-cannot-close",
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
    context = await _seed_pending_media_turn(registry, identity, text="再见")

    async def classify_guest(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return replace(
            _verified_owner_decision(),
            classification="guest",
            reason_code="owner_mismatch",
            permissions=permissions_for_speaker("guest"),
        )

    context.runtime.set_speaker_classifier(classify_guest, sample_rate=16_000)
    context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)
    context.runtime.set_target_speaker_focus(True)

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=600,
        retire_sample=640,
    )

    assert fence is None
    assert reason == "target_non_owner"
    assert all(
        event.WhichOneof("event") != "state"
        for event in tuple(connection.outgoing._critical)  # noqa: SLF001 - queue seam under test
    )
    assert registry.context(identity.session_id) is context.runtime
    assert provider.closed is False
    await registry.finalize_session(identity.session_id)


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
        await registry.open_session(identity)

    assert runtime_closed.is_set()
    assert provider.closed is True
    assert runtime.orchestrator.playback_stop_seam is None
    assert registry.context(identity.session_id) is None


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
    first = asyncio.create_task(registry.open_session(SessionIdentity("first-new-session")))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    duplicate = asyncio.create_task(registry.open_session(SessionIdentity("first-new-session")))
    second = asyncio.create_task(registry.open_session(SessionIdentity("second-new-session")))

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
async def test_reconnect_provider_reset_failure_keeps_old_epoch_authoritative() -> None:
    class FailingResetProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.reset_identities: list[SessionIdentity] = []

        async def reset_for_stream_epoch(self, identity: SessionIdentity) -> None:
            self.reset_identities.append(identity)
            raise RuntimeError("provider epoch reset failed")

    provider = FailingResetProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("failed-provider-epoch-reset", stream_epoch=1)
    context = await registry.open_session(identity)
    before_asr = (
        context.asr.stream_epoch,
        context.asr.task_epoch,
        context.asr.latest_authoritative_task_epoch,
    )
    before_runtime_epoch = context.runtime.speech_timeline.stream_epoch
    replacement = SessionIdentity(identity.session_id, stream_epoch=2)

    with pytest.raises(RuntimeError, match="provider epoch reset failed"):
        await registry._reuse_session(context, replacement)

    assert provider.reset_identities == [replacement]
    assert context.identity == identity
    assert context.stream_epoch == identity.stream_epoch
    assert (
        context.asr.stream_epoch,
        context.asr.task_epoch,
        context.asr.latest_authoritative_task_epoch,
    ) == before_asr
    assert context.runtime.speech_timeline.stream_epoch == before_runtime_epoch
    await context.runtime.close()
    await context.provider.close(context.identity)


@pytest.mark.asyncio
async def test_reconnect_aligns_provider_task_epoch_floor_after_transport_epoch_advance() -> None:
    identity = SessionIdentity("provider-task-floor-reconnect", stream_epoch=1)
    replacement = SessionIdentity(identity.session_id, stream_epoch=2)

    class FloorCapturingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.floors: list[int] = []

        def set_asr_task_epoch_floor(self, task_epoch: int) -> None:
            self.floors.append(task_epoch)

    provider = FloorCapturingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    context = await registry.open_session(identity)

    await registry._reuse_session(context, replacement)

    assert provider.floors == [1]
    assert context.asr.latest_authoritative_task_epoch == 1
    await context.runtime.close()
    await context.provider.close(context.identity)


@pytest.mark.asyncio
async def test_connected_reconnect_provider_reset_failure_retires_both_epochs() -> None:
    identity = SessionIdentity("failed-connected-provider-reset", stream_epoch=1)
    replacement = SessionIdentity(identity.session_id, stream_epoch=2)
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    runtime_closed = asyncio.Event()
    original_runtime_close = runtime.close

    async def close_runtime() -> None:
        runtime_closed.set()
        await original_runtime_close()

    runtime.close = close_runtime  # type: ignore[method-assign]

    class FailingResetProvider(FakeMediaProvider):
        async def reset_for_stream_epoch(self, _identity: SessionIdentity) -> None:
            raise RuntimeError("provider epoch reset failed")

    provider = FailingResetProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    bridge_session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    assert bridge_session.reconnect(replacement)
    callback = bridge.on_session_connected
    assert callback is not None

    with pytest.raises(RuntimeError, match="provider epoch reset failed"):
        await callback(bridge_session)

    assert registry.context(identity.session_id) is None
    assert registry.session_state(identity.session_id) is None
    assert context.closed is True
    assert runtime_closed.is_set()
    assert provider.closed is True
    assert bridge.bridge.get(identity.session_id) is None


@pytest.mark.asyncio
async def test_device_conversation_close_rule_hit_skips_semantic_resolver() -> None:
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
    identity = _device_identity("device-rule-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)

        async def resolver(_: str) -> bool:
            raise AssertionError("rule hit should not call semantic resolver")

        context.runtime.set_conversation_close_semantic_resolver(resolver)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="farewell-start",
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
            sentence_id="farewell-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="好的，再见",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.pending.conversation_close_semantic_task is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_close_phrase_recovered_after_cross_sentence_overlap() -> None:
    """A trailing 再见 must not starve when overlap policy drops the final."""

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
    identity = _device_identity("device-close-overlap")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        # The echo below repeats what the device just said; the echo guard
        # of a final that straddles the playback boundary reads that text.
        context.output.assistant_text = "你好，我是茉莉，今天想聊点什么呀？"
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
        from services.agent.src.voice_core.speech_timeline import ASRResult

        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=64_000,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        echo_decision = await registry._accept_asr_result_decision(
            identity.session_id,
            echo,
        )
        assert echo_decision.accepted is not None
        farewell = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="farewell-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=336_960,
            text="你说的好多呀，好的，我知道了，再见！",
            is_final=True,
            confidence=0.9,
            rescue_synthesized=True,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            farewell,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.pending.live_query_forced_text is None
        assert context.pending.conversation_close_endpoint_pinned == 336_960
        assert context.pending.turn_endpoint_sample == 336_960
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_late_provider_boundary_is_logged_and_rejected_after_stream_epoch_moves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.voice_core.media_audio_ingress")

    class LateBoundaryProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_started = asyncio.Event()
            self.release_finalize = asyncio.Event()
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
                audio_end_sample=2,
                send_count=1,
            )

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
            self.task_epoch = 2
            return ()

    provider = LateBoundaryProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    identity = SessionIdentity("late-vad-boundary", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))

    finalize = asyncio.create_task(
        registry._audio_ingress.finalize_speech_segment(
            context,
            vad_start_sample=0,
            vad_event_sample=2,
            voiced_end_sample=2,
            finalize_reason="vad_end",
        )
    )
    await asyncio.wait_for(provider.finalize_started.wait(), timeout=1)
    context.stream_epoch = 2
    provider.release_finalize.set()

    assert await asyncio.wait_for(finalize, timeout=1) is False
    assert context.ingress.last_finalized_audio_watermark == -1
    boundary_logs = [
        json.loads(record.message.removeprefix("media_asr_boundary "))
        for record in caplog.records
        if record.message.startswith("media_asr_boundary ")
    ]
    assert len(boundary_logs) == 1
    assert boundary_logs[0]["result"] == "stale"
    assert boundary_logs[0]["provider_task_epoch_before"] == 1
    assert boundary_logs[0]["provider_task_epoch_after"] == 2
    assert boundary_logs[0]["provider_pcm_samples"] == 2
    await context.runtime.close()


@pytest.mark.asyncio
async def test_device_close_verdict_resolved_after_the_boundary_advanced_cannot_pin(
    device_media_session: Any,
) -> None:
    """The post-await floor re-check drops a verdict that crossed the boundary.

    The resolver plays the split: it advances ``pending_turn_onset_floor`` while
    the verdict is in flight, so a late True must not pin the retained turn even
    though it returned normally instead of being cancelled.
    """

    window = await device_media_session("device-close-crossed-verdict")
    context = window.context
    crossed_text = "我不想继续聊这个话题"
    window.close_semantic.verdict_text = crossed_text
    window.close_semantic.on_resolve = lambda: setattr(
        context.pending, "pending_turn_onset_floor", 484_480
    )

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="candidate-close",
        start_sample=258_880,
        end_sample=264_640,
        text=crossed_text,
    )
    task = context.pending.conversation_close_semantic_task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)

    assert context.pending.conversation_close_endpoint_pinned is None
    assert context.standby_requested is False
    # The owning evaluation cleared its own handle; nothing else may have.
    assert context.pending.conversation_close_semantic_task is None


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
    context = await registry.open_session(identity)

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

    context = registry.session_state(identity.session_id)
    assert context.asr.latest_authoritative_task_epoch == 2
    assert bridge.transcripts == []
    assert context.runtime.speech_timeline.pending == ()
    await context.runtime.close()
    await provider.close(identity)
