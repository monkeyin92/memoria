"""Media session: barge-in, stop and interruption."""

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
    InteractionPhase,
)
from services.agent.src.prompts import (
    device_wake_phrase,
    is_allowlisted_device_phrase,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    MediaEnvelope,
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import (
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    TimedInterruptProvider,
    _accept_media_asr_final,
    _AckCapturingProvider,
    _bind_verified_owner_classifier,
    _CapturingGenerationBridge,
    _commit_pending_turn_from_device_endpoint,
    _connect_vad_segment,
    _DelegationProbeProvider,
    _device_identity,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _qa_commit_question_then_finish_ack_playback,
    _qa_commit_repeat_question,
    _seed_pending_media_turn,
    _start_retained_utterance,
    _start_speaking_reply,
    _verified_owner_decision,
    _wait_until,
    open_bridge_connection,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


@pytest.mark.asyncio
async def test_max_user_speech_watchdog_is_cancelled_by_vad_end() -> None:
    identity = SessionIdentity(
        "max-user-speech-vad-end",
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
        max_user_speech_duration_s=0.03,
    )
    context = await registry.open_session(identity)
    session = connection.session

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="vad-end-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
            final=False,
        ),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="vad-end-final",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=160,
            capture_end_sample=161,
            final=True,
            voiced_end_sample=160,
        ),
    )
    await asyncio.sleep(0.08)

    assert registry.context(identity.session_id) is context.runtime
    assert provider.closed is False
    assert all(
        event.WhichOneof("event") != "state"
        for event in tuple(connection.outgoing._critical)  # noqa: SLF001 - queue seam under test
    )
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_registry_installs_direct_playback_stop_seam_as_safe_noop() -> None:
    identity = SessionIdentity("direct-playback-seam-noop")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    provider = FakeMediaProvider()

    async def build_session(_identity: SessionIdentity) -> MediaSessionResources:
        return MediaSessionResources(runtime=runtime, provider=provider)

    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=build_session,
    )
    await registry.open_session(identity)

    seam = runtime.orchestrator.playback_stop_seam
    assert seam is not None
    before = runtime.fence
    await seam()

    assert runtime.fence.matches(before)
    assert provider.cancelled == []
    runtime.set_playback_stop_seam(None)
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_direct_playback_stop_seam_revokes_output_and_cancels_next_generation() -> None:
    reply_started = asyncio.Event()
    reply_drained = asyncio.Event()

    class BlockingFailCancelProvider(FakeMediaProvider):
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

        async def cancel_generation(self, fence: GenerationFence) -> None:
            self.cancelled.append(fence)
            raise RuntimeError("provider cancel failed")

    identity = SessionIdentity("direct-playback-seam-active")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    provider = BlockingFailCancelProvider()
    bridge = MediaBridgeGrpcServer()
    effects: list[tuple[int, GenerationFence, str]] = []

    async def emit_realtime_effect(
        _session_id: str,
        effect_kind: int,
        fence: GenerationFence,
        *,
        source_event_id: str,
        **_kwargs: Any,
    ) -> bool:
        effects.append((effect_kind, fence, source_event_id))
        return True

    bridge.emit_realtime_effect = emit_realtime_effect  # type: ignore[method-assign]

    async def build_session(_identity: SessionIdentity) -> MediaSessionResources:
        return MediaSessionResources(runtime=runtime, provider=provider)

    registry = MediaVoiceCoreRegistry(bridge=bridge, session_factory=build_session)
    context = await registry.open_session(identity)
    fence = await runtime.on_turn_committed("你好")
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "你好", fence))
    await asyncio.wait_for(reply_started.wait(), timeout=1)
    assert context.output.output_owner is not None
    assert context.output.playback.register_audio(fence, 0, 0, 2)
    assert context.output.playback.add_span(PlaybackSpan(fence, 0, 1, 0, 2, text="你", sequence=0))
    assert context.output.playback.acknowledge(fence, 2, received_sequence=0)
    stale_before = context.output.playback.stale_ack_count
    runtime_before = runtime.fence
    turns_before = [
        (turn.role, turn.content) for turn in context.runtime.orchestrator.context.turns
    ]
    interrupted: list[tuple[GenerationFence, str | None]] = []
    original_interrupted = context.runtime.on_media_playback_interrupted

    async def observe_interrupted(
        *,
        interrupted_from: GenerationFence,
        synchronized_transcript: str | None,
    ) -> str | None:
        interrupted.append((interrupted_from, synchronized_transcript))
        return await original_interrupted(
            interrupted_from=interrupted_from,
            synchronized_transcript=synchronized_transcript,
        )

    context.runtime.on_media_playback_interrupted = observe_interrupted  # type: ignore[method-assign]

    seam = runtime.orchestrator.playback_stop_seam
    assert seam is not None
    await seam()

    assert context.output.output_owner is None
    assert context.output.reply_task is None
    assert reply.done()
    assert reply_drained.is_set()
    assert provider.cancelled == [fence]
    assert runtime.fence.generation_id == runtime_before.generation_id + 1
    assert effects == [
        (
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            runtime.fence,
            "identity_epoch_rotated",
        )
    ]
    assert context.output.playback.actual_heard_text(fence) == ""
    assert context.output.playback.acknowledge(fence, 2, received_sequence=0) == ()
    assert context.output.playback.stale_ack_count == stale_before + 1
    assert interrupted == [(runtime_before, "")]
    assert [
        (turn.role, turn.content) for turn in context.runtime.orchestrator.context.turns
    ] == turns_before

    runtime.set_playback_stop_seam(None)
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_interrupt_assist_commit_keeps_provider_asr_open() -> None:
    identity = SessionIdentity(
        "commit-keeps-asr",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
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

    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=0,
        end_sample=600,
        retire_sample=640,
    )

    assert fence is not None, f"turn did not commit: {reason}"
    assert provider.pause_asr_calls == []
    await context.runtime.close()


