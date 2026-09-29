"""Media session: VAD, ASR finals, endpoints and turn commits."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any

import grpc
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
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
    LIVE_LOOKUP_FILLER,
)
from services.agent.src.voice_core import media_session_input
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
    MediaVoiceCoreRegistry,
    _OutputWork,
)
from services.agent.src.voice_core.media_session_commit import _resolve_media_turn_text
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
    OutputDispatchResult,
    OutputDispatchStatus,
    ProviderAudioTaskSnapshot,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)
from services.agent.tests.unit.media_session_support import (
    _ABANDONED_WINDOW_END,
    FakeMediaProvider,
    MultiFinalProvider,
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _AckCapturingProvider,
    _bind_verified_owner_classifier,
    _CapturingGenerationBridge,
    _CapturingMediaBridge,
    _commit_pending_turn_from_device_endpoint,
    _complete_previous_device_playback,
    _device_identity,
    _device_vad,
    _feed_playback_window_finals,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _next_event,
    _open_retained_window,
    _owner_silence_identity,
    _queued_event,
    _requests,
    _seed_pending_media_turn,
    _start_retained_utterance,
    _user_turn_texts,
    _verified_owner_decision,
    _wait_until,
    open_bridge_connection,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import set_floor
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


@pytest.mark.asyncio
async def test_verified_owner_close_phrase_projects_closed_and_returns_device_to_standby() -> None:
    identity = SessionIdentity(
        "owner-explicit-standby",
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

    assert fence is None
    assert reason == "conversation_end_explicit"
    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "conversation_end_explicit"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


@pytest.mark.asyncio
async def test_playback_farewell_with_shadow_score_returns_device_to_standby() -> None:
    identity = SessionIdentity(
        "playback-shadow-farewell",
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
    context = await _seed_pending_media_turn(registry, identity, text="好的，再见")
    context.runtime.set_device_conversation_controls(True)
    set_floor(context.runtime, assistant_speaking=True)

    async def classify_shadow_guest(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=0.41,
            quality_score=0.2,
            reason_code="shadow_guest_candidate",
            model_version="speaker-test-v1",
            template_version=1,
            profile_id="shadow-owner",
            permissions=permissions_for_speaker("uncertain"),
        )

    context.runtime.set_speaker_classifier(classify_shadow_guest, sample_rate=16_000)
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
async def test_device_owner_silence_timeout_closes_only_after_listening_window() -> None:
    identity = SessionIdentity(
        "owner-silence-standby",
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
        owner_silence_timeout_s=0.03,
    )
    context = await registry.open_session(identity)

    registry._sync_owner_silence_phase(context, "speaking")
    await asyncio.sleep(0.05)
    assert registry.context(identity.session_id) is context.runtime
    assert provider.closed is False

    registry._sync_owner_silence_phase(context, "listening")
    await asyncio.sleep(0.06)

    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "owner_silence_timeout"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


@pytest.mark.asyncio
async def test_device_owner_silence_timer_pauses_while_user_is_speaking() -> None:
    identity = SessionIdentity(
        "owner-silence-user-speaking",
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
        owner_silence_timeout_s=0.03,
    )
    context = await registry.open_session(identity)

    # The owner-silence window is armed by session creation.  A speaking
    # turn must suspend that window even if the asynchronous assistant-state
    # projection has not run yet.
    registry._sync_owner_silence_phase(context, "user_speaking")
    await asyncio.sleep(0.06)
    assert registry.context(identity.session_id) is context.runtime
    assert provider.closed is False

    registry._sync_owner_silence_phase(context, "listening")
    await asyncio.sleep(0.06)

    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "owner_silence_timeout"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


@pytest.mark.asyncio
async def test_assistant_nudge_playback_does_not_extend_owner_silence_window() -> None:
    """A missed-hearing nudge is assistant speech, not owner activity.

    Its playback returns the floor through the same ``listening`` projection
    seam a real reply uses.  If that seam minted a fresh window, the
    assistant's own voice would push the owner-silence deadline out by a full
    interval per nudge and the silent close would never be reached.
    """

    identity = SessionIdentity(
        "owner-silence-nudge",
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
        owner_silence_timeout_s=0.30,
    )
    context = await registry.open_session(identity)

    # Burn most of the window the owner was given at session creation.
    await asyncio.sleep(0.20)
    assert registry.context(identity.session_id) is context.runtime

    # One nudge: assistant speaks, then the floor returns to listening. No
    # owner turn was ever accepted, so no fresh interval may be granted.
    registry._sync_owner_silence_phase(context, "speaking")
    registry._sync_owner_silence_phase(context, "listening")

    await asyncio.sleep(0.20)

    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "owner_silence_timeout"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


@pytest.mark.asyncio
async def test_accepted_user_turn_refills_the_follow_up_window_without_authority() -> None:
    """Product contract (2026-09-20): every accepted user turn refills the window.

    Speaker authority is never established here (the device has no enrollment),
    so the old rule kept whatever the previous turn left behind: one single
    window was drained across the conversation, the reply finished with a few
    seconds left, and the session stood by before the next question.  Now an
    accepted turn refills the full interval, while the bound still exists -- a
    full window of silence after the last reply stands the device by.
    """

    identity = _owner_silence_identity("owner-silence-accepted-refresh")
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    connection = open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.30,
    )
    context = await registry.open_session(identity)

    # Authority is never established for this conversation: the runtime simply
    # has no decision to project (no enrollment), not a guest.
    assert context.runtime.current_speaker_authority_verified is False

    # Two owner turns in a row, each with its reply's playback in between: the
    # follow-up window is full again after both, so the conversation is never
    # drained by its own replies.
    for turn in range(2):
        await asyncio.sleep(0.12)
        registry._sync_owner_silence_phase(context, "user_speaking")
        registry._finish_owner_silence_turn(context, accepted=True)
        assert context.owner_silence_remaining_s == pytest.approx(0.30), turn
        registry._sync_owner_silence_phase(context, "speaking")
        registry._sync_owner_silence_phase(context, "listening")
        assert context.owner_silence_remaining_s == pytest.approx(0.30), turn
        assert registry.context(identity.session_id) is context.runtime, turn
        assert provider.closed is False

    # The bound is unchanged: one full window of silence still stands by.
    await asyncio.sleep(0.36)

    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "owner_silence_timeout"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


@pytest.mark.asyncio
async def test_verified_owner_turn_refreshes_the_full_owner_silence_window() -> None:
    """A verified owner turn is the one seam that refills the whole interval.

    This is the control case for the carry-over test above: the same sequence
    of projections, differing only in that the committed turn carries a
    verified owner decision.  The refreshed window must be the configured
    interval exactly, and the one-shot VAD grace must be re-armed so the next
    utterance is not judged against the previous turn's bookkeeping.
    """

    identity = _owner_silence_identity("owner-silence-verified-refresh")
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.30,
    )
    context = await registry.open_session(identity)
    try:
        # Burn most of the window, and spend the one-shot VAD grace so the
        # refresh has something observable to reset.
        await asyncio.sleep(0.12)
        context.owner_silence_grace_used = True

        decision = _verified_owner_decision()
        context.runtime._speaker_decision = decision
        context.runtime._speaker_class = decision.classification
        assert context.runtime.current_speaker_authority_verified is True

        registry._sync_owner_silence_phase(context, "user_speaking")
        registry._finish_owner_silence_turn(context, accepted=True)

        assert context.owner_silence_remaining_s == pytest.approx(0.30)
        assert context.owner_silence_grace_used is False

        # The refreshed budget survives the reply's playback and is what the
        # ``listening`` seam resumes once the floor comes back.
        registry._sync_owner_silence_phase(context, "speaking")
        registry._sync_owner_silence_phase(context, "listening")
        assert context.owner_silence_remaining_s == pytest.approx(0.30)
        assert registry.context(identity.session_id) is context.runtime
        assert provider.closed is False
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_max_user_speech_watchdog_closes_a_stuck_vad_turn() -> None:
    identity = SessionIdentity(
        "max-user-speech-watchdog",
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
    session = connection.session

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="stuck-vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
            final=False,
        ),
    )
    await asyncio.sleep(0.08)

    closed = _queued_event(connection, "state")
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "max_user_speech_duration_timeout"
    assert registry.context(identity.session_id) is None
    assert provider.closed is True


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


@pytest.mark.asyncio
async def test_device_vad_end_during_wake_playback_is_ignored() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()

    def runtime_factory(session_id: str) -> DuplexRuntime:
        return DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)

    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=runtime_factory,
    )
    registry.install()
    identity = _device_identity("device-wake-vad-ignore")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        set_floor(context.runtime, assistant_speaking=True)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id="wake-echo-end",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=1_280,
                capture_end_sample=1_281,
                final=True,
                voiced_end_sample=1_280,
            ),
        )
        assert context.pending.turn_endpoint_sample is None
        assert context.pending.pending_partial is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_final_commits_before_vad_end() -> None:
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
    identity = _device_identity("device-early-clock-fact")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_final_commits_before_vad_end() -> None:
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
    identity = _device_identity("device-early-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
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
            text="好的，那你早点休息，再见。",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.pending.conversation_close_endpoint_pinned == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_immediate_partial_commits_without_vad_end() -> None:
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
    identity = _device_identity("device-immediate-partial-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
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

        partial = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="farewell-partial",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="好的，再见",
            is_final=False,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, partial)
        assert context.pending.conversation_close_endpoint_pinned == 16_000
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_pin_blocks_late_vad_end_extension() -> None:
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
    identity = _device_identity("device-conversation-close-pin")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
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
        pinned = context.pending.conversation_close_endpoint_pinned
        assert pinned == 16_000
        assert context.pending.turn_endpoint_sample == pinned

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="late-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=200_000,
                capture_end_sample=200_001,
                final=True,
                voiced_end_sample=200_000,
            ),
        )
        assert context.pending.turn_endpoint_sample != 200_000
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_semantic_final_commits_before_vad_end() -> None:
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
    identity = _device_identity("device-semantic-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        calls: list[str] = []

        async def resolver(text: str) -> bool:
            calls.append(text)
            return text == "那先不聊了"

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
            text="那先不聊了",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        task = context.pending.conversation_close_semantic_task
        if task is not None:
            await task
        assert calls == ["那先不聊了"]
        assert (
            context.pending.conversation_close_endpoint_pinned == 16_000
            or context.pending.turn_endpoint_sample == 16_000
            or context.standby_requested
        )
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_bare_stop_word_never_pins_a_conversation_close() -> None:
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
    identity = _device_identity("device-stop-word-not-farewell")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        calls: list[str] = []

        async def resolver(text: str) -> bool:
            # The classifier that read a bare 「停」 as a farewell on the device.
            calls.append(text)
            return True

        context.runtime.set_conversation_close_semantic_resolver(resolver)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="stop-start",
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
            sentence_id="stop-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=8_000,
            text="停",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        task = context.pending.conversation_close_semantic_task
        if task is not None:
            await task
        assert calls == []
        assert context.pending.conversation_close_endpoint_pinned is None
        assert not context.standby_requested
        assert not context.runtime.conversation_close_needed("停")
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_pin_blocks_late_vad_end_extension() -> None:
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
    identity = _device_identity("device-clock-fact-pin")
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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.clock_fact_endpoint_pinned == 16_000
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.pending.turn_retire_sample == 16_000

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="late-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=200_000,
                capture_end_sample=200_001,
                final=True,
                voiced_end_sample=200_000,
            ),
        )
        assert context.pending.turn_endpoint_sample != 200_000
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_concatenated_timeline_commits_canonical_segment() -> None:
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
    identity = _device_identity("device-clock-fact-concat")
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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        partial = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-partial",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=False,
            confidence=0.8,
        )
        final = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=2,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, partial)
        assert await registry.accept_asr_result(identity.session_id, final)
        assert context.pending.turn_endpoint_sample == 16_000

        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=16_000,
        )
        assert fence is not None
        assert reason is None
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天是星期几"]
        assert context.projection.provisional is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_prepare_provisional_drift_still_commits() -> None:
    bridge = _CapturingGenerationBridge()
    identity = _device_identity("device-clock-fact-drift")
    runtime = DuplexRuntime.create(session_id=identity.session_id, barge_in_enabled=False)
    registry_holder: dict[str, MediaVoiceCoreRegistry] = {}
    context_holder: dict[str, Any] = {}

    class _ProvisionalDriftProvider(_AckCapturingProvider):
        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            drift = SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="late-partial",
                revision=3,
                kind=SegmentKind.ASR_PARTIAL,
                capture_start_sample=0,
                capture_end_sample=16_000,
                text="今天星期几",
            )
            context = context_holder["context"]
            assert runtime.ingest_media_speech_segment(drift)
            await registry_holder["registry"]._apply_projection_segment(context, drift)
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = _ProvisionalDriftProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: runtime,
    )
    registry.install()
    registry_holder["registry"] = registry
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        context_holder["context"] = context
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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000

        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=16_000,
        )
        assert fence is not None
        assert reason is None
        user_turns = [
            turn.content
            for turn in runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天是星期几"]
        assert context.projection.provisional is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_pin_survives_replayed_vad_start() -> None:
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
    identity = _device_identity("device-clock-fact-vad-start")
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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        pinned = context.pending.clock_fact_endpoint_pinned
        assert pinned == 16_000
        assert context.pending.turn_endpoint_sample == pinned

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="replayed-vad-start",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=20_000,
                capture_end_sample=20_001,
            ),
        )
        assert context.pending.clock_fact_endpoint_pinned == pinned
        assert context.pending.turn_endpoint_sample == pinned
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_pinned_clock_fact_commits_without_asr_endpoint_coverage() -> None:
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
    identity = _device_identity("device-clock-fact-coverage")
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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        context.pending.turn_end_sample = 8_000
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_commit_survives_recovery_reschedule() -> None:
    """Late overlap recovery must not abort an in-flight clock-fact commit.

    Field (2026-09-03 epoch 1366): FunASR early clock-fact entered commit
    (speaker classify), SenseVoice ``cross_sentence_overlap`` recovery
    re-armed ``_schedule_turn_commit`` and cancelled the grace task mid-flight;
    ASR tail timeout then discarded the weekday turn despite timeline text.
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
    identity = _device_identity("device-clock-fact-reschedule")
    session = bridge.bridge.open(identity)
    commit_entered = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = registry._commit_pending_turn

    async def delayed_commit(context, *, provider_final_missing=False, schedule_prepare_retry=True):
        commit_entered.set()
        await allow_commit.wait()
        return await original_commit(
            context,
            provider_final_missing=provider_final_missing,
            schedule_prepare_retry=schedule_prepare_retry,
        )

    registry._commit_pending_turn = delayed_commit  # type: ignore[method-assign]
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

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
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.clock_fact_endpoint_pinned == 16_000
        first_task = context.turn_endpoint_task
        assert first_task is not None

        await asyncio.wait_for(commit_entered.wait(), timeout=1)
        # Simulate late offline recovery re-arming the same endpoint.
        registry._schedule_turn_commit(context)
        second_task = context.turn_endpoint_task
        assert second_task is not None
        assert second_task is not first_task
        allow_commit.set()

        for _ in range(50):
            user_turns = [
                turn.content
                for turn in context.runtime.orchestrator.context.turns
                if turn.role == "user" and turn.content
            ]
            if user_turns == ["今天星期几"] and context.pending.turn_endpoint_sample is None:
                break
            await asyncio.sleep(0.05)
        else:
            user_turns = [
                turn.content
                for turn in context.runtime.orchestrator.context.turns
                if turn.role == "user" and turn.content
            ]
            raise AssertionError(
                f"clock-fact turn did not commit after recovery reschedule: "
                f"user_turns={user_turns!r} endpoint={context.pending.turn_endpoint_sample}"
            )
    finally:
        allow_commit.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_mostly_committed_straddling_final_is_dropped_not_readopted() -> None:
    """One question must not become two turns (real session 2026-09-14 epoch 1946).

    The offline rescue of already committed audio arrived as a shorter second
    transcript covering samples 11200-392000 while the committed watermark was
    369280, so 94% of its audio had already been committed and answered. Without
    complete word timings that text cannot be cut at the watermark, and adopting
    it re-answered the same audio: the operator heard the filler twice and the
    pending first answer was preempted.
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
    identity = _device_identity("device-straddle-rescue")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        context.asr.mark_committed(369_280)
        context.runtime.commit_media_speech_range(
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=369_280,
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
        from services.agent.src.voice_core.speech_timeline import ASRResult

        rescue = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=2,
            sentence_id="rescue-final",
            revision=1,
            capture_start_sample=11_200,
            capture_end_sample=392_000,
            text="明天南京的天气怎么样呢",
            is_final=True,
            confidence=0.9,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            rescue,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
        assert context.pending.live_query_forced_text is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_commits_when_provisional_lags_asr_end() -> None:
    """A weekday pin must commit even if projection still holds a short VAD range.

    Field (2026-09-04 epoch 1384): FunASR accepted ``今天星期几`` and early-
    committed endpoint=154880, then ``validate_commit`` failed closed as
    ``projection_range_mismatch``. The board stayed silent until owner silence.
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
    identity = _device_identity("device-clock-fact-range-lag")
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
                segment_id="clock-vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=84_160,
                capture_end_sample=84_161,
            ),
        )
        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=123_520,
            capture_end_sample=154_880,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        # Reproduce the field lag: VAD-only provisional end, ASR pin already armed.
        context.projection._provisional = replace(  # noqa: SLF001
            context.projection.provisional,
            capture_start_sample=84_160,
            capture_end_sample=84_161,
            text="今天星期几",
        )
        context.pending.turn_start_sample = 84_160
        context.pending.turn_end_sample = 154_880
        context.pending.turn_endpoint_sample = 154_880
        context.pending.turn_retire_sample = 154_880
        context.pending.clock_fact_endpoint_pinned = 154_880
        context.pending.turn_endpoint_grace_deadline = time.monotonic()
        registry._schedule_turn_commit(context)
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_recovered_after_overlap_without_vad() -> None:
    """Overlap recovery must arm turn_start, not only the clock-fact pin.

    Field (2026-09-04 epoch 1384 hop 2): the weekday final was rejected as
    ``cross_sentence_overlap``, recovery pinned endpoint=205760 with
    ``turn_start_sample is None``, then ASR tail timeout discarded the turn
    as ``low_rms``.
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
    identity = _device_identity("device-clock-fact-overlap-no-vad")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=180_000,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        echo_decision = await registry._accept_asr_result_decision(
            identity.session_id,
            echo,
        )
        assert echo_decision.accepted is not None
        weekday = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-overlap",
            revision=1,
            capture_start_sample=123_520,
            capture_end_sample=205_760,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
            rescue_synthesized=True,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            weekday,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.pending.turn_start_sample is not None
        assert context.pending.turn_endpoint_sample == 205_760
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_low_energy_rescue_close_rejection_does_not_request_standby() -> None:
    """A near-silent rescue final must not turn overlap rejection into close."""

    class LowEnergyRescueProvider(_AckCapturingProvider):
        def current_asr_audio_task_snapshot(self) -> ProviderAudioTaskSnapshot:
            return ProviderAudioTaskSnapshot(
                task_epoch=1,
                task_sample_origin=0,
                audio_start_sample=0,
                audio_end_sample=336_960,
                send_count=1,
                observed_sample_count=336_960,
                peak_abs=0,
                rms=0.0,
                all_zero=True,
            )

    provider = LowEnergyRescueProvider()
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
    identity = _device_identity("device-low-energy-close-rescue")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)

        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

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
        assert (
            await registry._accept_asr_result_decision(identity.session_id, echo)
        ).accepted is not None

        farewell = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="low-energy-farewell-final",
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
        assert context.pending.conversation_close_endpoint_pinned is None
        assert context.pending.turn_endpoint_sample is None
        assert context.standby_requested is False
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rescue_rms", "rescue_peak_abs", "expect_close"),
    [
        (389, 2_616, False),
        (3_263, 32_768, True),
    ],
    ids=["room-noise-rescue", "owner-speech-rescue"],
)
async def test_device_straddling_rescue_farewell_needs_speech_energy(
    rescue_rms: int,
    rescue_peak_abs: int,
    expect_close: bool,
) -> None:
    """A rescue farewell decoded from room noise must not end the session.

    Run 2026-09-24 session b18fede9: a weather answer waited behind noise
    VAD, SenseVoice turned the noise into three characters (rms 389, peak
    2616, FunASR silent), the straddle recovery honoured it as a farewell,
    and the pending answer was cancelled before its first frame.
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
    identity = _device_identity(f"device-straddle-rescue-close-{rescue_rms}")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        context.asr.mark_committed(64_000)
        context.runtime.commit_media_speech_range(
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=64_000,
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        farewell = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="0",
            revision=1,
            capture_start_sample=40_000,
            capture_end_sample=160_000,
            text="拜拜。",
            is_final=True,
            rescue_synthesized=True,
            rescue_rms=rescue_rms,
            rescue_peak_abs=rescue_peak_abs,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            farewell,
        )

        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
        if expect_close:
            assert context.pending.conversation_close_endpoint_pinned == 160_000
        else:
            assert context.pending.conversation_close_endpoint_pinned is None
            assert context.pending.turn_endpoint_sample is None
            assert context.standby_requested is False
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_empty_asr_asks_user_to_repeat() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=0.02,
    )
    registry.install()
    identity = _device_identity("device-empty-hear")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        async def classify_owner(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
            return _verified_owner_decision()

        context.runtime.set_speaker_classifier(classify_owner, sample_rate=16_000)
        context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id="empty-asr-start",
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
                segment_id="empty-asr-end",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=16_000,
                capture_end_sample=16_001,
                final=True,
                voiced_end_sample=16_000,
            ),
        )
        endpoint_sample = context.pending.turn_endpoint_sample
        assert endpoint_sample == 16_000
        endpoint_task = context.turn_endpoint_task
        assert endpoint_task is not None
        endpoint_task.cancel()
        await asyncio.gather(endpoint_task, return_exceptions=True)
        timeout_handle = context.pending.turn_endpoint_timeout_handle
        if timeout_handle is not None:
            timeout_handle.cancel()
            context.pending.turn_endpoint_timeout_handle = None

        decision = _verified_owner_decision()
        context.runtime._speaker_decision = decision
        context.runtime._speaker_class = decision.classification

        await registry._expire_endpoint_tail(
            identity.session_id,
            identity.stream_epoch,
            endpoint_sample,
        )

        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert provider.texts == [BRIDGE_PHRASES[2]]
        assert context.runtime.output_floor_allows_assistant
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_empty_asr_without_owner_stays_silent() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-empty-no-owner")
    session = bridge.bridge.open(identity)
    try:
        await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=16_000,
        )
        assert fence is None
        assert reason == "empty_media_turn"
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
    finally:
        await registry.finalize_session(identity.session_id)


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
        # The board re-transcribes the same question over the contiguous range.
        # Behind an accepted final the real pipeline rejects this preview as
        # straddles_committed_without_timing and still commits the extension, so
        # pin the timeline directly instead of going through accept_asr_result.
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
        context.pending.turn_start_sample = 600
        context.pending.turn_end_sample = 1200
        context.pending.turn_endpoint_sample = 1200
        context.pending.turn_retire_sample = 1240
        repeat_fence, repeat_reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=600,
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
        assert BRIDGE_PHRASES[4] not in provider.output_texts
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
async def test_vad_endpoint_rejects_final_beyond_bounded_clock_skew() -> None:
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

    await asyncio.sleep(0.05)

    assert [
        turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ] == []
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


