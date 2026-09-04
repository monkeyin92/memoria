from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.context_snapshot_manager import (
    MemoryCapsule,
    PersonaCapsule,
)
from services.agent.src.orchestration.conversation_projection import ConversationProjection
from services.agent.src.orchestration.delegation_coordinator import OutputIntentAdmission
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.prompts import BRIDGE_PHRASES, DEVICE_WAKE_PHRASES, device_wake_phrase
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    FunASRServerEvent,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    PlaybackEventType,
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
from services.agent.src.voice_core.media_session_output_stream import _next_pcm_send_slot
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
    OutputDispatchResult,
    OutputDispatchStatus,
    ProviderAudioTaskSnapshot,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
    asr_result_to_segment,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


class FakeMediaProvider(MediaVoiceProvider):
    def __init__(self) -> None:
        self.audio_calls: list[int] = []
        self.cancelled: list[GenerationFence] = []
        self.pause_asr_calls: list[int] = []
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

    async def pause_asr_for_playback(self, identity: SessionIdentity) -> None:
        self.pause_asr_calls.append(identity.stream_epoch)

    async def close(self, _identity: SessionIdentity) -> None:
        self.closed = True


def _verified_owner_decision() -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.95,
        quality_score=0.95,
        reason_code="owner_match",
        model_version="speaker-test-v1",
        template_version=1,
        profile_id="owner-profile",
        permissions=permissions_for_speaker("owner"),
    )


def test_pcm_output_pacer_spaces_frames_and_does_not_burst_after_a_stall() -> None:
    delay, next_send_at = _next_pcm_send_slot(
        now=10.0,
        next_send_at=10.0,
        frame_samples=480,
    )
    assert delay == 0
    assert next_send_at == pytest.approx(10.02)

    delay, next_send_at = _next_pcm_send_slot(
        now=10.005,
        next_send_at=next_send_at,
        frame_samples=480,
    )
    assert delay == pytest.approx(0.015)
    assert next_send_at == pytest.approx(10.04)

    delay, next_send_at = _next_pcm_send_slot(
        now=11.0,
        next_send_at=next_send_at,
        frame_samples=480,
    )
    assert delay == 0
    assert next_send_at == pytest.approx(11.02)


class _CapturingMediaBridge(MediaBridgeGrpcServer):
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


async def _seed_pending_media_turn(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    *,
    text: str,
    endpoint_sample: int = 600,
    retire_sample: int = 640,
) -> Any:
    context = await registry._get_or_create(identity)
    assert await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id=f"seed-{identity.session_id}",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=endpoint_sample,
            text=text,
            is_final=True,
            stream_epoch=identity.stream_epoch,
        ),
    )
    context.turn_endpoint_sample = endpoint_sample
    context.turn_retire_sample = retire_sample
    return context


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


def _queued_event(connection: Any, kind: str) -> Any:
    while not connection.outgoing.empty():
        event = connection.outgoing.get_nowait()
        if event is not None and event.WhichOneof("event") == kind:
            return event
    raise AssertionError(f"bridge queue did not contain {kind}")


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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
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
    await registry._finalize_session(identity.session_id)


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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.03,
    )
    context = await registry._get_or_create(identity)

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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.03,
    )
    context = await registry._get_or_create(identity)

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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.30,
    )
    context = await registry._get_or_create(identity)

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


def _owner_silence_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )


@pytest.mark.asyncio
async def test_owner_silence_budget_is_not_refreshed_without_verified_authority() -> None:
    """Unverified speakers spend the owner's window without ever refilling it.

    ``_finish_owner_silence_turn`` refreshes the full interval only when the
    committed turn carries a verified owner classification.  When speaker
    authority is unavailable or denied, each accepted turn keeps whatever the
    previous turn left behind, so a multi-turn conversation drains one single
    window and the session can close moments after a reply finishes playing.
    This test pins that carry-over arithmetic so the behaviour cannot change
    silently in either direction.
    """

    identity = _owner_silence_identity("owner-silence-unverified-carryover")
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.30,
    )
    context = await registry._get_or_create(identity)

    # Authority is never established for this conversation.  This is the
    # ``subject_capability_forbidden`` / ``authority_unavailable`` shape, not a
    # guest: the runtime simply has no decision to project.
    assert context.runtime.current_speaker_authority_verified is False

    # Turn one: the owner speaks 0.12s into the window and the turn commits.
    await asyncio.sleep(0.12)
    registry._sync_owner_silence_phase(context, "user_speaking")
    registry._finish_owner_silence_turn(context, accepted=True)
    after_first = context.owner_silence_remaining_s
    assert after_first is not None
    assert after_first < 0.30, "an unverified turn must not mint a fresh window"
    assert after_first == pytest.approx(0.18, abs=0.05)

    # Playback of the reply, then the floor returns to the owner.  The resumed
    # timer carries the first turn's leftover rather than a full interval.
    registry._sync_owner_silence_phase(context, "speaking")
    registry._sync_owner_silence_phase(context, "listening")
    assert context.owner_silence_remaining_s == pytest.approx(after_first)

    # Turn two burns most of what is left and still gets no refresh.
    await asyncio.sleep(0.12)
    registry._sync_owner_silence_phase(context, "user_speaking")
    registry._finish_owner_silence_turn(context, accepted=True)
    after_second = context.owner_silence_remaining_s
    assert after_second is not None
    assert after_second < after_first
    assert registry.context(identity.session_id) is context.runtime

    # The floor returns once more, and the residue expires almost immediately.
    registry._sync_owner_silence_phase(context, "speaking")
    registry._sync_owner_silence_phase(context, "listening")
    await asyncio.sleep(after_second + 0.06)

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
    bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=0.30,
    )
    context = await registry._get_or_create(identity)
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
        await registry._finalize_session(identity.session_id)


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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        max_user_speech_duration_s=0.03,
    )
    context = await registry._get_or_create(identity)
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
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_registry_rejects_late_audio_after_terminal_cleanup() -> None:
    identity = SessionIdentity(
        "terminal-registry-input",
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
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    await registry._get_or_create(identity)
    await registry._finalize_session(identity.session_id)

    assert bridge.bridge.is_terminal(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
    )
    await registry.on_audio_frame(
        connection.session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )
    assert registry.context(identity.session_id) is None
    with pytest.raises(ValueError, match="media session is terminal"):
        await registry._get_or_create(identity)


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
    assert runtime.orchestrator.playback_stop_seam is None
    assert registry.context(identity.session_id) is None


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
    await registry._get_or_create(identity)

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
    context = await registry._get_or_create(identity)
    fence = await runtime.on_turn_committed("你好")
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "你好", fence))
    await asyncio.wait_for(reply_started.wait(), timeout=1)
    assert context.output_owner is not None
    assert context.playback.register_audio(fence, 0, 0, 2)
    assert context.playback.add_span(PlaybackSpan(fence, 0, 1, 0, 2, text="你", sequence=0))
    assert context.playback.acknowledge(fence, 2, received_sequence=0)
    stale_before = context.playback.stale_ack_count
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

    assert context.output_owner is None
    assert context.reply_task is None
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
    assert context.playback.actual_heard_text(fence) == ""
    assert context.playback.acknowledge(fence, 2, received_sequence=0) == ()
    assert context.playback.stale_ack_count == stale_before + 1
    assert interrupted == [(runtime_before, "")]
    assert [
        (turn.role, turn.content) for turn in context.runtime.orchestrator.context.turns
    ] == turns_before

    runtime.set_playback_stop_seam(None)
    await runtime.close()
    await provider.close(identity)


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
async def test_commit_accepts_asr_subrange_of_leading_vad_projection() -> None:
    identity = SessionIdentity("leading-vad-subrange")
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    provider = FakeMediaProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)
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
    context.turn_start_sample = 0
    context.turn_end_sample = 320
    context.turn_endpoint_sample = 320
    context.turn_retire_sample = 320

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
    await registry._finalize_session(identity.session_id)


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
    context = await registry._get_or_create(identity)
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
    context.turn_start_sample = 0
    context.turn_end_sample = 320
    context.turn_endpoint_sample = 320
    context.turn_retire_sample = 320

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
    await registry._finalize_session(identity.session_id)


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
    context = await registry._get_or_create(identity)
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
    context.turn_start_sample = 0
    context.turn_end_sample = 320
    context.turn_endpoint_sample = 320
    context.turn_retire_sample = 320

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
    await registry._finalize_session(identity.session_id)


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
async def test_vad_empty_provisional_resyncs_timeline_asr_instead_of_text_mismatch() -> None:
    identity = SessionIdentity("weather-projection-resync", stream_epoch=1)
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    context = await registry._get_or_create(identity)
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
    await registry._finalize_session(identity.session_id)


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
    retry_task = context.turn_commit_retry_task
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
    assert context.turn_commit_retry_task is None
    assert context.turn_commit_retry_attempt == 0
    assert context.turn_commit_retry_stream_epoch is None
    assert context.turn_commit_retry_endpoint_sample is None
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "scheduled"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "attempt"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "succeeded"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "exhausted"}) == 0
    await registry._finalize_session(identity.session_id)


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
    retry_task = context.turn_commit_retry_task
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
    await registry._finalize_session(identity.session_id)


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
    assert context.turn_commit_retry_task is None
    assert context.turn_commit_retry_attempt == 0
    assert context.turn_commit_retry_stream_epoch is None
    assert context.turn_commit_retry_endpoint_sample is None
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
async def test_prepare_retry_exhaustion_discards_once_at_transport_retire_sample() -> None:
    identity = SessionIdentity("prepare-retry-exhaustion")
    runtime = DuplexRuntime.create(session_id=identity.session_id)

    class AlwaysFailingPreparingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_calls = 0
            self.reply_calls = 0

        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            _text: str,
        ) -> GenerationFence:
            self.prepare_calls += 1
            raise RuntimeError("preparation unavailable")

        def generate_reply(
            self,
            identity: SessionIdentity,
            user_text: str,
            fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            self.reply_calls += 1
            return super().generate_reply(identity, user_text, fence)

    provider = AlwaysFailingPreparingProvider()
    bridge = _CapturingMediaBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
    )
    context = await _seed_pending_media_turn(
        registry,
        identity,
        text="重试耗尽后确定性丢弃",
    )

    assert await registry._commit_pending_turn(context) == "provider_prepare_failed"
    retry_task = context.turn_commit_retry_task
    assert retry_task is not None
    await asyncio.wait_for(retry_task, timeout=1)

    discarded = [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
    ]
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "provider_prepare_retries_exhausted"
    assert not [
        payload
        for event_type, payload in bridge.events
        if event_type == "turn.provisional.discarded"
        and payload.get("reason") == "provider_final_missing"
    ]
    assert provider.prepare_calls == 3
    assert provider.reply_calls == 0
    assert context.projection.provisional is None
    assert context.runtime.speech_timeline.committed_sample == 640
    assert context.asr.last_committed_sample == 640
    assert context.turn_commit_retry_task is None
    assert context.turn_commit_retry_attempt == 0
    assert context.turn_commit_retry_stream_epoch is None
    assert context.turn_commit_retry_endpoint_sample is None
    assert [turn for turn in runtime.orchestrator.context.turns if turn.role == "user"] == []
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "scheduled"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "attempt"}) == 2
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "exhausted"}) == 1
    assert registry.metrics.get("voice_turn_prepare_retry_total", {"status": "succeeded"}) == 0
    assert not await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="late-hangover-after-exhaustion",
            revision=1,
            capture_start_sample=600,
            capture_end_sample=640,
            text="尾静音迟到文本",
            is_final=True,
            stream_epoch=identity.stream_epoch,
        ),
    )
    await registry._finalize_session(identity.session_id)


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
    retry_task = context.turn_commit_retry_task
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

    assert context.turn_commit_retry_task is retry_task
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
    assert context.turn_commit_retry_task is None
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
async def test_audio_ingress_overflow_drops_oldest_and_marks_discontinuity() -> None:
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
        if provider.audio_calls == [0, 2, 3]:
            break
        await asyncio.sleep(0)

    # Overflow drops only the oldest buffered frame (sequence 1) so the newest
    # speech still reaches the provider; both gap boundaries reset the task.
    assert provider.audio_calls == [0, 2, 3]
    assert provider.reset_samples == [4, 6]
    assert registry.metrics.get("media_pcm_overflow_total") >= 1
    assert registry.metrics.get("media_discontinuity_total") >= 1
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_audio_ingress_provider_failure_drains_backlog_and_recovers_next_frame() -> None:
    class RecoveringProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.recoveries = 0
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 0:
                self.first_started.set()
                await self.release_first.wait()
                raise RuntimeError("provider send failed")
            return ()

        async def recover_after_failure(self, _identity: SessionIdentity) -> None:
            self.recoveries += 1

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    provider = RecoveringProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=4,
    )
    identity = SessionIdentity("provider-send-recovery", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)

    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await registry.on_audio_frame(session, AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2))
    await registry.on_audio_frame(session, AudioFrame(identity, 2, 4, 2, b"\x02\x00" * 2))
    provider.release_first.set()
    for _ in range(50):
        if context.ingress.provider_failed:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0]
    assert context.ingress.queue.empty()
    assert context.ingress.provider_failed is True

    await registry.on_audio_frame(session, AudioFrame(identity, 3, 6, 2, b"\x03\x00" * 2))
    for _ in range(50):
        if provider.audio_calls == [0, 3]:
            break
        await asyncio.sleep(0)

    assert provider.recoveries == 1
    assert provider.reset_samples == [6]
    assert provider.audio_calls == [0, 3]
    assert context.ingress.provider_failed is False
    await context.runtime.close()