@pytest.mark.asyncio
async def test_reconnect_rejects_runtime_authority_change_before_cleanup_cancel() -> None:
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    identity = SessionIdentity(
        "registry-authority-reconnect",
        account_id="account-a",
        participant_id="participant-a",
        device_id="device-a",
        client_type="device",
        stream_epoch=1,
        subject_id="subject-a",
        binding_id="binding-a",
        binding_version=3,
        runtime_profile_version=27,
    )
    context = await registry.open_session(identity)
    cleanup = asyncio.create_task(asyncio.sleep(60))
    registry._cleanup_tasks[identity.session_id] = cleanup  # noqa: SLF001

    try:
        with pytest.raises(ValueError, match="runtime authority fence changed"):
            await registry._reuse_session(
                context,
                SessionIdentity(
                    "registry-authority-reconnect",
                    account_id="account-a",
                    participant_id="participant-a",
                    device_id="device-a",
                    client_type="device",
                    stream_epoch=2,
                    subject_id="",
                    binding_id="binding-a",
                    binding_version=3,
                    runtime_profile_version=28,
                ),
            )
        assert registry._cleanup_tasks[identity.session_id] is cleanup  # noqa: SLF001
        assert not cleanup.done()
        assert context.identity == identity
        assert context.stream_epoch == identity.stream_epoch
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        registry._cleanup_tasks.pop(identity.session_id, None)  # noqa: SLF001
        await context.runtime.close()
        await context.provider.close(context.identity)


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
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "你好", fence))
    await asyncio.wait_for(reply_started.wait(), timeout=1)
    assert context.output.output_owner is not None

    cancellation = asyncio.create_task(registry._cancel_reply_task(context, fence))
    await asyncio.wait_for(cancel_started.wait(), timeout=1)
    try:
        assert context.output.output_owner is None
        await asyncio.wait_for(reply_drained.wait(), timeout=1)
        assert reply.done()
    finally:
        release_cancel.set()
        await asyncio.gather(cancellation, return_exceptions=True)
        if not reply.done():
            reply.cancel()
        await asyncio.gather(reply, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "你好", fence))
    await asyncio.wait_for(reply_started.wait(), timeout=1)

    try:
        await registry._cancel_reply_task(context, fence)
        assert context.output.output_owner is None
        assert reply_drained.is_set()
        assert reply.done()
    finally:
        if not reply.done():
            reply.cancel()
        await asyncio.gather(reply, return_exceptions=True)
        await context.runtime.close()
        await provider.close(identity)