@pytest.mark.asyncio
async def test_media_playback_overlap_finals_stay_out_of_the_next_commit(
    device_media_session: Any,
) -> None:
    """Bridge evidence 2026-09-15 epoch 1955: a played-back reply on the uplink.

    Five finals were accepted while the previous reply still owned playback
    (text_len 3, 4, 11, 4 and 7), the device reported no VAD edge at all after
    sample 113600, and the real question then arrived as text_len=10 on samples
    484480-508800.  The turn committed as text_len=42: ``turn_start_sample`` had
    been dragged back to the earliest of those finals, so the commit joined
    every pending timeline segment inside the resulting range, and that
    transcript is what the location lookup then searched.  Per-segment provider
    text was not captured, so the placeholders keep the logged lengths.
    """

    window = await device_media_session("playback-overlap-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _feed_playback_window_finals(window)
    assert context.pending.pending_turn_playback_overlap is True

    # Before the real question arrives the pending candidate window already
    # resolves those five finals as one 29-character transcript: that is the
    # polluted shape the field session committed as 42 characters.  The turn
    # boundary below is what has to keep it out of the committed range.
    pre_split = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=158_560,
        end_sample=_ABANDONED_WINDOW_END,
    )
    assert pre_split is not None and "AAA" in pre_split
    # The live projection already carries the same polluted text, so the commit
    # has to reconcile an in-flight projection against the new boundary.
    assert context.projection.provisional is not None
    assert "AAA" in context.projection.provisional.text

    _start_retained_utterance(window)  # the previous reply ended here
    question = "今天南京的天气怎么样"
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="question-13",
        start_sample=484_480,
        end_sample=508_800,
        text=question,
    )

    # 350080 -> 484480 is 8.4 s, far beyond the conservative candidate gap: the
    # window closes and the question owns the turn on its own samples.
    assert context.pending.turn_start_sample == 484_480
    assert context.pending.turn_end_sample == 508_800

    text = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=context.pending.turn_start_sample,
        end_sample=context.pending.turn_end_sample,
    )
    assert text == question

    # The abandoned window is retired, not merely out of range: resolving it
    # again (and resolving the wide pre-fix range) can no longer reach its text.
    abandoned = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=158_560,
        end_sample=_ABANDONED_WINDOW_END,
    )
    assert not abandoned
    wide = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=158_560,
        end_sample=508_800,
    )
    assert wide == question

    # The device chain then prepares exactly one turn, and the provider is
    # handed only the new question.
    await _wait_until(lambda: bool(window.provider.prepared), timeout=2.0)
    assert window.provider.prepared == [question]
    assert _user_turn_texts(context) == [question]