@pytest.mark.asyncio
async def test_audio_ingress_lazy_task_start_failure_drains_backlog_and_resets_at_recovery_frame() -> (
    None
):
    class LazyStartFailureProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.failure_started = asyncio.Event()
            self.release_failure = asyncio.Event()
            self.recoveries = 0
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 1:
                self.failure_started.set()
                await self.release_failure.wait()
                raise RuntimeError("lazy task start failed")
            return ()

        async def recover_after_failure(self, _identity: SessionIdentity) -> None:
            self.recoveries += 1

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    provider = LazyStartFailureProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=4,
    )
    identity = SessionIdentity("lazy-task-start-recovery", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)

    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))
    await registry.on_audio_frame(session, AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2))
    await asyncio.wait_for(provider.failure_started.wait(), timeout=1)
    await registry.on_audio_frame(session, AudioFrame(identity, 2, 4, 2, b"\x02\x00" * 2))
    provider.release_failure.set()

    for _ in range(50):
        if context.ingress.provider_failed:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0, 1]
    assert context.ingress.queue.empty()
    assert context.ingress.provider_failed is True

    await registry.on_audio_frame(session, AudioFrame(identity, 3, 6, 2, b"\x03\x00" * 2))
    for _ in range(50):
        if provider.audio_calls == [0, 1, 3]:
            break
        await asyncio.sleep(0)

    assert provider.recoveries == 1
    assert provider.reset_samples == [6]
    assert provider.audio_calls == [0, 1, 3]
    assert context.ingress.provider_failed is False
    await context.runtime.close()


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
    context = await registry._get_or_create(identity)
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
    bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
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
    assert context.playback.current_fence == fence
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
    await registry._get_or_create(identity)

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
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)
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
    context.turn_start_sample = 0
    context.turn_endpoint_sample = 640
    context.turn_retire_sample = 640

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
    context = await registry._get_or_create(identity)
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
    assert context.turn_endpoint_sample is None
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
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)

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
    context = await registry._get_or_create(identity)
    assert bridge_session.reconnect(replacement)
    callback = bridge.on_session_connected
    assert callback is not None

    with pytest.raises(RuntimeError, match="provider epoch reset failed"):
        await callback(bridge_session)

    assert registry.context(identity.session_id) is None
    assert identity.session_id not in registry._sessions  # noqa: SLF001
    assert context.closed is True
    assert runtime_closed.is_set()
    assert provider.closed is True
    assert bridge.bridge.get(identity.session_id) is None


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
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    assert fence.session_epoch == 7
    context.playback.start(fence)

    assert await registry.generate_reply(identity.session_id, "你好", fence)

    assert bridge.output_admissions
    assert bridge.output_admissions[0].accepted is True
    assert bridge.output_admissions[0].selected is True
    assert provider.reply_fences == [fence]
    assert len(bridge.frames) == 1
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.delivery_id == (
        "nonzero-session-epoch-output/epoch-7/turn-1/generation-1/tool-0"
    )
    assert delivery.events == (
        ReplyDeliveryEvent.FIRST_FRAME_SENT,
        ReplyDeliveryEvent.PROVIDER_COMPLETED,
    )
    assert delivery.terminal is False
    assert context.output_results == [
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
    await registry._finalize_session(identity.session_id)


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
    await _wait_until(lambda: bool(context.output_results))

    terminal = context.output_results[-1]
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

    assert not await registry._enqueue_output_work(context, _OutputWork(reserved, fence))
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
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        metrics=metrics,
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
    delivery = context.reply_delivery.get(fence)
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
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_conversation_yield_proxy_resolves_only_on_matching_terminal() -> None:
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        metrics=metrics,
    )
    identity = SessionIdentity("conversation-yield-terminal")
    context = await registry._get_or_create(identity)
    candidate = GenerationFence(identity.session_id, 1, 1, 0)
    unrelated = GenerationFence(identity.session_id, 2, 1, 0)
    context.conversation_yield_candidate_fence = candidate

    registry._record_reply_delivery_event(
        context,
        unrelated,
        ReplyDeliveryEvent.PREEMPTED,
        "unrelated_generation",
    )
    registry._record_reply_delivery_event(
        context,
        candidate,
        ReplyDeliveryEvent.FIRST_FRAME_SENT,
        "still_speaking",
    )

    assert context.conversation_yield_candidate_fence == candidate
    assert metrics.get("voice_conversation_yield_proxy_total", {"status": "confirmed"}) == 0

    registry._record_reply_delivery_event(
        context,
        candidate,
        ReplyDeliveryEvent.PREEMPTED,
        "semantic_overlap",
    )

    assert context.conversation_yield_candidate_fence is None
    assert metrics.get("voice_conversation_yield_proxy_total", {"status": "confirmed"}) == 1
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
    assert context.output_owner is None
    await registry._finalize_session(identity.session_id)


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
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "查一下", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert context.output_owner is not None
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
    assert context.output_owner is None
    await registry._finalize_session(identity.session_id)


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
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.playback.start(fence)
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
    assert context.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.interaction_phase is InteractionPhase.LISTENING
    assert context.playback.current_fence is None
    assert any(
        effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
        and cancelled.generation_id == fence.generation_id + 1
        and source_event_id == "output_superseded"
        and payload == {"reason": "superseded"}
        for effect_kind, cancelled, source_event_id, payload in bridge.effects
    )
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.first_frame_sent is True
    assert delivery.terminal_event is ReplyDeliveryEvent.PREEMPTED
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
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)

    reply = asyncio.create_task(
        registry.generate_reply(identity.session_id, "你好", fence),
        name="test-stalled-output-generation",
    )
    await asyncio.wait_for(second_read_started.wait(), timeout=1)
    assert context.output_owner is not None
    result = await asyncio.wait_for(reply, timeout=1)

    assert result is False
    assert cancellation_started.is_set()
    await asyncio.wait_for(cancellation_finished.wait(), timeout=1)
    assert provider.cancelled == [fence]
    assert context.output_owner is None
    assert context.reply_task is None
    assert context.output_dispatch_task is None
    assert context.output_work == {}
    assert context.playback.current_fence is None
    assert context.playback.actual_heard_text(fence) == ""
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
    assert context.playback.actual_heard_text(fence) == ""
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name() == "test-stalled-output-generation"
    ]
    await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_reclassified_as_generation_deadline() -> None:
    class ProviderTimeout(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                raise TimeoutError("provider timeout")
                yield MediaReplyChunk(b"\x00\x00", 0)  # pragma: no cover

            return chunks()

    bridge = MediaBridgeGrpcServer()
    provider = ProviderTimeout()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        output_generation_timeout_s=1.0,
    )
    registry.install()
    identity = SessionIdentity("provider-timeout-error")
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)

    with pytest.raises(TimeoutError, match="provider timeout"):
        await registry.generate_reply(identity.session_id, "你好", fence)

    assert context.output_owner is None
    assert context.playback.current_fence is fence
    assert context.runtime.orchestrator.state is ConversationState.THINKING
    await registry._finalize_session(identity.session_id)


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
    assert str(provider.output_intents[0].tts_source).startswith("稍等，我查询一下。")
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