@pytest.mark.asyncio
async def test_empty_connect_vad_does_not_cancel_device_wake_ack() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-empty-connect-vad-wake")
    session = bridge.bridge.open(identity)
    original = registry._speak_device_wake_ack

    async def _inject_then_speak(context: Any) -> None:
        await registry.on_speech_segment(
            session,
            _connect_vad_segment(identity, rms=0.0, segment_id="empty-connect-start"),
        )
        await original(context)

    registry._speak_device_wake_ack = _inject_then_speak  # type: ignore[method-assign]
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert provider.texts == [device_wake_phrase(identity.session_id)]
        assert is_allowlisted_device_phrase(provider.texts[0])
        assert context.pending.turn_start_sample is None
        await _wait_until(lambda: context.device_wake_ack_pending is False)
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_energy_connect_vad_at_sample_zero_does_not_cancel_device_wake_ack() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-energy-connect-vad-wake")
    session = bridge.bridge.open(identity)
    original = registry._speak_device_wake_ack

    async def _inject_then_speak(context: Any) -> None:
        await registry.on_speech_segment(
            session,
            _connect_vad_segment(
                identity,
                sample=0,
                rms=400.0,
                segment_id="energy-connect-start",
            ),
        )
        await original(context)

    registry._speak_device_wake_ack = _inject_then_speak  # type: ignore[method-assign]
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert provider.texts == [device_wake_phrase(identity.session_id)]
        assert is_allowlisted_device_phrase(provider.texts[0])
        assert context.pending.turn_start_sample is None
        await _wait_until(lambda: context.device_wake_ack_pending is False)
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_energy_connect_vad_after_admit_before_first_frame_does_not_cancel_wake_ack() -> None:
    class _GatedAckCapturingProvider(_AckCapturingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

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
            self.texts.append(str(intent.tts_source))
            self.started.set()

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                await self.release.wait()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
                self.completed.set()

            return chunks()

    provider = _GatedAckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-energy-connect-vad-after-admit")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert context.device_wake_ack_pending is True
        await registry.on_speech_segment(
            session,
            _connect_vad_segment(
                identity,
                sample=0,
                rms=778.0,
                segment_id="wake-tail-start",
            ),
        )
        assert context.pending.turn_start_sample is None
        assert context.runtime.interaction_phase is not InteractionPhase.USER_SPEAKING
        provider.release.set()
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _wait_until(lambda: context.device_wake_ack_pending is False)
        assert provider.texts == [device_wake_phrase(identity.session_id)]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_delegation_initial_decision_timeout_replies_locally_and_cancels_late_delegation() -> (
    None
):
    start_gate = asyncio.Event()
    deep_gate = asyncio.Event()

    class SlowStartProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            await deep_gate.wait()
            return "迟到的深度结果。"

    provider = SlowStartProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
        delegation_initial_decision_timeout_s=0.05,
    )
    registry.install()
    identity = SessionIdentity("delegation-decision-timeout")
    context = await registry.open_session(identity)
    task_manager = context.runtime.orchestrator.task_manager
    original_start = task_manager.start

    async def slow_start(*args: Any, **kwargs: Any) -> Any:
        await start_gate.wait()
        return await original_start(*args, **kwargs)

    task_manager.start = slow_start  # type: ignore[method-assign]
    query = "今天南京天气怎么样"
    try:
        fence = await context.runtime.on_turn_committed(query)
        context.output.playback.start(fence)
        # The normal reply outlives the initial decision window: it must
        # release the claim, speak locally, and later cancel the late
        # delegation instead of accepting deep output.
        await registry.generate_reply(identity.session_id, query, fence)
        claim = context.output.delegation_output_claims[fence]
        assert claim.state is DelegationOutputState.RELEASED
        assert provider.reply_calls == 1
        assert provider.output_kinds == []
        assert provider.delegations == []

        start_gate.set()
        await _wait_until(
            lambda: not task_manager.tasks,
            timeout=2,
        )
        await asyncio.sleep(0.02)
        assert claim.state is DelegationOutputState.RELEASED
        assert provider.reply_calls == 1
        assert provider.output_kinds == []
    finally:
        start_gate.set()
        deep_gate.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_started_live_lookup_ack_cancelled_does_not_repeat_filler() -> None:
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
                    try:
                        await self.ack_hold.wait()
                    except asyncio.CancelledError:
                        raise
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
    identity = SessionIdentity("started-ack-no-repeat-filler")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output.output_owner
        assert ack_owner is not None
        await registry._cancel_reply_task(context, ack_owner.fence, reason="preempted")
        provider.ack_hold.set()
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT in provider.output_kinds
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=context.runtime.fence.generation_id,
                received_sequence=0,
                rendered_sample_end=2,
                client_monotonic_ms=1,
                turn_id=context.runtime.fence.turn_id,
                tool_epoch=context.runtime.fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
    finally:
        provider.ack_hold.set()
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_weather_interrupt_restarts_output_after_old_dispatch_drains() -> None:
    """An admitted follow-up must survive the old audible iterator's cleanup."""

    class DrainingProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.first_draining = asyncio.Event()
            self.finish_first = asyncio.Event()
            self.output_texts: list[str] = []

        async def start_delegation(self, text: str, _fence: GenerationFence) -> str:
            await self.release.wait()
            return f"答案：{text}。"

        def generate_output(
            self,
            identity: SessionIdentity,
            intent: Any,
            fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            text = str(intent.tts_source)
            self.output_texts.append(text)
            source = super().generate_output(
                identity, intent, fence,
                work_id=work_id, source_start_sample=source_start_sample,
            )

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                async for chunk in source:
                    yield chunk
                if text == "答案：今天南京天气怎么样。":
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.first_draining.set()
                        await self.finish_first.wait()

            return chunks()

    provider = DrainingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-weather-interrupt-draining")
    session = bridge.bridge.open(identity)
    cancellation = None
    try:
        context, _ = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样",
        )
        provider.release.set()
        await _wait_until(lambda: len(bridge.frames) == 2)
        first_owner = context.output.output_owner
        assert first_owner is not None
        first_dispatch = context.output.output_dispatch_task
        assert first_dispatch is not None and not first_dispatch.done()
        assert context.output.reply_delivery.get(first_owner.fence).first_frame_sent

        cancellation = asyncio.create_task(
            registry._cancel_reply_task(context, first_owner.fence),
        )
        await asyncio.wait_for(provider.first_draining.wait(), timeout=2)
        assert context.output.output_owner is None
        _bind_verified_owner_classifier(context.runtime)
        context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)
        second_fence, reason = await _qa_commit_repeat_question(
            registry, context, identity, text="明天南京天气怎么样",
            start_sample=600, end_sample=1200, retire_sample=1240,
        )
        assert second_fence is not None, reason
        await _wait_until(
            lambda: any(work.fence.matches(second_fence) for work in context.output.output_work.values()),
        )
        assert context.output.output_dispatch_task is first_dispatch
        assert not first_dispatch.done()
        assert not any(frame.turn_id == second_fence.turn_id for frame in bridge.frames)

        provider.finish_first.set()
        await asyncio.wait_for(cancellation, timeout=2)
        await _wait_until(
            lambda: any(
                result.fence.turn_id == second_fence.turn_id and result.emitted_audio
                for result in context.output.output_results
            ),
        )
        second_owner = context.output.output_owner
        assert second_owner is not None
        assert second_owner.fence.turn_id == second_fence.turn_id
        assert second_owner.fence in bridge.generation_starts
        second_frames = [frame for frame in bridge.frames if frame.turn_id == second_fence.turn_id]
        assert len(second_frames) == 1
        assert second_frames[0].first
        assert second_frames[0].sequence == second_frames[0].source_start_sample == 0
        assert sum(text.endswith("答案：明天南京天气怎么样。") for text in provider.output_texts) == 1
        assert context.output.reply_delivery.get(first_owner.fence).terminal_event is ReplyDeliveryEvent.PREEMPTED
        assert not any(result.reason == "output_intent_inactive" for result in context.output.output_results)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        await _wait_until(lambda: context.output.output_dispatch_task is None)
        assert context.output.output_retry_task is None
        assert context.output.output_owner is None
        assert not context.output.output_work
    finally:
        provider.release.set()
        provider.finish_first.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
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
async def test_media_backchannel_restores_without_persisting_a_turn() -> None:
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        metrics=metrics,
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
    context = await registry.open_session(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    vad_segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=1,
        provider_task_epoch=1,
        segment_id="backchannel-vad",
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=0,
        capture_end_sample=320,
        text="",
        final=False,
    )
    assert context.runtime.ingest_media_speech_segment(vad_segment)
    await registry._apply_projection_segment(context, vad_segment)
    asr_segment = SpeechSegment(
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
    assert context.runtime.ingest_media_speech_segment(asr_segment)
    await registry._apply_projection_segment(context, asr_segment)

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
    assert (
        metrics.get(
            "voice_conversation_turn_initiation_total",
            {"kind": "vad_first", "state": "assistant_overlap"},
        )
        == 1
    )
    assert metrics.get("voice_conversation_backchannel_total", {"status": "detected"}) == 1
    assert metrics.get("voice_conversation_backchannel_total", {"status": "continued"}) == 1
    assert metrics.get("voice_conversation_participation_proxy_ms_total", {"kind": "owner"}) == 0


@pytest.mark.asyncio
async def test_media_sustained_barge_in_restores_gain_and_commits() -> None:
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        metrics=metrics,
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
    context = await registry.open_session(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    bind_owner_policy(context.runtime)

    async def classify_owner(_pcm: bytes, sample_rate: int) -> SpeakerDecision:
        assert sample_rate == 16_000
        return SpeakerDecision(
            classification="owner",
            score=0.95,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="test-owner-v1",
            template_version=1,
            profile_id="owner-profile",
            permissions=permissions_for_speaker("owner"),
        )

    context.runtime.set_speaker_classifier(classify_owner, sample_rate=16_000)
    context.runtime.on_user_voice_started()
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
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
    assert (
        metrics.get(
            "voice_conversation_turn_initiation_total",
            {"kind": "asr_direct", "state": "assistant_overlap"},
        )
        == 1
    )
    assert metrics.get("voice_conversation_yield_proxy_total", {"status": "candidate"}) == 1
    assert metrics.get("voice_conversation_participation_proxy_ms_total", {"kind": "owner"}) == 500


@pytest.mark.asyncio
async def test_media_known_bystander_speech_does_not_cancel_playback() -> None:
    """A nearby voice during playback must not cut the reply or open a turn."""

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
    identity = SessionIdentity("bystander-session", stream_epoch=1)
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await context.runtime.on_assistant_speaking("还在播放的回答")
    bind_owner_policy(context.runtime)

    async def classify_bystander(_pcm: bytes, sample_rate: int) -> SpeakerDecision:
        assert sample_rate == 16_000
        return SpeakerDecision(
            classification="guest",
            score=0.11,
            quality_score=0.92,
            reason_code="owner_mismatch",
            model_version="test-guest-v1",
            template_version=1,
            profile_id="other-profile",
            permissions=permissions_for_speaker("guest"),
        )

    context.runtime.set_speaker_classifier(classify_bystander, sample_rate=16_000)
    context.runtime.on_user_voice_started()
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
    assert context.runtime.ingest_media_speech_segment(
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="bystander-asr",
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
    await asyncio.sleep(0)

    assert fence is None and reason == "known_non_owner_speech"
    assert context.runtime.current_speaker_class == "guest"
    assert context.runtime.fence.matches(before)
    assert context.runtime.assistant_speaking
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]
    assert media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION not in [
        effect_kind for effect_kind, _payload in effects
    ]
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
    context = await registry.open_session(identity)
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
async def test_device_swallowed_cancel_finishes_without_clearing_the_next_task(
    device_media_session: Any,
) -> None:
    """A step that swallows its cancellation must not clear the new same-text task.

    The old evaluation is confirmed to be inside its await first, so the cancel
    below cancels a running task rather than an unstarted one.
    """

    window = await device_media_session("device-close-swallow")
    window.close_semantic.swallow = True
    window.close_semantic.gate.clear()
    window.close_semantic.late_gate.clear()
    text = "我不想继续聊这个话题"
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="playback-close",
        start_sample=258_880,
        end_sample=264_640,
        text=text,
    )
    abandoned = window.context.pending.conversation_close_semantic_task
    assert abandoned is not None
    await asyncio.wait_for(window.close_semantic.entered.wait(), timeout=1.0)

    _start_retained_utterance(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=484_480,
        end_sample=508_800,
        text=text,
    )
    current = window.context.pending.conversation_close_semantic_task
    assert current is not None and current is not abandoned
    for _ in range(2):
        await asyncio.sleep(0)
    # The cancellation really was swallowed: the old evaluation is still alive,
    # and the new same-text handle survived it.
    assert not abandoned.done()
    assert window.context.pending.conversation_close_semantic_task is current

    # Finish only the swallowed old evaluation: the new same-text handle must
    # still be installed after its finally has run.
    window.close_semantic.gate.set()
    await asyncio.wait_for(asyncio.shield(abandoned), timeout=1.0)
    assert abandoned.done()
    assert window.context.pending.conversation_close_semantic_task is current
    assert window.context.pending.conversation_close_endpoint_pinned is None

    window.close_semantic.late_gate.set()
    await asyncio.wait_for(asyncio.shield(current), timeout=1.0)
    assert window.context.pending.conversation_close_semantic_task is None


@pytest.mark.asyncio
async def test_device_swallowed_cancel_true_verdict_cannot_pin_the_new_pending_turn(
    device_media_session: Any,
) -> None:
    """A swallowed cancel that resolves True later must not pin the new pending turn."""

    window = await device_media_session("device-close-swallow-true")
    window.close_semantic.swallow = True
    window.close_semantic.gate.clear()
    window.close_semantic.late_gate.clear()
    abandoned_text = "我不想继续聊这个话题"
    window.close_semantic.verdict_text = abandoned_text
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="playback-close",
        start_sample=258_880,
        end_sample=264_640,
        text=abandoned_text,
    )
    abandoned = window.context.pending.conversation_close_semantic_task
    assert abandoned is not None
    await asyncio.wait_for(window.close_semantic.entered.wait(), timeout=1.0)

    _start_retained_utterance(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=484_480,
        end_sample=508_800,
        text="下午一起出发吗",
    )
    current = window.context.pending.conversation_close_semantic_task
    assert current is not None and current is not abandoned
    assert window.context.pending.pending_turn_onset_floor == 484_480
    await asyncio.sleep(0)
    assert not abandoned.done()  # the cancel was swallowed

    # ABA: the boundary field returns to the value this evaluation captured, so
    # the floor re-check alone cannot reject it -- only task ownership can.
    window.context.pending.pending_turn_onset_floor = None
    window.close_semantic.gate.set()
    await asyncio.wait_for(asyncio.shield(abandoned), timeout=1.0)
    assert abandoned.done()
    # The abandoned window's True verdict arrived after the boundary moved.
    assert window.context.pending.conversation_close_endpoint_pinned is None
    assert window.context.standby_requested is False
    assert window.context.pending.conversation_close_semantic_task is current

    window.close_semantic.late_gate.set()
    await asyncio.wait_for(asyncio.shield(current), timeout=1.0)
    assert window.context.pending.conversation_close_endpoint_pinned is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_split_cancels_an_in_flight_close_verdict(
    device_media_session: Any,
) -> None:
    """A close verdict still in flight for the closed window cannot land later."""

    window = await device_media_session("device-abandoned-close-semantic")
    window.close_semantic.gate.clear()
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="playback-close",
        start_sample=258_880,
        end_sample=264_640,
        text="我不想继续聊这个话题",
        revision=1,
    )
    assert window.context.pending.conversation_close_semantic_task is not None

    abandoned_task = window.context.pending.conversation_close_semantic_task
    await asyncio.wait_for(window.close_semantic.entered.wait(), timeout=1.0)
    _start_retained_utterance(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=484_480,
        end_sample=508_800,
        text="下午一起出发吗",
    )
    await asyncio.sleep(0)
    assert abandoned_task.cancelled()

    # The abandoned window's verdict resolves only now: it must not pin.
    window.close_semantic.verdict_text = "我不想继续聊这个话题"
    window.close_semantic.gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert window.context.pending.conversation_close_endpoint_pinned is None
    assert window.context.standby_requested is False

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


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
    context = await registry.open_session(identity)
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
    context = await registry.open_session(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    context.output.playback.start(fence)
    assert context.output.playback.register_audio(fence, 0, 0, 2)
    assert context.output.playback.acknowledge(fence, 2, received_sequence=0) == ()

    await registry._record_interrupted_timed_spans(context, fence)

    assert context.output.playback.actual_heard_text(fence) == "你好。"


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
    assert context.output.playback.register_audio(fence, 0, 0, 2)
    assert context.output.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=3,
            audio_start_sample=0,
            audio_end_sample=2,
            text="你好。",
        )
    )
    assert context.output.playback.acknowledge(fence, 2)
    assert context.output.playback.actual_heard_text(fence) == "你好。"
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
    assert context.output.playback.register_audio(fence, 0, 0, 2)
    assert context.output.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=3,
            audio_start_sample=0,
            audio_end_sample=2,
            text="你好。",
        )
    )
    assert context.output.playback.acknowledge(fence, 2)
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
    connection = open_bridge_connection(bridge, identity)
    context = await registry.open_session(identity)
    baseline = connection.outgoing.get_nowait()
    assert baseline is not None and baseline.WhichOneof("event") == "floor_effect"
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好。")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
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
