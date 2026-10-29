from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace

import pytest
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.voice_core import media_audio_ingress, media_session_standby
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import AudioFrame, MediaEnvelope
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _connect_vad_segment,
    _device_identity,
    open_bridge_connection,
)

_SPEECH = b"\x01\x10" * 320


class _CaptureProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[AudioFrame] = []

    async def ingest_audio(self, _identity, frame: AudioFrame) -> tuple[()]:
        self.frames.append(frame)
        self.audio_calls.append(frame.sequence)
        return ()


def _clock() -> dict[str, float]:
    return {"now": 1_000.0}


def _patch_clock(monkeypatch: pytest.MonkeyPatch, clock: dict[str, float]) -> None:
    def _now() -> float:
        return clock["now"]

    monkeypatch.setattr(media_audio_ingress.time, "monotonic", _now)
    monkeypatch.setattr(media_session_standby.time, "monotonic", _now)


def _frame(identity, sequence: int, start: int) -> AudioFrame:
    return AudioFrame(
        identity=identity,
        sequence=sequence,
        capture_start_sample=start,
        frame_samples=len(_SPEECH) // 2,
        payload=_SPEECH,
    )


async def _skip(*_args, **_kwargs) -> None:
    return None


async def _open(monkeypatch: pytest.MonkeyPatch, clock: dict[str, float]):
    _patch_clock(monkeypatch, clock)
    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", _skip)
    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_aigc_opening_marker", _skip)
    monkeypatch.setattr(MediaVoiceCoreRegistry, "_wait_aigc_opening_lead_in", _skip)
    identity = _device_identity("listening-capture")
    provider = _CaptureProvider()
    bridge = MediaBridgeGrpcServer()
    open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        owner_silence_timeout_s=180.0,
        listening_capture_window_s=30.0,
    )
    context = await registry.open_session(identity)
    context.runtime.set_interaction_phase(
        InteractionPhase.LISTENING,
        cause="test",
        publish=False,
    )
    registry._sync_owner_silence_phase(context, "listening")
    return registry, context, provider, identity


def _spy_states(monkeypatch: pytest.MonkeyPatch, runtime: object) -> list[str]:
    published: list[str] = []
    original = runtime.publish_assistant_state

    def _publish(state: str, **kwargs: object) -> object:
        published.append(state)
        return original(state, **kwargs)

    monkeypatch.setattr(runtime, "publish_assistant_state", _publish)
    return published


