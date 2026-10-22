"""AIGC markers and failover around consecutive device reply rounds.

Each scenario drives the real ``MediaVoiceCoreRegistry`` on a device session.
A round is one audible assistant turn: it opens with a START marker before the
reply and closes with an END marker after playback, a stop, or a failure.
Marker counts come from the real output dispatch, not from a counter the
production path could forget to increment. The last cases pin the 2026-10-22
field failure: a clean StepFun close on stop must not move the session to
Doubao, and a failed Doubao backup must not pin later turns to it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import services.agent.src.voice_core.media_session_aigc_notice as aigc_notice_module
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
    TTSUnavailableError,
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
from services.agent.src.voice_core.media_session_aigc_notice import MediaSessionAigcNoticeMixin
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.unit.media_session_support import (
    _CapturingGenerationBridge,
    _device_identity,
    _wait_until,
)
from services.agent.tests.unit.test_minor_session_limits import _ledger
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

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
        if engine.behavior == "forbidden":
            raise APIStatusError("backup account in arrears", status_code=403)
        if engine.behavior == "normal_close" and self._yielded:
            raise ConnectionClosedOK(Close(1000, "stop"), None)
        if engine.behavior == "hold" and self._yielded:
            await engine.release.wait()
            raise StopAsyncIteration
        if engine.behavior == "empty" or self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        # The emitter holds a ~240 ms tail until a segment ends. A held unit
        # must push more than that to put audio on the wire before the stop.
        samples = _FRAME * 30 if engine.behavior == "hold" else _FRAME
        pcm = b"\x01\x00" * samples
        return SynthesizedAudio(
            frame=AudioFrame(
                data=pcm,
                sample_rate=24_000,
                num_channels=1,
                samples_per_channel=len(pcm) // 2,
            ),
            request_id=engine.provider,
            segment_id=engine.provider,
            # A held unit must stay open after its first audio so the stop
            # round cancels a speaking reply, not a finished stream.
            is_final=engine.behavior != "hold",
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
        self.release = asyncio.Event()

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

    def __init__(
        self, *args: Any, markers: list[tuple[GenerationFence, bytes]], **kwargs: Any
    ) -> None:
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
        if (
            int(intent.kind) == int(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT)
            and intent.pcm_s16le
        ):
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
            fixed_safety_primary_first_audio_s=0.05,
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
        if frame.generation_id == fence.generation_id and frame.turn_id == fence.turn_id
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


async def _round_audio(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    context: Any,
    *,
    wait_provider: bool = True,
) -> tuple[GenerationFence, asyncio.Task[bool]]:
    """Drive one reply turn; the opening cue is the head of its own audio."""

    await registry._wait_for_output_idle(context, timeout_s=4.0)
    fence = await context.runtime.on_turn_committed(_USER_TEXT)
    context.output.begin_turn_output(fence)
    task = asyncio.create_task(registry.generate_reply(identity.session_id, _USER_TEXT, fence))
    context.output.reply_task = task
    if wait_provider:
        await _wait_until(lambda: context.output.provider_complete, timeout=3.0)
    return (
        context.output.playback.current_fence or context.runtime.fence,
        task,
    )


async def _ack_marker(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    session: Any,
    fence: GenerationFence,
) -> None:
    await _wait_until(
        lambda: any(
            frame.generation_id == fence.generation_id and frame.turn_id == fence.turn_id
            for frame in registry.bridge.frames
        )
    )
    await _playback(registry, identity, session, fence)


def _reply_fence_frames(bridge: _RoundBridge, fence: GenerationFence) -> list[Any]:
    return [
        frame
        for frame in bridge.frames
        if frame.generation_id == fence.generation_id and frame.turn_id == fence.turn_id
    ]


def _marker_frame_count(notice: AigcNoticeConfig) -> int:
    frame_bytes = _FRAME * 2
    return (len(notice.tone_pcm()) + frame_bytes - 1) // frame_bytes


async def _wait_for_reply_frames(
    bridge: _RoundBridge, fence: GenerationFence, notice: AigcNoticeConfig
) -> None:
    marker_frames = _marker_frame_count(notice)
    await _wait_until(
        lambda: len(_reply_fence_frames(bridge, fence)) > marker_frames,
        timeout=3.0,
    )


def _assert_reply_round_audio(
    bridge: _RoundBridge, fence: GenerationFence, notice: AigcNoticeConfig
) -> None:
    """Every round: cue first, then the reply's own frames, then the END cue."""

    frames = _reply_fence_frames(bridge, fence)
    assert frames, "the reply fence emitted no frames"
    audio = b"".join(bytes(frame.pcm_s16le) for frame in frames)
    assert audio.startswith(notice.tone_pcm()), "opening cue must lead the reply audio"
    reply = b"".join(bytes(frame.pcm_s16le) for frame in frames[_marker_frame_count(notice) :])
    assert reply, "the reply itself emitted no audio frames"