@pytest.mark.asyncio
async def test_media_out_of_order_final_cannot_rejoin_a_split_pending_turn(
    device_media_session: Any,
) -> None:
    """A late non-overlapping final for the abandoned window must not flow back.

    The ASR supervisor deliberately keeps out-of-order non-overlapping finals
    (``accept_result``), so the boundary cannot rely on arrival order: the split
    has to be a lifecycle decision that survives a later final for the audio it
    already closed.
    """

    window = await device_media_session("split-out-of-order-session")
    registry, identity, context = window.registry, window.identity, window.context
    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    for sentence_id, start_sample, end_sample, text, revision in (
        ("playback-2", 158_560, 163_040, "AAA", 1),
        ("playback-3", 185_280, 204_480, "BBBB", 2),
    ):
        await _accept_media_asr_final(
            registry,
            identity,
            sentence_id=sentence_id,
            start_sample=start_sample,
            end_sample=end_sample,
            text=text,
            revision=revision,
        )
    _start_retained_utterance(window)
    question = "下午一起出发吗"
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="question-13",
        start_sample=484_480,
        end_sample=508_800,
        text=question,
    )

    # Non-overlapping, so the supervisor would accept it on interval grounds;
    # the pending-turn boundary is what has to fail it closed.
    late = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="late-playback-9",
        start_sample=317_280,
        end_sample=332_640,
        text="DDDD",
        revision=9,
    )
    assert late.accepted is None
    assert late.reason is ASRDecisionReason.INTERVAL_CONFLICT

    text = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=context.pending.turn_start_sample,
        end_sample=context.pending.turn_end_sample,
    )
    assert text == question
    assert context.pending.turn_start_sample == 484_480