async def _ingested(provider: _CaptureProvider, count: int) -> None:
    for _ in range(50):
        if len(provider.frames) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"ingest_audio calls stopped at {len(provider.frames)}")


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_reason", ["expired", "reclaimed"])
async def test_held_capture_ignores_vad_end_without_opening_an_empty_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    hold_reason: str,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        if hold_reason == "expired":
            clock["now"] = 1_031.0
            await registry._audio_ingress.accept(context, _frame(identity, 1, 48_000))
        else:
            registry.hold_listening_capture_until_tap(context)
        assert context.listening_capture_hold is True
        session = registry.bridge._connections[identity.session_id].session
        projection_before = context.projection.current_frame
        provisional_before = context.projection.provisional

        with caplog.at_level(logging.INFO):
            for sample in (48_000, 64_000, 80_000):
                await registry.on_speech_segment(
                    session,
                    replace(
                        _connect_vad_segment(
                            identity, sample=sample, segment_id=f"held-end-{sample}"
                        ),
                        final=True,
                        voiced_end_sample=sample,
                    ),
                )
                assert context.pending.turn_start_sample is None
                assert context.pending.turn_endpoint_sample is None
                assert context.pending.turn_endpoint_timeout_handle is None
                assert context.turn_endpoint_task is None
                assert context.projection.current_frame is projection_before
                assert context.projection.provisional is provisional_before

        assert provider.frames == []
        assert provider.closed is False
        assert context.listening_capture_hold is True
        assert registry.context(identity.session_id) is context.runtime
        assert not [record for record in caplog.records if "media_asr_boundary" in record.message]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_capture_hold_preserves_an_already_open_pending_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        session = registry.bridge._connections[identity.session_id].session
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=48_000, segment_id="open-turn"),
                capture_end_sample=49_280,
            ),
        )
        existing_turn_start = context.pending.turn_start_sample
        existing_vad_epoch = context.pending.active_vad_stream_epoch
        existing_vad_start = context.pending.active_vad_start_sample
        existing_frame = context.projection.current_frame
        assert existing_turn_start == 48_000

        registry.hold_listening_capture_until_tap(context)
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=64_000, segment_id="held-final"),
                final=True,
                voiced_end_sample=64_000,
            ),
        )

        assert context.pending.turn_start_sample == existing_turn_start
        assert context.pending.active_vad_stream_epoch == existing_vad_epoch
        assert context.pending.active_vad_start_sample == existing_vad_start
        assert context.projection.current_frame is existing_frame
        assert context.pending.turn_endpoint_sample is None
        assert context.pending.turn_endpoint_timeout_handle is None
        assert context.turn_endpoint_task is None
        assert provider.frames == []
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_vad_during_projection_that_latches_capture_cannot_endpoint_a_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        session = registry.bridge._connections[identity.session_id].session
        apply_projection = registry._apply_projection_segment

        async def latch_during_projection(target, segment) -> None:
            await apply_projection(target, segment)
            registry.hold_listening_capture_until_tap(target)

        monkeypatch.setattr(registry, "_apply_projection_segment", latch_during_projection)
        with caplog.at_level(logging.INFO):
            await registry.on_speech_segment(
                session,
                replace(
                    _connect_vad_segment(identity, sample=48_000, segment_id="latch-end"),
                    final=True,
                    voiced_end_sample=48_000,
                ),
            )

        assert context.listening_capture_hold is True
        assert context.pending.turn_start_sample is None
        assert context.pending.turn_endpoint_sample is None
        assert context.pending.turn_endpoint_timeout_handle is None
        assert context.turn_endpoint_task is None
        assert context.projection.provisional is not None
        assert context.projection.current_frame is None
        assert provider.frames == []
        assert not [record for record in caplog.records if "media_asr_boundary" in record.message]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_capture_hold_does_not_swallow_a_local_stop_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        events: list[str] = []

        async def record(_session_id: str, event_type: str, _payload: object, **_kwargs: object) -> bool:
            events.append(event_type)
            return True

        monkeypatch.setattr(registry.bridge, "emit_event", record)
        registry.hold_listening_capture_until_tap(context)
        assert context.listening_capture_hold is True
        session = registry.bridge._connections[identity.session_id].session

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id="held-keyword-stop",
                revision=1,
                kind=SegmentKind.KWS,
                capture_start_sample=48_000,
                capture_end_sample=48_002,
                text="停",
                final=True,
                confidence=0.95,
                hard_stop=True,
            ),
        )

        assert "keyword.hit" in events
        assert context.pending.turn_start_sample is None
        assert context.pending.turn_endpoint_timeout_handle is None
        assert provider.frames == []
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_screen_tap_restores_vad_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        registry.hold_listening_capture_until_tap(context)
        session = registry.bridge._connections[identity.session_id].session
        resume_sample = 40_000
        await registry.on_client_event(
            session,
            MediaEnvelope.create(
                type="listening.resume",
                event_id="resume-before-vad",
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                sequence=1,
                payload={"wake_sample": resume_sample},
            ),
            0,
        )
        assert context.listening_capture_hold is False

        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(
                    identity, sample=resume_sample + 1, segment_id="resumed-vad-start"
                ),
                capture_end_sample=resume_sample + 1_280,
            ),
        )

        assert context.pending.turn_start_sample == resume_sample + 1
        assert context.pending.active_vad_stream_epoch == identity.stream_epoch
        assert context.projection.provisional is not None
        assert context.projection.provisional.capture_start_sample == resume_sample + 1
        assert context.pending.turn_endpoint_sample is None
        assert provider.frames == []
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_listening_capture_stops_after_thirty_seconds_and_keeps_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR", str(tmp_path))
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        clock["now"] = 1_030.0
        await registry._audio_ingress.accept(context, _frame(identity, 1, 48_000))
        await _ingested(provider, 1)

        clock["now"] = 1_031.0
        published = _spy_states(monkeypatch, context.runtime)
        denoiser_frames = context.ingress.denoiser.get_stats()["total_frames"]
        tap_files = set(tmp_path.iterdir())
        with caplog.at_level(logging.DEBUG):
            await registry._audio_ingress.accept(context, _frame(identity, 2, 64_000))

        assert len(provider.frames) == 1
        assert provider.audio_calls == [1]
        assert context.listening_capture_hold is True
        assert context.runtime.interaction_phase is InteractionPhase.CONNECTING
        assert "listening_closed" in published
        for _ in range(5):
            await asyncio.sleep(0)
        assert context.standby_requested is False
        assert provider.closed is False
        assert registry.context(identity.session_id) is context.runtime
        assert context.owner_silence_task is not None
        assert context.ingress.denoiser.get_stats()["total_frames"] == denoiser_frames
        assert context.ingress.listening_capture_dropped_frames >= 1
        assert set(tmp_path.iterdir()) == tap_files
        assert not [record for record in caplog.records if "media_asr" in record.message]
        assert not [record for record in caplog.records if "PCM tap" in record.message]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_screen_tap_resumes_capture_without_replaying_or_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        context.ingress.pump_task = asyncio.create_task(asyncio.sleep(3600))
        await registry._audio_ingress.accept(context, _frame(identity, 1, 100))
        assert context.ingress.queue.qsize() == 1
        assert provider.frames == []

        clock["now"] = 1_031.0
        await registry._audio_ingress.accept(context, _frame(identity, 2, 200))
        assert context.listening_capture_hold is True
        assert context.ingress.queue.empty()
        assert provider.frames == []

        context.pending.active_vad_stream_epoch = identity.stream_epoch
        context.runtime.set_interaction_phase(
            InteractionPhase.LISTENING,
            cause="test",
            publish=False,
        )
        assert (
            registry._admit_vad_start(
                context,
                _connect_vad_segment(identity, sample=4_000),
            )
            is False
        )
        await registry._audio_ingress.accept(context, _frame(identity, 3, 4_000))
        assert provider.audio_calls == []

        registry._sync_owner_silence_phase(context, "listening")
        assert context.runtime.interaction_phase is InteractionPhase.CONNECTING
        assert context.listening_capture_hold is True

        sleeper = context.ingress.pump_task
        assert sleeper is not None
        sleeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sleeper
        context.ingress.pump_task = None

        wake_sample = 8_000
        published = _spy_states(monkeypatch, context.runtime)
        connection = registry.bridge._connections[identity.session_id]
        await registry.on_client_event(
            connection.session,
            MediaEnvelope.create(
                type="listening.resume",
                event_id="listening-resume",
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                sequence=1,
                payload={"wake_sample": wake_sample},
            ),
            0,
        )
        assert "listening" in published
        assert context.runtime.interaction_phase is InteractionPhase.LISTENING
        assert context.standby_requested is False
        await registry._audio_ingress.accept(context, _frame(identity, 4, wake_sample))
        assert provider.frames == []

        await registry._audio_ingress.accept(context, _frame(identity, 5, wake_sample + 1))
        await _ingested(provider, 1)
        assert provider.frames[0].capture_start_sample > wake_sample
        assert provider.closed is False
        assert registry.context(identity.session_id) is context.runtime
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_accepted_speech_restarts_the_thirty_second_capture_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        clock["now"] = 1_010.0
        await registry._audio_ingress.accept(context, _frame(identity, 1, 1_000))
        await _ingested(provider, 1)

        registry._finish_owner_silence_turn(context, accepted=True)
        assert context.listening_capture_deadline == 1_040.0

        clock["now"] = 1_039.0
        await registry._audio_ingress.accept(context, _frame(identity, 2, 2_000))
        await _ingested(provider, 2)
        assert context.listening_capture_hold is False
        assert provider.closed is False

        clock["now"] = 1_041.0
        await registry._audio_ingress.accept(context, _frame(identity, 3, 3_000))
        assert len(provider.frames) == 2
        assert context.listening_capture_hold is True
        assert context.runtime.interaction_phase is not InteractionPhase.LISTENING
        assert registry.context(identity.session_id) is context.runtime
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_capture_window_does_not_close_on_a_child_mid_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        session = registry.bridge._connections[identity.session_id].session
        clock["now"] = 1_028.0
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=48_000, segment_id="mid-sentence"),
                capture_end_sample=49_280,
            ),
        )
        assert context.runtime.interaction_phase is InteractionPhase.USER_SPEAKING

        clock["now"] = 1_031.0
        await registry._audio_ingress.accept(context, _frame(identity, 1, 49_281))
        await _ingested(provider, 1)
        assert context.listening_capture_hold is False
        assert context.runtime.interaction_phase is InteractionPhase.USER_SPEAKING
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_latched", [False, True])
async def test_empty_asr_retire_admits_the_next_vad_after_the_capture_window(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    already_latched: bool,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        session = registry.bridge._connections[identity.session_id].session
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=48_000, segment_id="empty-start"),
                capture_end_sample=49_280,
            ),
        )
        assert context.pending.turn_start_sample == 48_000
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=64_000, segment_id="empty-end"),
                final=True,
                voiced_end_sample=64_000,
            ),
        )
        endpoint = context.pending.turn_endpoint_sample
        assert endpoint == 64_000
        grace = context.turn_endpoint_task
        if grace is not None:
            grace.cancel()
            await asyncio.gather(grace, return_exceptions=True)
        timeout_handle = context.pending.turn_endpoint_timeout_handle
        if timeout_handle is not None:
            timeout_handle.cancel()
            context.pending.turn_endpoint_timeout_handle = None

        clock["now"] = 1_031.0
        if already_latched:
            registry.hold_listening_capture_until_tap(context)
            assert context.listening_capture_hold is True
        with caplog.at_level(logging.INFO):
            await registry._expire_endpoint_tail(
                identity.session_id,
                identity.stream_epoch,
                endpoint,
            )
            for _ in range(10):
                await asyncio.sleep(0)
            next_sample = 80_000
            await registry.on_speech_segment(
                session,
                replace(
                    _connect_vad_segment(
                        identity, sample=next_sample, segment_id="after-empty"
                    ),
                    capture_end_sample=next_sample + 1_280,
                ),
            )

        assert context.pending.turn_start_sample == next_sample
        await registry._audio_ingress.accept(
            context, _frame(identity, 2, next_sample + 1)
        )
        await _ingested(provider, 1)
        assert context.listening_capture_hold is False
        assert not [
            record
            for record in caplog.records
            if "during listening capture hold" in record.message
        ]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_long_playout_opens_a_fresh_capture_window_for_the_next_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        context.runtime.barge_in_enabled = False
        clock["now"] = 1_010.0
        registry._finish_owner_silence_turn(context, accepted=True)
        await context.runtime.on_assistant_speaking("reply")
        assert context.runtime.interaction_phase is InteractionPhase.SPEAKING
        assert context.runtime.playback_overlap_input_blocked() is True

        clock["now"] = 1_050.0
        session = registry.bridge._connections[identity.session_id].session
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=48_000, segment_id="during-story"),
                capture_end_sample=49_280,
            ),
        )
        assert context.pending.turn_start_sample is None
        assert context.runtime.interaction_phase is InteractionPhase.SPEAKING
        assert context.listening_capture_hold is False
        assert context.standby_requested is False

        context.runtime._voice_floor.update(assistant_speaking=False)
        context.runtime.set_interaction_phase(
            InteractionPhase.LISTENING,
            cause="media_playback_ack",
        )
        for _ in range(8):
            await asyncio.sleep(0)
        assert context.listening_capture_hold is False
        assert context.runtime.interaction_phase is InteractionPhase.LISTENING

        clock["now"] = 1_055.0
        next_sample = 96_000
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=next_sample, segment_id="after-story"),
                capture_end_sample=next_sample + 1_280,
            ),
        )
        assert context.pending.turn_start_sample == next_sample
        await registry._audio_ingress.accept(context, _frame(identity, 1, next_sample + 1))
        await _ingested(provider, 1)
        assert context.listening_capture_hold is False
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_speech_after_the_capture_window_reopens_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    try:
        clock["now"] = 1_031.0
        await registry._audio_ingress.accept(context, _frame(identity, 1, 48_000))
        assert context.listening_capture_hold is True
        assert context.runtime.interaction_phase is InteractionPhase.CONNECTING
        published = _spy_states(monkeypatch, context.runtime)

        session = registry.bridge._connections[identity.session_id].session
        rewake_sample = 64_000
        await registry.on_speech_segment(
            session,
            replace(
                _connect_vad_segment(identity, sample=rewake_sample, segment_id="rewake"),
                capture_end_sample=rewake_sample + 1_280,
            ),
        )

        assert context.listening_capture_hold is False
        assert "listening" in published
        assert context.pending.turn_start_sample == rewake_sample
        await registry._audio_ingress.accept(context, _frame(identity, 2, rewake_sample + 1))
        await _ingested(provider, 1)
        assert provider.frames[-1].capture_start_sample == rewake_sample + 1
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("noise_after_reply_s", [None, 27.6])
async def test_listening_closes_within_sixty_seconds_of_a_silent_room_after_the_reply(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    noise_after_reply_s: float | None,
) -> None:
    clock = _clock()
    registry, context, provider, identity = await _open(monkeypatch, clock)
    monkeypatch.setattr(
        media_session_standby.time, "monotonic_ns", lambda: int(clock["now"] * 1e9)
    )
    caplog.set_level(logging.INFO, logger=media_session_standby.__name__)
    try:
        session = registry.bridge._connections[identity.session_id].session
        context.runtime.barge_in_enabled = False
        await context.runtime.on_assistant_speaking("reply")
        clock["now"] = 1_010.0
        reply_end = clock["now"]
        context.runtime._voice_floor.update(
            assistant_speaking=False,
            last_playback_completed_ns=int(reply_end * 1e9),
        )
        context.runtime.set_interaction_phase(
            InteractionPhase.LISTENING,
            cause="media_playback_ack",
        )
        for _ in range(8):
            await asyncio.sleep(0)
        closed_at: list[float] = []
        original = context.runtime.publish_assistant_state

        def _publish(state: str, **kwargs: object) -> object:
            if state == "listening_closed":
                closed_at.append(clock["now"])
            return original(state, **kwargs)

        monkeypatch.setattr(context.runtime, "publish_assistant_state", _publish)

        if noise_after_reply_s is not None:
            clock["now"] = reply_end + noise_after_reply_s
            await registry.on_speech_segment(
                session,
                replace(
                    _connect_vad_segment(identity, sample=48_000, segment_id="noise"),
                    capture_end_sample=49_280,
                ),
            )
            clock["now"] += 1.0
            await registry.on_speech_segment(
                session,
                replace(
                    _connect_vad_segment(identity, sample=64_000, segment_id="noise-end"),
                    final=True,
                    voiced_end_sample=64_000,
                ),
            )
            endpoint = context.pending.turn_endpoint_sample
            assert endpoint == 64_000
            clock["now"] += 6.0
            await registry._expire_endpoint_tail(
                identity.session_id, identity.stream_epoch, endpoint
            )
            for _ in range(10):
                await asyncio.sleep(0)

        sample = 80_000
        while not closed_at and clock["now"] < reply_end + 90.0:
            clock["now"] += 1.0
            sample += 16_000
            await registry._audio_ingress.accept(context, _frame(identity, sample, sample))

        assert closed_at, "listening_closed never published"
        assert closed_at[0] - reply_end <= 60.0
        assert [r.getMessage() for r in caplog.records if "listening closed" in r.getMessage()] == [
            f"media listening closed session={identity.session_id} "
            f"silence_since_reply_s={closed_at[0] - reply_end:.1f}"
        ]
    finally:
        await registry.finalize_session(identity.session_id)