class _LateOwnedDelegationProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.output_kinds: list[int] = []
        self.release = asyncio.Event()
        self.ack_started = asyncio.Event()
        self.ack_completed = asyncio.Event()
        self.deep_started = asyncio.Event()

    async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
        await self.release.wait()
        return "南京今天多云，气温二十二度。"

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


class _CapturingGenerationBridge(MediaBridgeGrpcServer):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[object] = []
        self.generation_starts: list[GenerationFence] = []

    async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
        self.frames.append(_frame)
        return True

    async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
        action = _kwargs.get("action")
        fence = _args[1] if len(_args) > 1 else _kwargs.get("fence")
        if action == media_pb2.GENERATION_ACTION_START and isinstance(fence, GenerationFence):
            self.generation_starts.append(fence)
        return True


class _AckCapturingProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.started = asyncio.Event()
        self.completed = asyncio.Event()

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
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )
            self.completed.set()

        return chunks()


async def _finish_output_owner_playback(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    bridge: _CapturingGenerationBridge,
    session: Any,
) -> None:
    context = registry._sessions[identity.session_id]  # noqa: SLF001
    owner = context.output_owner
    assert owner is not None
    ack_frame = bridge.frames[-1]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=owner.fence.generation_id,
            received_sequence=ack_frame.sequence,
            rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
            client_monotonic_ms=1,
            turn_id=owner.fence.turn_id,
            tool_epoch=owner.fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )


async def _finish_device_wake_ack_if_any(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    provider: _LateOwnedDelegationProvider,
    bridge: _CapturingGenerationBridge,
    session: Any,
) -> None:
    if identity.client_type != "device":
        return
    try:
        await asyncio.wait_for(provider.ack_started.wait(), timeout=1)
    except TimeoutError:
        return
    await asyncio.wait_for(provider.ack_completed.wait(), timeout=1)
    context = registry._sessions[identity.session_id]  # noqa: SLF001
    for _ in range(40):
        if context.output_owner is not None or bridge.frames:
            break
        await asyncio.sleep(0)
    if context.output_owner is not None:
        await _finish_output_owner_playback(registry, identity, bridge, session)
    elif bridge.frames:
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_frame.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_frame.turn_id,
                tool_epoch=ack_frame.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
    provider.ack_started.clear()
    provider.ack_completed.clear()


async def _ack_owned_filler_then_wait(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    provider: _LateOwnedDelegationProvider,
    bridge: _CapturingGenerationBridge,
) -> tuple[Any, GenerationFence]:
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await _finish_device_wake_ack_if_any(registry, identity, provider, bridge, session)
    committed = await context.runtime.on_turn_committed("今天南京天气怎么样")
    await asyncio.wait_for(provider.ack_started.wait(), timeout=1)
    ack_owner = context.output_owner
    assert ack_owner is not None
    ack_fence = ack_owner.fence
    claim = context.delegation_output_claims[committed]
    assert ack_fence.turn_id == committed.turn_id
    assert ack_fence.generation_id == committed.generation_id
    assert claim.state is DelegationOutputState.OWNED
    await asyncio.wait_for(provider.ack_completed.wait(), timeout=1)
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
    assert claim.state is DelegationOutputState.OWNED
    assert context.runtime.orchestrator.state is ConversationState.TOOL_WAITING
    assert context.runtime.interaction_phase is InteractionPhase.TOOL_WAITING
    assert context.runtime.fence.matches(ack_fence)
    assert not provider.deep_started.is_set()
    return context, ack_fence