@pytest.mark.asyncio
async def test_media_long_pause_without_playback_overlap_still_merges(
    device_media_session: Any,
) -> None:
    """An ordinary utterance that pauses longer than the tail keeps both clauses."""

    window = await device_media_session("long-pause-session", during_playback=False)
    registry, identity, context = window.registry, window.identity, window.context

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="clause-1",
        start_sample=0,
        end_sample=8_000,
        text="未来三天南京天气",
    )
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="clause-2",
        start_sample=90_000,
        end_sample=100_000,
        text="还有明天呢",
        revision=2,
    )

    assert context.pending.turn_start_sample == 0
    assert context.pending.turn_end_sample == 100_000
    text = _resolve_media_turn_text(
        context, stream_epoch=1, start_sample=0, end_sample=100_000
    )
    assert text is not None
    assert "未来三天南京天气" in text and "还有明天呢" in text


@pytest.mark.asyncio
async def test_media_out_of_order_final_inside_the_retained_window_still_merges(
    device_media_session: Any,
) -> None:
    """The pending-turn boundary must not cost same-turn out-of-order support.

    The retained window is one utterance with a hole in it (two clauses closer
    than the tail).  A final that arrives last but belongs inside that hole is
    still part of the same turn and has to merge.
    """

    window = await device_media_session("split-retained-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _accept_media_asr_final(
       registry,
       identity,
       sentence_id="playback-2",
       start_sample=158_560,
       end_sample=163_040,
       text="AAA",
    )
    _start_retained_utterance(window)
    await _accept_media_asr_final(
       registry,
       identity,
       sentence_id="clause-1",
       start_sample=484_480,
       end_sample=490_000,
       text="今天南京",
    )
    await _accept_media_asr_final(
       registry,
       identity,
       sentence_id="clause-2",
       start_sample=500_000,
       end_sample=508_000,
       text="还有明天呢",
       revision=2,
    )

    # Non-overlapping, inside the retained window's hole: same turn, same onset.
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="clause-1b",
        start_sample=493_000,
        end_sample=498_000,
        text="的",
        revision=3,
    )

    assert context.pending.turn_start_sample == 484_480
    text = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=context.pending.turn_start_sample,
        end_sample=context.pending.turn_end_sample,
    )
    assert text is not None
    assert "AAA" not in text
    assert "今天南京" in text and "的" in text and "还有明天呢" in text