@pytest.mark.asyncio
async def test_three_rounds_play_start_and_end_markers_after_normal_stop_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = _notice_config()
    markers: list[tuple[GenerationFence, bytes]] = []
    speech, primary, backup = _speech("audio", "audio")
    adapter = _MarkerRecordingAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=cast(Any, _RoundReply(_NORMAL_REPLY)),
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
    identity = dataclasses.replace(
        _device_identity("aigc-three-rounds"), audio_mode="interrupt_assist"
    )

    async def no_wake_ack(_registry: Any, _context: Any) -> None:
        return None

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", no_wake_ack)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    if context.runtime.orchestrator.state is ConversationState.CONNECTING:
        await context.runtime.orchestrator.ready()
        context.runtime.set_interaction_phase(InteractionPhase.LISTENING, cause="test_ready")
    try:
        await _wait_until(lambda: len(markers) == 1)
        # The wake acknowledgement is its own audible turn: open and close it
        # before the three reply rounds so the loop starts at a clean boundary.
        await registry._close_aigc_round(context, close_reason="wake_ack")
        await _wait_until(lambda: len(markers) == 2)
        await registry._wait_for_output_idle(context)
        await _ack_marker(registry, identity, session, markers[1][0])
        markers.clear()

        # Round 1: ordinary StepFun reply. Its opening cue rides the reply
        # work, so only the closing cue is admitted as a marker.
        fence, reply_task = await _round_audio(registry, identity, context)
        await _playback(registry, identity, session, fence)
        await _wait_until(lambda: len(markers) == 1)
        await _ack_marker(registry, identity, session, markers[0][0])
        assert [pcm for _fence, pcm in markers] == [notice.tone_pcm()]
        _assert_reply_round_audio(bridge, fence, notice)
        assert primary.received.count(_NORMAL_REPLY) >= 1
        assert backup.received == []

        # Round 2: the child stops a still-playing reply; the END marker plays.
        primary.behavior = "hold"
        primary.release.clear()
        fence, reply_task = await _round_audio(registry, identity, context, wait_provider=False)
        await _wait_until(
            lambda: (
                context.output.output_owner is not None
                and int(context.output.output_owner.intent.kind)
                != int(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT)
            )
        )
        await _wait_until(lambda: primary.received.count(_NORMAL_REPLY) >= 2, timeout=3.0)
        await _wait_for_reply_frames(bridge, fence, notice)
        await registry._cancel_reply_task(context, fence, reason="voice_stop_command")
        registry._schedule_aigc_round_closing(context, close_reason="voice_stop_command")
        primary.release.set()
        await asyncio.gather(reply_task, return_exceptions=True)
        await _wait_until(lambda: len(markers) == 2)
        await _ack_marker(registry, identity, session, markers[1][0])
        assert [pcm for _fence, pcm in markers[1:]] == [notice.tone_pcm()]
        _assert_reply_round_audio(bridge, fence, notice)
        assert primary.received.count(_NORMAL_REPLY) >= 2
        assert backup.received == []

        # Round 3: StepFun drops before audio, Doubao serves the reply.
        primary.behavior = "connection"
        fence, reply_task = await _round_audio(registry, identity, context)
        await _playback(registry, identity, session, fence)
        await _wait_until(lambda: len(markers) == 3)
        await _ack_marker(registry, identity, session, markers[2][0])
        assert [pcm for _fence, pcm in markers[2:]] == [notice.tone_pcm()]
        _assert_reply_round_audio(bridge, fence, notice)
        assert _NORMAL_REPLY in primary.received
        assert _NORMAL_REPLY in backup.received
        assert len(markers) == 3
        await asyncio.gather(reply_task, return_exceptions=True)
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_clean_stepfun_close_on_stop_does_not_move_the_session_to_doubao() -> None:
    speech, primary, backup = _speech("normal_close", "audio")

    stream = speech.stream(conn_options=cast(Any, None))
    stream.push_text(_NORMAL_REPLY)
    stream.end_input()
    events = [event async for event in stream]

    assert len(events) == 1
    assert backup.received == []
    assert speech.degraded is False
    assert speech.breaker.state == "closed"

    # The next turn retries StepFun, not the backup.
    primary.behavior = "audio"
    second = speech.stream(conn_options=cast(Any, None))
    second.push_text(_NORMAL_REPLY)
    second.end_input()
    assert [event async for event in second]
    assert backup.received == []
    assert primary.received == [_NORMAL_REPLY, _NORMAL_REPLY]


