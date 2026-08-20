"""Unit tests for Media ingress overflow behaviour under a stalled provider.

The 2026-08-19 PCM tap diagnosis showed ~97% of admitted uplink audio being
silently drained while the pump waited on FunASR.  Overflow must now drop only
the oldest frame, keep the newest speech, and log a rate-limited warning.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields
from types import SimpleNamespace

from services.agent.src.voice_core.media_audio_ingress import (
    MediaAudioIngress,
    MediaAudioIngressState,
)
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry


def _frame(sequence: int, capture_start_sample: int) -> AudioFrame:
    return AudioFrame(
        identity=SessionIdentity(session_id="sess-1", stream_epoch=7),
        sequence=sequence,
        capture_start_sample=capture_start_sample,
        frame_samples=2,
        payload=b"\x01\x00\x02\x00",
    )


def _fake_host() -> SimpleNamespace:
    metrics = SimpleNamespace(incidents=[], gauges={})
    metrics.inc_media_metric = lambda name: metrics.incidents.append(name)
    metrics.set_media_metric = lambda name, value: metrics.gauges.__setitem__(name, value)
    return SimpleNamespace(
        metrics=metrics,
        _stream_epoch_is_current=lambda context, epoch: True,
    )


def _fake_context(state: MediaAudioIngressState) -> SimpleNamespace:
    admitted: list[int] = []

    def record_audio(*, start_sample: int, frame_samples: int) -> bool:
        admitted.append(start_sample)
        return True

    context = SimpleNamespace(
        identity=SessionIdentity(session_id="sess-1", stream_epoch=7),
        stream_epoch=7,
        ingress=state,
        asr=SimpleNamespace(record_audio=record_audio),
    )
    context.admitted = admitted
    return context


def _block_pump(state: MediaAudioIngressState) -> None:
    # Keep accept() from spawning a real pump task during unit tests.
    state.pump_task = SimpleNamespace(done=lambda: False)


def test_overflow_drops_only_oldest_frame() -> None:
    async def scenario() -> tuple[list[int], MediaAudioIngressState]:
        state = MediaAudioIngressState.create(max_frames=2)
        context = _fake_context(state)
        ingress = MediaAudioIngress(_fake_host())
        _block_pump(state)
        await ingress.accept(context, _frame(0, 0))
        await ingress.accept(context, _frame(1, 2))
        await ingress.accept(context, _frame(2, 4))
        queued: list[int] = []
        while not state.queue.empty():
            frame, _ = state.queue.get_nowait()
            queued.append(frame.capture_start_sample)
        return queued, state

    queued, state = asyncio.run(scenario())
    assert queued == [2, 4]
    assert state.overflow_dropped_frames == 1
    assert state.overflow_dropped_samples == 2
    assert state.discontinuity_pending


def test_overflow_warning_is_logged_rate_limited(caplog) -> None:
    async def scenario() -> MediaAudioIngressState:
        state = MediaAudioIngressState.create(max_frames=2)
        context = _fake_context(state)
        ingress = MediaAudioIngress(_fake_host())
        _block_pump(state)
        await ingress.accept(context, _frame(0, 0))
        await ingress.accept(context, _frame(1, 2))
        await ingress.accept(context, _frame(2, 4))
        await ingress.accept(context, _frame(3, 6))
        return state

    with caplog.at_level(logging.WARNING):
        state = asyncio.run(scenario())
    overflow_records = [
        record for record in caplog.records if "queue overflow" in record.getMessage()
    ]
    assert len(overflow_records) == 1
    assert state.overflow_dropped_frames == 2
    # The warning is rate limited: it reports the counters at first emission.
    assert "dropped_frames=1" in overflow_records[0].getMessage()


def test_no_overflow_keeps_all_frames() -> None:
    async def scenario() -> MediaAudioIngressState:
        state = MediaAudioIngressState.create(max_frames=2)
        context = _fake_context(state)
        ingress = MediaAudioIngress(_fake_host())
        _block_pump(state)
        await ingress.accept(context, _frame(0, 0))
        await ingress.accept(context, _frame(1, 2))
        return state

    state = asyncio.run(scenario())
    assert state.queue.qsize() == 2
    assert state.overflow_dropped_frames == 0
    assert not state.discontinuity_pending


def test_registry_default_ingress_capacity_covers_provider_stalls() -> None:
    default = next(
        item.default
        for item in fields(MediaVoiceCoreRegistry)
        if item.name == "audio_ingress_max_frames"
    )
    # At least ~5s of 20 ms frames so task rotations and slow ASR sends do not
    # destroy admitted speech.
    assert default >= 250