@pytest.mark.asyncio
async def test_media_playback_followup_endpoints_without_vad_edge(
    device_media_session: Any,
) -> None:
    """Run 20260921 window-a: a follow-up after playback must not wait ~20 s.

    The realtime chain recognized the follow-up, but the device VAD stayed
    active across the playback echo and the offline paragraph was rejected
    for straddling the committed boundary, so nothing endpointed the turn
    until the next vad.start.  A final that begins wholly after the playback
    boundary now owns the turn and pins the endpoint itself, with no vad.end
    and no offline paragraph ever arriving.
    """

    window = await device_media_session("followup-early-endpoint-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    # Boundary = last accepted evidence end (190_000) + echo-tail margin.
    assert context.last_playback_end_sample == 202_800

    # The echo-holdover VAD stays active across the boundary; it must not
    # keep suppressing the split or the follow-up endpoint.
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )

    assert context.pending.turn_start_sample == 215_000
    assert context.pending.turn_endpoint_sample == 230_000
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert "后天呢" in _user_turn_texts(context)
    # The abandoned echo transcript is evicted, not merely out of range.
    assert not _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=160_000,
        end_sample=190_000,
    )


@pytest.mark.asyncio
async def test_media_playback_followup_echo_tail_cannot_endpoint(
    device_media_session: Any,
) -> None:
    """A final that begins before the playback boundary may still be echo.

    The supervisor accepts interior finals on interval grounds, so the echo
    guard has to fail them closed on its own: no early endpoint, and the
    later real follow-up still owns the turn on exclusively its samples.
    """

    window = await device_media_session("followup-echo-guard-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000

    tail = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="tail-1",
        start_sample=195_000,
        end_sample=208_000,
        text="多云转晴",
    )
    assert tail.accepted is not None, tail.reason
    assert context.pending.turn_endpoint_sample is None
    assert window.provider.prepared == []

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )
    assert context.pending.turn_start_sample == 215_000
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert "多云转晴" not in window.provider.prepared[0]