def _device_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )


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
        assert provider.texts[0] in DEVICE_WAKE_PHRASES
        assert context.runtime.fence.turn_id >= 1
        assert context.runtime.fence.generation_id >= 1
        assert bridge.generation_starts
        wake_fence = bridge.generation_starts[0]
        assert wake_fence.turn_id >= 1
        assert wake_fence.generation_id >= 1
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_unheard_wake_pcm_restores_half_duplex_listen() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()

    def runtime_factory(session_id: str) -> DuplexRuntime:
        runtime = DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)

        def drop_pcm(_cancellation: object, _pcm: bytes) -> bytes | None:
            return None

        runtime.gate_tts_audio = drop_pcm  # type: ignore[method-assign]
        return runtime

    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=runtime_factory,
    )
    registry.install()
    identity = _device_identity("device-unheard-wake")
    bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await _wait_until(
            lambda: (
                context.output_owner is None
                and context.runtime._was_speaking is False
                and context.runtime.orchestrator.state is ConversationState.LISTENING
                and bool(provider.texts)
            )
        )
        assert context.runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.attempted.wait(), timeout=1)
        await _wait_until(
            lambda: (
                context.runtime.orchestrator.state is ConversationState.LISTENING
                and context.runtime._was_speaking is False
            )
        )
        # Without an open input gate the next endpoint is discarded as echo.
        assert context.runtime.playback_overlap_input_blocked() is False
        assert context.runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        context.runtime._was_speaking = True
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
        assert context.turn_endpoint_sample is None
        assert context.pending_partial is None
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        assert context.turn_endpoint_sample == 16_000
        assert context.turn_endpoint_task is not None
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
        context = await registry._get_or_create(identity)
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
        assert context.turn_endpoint_sample == 16_000
        assert context.conversation_close_endpoint_pinned == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        pinned = context.conversation_close_endpoint_pinned
        assert pinned == 16_000
        assert context.turn_endpoint_sample == pinned

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
        assert context.turn_endpoint_sample != 200_000
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        task = context.conversation_close_semantic_task
        if task is not None:
            await task
        assert calls == ["那先不聊了"]
        assert (
            context.conversation_close_endpoint_pinned == 16_000
            or context.turn_endpoint_sample == 16_000
            or context.standby_requested
        )
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)

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
        assert context.turn_endpoint_sample == 16_000
        assert context.conversation_close_semantic_task is None
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        assert context.clock_fact_endpoint_pinned == 16_000
        assert context.turn_endpoint_sample == 16_000
        assert context.turn_retire_sample == 16_000

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
        assert context.turn_endpoint_sample != 200_000
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        assert context.turn_endpoint_sample == 16_000

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
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        assert context.turn_endpoint_sample == 16_000

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
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        pinned = context.clock_fact_endpoint_pinned
        assert pinned == 16_000
        assert context.turn_endpoint_sample == pinned

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
        assert context.clock_fact_endpoint_pinned == pinned
        assert context.turn_endpoint_sample == pinned
        assert context.turn_endpoint_task is not None
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        context.turn_end_sample = 8_000
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        assert context.clock_fact_endpoint_pinned == 16_000
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
            if user_turns == ["今天星期几"] and context.turn_endpoint_sample is None:
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
                f"user_turns={user_turns!r} endpoint={context.turn_endpoint_sample}"
            )
    finally:
        allow_commit.set()
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
        context = await registry._get_or_create(identity)
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
        context.turn_start_sample = 84_160
        context.turn_end_sample = 154_880
        context.turn_endpoint_sample = 154_880
        context.turn_retire_sample = 154_880
        context.clock_fact_endpoint_pinned = 154_880
        context.turn_endpoint_grace_deadline = time.monotonic()
        registry._schedule_turn_commit(context)
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        assert context.turn_start_sample is not None
        assert context.turn_endpoint_sample == 205_760
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
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
        assert context.live_query_forced_text is None
        assert context.conversation_close_endpoint_pinned == 336_960
        assert context.turn_endpoint_sample == 336_960
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
async def test_late_clock_fact_recovery_does_not_preempt_in_flight_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Late overlap-rejected clock-fact must not cancel an in-flight reply.

    Regression for stage-7 epoch 1363: weekday early-committed turn3 and
    started Doubao TTS, then a longer-range offline final was rejected as
    cross_sentence_overlap, recovery early-committed turn4, and cancel_reply
    killed turn3 (reply_task_exception). While a reply owns the session,
    recovery may still ingest timeline text but must not re-arm/commit.
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
    identity = _device_identity("device-late-clock-fact-inflight")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
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
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        first = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=178_240,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, first)

        async def _reply_owns_session() -> bool:
            return bool(
                context.output_owner is not None
                and any(
                    turn.role == "user" and turn.content == "今天星期几"
                    for turn in context.runtime.orchestrator.context.turns
                )
            )

        for _ in range(40):
            if await _reply_owns_session():
                break
            await asyncio.sleep(0.05)
        assert await _reply_owns_session()
        assert MediaVoiceCoreRegistry._reply_in_flight(context)
        in_flight_fence = context.output_owner.fence
        in_flight_task = context.reply_task
        generation_count_before = len(bridge.generation_starts)
        user_turns_before = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns_before == ["今天星期几"]
        committed = context.asr.last_committed_sample
        assert committed >= 178_240

        # Blocking interval after the committed watermark (playback echo shape).
        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="playback-echo",
            revision=1,
            capture_start_sample=committed,
            capture_end_sample=committed + 62_080,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, echo)

        # Longer overlapping rescue final cannot supersede a real provider
        # interval → cross_sentence_overlap → recovery must skip re-commit.
        late = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-late-offline",
            revision=1,
            capture_start_sample=committed,
            capture_end_sample=committed + 120_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.95,
            rescue_synthesized=True,
        )
        with caplog.at_level(logging.WARNING):
            decision = await registry._accept_asr_result_decision(
                identity.session_id,
                late,
            )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.turn_endpoint_sample is None
        assert context.output_owner is not None
        assert context.output_owner.fence == in_flight_fence
        if in_flight_task is not None:
            assert context.reply_task is in_flight_task
            assert not in_flight_task.cancelled()
        assert len(bridge.generation_starts) == generation_count_before
        user_turns_after = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns_after == ["今天星期几"]
        assert any(
            "clock-fact recovery commit skipped: reply in flight" in record.message
            for record in caplog.records
        )
        await asyncio.sleep(0.05)
        assert not any(
            "reply_task_exception" in record.message or "reply_task_cancelled" in record.message
            for record in caplog.records
        )
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
        await registry._get_or_create(identity)
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
    finally:
        await registry._finalize_session(identity.session_id)


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
        context = await registry._get_or_create(identity)
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
        endpoint_sample = context.turn_endpoint_sample
        assert endpoint_sample == 16_000
        endpoint_task = context.turn_endpoint_task
        assert endpoint_task is not None
        endpoint_task.cancel()
        await asyncio.gather(endpoint_task, return_exceptions=True)
        timeout_handle = context.turn_endpoint_timeout_handle
        if timeout_handle is not None:
            timeout_handle.cancel()
            context.turn_endpoint_timeout_handle = None

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
        await registry._finalize_session(identity.session_id)


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
        await registry._get_or_create(identity)
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
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_missed_hearing_nudge_cooldown_blocks_back_to_back_prompts() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-nudge-cooldown")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        context.turn_endpoint_sample = 16_000
        decision = _verified_owner_decision()
        context.runtime._speaker_decision = decision
        context.runtime._speaker_class = decision.classification

        registry._nudge_missed_hearing(context)
        assert context.missed_hearing_nudge_count == 1
        registry._nudge_missed_hearing(context)
        assert context.missed_hearing_nudge_count == 1
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


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Poll until predicate() is truthy or the deadline passes."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.005)


