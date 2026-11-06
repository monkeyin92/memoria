"""Media session: verified-owner close phrases and owner silence windows."""

from __future__ import annotations

import asyncio

import pytest
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.speech_timeline import (
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _owner_silence_identity,
    _queued_event,
    _seed_pending_media_turn,
    _verified_owner_decision,
    open_bridge_connection,
)
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