@pytest.mark.asyncio
async def test_media_playback_followup_survives_late_rejected_result(
    device_media_session: Any,
) -> None:
    """A rejection between arming and commit must not cancel the endpoint.

    Window-a lost the follow-ups partly because the only authoritative
    confirmation (the offline paragraph) was rejected and nothing re-armed
    the endpoint.  The armed follow-up endpoint now stands on the accepted
    realtime final alone.
    """

    window = await device_media_session("followup-rejection-tolerant-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )
    assert context.pending.turn_endpoint_sample == 230_000

    # Interior cross-sentence overlap: rejected by the supervisor, the same
    # class of rejection the offline paragraphs died from in the field run.
    late = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="offline-9",
        start_sample=220_000,
        end_sample=228_000,
        text="南京呢",
        revision=9,
    )
    assert late.accepted is None

    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)


@pytest.mark.asyncio
async def test_media_playback_followup_advances_with_continued_speech(
    device_media_session: Any,
) -> None:
    """Continued post-boundary finals extend one follow-up turn, not two.

    A stuck VAD must not truncate the utterance at the first final either:
    each later guarded final advances the pinned endpoint and resets the
    grace, so both clauses commit together as one turn.
    """

    window = await device_media_session("followup-continued-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=225_000,
        text="那后天呢",
    )
    assert context.pending.turn_endpoint_sample == 225_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-2",
        start_sample=230_000,
        end_sample=240_000,
        text="天气怎么样",
        revision=2,
    )
    assert context.pending.turn_endpoint_sample == 240_000

    await _wait_until(lambda: len(window.provider.prepared) == 1, timeout=3.0)
    prepared = window.provider.prepared[0]
    assert "那后天呢" in prepared and "天气怎么样" in prepared


@pytest.mark.asyncio
async def test_media_playback_followup_empty_final_does_not_pin_the_endpoint(
    device_media_session: Any,
) -> None:
    """2026-09-28 field: an empty post-playback final pinned the endpoint.

    Pinned before a single word was recognized, the turn's absolute tail
    bound ran out while the user was still asking about the weather.
    """

    window = await device_media_session("followup-empty-final-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="followup-empty",
        start_sample=215_000,
        end_sample=225_000,
        text="",
    )
    assert context.pending.turn_endpoint_sample is None
    assert context.pending.turn_endpoint_tail_deadline is None

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=226_000,
        end_sample=236_000,
        text="今天天气怎么样",
    )
    assert context.pending.turn_endpoint_sample == 236_000
    await _wait_until(lambda: window.provider.prepared == ["今天天气怎么样"], timeout=3.0)


@pytest.mark.asyncio
async def test_media_playback_followup_advance_restarts_the_absolute_tail_bound(
    device_media_session: Any,
) -> None:
    """Continued speech moves the absolute tail bound with the endpoint.

    Kept from the first pin, the bound expired mid-sentence and the device
    was put on standby (turn_prepare_timeout) with the question unanswered.
    """

    window = await device_media_session("followup-tail-bound-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=225_000,
        text="今天",
    )
    first_deadline = context.pending.turn_endpoint_tail_deadline
    first_handle = context.pending.turn_endpoint_timeout_handle
    assert first_deadline is not None and first_handle is not None

    await asyncio.sleep(0.05)
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-2",
        start_sample=230_000,
        end_sample=240_000,
        text="天气怎么样",
        revision=2,
    )
    assert context.pending.turn_endpoint_sample == 240_000
    assert context.pending.turn_endpoint_tail_deadline is not None
    assert context.pending.turn_endpoint_tail_deadline > first_deadline
    assert first_handle.cancelled()
    assert context.pending.turn_endpoint_timeout_handle is not None
    assert not context.standby_requested


