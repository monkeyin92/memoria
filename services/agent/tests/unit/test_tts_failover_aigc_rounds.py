"""AIGC markers around rounds whose reply changes TTS engine.

Each scenario drives the real ``MediaVoiceCoreRegistry`` on a device session:
the opening marker, one conversation reply that either stays on StepFun or
fails over to Doubao (and, for a crisis line, from Doubao to StepFun), then the
closing marker. Marker counts come from the real output dispatch, not from a
counter the production path could forget to increment.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.providers.provider_errors import APIConnectionError, APIStatusError
from services.agent.src.providers.stepfun_tts import StepFunFirstAudioTimeoutError
from services.agent.src.providers.tts_failover import (
    StepFunFailoverConfig,
    StepFunPrimaryTTS,
    TTSFailoverBreaker,
)
from services.agent.src.providers.tts_stream import AudioFrame, SynthesizedAudio
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.unit.media_session_support import (
    _CapturingGenerationBridge,
    _device_identity,
    _wait_until,
)
from services.agent.tests.unit.test_minor_session_limits import _ledger
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT

_FRAME = 480
_USER_TEXT = "你好呀"
_NORMAL_REPLY = "我在听。"
_CRISIS_REPLY = CRISIS_SELF_SCRIPT


class _RoundReply:
    def __init__(self, reply: Any) -> None:
        self.reply = reply

    def stream(self, _request: LanguageModelRequest) -> AsyncIterator[Any]:
        async def items() -> AsyncIterator[Any]:
            yield self.reply

        return items()


class _RoundStream:
    """One engine attempt: audio, a moderation error, a drop, a hang or silence."""

    def __init__(self, engine: _RoundEngine) -> None:
        self._engine = engine
        self._yielded = False

    def push_text(self, text: str) -> None:
        self._engine.received.append(text)

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> _RoundStream:
        return self

    async def __anext__(self) -> SynthesizedAudio:
        engine = self._engine
        if engine.behavior == "timeout":
            raise StepFunFirstAudioTimeoutError("upstream first audio timeout")
        if engine.behavior == "moderation":
            raise APIStatusError("content moderation rejected", status_code=451)
        if engine.behavior == "connection":
            raise APIConnectionError("upstream closed before audio")
        if engine.behavior == "empty" or self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        pcm = b"\x01\x00" * _FRAME
        return SynthesizedAudio(
            frame=AudioFrame(
                data=pcm,
                sample_rate=24_000,
                num_channels=1,
                samples_per_channel=len(pcm) // 2,
            ),
            request_id=engine.provider,
            segment_id=engine.provider,
            is_final=True,
        )

    async def aclose(self) -> None:
        return None


class _RoundEngine:
    def __init__(self, provider: str, model: str, behavior: str = "audio") -> None:
        self.provider = provider
        self.model = model
        self.current_model = model
        self.current_voice = f"{provider}-voice"
        self.behavior = behavior
        self.received: list[str] = []

    def bind_fence(self, _fence: GenerationFence) -> None:
        return None

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> _RoundStream:
        del conn_options
        return _RoundStream(self)

    async def aclose(self) -> None:
        return None


class _MarkerRecordingAdapter(ExistingVoiceProviderAdapter):
    """Record every AIGC marker that reaches the real output dispatch."""

    def __init__(self, *args: Any, markers: list[tuple[GenerationFence, bytes]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.markers = markers

    async def generate_output(
        self,
        identity: SessionIdentity,
        intent: Any,
        fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[Any]:
        if int(intent.kind) == int(
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
        ) and intent.pcm_s16le:
            self.markers.append((fence, bytes(intent.pcm_s16le)))
        async for chunk in super().generate_output(
            identity,
            intent,
            fence,
            work_id=work_id,
            source_start_sample=source_start_sample,
        ):
            yield chunk


class _RoundBridge(_CapturingGenerationBridge):
    def __init__(self) -> None:
        super().__init__()
        self.generations: list[tuple[int, GenerationFence]] = []

    async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
        action = _kwargs.get("action")
        fence = _args[1] if len(_args) > 1 else _kwargs.get("fence")
        if isinstance(fence, GenerationFence) and isinstance(action, int):
            self.generations.append((action, fence))
        return await super().emit_generation(*_args, **_kwargs)


def _notice_config() -> AigcNoticeConfig:
    return AigcNoticeConfig(
        tone_short_ms=20,
        tone_long_ms=30,
        tone_gap_ms=0,
        tone_group_gap_ms=0,
        lead_interval_ms=0,
        marker_duration_ms=200,
    )


def _speech(
    primary_behavior: str,
    backup_behavior: str,
) -> tuple[StepFunPrimaryTTS, _RoundEngine, _RoundEngine]:
    primary = _RoundEngine("stepfun", "stepaudio-3-tts", primary_behavior)
    backup = _RoundEngine("volcengine_doubao", "seed-tts-2.0", backup_behavior)
    wrapper = StepFunPrimaryTTS(
        primary=primary,
        backup=backup,
        config=StepFunFailoverConfig(
            failures=3,
            open_s=30,
            fixed_safety_budget_s=2.0,
            fixed_safety_backup_first_audio_s=0.05,
            fixed_safety_primary_first_audio_s=0.05,
            fixed_safety_primary_first=False,
        ),
        breaker=TTSFailoverBreaker(failures=3, open_s=30),
    )
    return wrapper, primary, backup


async def _playback(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    session: Any,
    fence: GenerationFence,
    *,
    event_type: PlaybackEventType = PlaybackEventType.ENDED,
) -> None:
    frame = next(
        frame
        for frame in reversed(registry.bridge.frames)
        if frame.generation_id == fence.generation_id
        and frame.turn_id == fence.turn_id
    )
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=frame.sequence,
            rendered_sample_end=frame.source_start_sample + frame.frame_samples,
            client_monotonic_ms=1,
            approximate=False,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=event_type,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("primary_behavior", "backup_behavior", "reply"),
    [
        ("timeout", "audio", _NORMAL_REPLY),
        ("moderation", "audio", _NORMAL_REPLY),
        ("connection", "audio", _NORMAL_REPLY),
        ("audio", "connection", _CRISIS_REPLY),
    ],
    ids=("first_audio_timeout", "moderation_451", "connection_no_audio", "crisis"),
)
async def test_one_round_plays_each_aigc_marker_once(
    monkeypatch: pytest.MonkeyPatch,
    primary_behavior: str,
    backup_behavior: str,
    reply: Any,
) -> None:
    notice = _notice_config()
    markers: list[tuple[GenerationFence, bytes]] = []
    speech, primary, backup = _speech(primary_behavior, backup_behavior)
    adapter = _MarkerRecordingAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=cast(Any, _RoundReply(reply)),
        speech_synthesis=cast(Any, speech),
        markers=markers,
    )
    bridge = _RoundBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: adapter,
        aigc_notice_config=notice,
        aigc_round_logger=None,
        daily_chat_client=_ledger(
            httpx.Response(200, json={"used_seconds": 0}),
            [],
        ),
        owner_silence_timeout_s=100,
        speaking_stall_timeout_s=0.3,
    )
    registry.install()
    identity = _device_identity(f"aigc-round-{primary_behavior}-{backup_behavior}")

    async def no_wake_ack(_registry: Any, _context: Any) -> None:
        return None

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", no_wake_ack)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    if context.runtime.orchestrator.state is ConversationState.CONNECTING:
        await context.runtime.orchestrator.ready()
        context.runtime.set_interaction_phase(InteractionPhase.LISTENING, cause="test_ready")
    try:
        await _wait_until(
            lambda: context.output.output_owner is None
            and context.runtime.interaction_phase is InteractionPhase.LISTENING
        )
        assert len(markers) == 1

        fence = await context.runtime.on_turn_committed(_USER_TEXT)
        context.output.begin_turn_output(fence)
        await registry.generate_reply(identity.session_id, _USER_TEXT, fence)
        await _wait_until(lambda: context.output.provider_complete)
        await _playback(registry, identity, session, fence)
        await _wait_until(lambda: context.output.output_owner is None)

        closing = asyncio.create_task(registry._play_aigc_closing_marker(context))
        await _wait_until(lambda: len(markers) >= 2)
        closing_fence = markers[1][0]
        await _wait_until(
            lambda: any(
                frame.generation_id == closing_fence.generation_id
                and frame.turn_id == closing_fence.turn_id
                for frame in bridge.frames
            )
        )
        assert await asyncio.wait_for(closing, timeout=2)

        assert len(markers) == 2
        assert [pcm for _fence, pcm in markers] == [notice.tone_pcm(), notice.tone_pcm()]
        for marker_fence, _pcm in markers:
            actions = [
                action
                for action, fence in bridge.generations
                if fence.matches(marker_fence)
            ]
            assert actions.count(media_pb2.GENERATION_ACTION_START) == 1
            assert any(
                frame.generation_id == marker_fence.generation_id
                and frame.turn_id == marker_fence.turn_id
                for frame in bridge.frames
            )
        expected = reply.spoken if reply is _CRISIS_REPLY else _NORMAL_REPLY
        assert primary.received == [expected]
        assert backup.received == [expected]
        assert any(
            frame.generation_id == fence.generation_id
            and frame.turn_id == fence.turn_id
            for frame in bridge.frames
        )
    finally:
        await registry.finalize_session(identity.session_id)