@pytest.mark.asyncio
async def test_one_stepfun_failure_does_not_pin_later_turns_to_doubao() -> None:
    speech, primary, backup = _speech("connection", "audio")

    first = speech.stream(conn_options=cast(Any, None))
    first.push_text(_NORMAL_REPLY)
    first.end_input()
    assert [event async for event in first]

    assert backup.received == [_NORMAL_REPLY]
    assert speech.degraded is False
    assert speech.breaker.state == "closed"

    primary.behavior = "audio"
    second = speech.stream(conn_options=cast(Any, None))
    second.push_text(_NORMAL_REPLY)
    second.end_input()
    assert [event async for event in second]
    assert primary.received == [_NORMAL_REPLY, _NORMAL_REPLY]
    assert backup.received == [_NORMAL_REPLY]


@pytest.mark.asyncio
async def test_doubao_403_does_not_pin_later_turns_to_the_backup() -> None:
    speech, primary, backup = _speech("connection", "forbidden")

    first = speech.stream(conn_options=cast(Any, None))
    first.push_text(_NORMAL_REPLY)
    first.end_input()
    with pytest.raises(TTSUnavailableError, match="403"):
        _ = [event async for event in first]

    assert speech.degraded is False
    assert backup.received == [_NORMAL_REPLY]

    primary.behavior = "audio"
    backup.behavior = "audio"
    second = speech.stream(conn_options=cast(Any, None))
    second.push_text(_NORMAL_REPLY)
    second.end_input()
    events = [event async for event in second]
    assert len(events) == 1
    assert primary.received == [_NORMAL_REPLY, _NORMAL_REPLY]
    assert backup.received == [_NORMAL_REPLY]


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
async def test_one_round_still_plays_each_marker_once(
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
        await _wait_until(lambda: len(markers) == 1)
        await registry._close_aigc_round(context, close_reason="wake_ack")
        await _wait_until(lambda: len(markers) == 2)
        await _ack_marker(registry, identity, session, markers[1][0])
        await registry._wait_for_output_idle(context)
        markers.clear()

        fence, reply_task = await _round_audio(registry, identity, context)
        await _playback(registry, identity, session, fence)
        await _wait_until(lambda: len(markers) == 1)
        await _ack_marker(registry, identity, session, markers[0][0])

        assert len(markers) == 1
        assert [pcm for _fence, pcm in markers] == [notice.tone_pcm()]
        _assert_reply_round_audio(bridge, fence, notice)
        expected = reply.spoken if reply is _CRISIS_REPLY else _NORMAL_REPLY
        assert primary.received == [expected]
        # A crisis unit never opens Doubao; an ordinary unit falls back once.
        assert backup.received == ([] if reply is _CRISIS_REPLY else [expected])
    finally:
        await registry.finalize_session(identity.session_id)


class _HangingCloseNotice(MediaSessionAigcNoticeMixin):
    def __init__(self) -> None:
        self.close_started = asyncio.Event()

    async def _close_aigc_round(self, context: Any, *, close_reason: str) -> None:
        _ = context, close_reason
        self.close_started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_timed_out_round_close_is_cancelled_before_the_next_end_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale closing task must not suppress the next reply turn's END marker."""

    monkeypatch.setattr(aigc_notice_module, "_AIGC_ROUND_CLOSE_WAIT_S", 0.01)
    notices = _HangingCloseNotice()
    context = SimpleNamespace(
        identity=SimpleNamespace(session_id="aigc-close-timeout", client_type="device"),
        aigc_closing_task=None,
        aigc_closing_marker_state="idle",
        aigc_round_active=True,
    )
    stale = asyncio.create_task(
        notices._close_aigc_round(context, close_reason="stale"),
        name="stale-aigc-close",
    )
    context.aigc_closing_task = stale
    await notices.close_started.wait()

    assert await notices._wait_aigc_round_closed(context) is True
    assert stale.cancelled()
    assert context.aigc_closing_task is None

    notices._schedule_aigc_round_closing(context, close_reason="next_turn")
    replacement = context.aigc_closing_task
    assert replacement is not None and replacement is not stale
    replacement.cancel()
    await asyncio.gather(replacement, return_exceptions=True)