@pytest.mark.asyncio
async def test_textless_vad_cannot_hold_an_answered_question_open(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-28 field: background sound deferred a weather answer 17 s.

    The question was recognized and endpointed, then vad.start/vad.end kept
    cycling on sound that never produced any text; each start reopened the
    turn, so it committed only when the room went quiet. The reopened turn
    now commits the question once a bounded window passes with no new text,
    however many times it is reopened meanwhile.
    """

    monkeypatch.setattr(media_session_input, "_REOPEN_EVIDENCE_WINDOW_S", 0.2)
    window = await device_media_session("reopen-noise-session")
    context = window.context
    await _complete_previous_device_playback(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=215_000,
        end_sample=230_000,
        text="给我讲个小故事吧",
    )
    assert context.pending.turn_endpoint_sample == 230_000
    # Background sound: starts and ends that never bring text.
    await _device_vad(window, "noise-1", 235_000, final=False)
    assert context.pending.turn_endpoint_sample is None
    await _device_vad(window, "noise-1-end", 250_000, final=True)
    await asyncio.sleep(0.05)
    await _device_vad(window, "noise-2", 252_000, final=False)
    await asyncio.sleep(0.05)
    await _device_vad(window, "noise-3", 260_000, final=False)

    await _wait_until(lambda: window.provider.prepared == ["给我讲个小故事吧"], timeout=2.0)


@pytest.mark.asyncio
async def test_reopened_turn_keeps_waiting_when_the_new_speech_has_text(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window only ends a reopen that brought no words of its own."""

    monkeypatch.setattr(media_session_input, "_REOPEN_EVIDENCE_WINDOW_S", 0.2)
    window = await device_media_session("reopen-continued-session")
    context = window.context
    await _complete_previous_device_playback(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question-1",
        start_sample=215_000,
        end_sample=225_000,
        text="我今天有点累",
    )
    await _device_vad(window, "continued", 228_000, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question-2",
        start_sample=229_000,
        end_sample=240_000,
        text="想早点休息",
        revision=2,
    )
    await asyncio.sleep(0.35)
    # Past the window, the still-open speech that did bring text has not
    # been cut at the first endpoint.
    assert window.provider.prepared == []
    assert context.pending.turn_end_sample == 240_000
    await _device_vad(window, "continued-end", 240_000, final=True)
    await _wait_until(lambda: len(window.provider.prepared) == 1, timeout=3.0)
    prepared = window.provider.prepared[0]
    assert "我今天有点累" in prepared and "想早点休息" in prepared


@pytest.mark.asyncio
async def test_reopen_window_never_commits_a_newer_turn_at_the_old_endpoint(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_session_input, "_REOPEN_EVIDENCE_WINDOW_S", 60.0)
    window = await device_media_session("reopen-newer-turn-session")
    context = window.context
    await _complete_previous_device_playback(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=215_000,
        end_sample=230_000,
        text="给我讲个小故事吧",
    )
    await _device_vad(window, "noise", 235_000, final=False)
    handle = context.pending.reopen_evidence_handle
    assert handle is not None and context.pending.reopen_evidence_endpoint == 230_000
    handle.cancel()
    # The logical turn changed meanwhile (committed, then a new one opened).
    context.pending.turn_start_sample = 300_000
    window.registry._expire_reopen_evidence_window(
        window.identity.session_id, window.identity.stream_epoch, 230_000
    )
    assert context.pending.turn_endpoint_sample is None
    assert context.pending.reopen_evidence_endpoint is None
    await asyncio.sleep(0.05)
    assert window.provider.prepared == []


@pytest.mark.asyncio
async def test_media_playback_overlap_split_is_blocked_by_a_vad_anchored_turn(
    device_media_session: Any,
) -> None:
    """A VAD-admitted candidate keeps its onset; dropping the anchor splits it.

    The positive control is the point: with the anchor removed the very same
    accepted final and gap must split the window, so the blocked assertion
    cannot be passing because an unrelated gate rejected the final first.
    """

    window = await device_media_session("split-vad-anchor")
    await _feed_playback_window_finals(window)
    _start_retained_utterance(window)
    context = window.context
    assert context.pending.pending_turn_playback_overlap is True
    assert context.pending.turn_start_sample == 158_560
    assert context.pending.turn_end_sample == _ABANDONED_WINDOW_END

    context.pending.active_vad_start_sample = _ABANDONED_WINDOW_END
    anchored = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-1",
        start_sample=400_000,
        end_sample=410_000,
        text="下午一起出发吗",
    )
    assert anchored.accepted is not None
    assert context.pending.turn_start_sample == 158_560

    context.pending.active_vad_start_sample = None
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


@pytest.mark.parametrize(
    ("late_text", "pin_attribute"),
    (
        ("今天几号", "clock_fact_endpoint_pinned"),
        ("明天南京天气怎么样", "live_query_endpoint_pinned"),
        ("再见", "conversation_close_endpoint_pinned"),
    ),
)
@pytest.mark.asyncio
async def test_device_abandoned_window_final_cannot_pin_pollute_or_close(
    device_media_session: Any,
    late_text: str,
    pin_attribute: str,
) -> None:
    """Audio for the closed candidate window may not pin, pollute or close."""

    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    window = await _open_retained_window(
        device_media_session,
        f"device-abandoned-{pin_attribute}",
    )
    late = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="late-old-1",
        start_sample=352_000,
        end_sample=360_000,
        text=late_text,
    )
    assert late.accepted is None
    assert late.reason is ASRDecisionReason.INTERVAL_CONFLICT
    assert getattr(window.context.pending, pin_attribute) is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]
    assert _user_turn_texts(window.context) == ["下午一起出发吗"]
    assert window.context.standby_requested is False


