"""Media session: device wake acks, live lookups, recovery and fallbacks."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
    device_wake_phrase,
    is_allowlisted_device_phrase,
)
from services.agent.src.voice_core import media_session_projection
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
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
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
)
from services.agent.src.voice_core.speech_timeline import (
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
    _seed_pending_media_turn,
    _wait_until,
)


@pytest.fixture(autouse=True)
def short_lookup_filler_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fakes model a slow lookup; keep the filler decision near-immediate."""
    monkeypatch.setattr(media_session_projection, "_LIVE_LOOKUP_FILLER_GRACE_S", 0.02)


def _aigc_notice_off(registry: MediaVoiceCoreRegistry) -> MediaVoiceCoreRegistry:
    """These fixtures' first output is a wake ack, a cue or a reply, not an AIGC marker."""

    registry.aigc_notice_config = AigcNoticeConfig(enabled=False)
    registry.aigc_round_logger = None
    return registry


@pytest.mark.asyncio
async def test_device_session_speaks_wake_ack_without_user_speech() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = _aigc_notice_off(
        MediaVoiceCoreRegistry(
            bridge=bridge,
            provider_factory=lambda _identity: provider,
        )
    )
    registry.install()
    identity = _device_identity("device-wake-ack")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
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
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_energy_connect_vad_after_sample_zero_skips_device_wake_ack() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = _aigc_notice_off(
        MediaVoiceCoreRegistry(
            bridge=bridge,
            provider_factory=lambda _identity: provider,
        )
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
        context = await registry.open_session(identity)
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
        assert context.pending.turn_start_sample == 3200
        assert context.device_wake_ack_pending is False
    finally:
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
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
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.pending.live_query_endpoint_pinned == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
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
        assert context.pending.live_query_forced_text == "今天南京的天气怎么样"
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
        context.pending.turn_start_sample = 113_280
        context.pending.turn_end_sample = 173_120
        context.pending.turn_endpoint_sample = 173_120
        context.pending.turn_retire_sample = 187_520
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
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
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
        assert context.pending.live_query_forced_text == "今天南京的天气怎么样"
        assert context.pending.live_query_forced_authoritative
        # Recovery must arm turn bounds itself; do not depend on a later
        # timeline inject or a test harness manually setting samples.
        assert context.pending.turn_start_sample is not None
        assert context.pending.turn_end_sample == 148_160
        assert context.pending.turn_endpoint_sample == 148_160
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
        assert context.pending.turn_endpoint_sample == 148_160
        await asyncio.sleep(0.05)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        # The longer echo text must not win; the authoritative forced text does.
        assert user_turns == ["今天南京的天气怎么样"]
    finally:
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
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
        assert context.pending.live_query_forced_text == "今天南京的天气怎么样"
        assert context.pending.live_query_forced_authoritative
        assert context.pending.turn_end_sample == 85_120
        assert context.pending.turn_endpoint_sample == 85_120

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
        assert context.pending.turn_endpoint_sample == 85_120

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
        assert context.pending.turn_endpoint_sample == 85_120
        assert context.pending.live_query_forced_text == "今天南京的天气怎么样"

        await asyncio.sleep(0.05)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天南京的天气怎么样"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_h5_session_does_not_speak_device_wake_ack() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = _aigc_notice_off(
        MediaVoiceCoreRegistry(
            bridge=bridge,
            provider_factory=lambda _identity: provider,
        )
    )
    registry.install()
    identity = SessionIdentity("h5-no-wake")
    try:
        context = await registry.open_session(identity)
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
        assert context.device_wake_ack_pending is False
    finally:
        await registry.finalize_session(identity.session_id)


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
        claim = context.output.delegation_output_claims[ack_fence]
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=1)
        assert media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT in provider.output_kinds
        deep_owner = context.output.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
    finally:
        provider.release.set()
        if context is not None:
            await registry.finalize_session(identity.session_id)


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
        claim = context.output.delegation_output_claims[ack_fence]
        assert claim.state is DelegationOutputState.OWNED
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=1)
        assert claim.state is not DelegationOutputState.RELEASED
        deep_owner = context.output.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
    finally:
        provider.release.set()
        if context is not None:
            await registry.finalize_session(identity.session_id)


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
        assert context.pending.turn_start_sample is None
        assert context.runtime.orchestrator.state is ConversationState.TOOL_WAITING
        claim = context.output.delegation_output_claims[ack_fence]
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=1)
        assert claim.state is not DelegationOutputState.RELEASED
        deep_owner = context.output.output_owner
        assert deep_owner is not None
        assert deep_owner.fence.turn_id == ack_fence.turn_id
        assert deep_owner.fence.generation_id == ack_fence.generation_id + 1
    finally:
        provider.release.set()
        if context is not None:
            await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(SessionIdentity("no-delegation-seam"))

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
    context = await registry.open_session(identity)

    assert context.runtime._delegation_starter is not legacy_starter
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)
    await _wait_until(lambda: provider.output_kinds == [media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT])

    assert legacy_calls == []
    assert provider.delegations == [(query, fence)]
    assert context.output.delegation_output_claims[fence].state is DelegationOutputState.COMPLETED
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)

    # A realtime-shaped chat turn that resolves locally must never create a
    # claim: the starter guard rejects it before any claim is registered.
    fence = await context.runtime.on_turn_committed("现在几点开会")
    assert context.output.delegation_output_claims == {}
    assert provider.delegations == []
    context.output.playback.start(fence)
    # The provider audio path does not complete end-to-end in this harness;
    # the claim contract is that exactly one local reply is produced.
    await registry.generate_reply(identity.session_id, "现在几点开会", fence)
    assert provider.reply_calls == 1

    # A control utterance whose interaction decision never starts delegation
    # must likewise leave no claim behind.
    await context.runtime.on_turn_committed("停一下")
    assert context.output.delegation_output_claims == {}
    assert provider.delegations == []
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.output.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    # Remove the registered tool authority so ``coordinator.delegate`` itself
    # rejects the request before any provider work is started.
    context.runtime.orchestrator.task_manager.specs.pop("media_deep_response", None)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.output.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.delegations == []
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.output.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry.finalize_session(identity.session_id)


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
    context = await registry.open_session(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, query, fence)
    await _wait_until(lambda: provider.reply_calls == 1)

    claim = context.output.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await asyncio.sleep(0.02)
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_fast_media_delegation_skips_lookup_filler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_session_projection, "_LIVE_LOOKUP_FILLER_GRACE_S", 1.5)

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
    context = await registry.open_session(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)

    await _wait_until(
        lambda: provider.output_kinds == [media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT],
    )
    claim = context.output.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.COMPLETED
    assert media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT not in provider.output_kinds
    assert provider.output_texts == ["南京今天多云。"]
    assert provider.reply_calls == 0
    assert await registry.generate_reply(identity.session_id, query, fence)
    assert provider.reply_calls == 0
    await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
        await _finish_device_wake_ack_if_any(registry, identity, provider, bridge, session)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.accepted_pcm), timeout=2.0)
        owner = context.output.output_owner
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
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: context.output.output_owner is None, timeout=2.0)
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT in provider.output_kinds
        assert media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT in provider.output_kinds
        assert provider.output_texts[-1] == f"{BRIDGE_PHRASES[1]}南京今天多云，气温二十二度。"
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output.output_owner
        assert ack_owner is not None
        ack_fence = ack_owner.fence
        context.runtime.orchestrator.delegation.reset_output_intent_state(identity.session_id)
        provider.release.set()
        await asyncio.sleep(0.05)
        assert not provider.deep_started.is_set()
        assert context.output.output_owner is ack_owner
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
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output.output_owner
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
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output.output_owner
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
        await registry.finalize_session(identity.session_id)


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
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output.output_owner
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
        await registry.finalize_session(identity.session_id)


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
        ack_owner = context.output.output_owner
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
            for claim in context.output.delegation_output_claims.values()
        )
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
        # The single surviving delegation then delivers the answer once.
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        assert (
            provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT) == 1
        )
        assert provider.output_texts[-1] == "南京今天多云，气温二十二度。"
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


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
        claim = context.output.delegation_output_claims[fence]
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
            lambda: context.output.output_owner is not None
            and context.output.output_owner.fence.generation_id != fence.generation_id,
            timeout=2.0,
        )
        deep_owner = context.output.output_owner
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
        await registry.finalize_session(identity.session_id)
