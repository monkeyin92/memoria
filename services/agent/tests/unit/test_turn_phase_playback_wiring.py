"""Accepted playback ACKs must feed the shadow TurnPhase projection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.conversation_projection import (
    ConversationProjection,
    FloorState,
    PhaseReason,
    TurnPhase,
)
from services.agent.src.orchestration.speech_timeline import (
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session_output_stream import MediaOutputStreamMixin
from services.agent.src.voice_core.playback_ledger import PlaybackLedger, PlaybackSpan
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent, ReplyDeliveryLedger

SESSION = "turn-phase-wiring"


class _PlaybackWiringHost(MediaOutputStreamMixin):
    def __init__(self) -> None:
        self.metrics = MetricsRegistry()
        self._sessions: dict[str, SimpleNamespace] = {}
        self.phase_transitions: list[tuple[TurnPhase, TurnPhase]] = []
        self.delivery_events: list[ReplyDeliveryEvent] = []

    def _observe_turn_phase(self, context: SimpleNamespace, previous_phase: TurnPhase) -> None:
        current = context.projection.phase
        if current is previous_phase:
            return
        self.phase_transitions.append((previous_phase, current))

    def _record_reply_delivery_event(
        self,
        context: SimpleNamespace,
        fence: GenerationFence,
        event: ReplyDeliveryEvent,
        reason: str = "",
    ) -> None:
        self.delivery_events.append(event)


def _build_context() -> tuple[_PlaybackWiringHost, SimpleNamespace]:
    host = _PlaybackWiringHost()
    identity = SessionIdentity(SESSION)
    runtime_fence = GenerationFence(
        SESSION, turn_id=99, generation_id=99, tool_epoch=9, session_epoch=9
    )
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    context = SimpleNamespace(
        identity=identity,
        closed=False,
        runtime=SimpleNamespace(fence=runtime_fence),
        provider_complete=False,
        playback=PlaybackLedger(),
        reply_delivery=ReplyDeliveryLedger(),
        projection=ConversationProjection(SESSION, timeline),
    )
    host._sessions[identity.session_id] = context
    context.projection.reset_phase(stream_epoch=1)
    return host, context


def _segment(segment_id: str, start: int, end: int, *, text: str = "") -> SpeechSegment:
    kind = SegmentKind.VAD if not text else SegmentKind.ASR_PARTIAL
    return SpeechSegment(
        session_id=SESSION,
        stream_epoch=1,
        provider_task_epoch=0 if kind is SegmentKind.VAD else 1,
        segment_id=segment_id,
        revision=1,
        kind=kind,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        final=False,
    )


def _seed_backchannel(projection: ConversationProjection) -> None:
    """User says 嗯 over an active reply: VAD start then backchannel ASR."""

    vad = _segment("vad", 0, 320)
    assert projection.timeline.add(vad)
    projection.apply_continuous_event(vad, turn_id_hint=1, playback_active=True)
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY
    asr = _segment("asr", 320, 480, text="嗯")
    assert projection.timeline.add(asr)
    projection.apply_continuous_event(asr, turn_id_hint=1, playback_active=True)
    assert projection.phase is TurnPhase.BACKCHANNEL


def _start_generation(context: SimpleNamespace) -> GenerationFence:
    fence = GenerationFence(SESSION, turn_id=1, generation_id=1, tool_epoch=0)
    context.playback.start(fence)
    assert context.playback.register_audio(
        fence, sequence=0, source_start_sample=0, frame_samples=640
    )
    assert context.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=2,
            audio_start_sample=0,
            audio_end_sample=640,
            text="嗯。",
        )
    )
    return fence


def _progress(
    identity: SessionIdentity,
    fence: GenerationFence,
    event_type: PlaybackEventType,
) -> PlaybackProgress:
    return PlaybackProgress(
        identity=identity,
        generation_id=fence.generation_id,
        received_sequence=0,
        rendered_sample_end=640,
        client_monotonic_ms=0,
        approximate=False,
        turn_id=fence.turn_id,
        tool_epoch=fence.tool_epoch,
        session_epoch=fence.session_epoch,
        event_type=event_type,
    )


@pytest.mark.asyncio
async def test_accepted_started_keeps_uplink_clock_without_forcing_frames() -> None:
    host, context = _build_context()
    projection = context.projection
    _seed_backchannel(projection)
    fence = _start_generation(context)
    emitted_before = projection.emitted_frame_count

    await host.on_playback_progress(
        SimpleNamespace(identity=context.identity),
        _progress(context.identity, fence, PlaybackEventType.STARTED),
    )

    # The 80ms core emits nothing for an incomplete bucket; the ACK must not
    # fabricate a frame from downlink rendered samples (640) either.
    assert projection.phase is TurnPhase.BACKCHANNEL
    assert projection.current_frame is None
    assert projection.emitted_frame_count == emitted_before
    assert host.phase_transitions == []


@pytest.mark.asyncio
async def test_accepted_progress_marks_playback_active_in_projection() -> None:
    host, context = _build_context()
    projection = context.projection
    fence = _start_generation(context)

    await host.on_playback_progress(
        SimpleNamespace(identity=context.identity),
        _progress(context.identity, fence, PlaybackEventType.PROGRESS),
    )

    assert projection.phase is TurnPhase.IDLE
    assert projection.floor_state is FloorState.ASSISTANT_HOLDS_FLOOR
    assert projection._playback_active is True
    assert projection.current_frame is None


@pytest.mark.asyncio
async def test_ended_recovers_backchannel_to_idle_with_current_fence() -> None:
    host, context = _build_context()
    projection = context.projection
    _seed_backchannel(projection)
    fence = _start_generation(context)
    session = SimpleNamespace(identity=context.identity)

    await host.on_playback_progress(
        session,
        _progress(context.identity, fence, PlaybackEventType.WATERMARK),
    )
    assert projection.phase is TurnPhase.BACKCHANNEL
    await host.on_playback_progress(
        session,
        _progress(context.identity, fence, PlaybackEventType.ENDED),
    )

    assert projection.phase is TurnPhase.IDLE
    assert projection.phase_reason is PhaseReason.BACKCHANNEL_IDLE
    frame = projection.current_frame
    assert frame is None or frame.floor_state is FloorState.SILENCE
    assert (TurnPhase.BACKCHANNEL, TurnPhase.IDLE) in host.phase_transitions


@pytest.mark.asyncio
async def test_stale_generation_ack_never_reaches_projection() -> None:
    host, context = _build_context()
    projection = context.projection
    _seed_backchannel(projection)
    fence = _start_generation(context)
    stale_fence = GenerationFence(
        SESSION,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id + 1,
        tool_epoch=fence.tool_epoch,
    )
    stale = _progress(context.identity, stale_fence, PlaybackEventType.WATERMARK)
    frame_before = projection.current_frame
    emitted_before = projection.emitted_frame_count

    await host.on_playback_progress(SimpleNamespace(identity=context.identity), stale)

    assert context.playback.stale_ack_count >= 1
    assert projection.current_frame is frame_before
    assert projection.emitted_frame_count == emitted_before
    assert host.phase_transitions == []


@pytest.mark.asyncio
async def test_duplicate_watermark_does_not_move_phase() -> None:
    host, context = _build_context()
    projection = context.projection
    _seed_backchannel(projection)
    fence = _start_generation(context)
    progress = _progress(context.identity, fence, PlaybackEventType.WATERMARK)

    await host.on_playback_progress(SimpleNamespace(identity=context.identity), progress)
    phase_after_first = projection.phase
    emitted_after_first = projection.emitted_frame_count
    transitions_after_first = list(host.phase_transitions)

    await host.on_playback_progress(SimpleNamespace(identity=context.identity), progress)

    assert projection.phase is phase_after_first
    assert projection.emitted_frame_count == emitted_after_first
    assert host.phase_transitions == transitions_after_first


@pytest.mark.asyncio
async def test_error_marks_inactive_without_output_side_effects() -> None:
    host, context = _build_context()
    projection = context.projection
    _seed_backchannel(projection)
    fence = _start_generation(context)

    await host.on_playback_progress(
        SimpleNamespace(identity=context.identity),
        _progress(context.identity, fence, PlaybackEventType.ERROR),
    )

    assert projection.phase is TurnPhase.IDLE
    assert projection.phase_reason is PhaseReason.BACKCHANNEL_IDLE
    # Runtime fence never matches, so the failure lifecycle must stay inert.
    assert ReplyDeliveryEvent.ERROR not in host.delivery_events
    stale_before = context.playback.stale_ack_count

    await host.on_playback_progress(
        SimpleNamespace(identity=context.identity),
        _progress(context.identity, fence, PlaybackEventType.STARTED),
    )
    assert context.playback.stale_ack_count == stale_before + 1
    assert projection.phase is TurnPhase.IDLE
    assert projection.floor_state is FloorState.SILENCE


@pytest.mark.asyncio
async def test_no_stream_epoch_never_synthesizes_frames() -> None:
    host, context = _build_context()
    context.projection.reset_phase()  # clear the established epoch
    projection = context.projection
    fence = GenerationFence(SESSION, turn_id=1, generation_id=1, tool_epoch=0)
    context.playback.start(fence)
    assert context.playback.register_audio(
        fence, sequence=0, source_start_sample=0, frame_samples=640
    )

    await host.on_playback_progress(
        SimpleNamespace(identity=context.identity),
        _progress(context.identity, fence, PlaybackEventType.STARTED),
    )

    assert projection.current_frame is None
    assert projection.emitted_frame_count == 0
    assert host.phase_transitions == []


@pytest.mark.asyncio
async def test_terminal_ack_cannot_be_resurrected_by_reordered_started() -> None:
    host, context = _build_context()
    projection = context.projection
    _seed_backchannel(projection)
    fence = _start_generation(context)
    session = SimpleNamespace(identity=context.identity)

    await host.on_playback_progress(
        session,
        _progress(context.identity, fence, PlaybackEventType.ENDED),
    )
    assert projection.phase is TurnPhase.IDLE
    stale_before = context.playback.stale_ack_count
    transitions_before = list(host.phase_transitions)

    await host.on_playback_progress(
        session,
        _progress(context.identity, fence, PlaybackEventType.STARTED),
    )

    assert context.playback.stale_ack_count == stale_before + 1
    assert projection.phase is TurnPhase.IDLE
    assert projection.floor_state is FloorState.SILENCE
    assert host.phase_transitions == transitions_before