@pytest.mark.asyncio
async def test_device_abandoned_window_partial_cannot_pin_or_pollute(
    device_media_session: Any,
) -> None:
    """A late partial for the closed window may not seed a semantic endpoint."""

    window = await _open_retained_window(device_media_session, "device-abandoned-partial")
    late = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="late-partial-1",
        start_sample=352_000,
        end_sample=360_000,
        text="再见",
        is_final=False,
    )
    assert late.accepted is None
    assert window.context.pending.conversation_close_partial_text is None
    assert window.context.pending.conversation_close_endpoint_pinned is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_old_vad_edge_cannot_pull_the_retained_onset_back(
    device_media_session: Any,
) -> None:
    """A late VAD edge for the closed window is not admitted and cannot re-anchor."""

    window = await _open_retained_window(device_media_session, "device-old-vad")
    context = window.context
    for segment_id, revision, final, sample, voiced_end in (
        ("stale-vad-start", 1, False, 317_280, None),
        ("stale-vad-end", 2, True, 332_640, 332_640),
    ):
        await window.registry.on_speech_segment(
            window.session,
            SpeechSegment(
                session_id=window.identity.session_id,
                stream_epoch=window.identity.stream_epoch,
                provider_task_epoch=8,
                segment_id=segment_id,
                revision=revision,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=voiced_end,
            ),
        )

    assert context.pending.active_vad_start_sample is None
    assert context.pending.turn_start_sample == 484_480
    assert context.pending.turn_endpoint_sample is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_current_vad_still_extends_the_retained_window(
    device_media_session: Any,
) -> None:
    """The current utterance's VAD edges still open and close the retained window."""

    window = await _open_retained_window(
        device_media_session,
        "device-current-vad",
        retained_end=490_000,
    )
    context = window.context
    await window.registry.on_speech_segment(
        window.session,
        SpeechSegment(
            session_id=window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            provider_task_epoch=8,
            segment_id="current-vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=500_000,
            capture_end_sample=500_001,
        ),
    )
    assert context.pending.active_vad_start_sample == 500_000
    await _accept_media_asr_final(
       window.registry,
       window.identity,
       sentence_id="clause-2",
       start_sample=500_000,
       end_sample=508_000,
       text="还有明天呢",
       revision=2,
    )
    assert context.pending.turn_start_sample == 484_480

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_000)
    text = window.provider.prepared[-1]
    assert "下午一起出发吗" in text and "还有明天呢" in text


@pytest.mark.asyncio
async def test_device_pinned_endpoint_is_never_split(device_media_session: Any) -> None:
    """An already endpointed turn keeps its range while it is committing."""

    window = await _open_retained_window(
        device_media_session,
        "device-pinned-endpoint",
        retained_end=490_000,
    )
    context = window.context
    context.pending.pending_turn_playback_overlap = True
    context.pending.turn_endpoint_sample = 490_000
    context.pending.turn_retire_sample = 490_000
    context.pending.turn_endpoint_grace_deadline = time.monotonic() + 60.0

    far = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-1",
        start_sample=532_000,
        end_sample=540_000,
        text="还有明天呢",
        revision=2,
    )
    assert far.accepted is not None
    assert context.pending.turn_start_sample == 484_480
    assert context.pending.turn_endpoint_sample == 490_000

    # Positive control: the accepted final and its 42 000-sample gap above the
    # candidate tail do split once the endpoint is gone, so the endpoint, not an
    # unrelated gate, was what blocked the earlier final.
    context.pending.turn_endpoint_sample = None
    context.pending.turn_retire_sample = None
    split = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-2",
        start_sample=600_000,
        end_sample=610_000,
        text="还有明天呢",
        revision=3,
    )
    assert split.accepted is not None
    assert context.pending.turn_start_sample == 600_000


@pytest.mark.asyncio
async def test_device_final_crossing_the_retained_onset_fails_closed(
    device_media_session: Any,
) -> None:
    """A revision that reaches back across the boundary may not carry old text."""

    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    window = await _open_retained_window(
        device_media_session,
        "device-boundary-revision",
        retained_end=490_000,
    )
    crossing = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=400_000,
        end_sample=495_000,
        text="AAA 下午一起出发吗",
        revision=2,
    )
    assert crossing.accepted is None
    assert crossing.reason is ASRDecisionReason.INTERVAL_CONFLICT

    text = _resolve_media_turn_text(
        window.context,
        stream_epoch=1,
        start_sample=window.context.pending.turn_start_sample,
        end_sample=window.context.pending.turn_end_sample,
    )
    assert text == "下午一起出发吗"

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=490_000)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_partial_only_overlap_window_cannot_pollute_the_next_commit(
    device_media_session: Any,
) -> None:
    """A candidate window built only from partials still owns no committed text."""

    window = await device_media_session("device-partial-overlap")
    partial = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="playback-partial-6",
        start_sample=258_880,
        end_sample=264_640,
        text="CCCCCCCCCCC",
        is_final=False,
    )
    assert partial.accepted is not None
    _start_retained_utterance(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=484_480,
        end_sample=508_800,
        text="下午一起出发吗",
    )
    assert window.context.pending.turn_start_sample == 484_480

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_media_registry_runs_fake_asr_llm_tts_through_both_fences() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
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
        # A Go-shadow request degrades to the only supported authority.
        assert (
            accepted.accepted.interaction_authority
            == media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
        )
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
        started = await _next_event(call, "generation")
        assert started.generation.generation_id == 1
        fence = GenerationFence(session_identity.session_id, 1, 1, 0)
        audio = await _next_event(call, "audio")
        assert audio.audio.generation_id == fence.generation_id
        completed = await _next_event(call, "generation")
        assert completed.generation.action == media_pb2.GENERATION_ACTION_COMPLETE
        context = registry.session_state(session_identity.session_id)
        assert context.output.playback.current_fence == fence
        assert context.output.playback._spans[fence]
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
async def test_registry_forwards_only_normalized_watermark_tail() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("registry-normalized-tail")
    context = await registry.open_session(identity)
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
    context = await registry.open_session(identity)
    # Successive overlapping turns are ordinary barge-ins: they need verified
    # owner authority for the utterance that produces them.
    _bind_verified_owner_classifier(context.runtime)

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
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
    await audio(0, 0)
    assert context.projection.provisional is not None
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]
    await commit("end-1", 320)
    assert context.projection.provisional is None

    await vad("start-2", 320, final=False)
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
    await audio(1, 320)
    await commit("end-2", 640)
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界"]

    await audio(2, 640)
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界"]
    assert context.pending.turn_start_sample is None
    assert context.pending.turn_end_sample is None

    await vad("start-3", 960, final=False)
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
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
