"""Listening-silence compliance at the Media audio ingress seam.

After the device returns to LISTENING the uplink keeps streaming microphone
PCM. Those frames must never reach the denoiser, the diagnostics PCM tap, the
ASR provider, or a log line carrying audio. Teardown must also clear every
buffer that could still hold uplink PCM.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.voice_core.media_audio_ingress import (
    MediaAudioIngress,
    MediaAudioIngressState,
)
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session_pending_turn import PendingTurn

_STREAM_EPOCH = 7
_IDENTITY = SessionIdentity(session_id="silence-gate", stream_epoch=_STREAM_EPOCH)
_SPEECH_PCM = b"\x01\x10" * 320
_SILENT_PCM = b"\x00\x00" * 320


class _Host:
    def __init__(self) -> None:
        self.metrics = SimpleNamespace(
            inc_media_metric=lambda *_args, **_kwargs: None,
            set_media_metric=lambda *_args, **_kwargs: None,
        )

    @staticmethod
    def _stream_epoch_is_current(_context: object, _stream_epoch: int) -> bool:
        return True


class _Provider:
    def __init__(self) -> None:
        self.frames: list[AudioFrame] = []
        self.ingested = asyncio.Event()

    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> tuple[Any, ...]:
        self.frames.append(frame)
        self.ingested.set()
        return ()


def _context(
    state: MediaAudioIngressState,
    provider: _Provider,
    *,
    phase: InteractionPhase = InteractionPhase.LISTENING,
) -> SimpleNamespace:
    return SimpleNamespace(
        identity=_IDENTITY,
        stream_epoch=_STREAM_EPOCH,
        ingress=state,
        asr=SimpleNamespace(record_audio=lambda **_kwargs: True),
        runtime=SimpleNamespace(
            interaction_phase=phase,
            feed_speaker_pcm=lambda _pcm: None,
        ),
        pending=PendingTurn(),
        turn_commit_task=None,
        closed=False,
        standby_requested=False,
        provider=provider,
    )


def _frame(sequence: int, capture_start_sample: int, payload: bytes) -> AudioFrame:
    return AudioFrame(
        identity=_IDENTITY,
        sequence=sequence,
        capture_start_sample=capture_start_sample,
        frame_samples=len(payload) // 2,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_listening_silence_is_not_tapped_uploaded_or_logged(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR", str(tmp_path))
    state = MediaAudioIngressState.create(max_frames=4)
    provider = _Provider()
    ingress = MediaAudioIngress(_Host())
    context = _context(state, provider)

    with caplog.at_level(logging.DEBUG):
        await ingress.accept(context, _frame(0, 0, _SILENT_PCM))
    await asyncio.sleep(0.05)

    assert provider.frames == []
    assert state.queue.empty()
    assert state.listening_silence_dropped_frames == 1
    assert state.listening_silence_dropped_samples == 320
    # The tap is created lazily inside the pump; refusing admission means no
    # WAV is opened and no byte is written.
    assert list(tmp_path.iterdir()) == []
    assert not [record for record in caplog.records if "media PCM tap" in record.message]
    assert not [record for record in caplog.records if "media_asr" in record.message]


@pytest.mark.asyncio
async def test_accepted_utterance_audio_still_reaches_provider(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR", str(tmp_path))
    state = MediaAudioIngressState.create(max_frames=4)
    provider = _Provider()
    ingress = MediaAudioIngress(_Host())
    context = _context(state, provider)
    context.pending.active_vad_stream_epoch = _STREAM_EPOCH

    await ingress.accept(context, _frame(0, 0, _SPEECH_PCM))
    await asyncio.wait_for(provider.ingested.wait(), timeout=1)

    assert len(provider.frames) == 1
    assert provider.frames[0].frame_samples == 320
    assert provider.frames[0].capture_start_sample == 0
    assert state.listening_silence_dropped_frames == 0
    assert len(list(tmp_path.glob("*.wav"))) == 1
    await ingress.cancel(context)


@pytest.mark.asyncio
async def test_non_silent_uplink_without_vad_is_still_admitted() -> None:
    """A device question can begin without a VAD edge during playback."""

    state = MediaAudioIngressState.create(max_frames=4)
    provider = _Provider()
    ingress = MediaAudioIngress(_Host())
    context = _context(state, provider)

    await ingress.accept(context, _frame(0, 0, _SPEECH_PCM))
    await asyncio.wait_for(provider.ingested.wait(), timeout=1)

    assert len(provider.frames) == 1
    assert state.listening_silence_dropped_frames == 0
    await ingress.cancel(context)


@pytest.mark.asyncio
async def test_cancel_clears_queue_denoiser_and_tap() -> None:
    state = MediaAudioIngressState.create(max_frames=4)
    provider = _Provider()
    ingress = MediaAudioIngress(_Host())
    context = _context(state, provider)
    context.pending.active_vad_stream_epoch = _STREAM_EPOCH

    state.pump_task = asyncio.create_task(asyncio.sleep(3600))
    await ingress.accept(context, _frame(0, 0, _SPEECH_PCM))
    assert state.queue.qsize() == 1
    state.denoiser.process(_SPEECH_PCM)
    assert state.denoiser.get_stats()["total_frames"] == 1
    state.loss_concealed_ranges.append((0, 320))
    state.discontinuity_pending = True
    closed: list[bool] = []
    state.pcm_tap = SimpleNamespace(close=lambda: closed.append(True))  # type: ignore[assignment]
    state.pcm_tap_initialized = True

    await ingress.cancel(context)

    assert state.queue.empty()
    assert state.pump_task is None
    assert state.pcm_tap is None
    assert state.pcm_tap_initialized is False
    assert closed == [True]
    assert state.denoiser.get_stats()["total_frames"] == 0
    assert not state.loss_concealed_ranges
    assert state.discontinuity_pending is False
