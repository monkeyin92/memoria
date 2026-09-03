from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.prompts import (
    SPEAKER_ENROLLMENT_DONE_PHRASE,
    SPEAKER_ENROLLMENT_SAMPLE_PROMPTS,
    device_wake_phrase,
)
from services.agent.src.voice_core.media_protocol import PlaybackEventType, PlaybackProgress
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment
from services.agent.tests.unit.test_media_session import (
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _device_identity,
    _finish_output_owner_playback,
    _wait_until,
)


def _pcm(seconds: float, sample_rate: int = 16_000) -> bytes:
    samples = np.full(int(seconds * sample_rate), 2_000, dtype=np.int16)
    return samples.tobytes()


async def _finish_current_playback(
    registry: MediaVoiceCoreRegistry,
    identity: Any,
    bridge: _CapturingGenerationBridge,
    session: Any,
) -> None:
    context = registry._sessions[identity.session_id]
    await _wait_until(
        lambda: context.output_owner is not None or bool(bridge.frames),
        timeout=2.0,
    )
    if context.output_owner is not None:
        await _finish_output_owner_playback(registry, identity, bridge, session)
        return
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


@pytest.mark.asyncio
async def test_device_session_skips_enrollment_without_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "true")

    async def _status(*, session_id: str) -> dict[str, object]:
        _ = session_id
        return {"enrollment": {"state": "required", "intent_id": None}}

    monkeypatch.setattr(
        "services.agent.src.speaker_authority_client.load_device_enrollment_status",
        _status,
    )
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
    identity = _device_identity("device-enroll-skip")
    bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        task = context.speaker_enrollment_task
        if task is not None:
            await asyncio.wait_for(task, timeout=1)
        assert provider.texts == [device_wake_phrase(identity.session_id)]
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_session_speaks_enrollment_prompts_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "true")
    enrolled: dict[str, Any] = {}

    async def _status(*, session_id: str) -> dict[str, object]:
        _ = session_id
        return {"enrollment": {"state": "requested", "intent_id": "intent-device"}}

    class _Authority:
        async def enroll(self, **payload: Any) -> dict[str, str]:
            enrolled.update(payload)
            return {"profile_id": "profile-shadow", "status": "shadow"}

    monkeypatch.setattr(
        "services.agent.src.speaker_authority_client.load_device_enrollment_status",
        _status,
    )
    monkeypatch.setattr(
        "services.agent.src.speaker_authority_client.speaker_authority_client_from_settings",
        lambda: _Authority(),
    )
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
    identity = _device_identity("device-enroll-run")
    session = bridge.bridge.open(identity)
    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await _finish_current_playback(registry, identity, bridge, session)
        await _wait_until(
            lambda: (
                context.output_owner is None
                and context.runtime.orchestrator.state is ConversationState.LISTENING
            ),
            timeout=2.0,
        )
        for prompt in SPEAKER_ENROLLMENT_SAMPLE_PROMPTS:
            await _wait_until(lambda current=prompt: current in provider.texts, timeout=2.0)
            await _finish_current_playback(registry, identity, bridge, session)
            assert context.runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
            context.runtime.feed_speaker_pcm(_pcm(1.5))
            context.runtime.on_user_voice_stopped()
            await asyncio.sleep(0)
        await _wait_until(
            lambda: SPEAKER_ENROLLMENT_DONE_PHRASE in provider.texts,
            timeout=2.0,
        )
        assert enrolled["session_id"] == identity.session_id
        assert enrolled["intent_id"] == "intent-device"
        assert len(enrolled["samples"]) == 4
        assert provider.texts[0] == device_wake_phrase(identity.session_id)
        assert provider.texts[1:5] == list(SPEAKER_ENROLLMENT_SAMPLE_PROMPTS)
        assert provider.texts[5] == SPEAKER_ENROLLMENT_DONE_PHRASE
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_vad_end_collects_enrollment_sample_without_asr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "true")

    async def _status(*, session_id: str) -> dict[str, object]:
        _ = session_id
        return {"enrollment": {"state": "blocked"}}

    monkeypatch.setattr(
        "services.agent.src.speaker_authority_client.load_device_enrollment_status",
        _status,
    )
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
    identity = _device_identity("device-enroll-vad")
    session = bridge.bridge.open(identity)
    received: list[tuple[bytes, int]] = []

    async def _sink(pcm: bytes, sample_rate: int) -> None:
        received.append((pcm, sample_rate))

    try:
        context = await registry._get_or_create(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await _finish_current_playback(registry, identity, bridge, session)
        context.runtime.set_formal_speaker_enrollment_sample_sink(_sink)
        context.runtime.begin_formal_speaker_enrollment(target_samples=4)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="enroll-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        context.runtime.feed_speaker_pcm(_pcm(1.5))
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="enroll-end",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=24_000,
                capture_end_sample=24_001,
                final=True,
                voiced_end_sample=24_000,
            ),
        )
        await asyncio.sleep(0)
        assert received
        assert received[0][1] == 16_000
        assert context.turn_start_sample is None
        assert identity.stream_epoch in provider.pause_asr_calls
        assert context.runtime.formal_speaker_enrollment_active is True
    finally:
        context.runtime.end_formal_speaker_enrollment()
        await registry._finalize_session(identity.session_id)