class _DelegationProbeProvider(FakeMediaProvider):
    """Records conversation replies, deep/ACK outputs and delegation starts."""

    def __init__(self) -> None:
        super().__init__()
        self.reply_calls = 0
        self.output_kinds: list[int] = []
        self.delegations: list[tuple[str, GenerationFence]] = []

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent

    def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        return super().generate_reply(identity, user_text, fence)

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

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                first=True,
                final=True,
            )

        return chunks()


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
    context = await registry._get_or_create(identity)
    task_manager = context.runtime.orchestrator.task_manager
    original_start = task_manager.start

    async def slow_start(*args: Any, **kwargs: Any) -> Any:
        await start_gate.wait()
        return await original_start(*args, **kwargs)

    task_manager.start = slow_start  # type: ignore[method-assign]
    query = "今天南京天气怎么样"
    try:
        fence = await context.runtime.on_turn_committed(query)
        context.playback.start(fence)
        # The normal reply outlives the initial decision window: it must
        # release the claim, speak locally, and later cancel the late
        # delegation instead of accepting deep output.
        await registry.generate_reply(identity.session_id, query, fence)
        claim = context.delegation_output_claims[fence]
        assert claim.state is DelegationOutputState.RELEASED
        assert provider.reply_calls == 1
        assert provider.output_kinds == []
        assert provider.delegations == []

        start_gate.set()
        await _wait_until(
            lambda: any(
                rec.tool_name == "media_deep_response" and rec.cancelled
                for rec in task_manager.tasks.values()
            )
        )
        await asyncio.sleep(0.02)
        assert claim.state is DelegationOutputState.RELEASED
        assert provider.reply_calls == 1
        assert provider.output_kinds == []
    finally:
        start_gate.set()
        deep_gate.set()
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
    assert provider.output_texts == ["稍等，我查询一下。南京今天多云。"]
    assert provider.reply_calls == 0
    assert await registry.generate_reply(identity.session_id, query, fence)
    assert provider.reply_calls == 0
    await registry._finalize_session(identity.session_id)


