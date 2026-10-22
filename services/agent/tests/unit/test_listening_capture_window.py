from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.voice_core import media_audio_ingress, media_session_standby
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import AudioFrame
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
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


async def _ingested(provider: _CaptureProvider, count: int) -> None:
    for _ in range(50):
        if len(provider.frames) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"ingest_audio calls stopped at {len(provider.frames)}")


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
        denoiser_frames = context.ingress.denoiser.get_stats()["total_frames"]
        tap_files = set(tmp_path.iterdir())
        with caplog.at_level(logging.DEBUG):
            await registry._audio_ingress.accept(context, _frame(identity, 2, 64_000))

        assert len(provider.frames) == 1
        assert provider.audio_calls == [1]
        assert context.listening_capture_hold is True
        assert context.runtime.interaction_phase is InteractionPhase.CONNECTING
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
        registry.resume_listening_from_screen_tap(context, wake_sample=wake_sample)
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