class _FenceCheckingBridge(MediaBridgeGrpcServer):
    def __init__(self) -> None:
        super().__init__()
        self.accepted_pcm: list[object] = []
        self.rejected_pcm: list[object] = []
        self.frames = self.accepted_pcm
        self.start_reasons: list[str] = []
        self.generation_starts: list[GenerationFence] = []

    async def emit_generation(
        self, session_id: str, fence: object, *, action: int, reason: str = "", **_kwargs: object
    ) -> bool:
        if action == media_pb2.GENERATION_ACTION_START:
            session = self.bridge.get(session_id)
            if session is not None and isinstance(fence, GenerationFence):
                session.generation.advance(fence)
                session.reset_downlink_generation(fence)
                session.generation_active = True
                self.generation_starts.append(fence)
            self.start_reasons.append(reason)
        return True

    async def emit_pcm(self, session_id: str, frame: object) -> bool:
        session = self.bridge.get(session_id)
        if session is None or not session.accept_downlink(frame):  # type: ignore[arg-type]
            self.rejected_pcm.append(frame)
            return False
        self.accepted_pcm.append(frame)
        return True


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
        context = await registry._get_or_create(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        ack_owner = context.output_owner
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

    context = registry._sessions[identity.session_id]
    assert provider.finalize_called is True
    assert context.turn_end_sample == 320
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
    context = await registry._get_or_create(identity)
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
    context = registry._sessions[identity.session_id]
    endpoint_task = context.turn_endpoint_task
    assert endpoint_task is not None
    endpoint_task.cancel()
    await asyncio.gather(endpoint_task, return_exceptions=True)
    timeout_handle = context.turn_endpoint_timeout_handle
    assert timeout_handle is not None
    timeout_handle.cancel()
    context.turn_endpoint_timeout_handle = None
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
    context = registry._sessions[identity.session_id]
    assert context.pending_partial is not None
    assert context.pending_partial.capture_end_sample == 40_000
    assert context.pending_partial.revision == 2

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
    assert context.pending_partial is None
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
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)
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
    assert context.turn_endpoint_sample is None
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
    context = await registry._get_or_create(identity)
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
    context = await registry._get_or_create(identity)
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


@pytest.mark.asyncio
async def test_approximate_device_progress_completes_without_actual_heard() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
        metrics=metrics,
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
            event_type=PlaybackEventType.ENDED,
        ),
    )

    assert context.playback.actual_heard_text(fence) == ""
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert all(turn.content != "你好" for turn in context.runtime.orchestrator.context.turns)
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
    assert delivery.actual_heard is False
    assert (
        metrics.get("voice_conversation_participation_proxy_ms_total", {"kind": "assistant"}) == 0
    )


@pytest.mark.asyncio
async def test_typed_device_progress_waits_for_ended_before_completion() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity(
        "device-typed-playback",
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
    fence = GenerationFence(identity.session_id, 1, 1, 0, session_epoch=7)
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
            approximate=False,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.PROGRESS,
        ),
    )

    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.actual_heard is True
    assert delivery.terminal_event is None

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=320,
            client_monotonic_ms=2,
            approximate=False,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )

    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED


@pytest.mark.asyncio
async def test_typed_device_playback_error_is_not_successful_completion() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.controls: list[tuple[int, GenerationFence, str]] = []

        async def emit_generation(
            self,
            _session_id: str,
            fence: GenerationFence,
            *,
            action: int,
            reason: str = "",
            **_kwargs: object,
        ) -> bool:
            self.controls.append((action, fence, reason))
            return True

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity(
        "device-playback-error",
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
    context.provider_complete = True

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=0,
            client_monotonic_ms=1,
            approximate=True,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ERROR,
        ),
    )

    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.ERROR
    assert delivery.playback_ended is False
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.fence.generation_id == fence.generation_id + 1
    assert bridge.controls == [
        (
            media_pb2.GENERATION_ACTION_CANCEL,
            context.runtime.fence,
            "device_playback_error",
        )
    ]


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
