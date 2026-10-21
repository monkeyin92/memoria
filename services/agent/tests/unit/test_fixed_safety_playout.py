from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast, get_args

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration import phrase_segmenter
from services.agent.src.orchestration.handlers import (
    LanguageModelRequest,
    ReplyItem,
    SpeechSynthesisRequest,
)
from services.agent.src.orchestration.orchestrator import OfflinePipeline, Orchestrator
from services.agent.src.providers.provider_errors import APIStatusError
from services.agent.src.providers.tts_failover import (
    StepFunFailoverConfig,
    StepFunPrimaryTTS,
    TTSFailoverBreaker,
)
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.guardian.crisis import NotificationStatus

CHILD_TEXT = "我不想活了"
FRAME = 480
SPOKEN_SCRIPT = "".join(phrase.spoken for phrase in CRISIS_SELF_SCRIPT.phrases)


class ReplyLLM:
    def __init__(self, *items: ReplyItem) -> None:
        self.items = items

    def stream(self, request: LanguageModelRequest) -> AsyncIterator[ReplyItem]:
        _ = request

        async def items() -> AsyncIterator[ReplyItem]:
            for item in self.items:
                yield item

        return items()


class SubtitledSpeech:
    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.queue: asyncio.Queue[Any | None] = asyncio.Queue()

    def stream(self) -> SubtitledSpeech:
        return self

    def push_text(self, text: str) -> None:
        self.pushed.append(text)
        frame = SimpleNamespace(data=b"\x01\x00" * FRAME, sample_rate=24_000)
        self.queue.put_nowait(SimpleNamespace(frame=frame))

    def end_input(self) -> None:
        self.queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            while (item := await self.queue.get()) is not None:
                yield item

        return events()

    def timed_transcript(self) -> tuple[SimpleNamespace, ...]:
        return tuple(
            SimpleNamespace(text=text, start_time=index * 0.02, end_time=(index + 1) * 0.02)
            for index, text in enumerate(self.pushed)
        )

    def timed_transcript_alignment(self) -> str:
        return "ok"

    async def aclose(self) -> None:
        return None


class StoppingSpeech(SubtitledSpeech):
    def __init__(self) -> None:
        super().__init__()
        self.cancel: Any = None

    def push_text(self, text: str) -> None:
        super().push_text(text)
        cancel = self.cancel
        self.cancel = None
        if cancel is not None:
            cancel()


class RecordingSynthesis:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
        self.requests.append(request.phrases)
        return SimpleNamespace(pcm=b"\x01\x00" * FRAME)


@pytest.fixture(autouse=True)
def eager_segmenter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phrase_segmenter, "HARD_LIMIT", 2)


def _adapter(llm: ReplyLLM, speech: object) -> ExistingVoiceProviderAdapter:
    return ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=llm,
        speech_synthesis=cast(Any, speech),
    )


def _generation(name: str) -> tuple[SessionIdentity, GenerationFence]:
    identity = SessionIdentity(name, stream_epoch=1)
    return identity, GenerationFence(identity.session_id, 1, 1, 0)


def _spans(chunk: Any) -> list[tuple[str, int, int]]:
    return [
        (span.text, span.audio_start_sample, span.audio_end_sample) for span in chunk.text_spans
    ]


def _playout_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("fixed_safety_playout")
    ]


@pytest.mark.asyncio
async def test_live_speech_gets_spoken_copy_and_heard_history_gets_display_copy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    speech = SubtitledSpeech()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-live")

    with caplog.at_level(logging.INFO):
        chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    # The approved line is synthesized as one whole sentence so no provider
    # fault can truncate it mid-script (C1, 2026-10-21).
    assert speech.pushed == [SPOKEN_SCRIPT]
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
    assert [chunk.final for chunk in chunks] == [True]
    assert _spans(chunks[-1]) == [(CRISIS_SELF_SCRIPT.display, 0, FRAME)]
    playout = _playout_lines(caplog)
    assert len(playout) == 1
    assert re.fullmatch(
        r"fixed_safety_playout reply_template_id=crisis-self-v1 "
        r"at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        playout[0],
    )
    for record in caplog.records:
        message = record.getMessage()
        assert CHILD_TEXT not in message
        for phrase in CRISIS_SELF_SCRIPT.phrases:
            assert phrase.spoken not in message
            assert phrase.display not in message


@pytest.mark.asyncio
async def test_model_text_with_digits_reaches_the_engine_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    speech = SubtitledSpeech()
    adapter = _adapter(ReplyLLM("请打 120。"), speech)
    identity, fence = _generation("model-digits")

    with caplog.at_level(logging.INFO):
        chunks = [chunk async for chunk in adapter.generate_reply(identity, "急救电话", fence)]

    assert speech.pushed == ["请打 120。"]
    assert _spans(chunks[-1]) == [("请打 120。", 0, FRAME)]
    assert _playout_lines(caplog) == []


@pytest.mark.asyncio
async def test_stop_cuts_the_crisis_script_and_leaves_the_guardian_pin() -> None:
    crisis = route_crisis(CHILD_TEXT)
    assert crisis.notify_guardian is True
    assert crisis.attempt_push is True
    assert crisis.script is CRISIS_SELF_SCRIPT
    for status in ("pending", "pin_only", "delivered", "failed", "no_subscription"):
        assert status in get_args(NotificationStatus), status

    speech = StoppingSpeech()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("crisis-stopped")
    speech.cancel = lambda: adapter.cancel_generation(fence)
    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    spoken = "".join(speech.pushed)
    assert speech.pushed == [SPOKEN_SCRIPT]
    assert "好的" not in spoken
    assert "嗯，你说" not in spoken
    # The stop lands before the first frame, so nothing of the script is claimed as spoken.
    assert chunks == []
    assert route_crisis(CHILD_TEXT).notify_guardian is True
    assert "pending" in get_args(NotificationStatus)
    assert "delivered" in get_args(NotificationStatus)


@pytest.mark.asyncio
async def test_interrupted_script_claims_no_provider_subtitles() -> None:
    speech = SubtitledSpeech()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-interrupt")
    stream = adapter.generate_reply(identity, CHILD_TEXT, fence)

    assert (await anext(stream)).first
    assert await adapter.interrupted_timed_text_spans(fence) == ()
    assert adapter.cancel_generation(fence)
    await stream.aclose()


@pytest.mark.asyncio
async def test_buffered_synthesis_gets_spoken_copy_per_approved_phrase() -> None:
    synthesis = RecordingSynthesis()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), synthesis)
    identity, fence = _generation("fixed-safety-buffered")

    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    assert synthesis.requests == [(phrase.spoken,) for phrase in CRISIS_SELF_SCRIPT.phrases]
    assert [chunk.text for chunk in chunks] == [
        phrase.display for phrase in CRISIS_SELF_SCRIPT.phrases
    ]
    assert [chunk.final for chunk in chunks] == [False, False, False, True]


@pytest.mark.asyncio
async def test_offline_pipeline_speaks_spoken_copy_and_keeps_display_copy() -> None:
    synthesis = RecordingSynthesis()
    orchestrator = Orchestrator()

    result = await OfflinePipeline(
        orchestrator=orchestrator,
        llm_handler=ReplyLLM(CRISIS_SELF_SCRIPT),
        tts_handler=synthesis,
    ).run_turn(CHILD_TEXT)
    await orchestrator.close()

    assert synthesis.requests == [tuple(phrase.spoken for phrase in CRISIS_SELF_SCRIPT.phrases)]
    assert result["full_generated"] == CRISIS_SELF_SCRIPT.display


class _FixedScriptBackupStream:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self._yielded = False

    def push_text(self, text: str) -> None:
        self.texts.append(text)

    def end_input(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def __aiter__(self) -> _FixedScriptBackupStream:
        return self

    async def __anext__(self) -> Any:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return SimpleNamespace(frame=SimpleNamespace(data=b"\x01\x00" * FRAME, sample_rate=24_000))


class _FixedScriptPrimary:
    """A StepFun primary that must never be opened for an approved crisis line."""

    provider = "stepfun"
    model = "stepaudio-3-tts"
    sample_rate = 24_000

    def __init__(self) -> None:
        self.opened = 0

    def bind_fence(self, fence: GenerationFence) -> None:
        del fence

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> Any:
        del conn_options
        self.opened += 1
        raise AssertionError("StepFun must not synthesize a fixed safety script")

    async def aclose(self) -> None:
        return None


class _FixedScriptBackup:
    provider = "volcengine_doubao"
    current_model = "seed-tts-2.0"
    model = "seed-tts-2.0"
    current_voice = "zh_female_kailangjiejie_uranus_bigtts"

    def __init__(self) -> None:
        self.streams: list[_FixedScriptBackupStream] = []

    def bind_fence(self, fence: GenerationFence) -> None:
        del fence

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> _FixedScriptBackupStream:
        del conn_options
        stream = _FixedScriptBackupStream()
        self.streams.append(stream)
        return stream

    async def aclose(self) -> None:
        return None

    @property
    def received(self) -> list[str]:
        return [text for stream in self.streams for text in stream.texts]


def _failover_speech() -> tuple[StepFunPrimaryTTS, Any, Any]:
    primary = _FixedScriptPrimary()
    backup = _FixedScriptBackup()

    wrapper = StepFunPrimaryTTS(
        primary=primary,
        backup=backup,
        breaker=TTSFailoverBreaker(failures=3, open_s=30),
    )
    return wrapper, primary, backup


def _assert_spoken_only(received: list[str]) -> None:
    spoken = [phrase.spoken for phrase in CRISIS_SELF_SCRIPT.phrases]
    assert received
    assert all(text == SPOKEN_SCRIPT or text in spoken for text in received)
    assert all(not any(character.isdigit() for character in text) for text in received)
    assert any(character.isdigit() for character in CRISIS_SELF_SCRIPT.display)


@pytest.mark.asyncio
async def test_fixed_safety_script_bypasses_stepfun_and_speaks_on_doubao() -> None:
    wrapper, primary, backup = _failover_speech()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), wrapper)
    identity, fence = _generation("fixed-safety-direct-doubao")

    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    # StepFun's content moderation can reject the approved crisis copy before
    # any audio, so the unit starts on Doubao; the primary is only opened when
    # Doubao produces no audio.
    assert primary.opened == 0
    _assert_spoken_only(backup.received)
    assert backup.received == [SPOKEN_SCRIPT]
    assert any(chunk.pcm_s16le for chunk in chunks)
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display


class _QuotaError(ConnectionError):
    auth_failure = True


class _ScriptAttemptStream:
    def __init__(self, engine: _ScriptEngine) -> None:
        self._engine = engine
        self._yielded = False

    def push_text(self, text: str) -> None:
        self._engine.received.append(text)

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> _ScriptAttemptStream:
        return self

    async def __anext__(self) -> Any:
        engine = self._engine
        if engine.behavior == "timeout":
            await asyncio.Event().wait()
        if engine.behavior == "auth":
            raise _QuotaError("doubao account balance exhausted")
        if engine.behavior == "audio_then_error" and self._yielded:
            raise engine.error or ConnectionError("upstream dropped after audio")
        if engine.behavior != "audio_then_error" and engine.error is not None:
            raise engine.error
        if engine.behavior == "empty" or self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return SimpleNamespace(
            frame=SimpleNamespace(data=b"\x01\x00" * FRAME, sample_rate=24_000)
        )

    async def aclose(self) -> None:
        return None


class _ScriptEngine:
    """One approved-script engine: audio, a provider error, a hang or silence."""

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        behavior: str = "audio",
        error: BaseException | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.current_model = model
        self.current_voice = f"{provider}-voice"
        self.behavior = behavior
        self.error = error
        self.received: list[str] = []

    def bind_fence(self, _fence: GenerationFence) -> None:
        return None

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> _ScriptAttemptStream:
        del conn_options
        return _ScriptAttemptStream(self)

    async def aclose(self) -> None:
        return None


def _script_speech(
    *,
    primary: _ScriptEngine,
    backup: _ScriptEngine,
) -> StepFunPrimaryTTS:
    return StepFunPrimaryTTS(
        primary=primary,
        backup=backup,
        config=StepFunFailoverConfig(
            failures=3,
            open_s=30,
            fixed_safety_budget_s=2.0,
            fixed_safety_backup_first_audio_s=0.05,
            fixed_safety_primary_first_audio_s=0.05,
        ),
        breaker=TTSFailoverBreaker(failures=3, open_s=30),
    )


def _phrase_frame(engine_tag: bytes, phrase_index: int) -> bytes:
    """One 20 ms frame whose bytes identify the engine and approved phrase."""

    marker = engine_tag + bytes((phrase_index,))
    return (marker * (FRAME * 2 // len(marker) + 1))[: FRAME * 2]


def _whole_script_pcm(engine_tag: bytes) -> bytes:
    return b"".join(
        _phrase_frame(engine_tag, index)
        for index in range(len(CRISIS_SELF_SCRIPT.phrases))
    )


class _PhraseScriptStream:
    """Emit one tagged frame per approved phrase, then an optional drop."""

    def __init__(self, engine: _PhraseScriptEngine) -> None:
        self._engine = engine
        self._index = 0

    def push_text(self, text: str) -> None:
        self._engine.received.append(text)

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> _PhraseScriptStream:
        return self

    async def __anext__(self) -> Any:
        engine = self._engine
        if self._index >= engine.emit_frames:
            if engine.error is not None:
                raise engine.error
            raise StopAsyncIteration
        data = _phrase_frame(engine.tag, self._index)
        self._index += 1
        return SimpleNamespace(frame=SimpleNamespace(data=data, sample_rate=24_000))

    async def aclose(self) -> None:
        return None


class _PhraseScriptEngine:
    """An approved-script engine that can stop after any phrase."""

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        tag: bytes,
        emit_frames: int,
        error: BaseException | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.current_model = model
        self.current_voice = f"{provider}-voice"
        self.tag = tag
        self.emit_frames = emit_frames
        self.error = error
        self.received: list[str] = []

    def bind_fence(self, _fence: GenerationFence) -> None:
        return None

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> _PhraseScriptStream:
        del conn_options
        return _PhraseScriptStream(self)

    async def aclose(self) -> None:
        return None


def _fixed_safety_cache_key(
    adapter: ExistingVoiceProviderAdapter,
    spoken: str,
) -> tuple[str, str]:
    return adapter._fixed_safety_voice_key(), spoken


def _assert_whole_script_reached_the_device(
    chunks: list[Any],
    engine_tag: bytes,
) -> None:
    """The emitted frames cover every approved phrase and end with the last one."""

    phrases = CRISIS_SELF_SCRIPT.phrases
    pcm = b"".join(chunk.pcm_s16le for chunk in chunks)
    assert pcm == _whole_script_pcm(engine_tag)
    assert [chunk.source_start_sample for chunk in chunks] == [
        index * FRAME for index in range(len(phrases))
    ]
    text = "".join(chunk.text for chunk in chunks)
    assert text == CRISIS_SELF_SCRIPT.display
    offset = 0
    for phrase in phrases:
        assert text[offset : offset + len(phrase.display)] == phrase.display
        offset += len(phrase.display)
    assert offset == len(text)
    assert chunks[-1].text.endswith(phrases[-1].display)
    assert chunks[-1].final is True
    assert _spans(chunks[-1]) == [(CRISIS_SELF_SCRIPT.display, 0, len(phrases) * FRAME)]


@pytest.mark.asyncio
@pytest.mark.parametrize("backup_behavior", ["connection", "auth", "timeout", "empty"])
async def test_doubao_outage_falls_back_to_stepfun_for_the_same_crisis_script(
    backup_behavior: str,
) -> None:
    if backup_behavior == "connection":
        backup = _ScriptEngine(
            "volcengine_doubao",
            "seed-tts-2.0",
            behavior="error",
            error=ConnectionError("doubao connection closed"),
        )
    else:
        backup = _ScriptEngine("volcengine_doubao", "seed-tts-2.0", behavior=backup_behavior)
    primary = _ScriptEngine("stepfun", "stepaudio-3-tts")
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), _script_speech(primary=primary, backup=backup))
    identity, fence = _generation(f"fixed-safety-fallback-{backup_behavior}")

    started = time.monotonic()
    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]
    elapsed = time.monotonic() - started

    assert backup.received == [SPOKEN_SCRIPT]
    assert primary.received == [SPOKEN_SCRIPT]
    assert any(chunk.pcm_s16le for chunk in chunks)
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
    assert chunks[-1].final is True
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_doubao_and_stepfun_failure_replays_the_approved_cache(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = _ScriptEngine("stepfun", "stepaudio-3-tts")
    backup = _ScriptEngine("volcengine_doubao", "seed-tts-2.0")
    speech = _script_speech(primary=primary, backup=backup)
    warm = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-cache-fallback-warm")
    [warm_chunk] = [chunk async for chunk in warm.generate_reply(identity, CHILD_TEXT, fence)]
    assert warm_chunk.pcm_s16le
    backup.received.clear()

    backup.behavior = "error"
    backup.error = ConnectionError("doubao unavailable")
    primary.error = APIStatusError("content moderation rejected", status_code=451)

    cold = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-cache-fallback-cold")
    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        chunks = [chunk async for chunk in cold.generate_reply(identity, CHILD_TEXT, fence)]
    elapsed = time.monotonic() - started

    ready = [
        record.getMessage()
        for record in caplog.records
        if "fixed safety speech ready" in record.getMessage()
    ]
    assert len(ready) == 1 and "source=cache" in ready[0]
    assert backup.received == [SPOKEN_SCRIPT]
    assert primary.received == [SPOKEN_SCRIPT]
    assert any(chunk.pcm_s16le for chunk in chunks)
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_doubao_start_failure_speaks_the_whole_script_on_stepfun() -> None:
    phrases = CRISIS_SELF_SCRIPT.phrases
    backup = _PhraseScriptEngine(
        "volcengine_doubao",
        "seed-tts-2.0",
        tag=b"D",
        emit_frames=0,
        error=ConnectionError("doubao unavailable"),
    )
    primary = _PhraseScriptEngine(
        "stepfun",
        "stepaudio-3-tts",
        tag=b"S",
        emit_frames=len(phrases),
    )
    adapter = _adapter(
        ReplyLLM(CRISIS_SELF_SCRIPT),
        _script_speech(primary=primary, backup=backup),
    )
    identity, fence = _generation("fixed-safety-start-failure")

    started = time.monotonic()
    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]
    elapsed = time.monotonic() - started

    assert backup.received == [SPOKEN_SCRIPT]
    assert primary.received == [SPOKEN_SCRIPT]
    _assert_whole_script_reached_the_device(chunks, b"S")
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_doubao_mid_script_drop_speaks_the_whole_script_on_stepfun() -> None:
    phrases = CRISIS_SELF_SCRIPT.phrases
    backup = _PhraseScriptEngine(
        "volcengine_doubao",
        "seed-tts-2.0",
        tag=b"D",
        emit_frames=2,
        error=ConnectionError("doubao dropped mid-script"),
    )
    primary = _PhraseScriptEngine(
        "stepfun",
        "stepaudio-3-tts",
        tag=b"S",
        emit_frames=len(phrases),
    )
    speech = _script_speech(primary=primary, backup=backup)
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-mid-drop")

    started = time.monotonic()
    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]
    elapsed = time.monotonic() - started

    assert backup.received == [SPOKEN_SCRIPT]
    assert primary.received == [SPOKEN_SCRIPT]
    # The two frames Doubao produced are discarded before the device sees
    # anything; the emitted audio is one complete StepFun take.
    pcm = b"".join(chunk.pcm_s16le for chunk in chunks)
    assert _phrase_frame(b"D", 0) not in pcm
    assert _phrase_frame(b"D", 1) not in pcm
    _assert_whole_script_reached_the_device(chunks, b"S")
    # A partial Doubao take never joins the whole StepFun take in the cache.
    from services.agent.src.voice_core import provider_adapter

    assert provider_adapter._FIXED_SAFETY_PCM_CACHE[
        _fixed_safety_cache_key(adapter, SPOKEN_SCRIPT)
    ] == _whole_script_pcm(b"S")
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_doubao_and_stepfun_failure_replays_the_whole_cached_script(
    caplog: pytest.LogCaptureFixture,
) -> None:
    phrases = CRISIS_SELF_SCRIPT.phrases
    backup = _PhraseScriptEngine(
        "volcengine_doubao",
        "seed-tts-2.0",
        tag=b"D",
        emit_frames=len(phrases),
    )
    primary = _PhraseScriptEngine(
        "stepfun",
        "stepaudio-3-tts",
        tag=b"S",
        emit_frames=len(phrases),
    )
    speech = _script_speech(primary=primary, backup=backup)
    warm = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-whole-cache-warm")
    warm_chunks = [
        chunk async for chunk in warm.generate_reply(identity, CHILD_TEXT, fence)
    ]
    _assert_whole_script_reached_the_device(warm_chunks, b"D")

    from services.agent.src.voice_core import provider_adapter

    key = _fixed_safety_cache_key(warm, SPOKEN_SCRIPT)
    cached = provider_adapter._FIXED_SAFETY_PCM_CACHE[key]
    assert cached == _whole_script_pcm(b"D")
    backup.received.clear()
    primary.received.clear()

    backup.emit_frames = 0
    backup.error = ConnectionError("doubao unavailable")
    primary.emit_frames = 0
    primary.error = APIStatusError("content moderation rejected", status_code=451)

    cold = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-whole-cache-cold")
    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        chunks = [chunk async for chunk in cold.generate_reply(identity, CHILD_TEXT, fence)]
    elapsed = time.monotonic() - started

    ready = [
        record.getMessage()
        for record in caplog.records
        if "fixed safety speech ready" in record.getMessage()
    ]
    assert len(ready) == 1 and "source=cache" in ready[0]
    assert backup.received == [SPOKEN_SCRIPT]
    assert primary.received == [SPOKEN_SCRIPT]
    _assert_whole_script_reached_the_device(chunks, b"D")
    assert provider_adapter._FIXED_SAFETY_PCM_CACHE[key] == cached
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_two_engine_first_packet_timeouts_stay_inside_the_crisis_budget() -> None:
    primary = _ScriptEngine("stepfun", "stepaudio-3-tts", behavior="timeout")
    backup = _ScriptEngine("volcengine_doubao", "seed-tts-2.0", behavior="timeout")
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), _script_speech(primary=primary, backup=backup))
    identity, fence = _generation("fixed-safety-both-timeout")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="fixed safety script produced no audio"):
        [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]
    elapsed = time.monotonic() - started

    assert backup.received
    assert primary.received
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_doubao_and_stepfun_failure_without_cache_fails_loudly() -> None:
    primary = _ScriptEngine(
        "stepfun",
        "stepaudio-3-tts",
        behavior="error",
        error=APIStatusError("content moderation rejected", status_code=451),
    )
    backup = _ScriptEngine(
        "volcengine_doubao",
        "seed-tts-2.0",
        behavior="error",
        error=ConnectionError("doubao unavailable"),
    )
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), _script_speech(primary=primary, backup=backup))
    identity, fence = _generation("fixed-safety-cache-fallback-cold-failure")

    with pytest.raises(RuntimeError, match="fixed safety script produced no audio"):
        [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    # A cold cache retries the whole cascade once, then fails instead of
    # leaving the child in silence with a half-reported success.
    assert backup.received == [SPOKEN_SCRIPT, SPOKEN_SCRIPT]
    assert primary.received == [SPOKEN_SCRIPT, SPOKEN_SCRIPT]


class _FailureOnceSpeech:
    """The provider drops the connection on its first whole-script request."""

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.requests = 0

    def bind_fence(self, fence: GenerationFence) -> None:
        del fence

    def stream(self) -> _FailureOnceSpeech:
        self.requests += 1
        return self

    def push_text(self, text: str) -> None:
        self.pushed.append(text)

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            if self.requests == 1:
                raise ConnectionError("doubao stream dropped before any audio")
            yield SimpleNamespace(frame=SimpleNamespace(data=b"\x01\x00" * FRAME, sample_rate=24_000))

        return events()

    async def aclose(self) -> None:
        return None


class _UnavailableSpeech(_FailureOnceSpeech):
    """The provider never produces audio for the script."""

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            raise ConnectionError("doubao unavailable")
            yield  # pragma: no cover - unreachable, keeps this an async generator

        return events()


@pytest.fixture(autouse=True)
def clear_fixed_safety_cache() -> Any:
    from services.agent.src.voice_core import provider_adapter

    provider_adapter._FIXED_SAFETY_PCM_CACHE.clear()
    yield
    provider_adapter._FIXED_SAFETY_PCM_CACHE.clear()


@pytest.mark.asyncio
async def test_a_failed_synthesis_is_retried_before_the_crisis_line_is_given_up_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C1: ``failed to synthesize speech text_len=100`` must not cost the child the line."""

    speech = _FailureOnceSpeech()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), speech)
    identity, fence = _generation("fixed-safety-retry")

    with caplog.at_level(logging.INFO):
        chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    assert speech.pushed == [SPOKEN_SCRIPT, SPOKEN_SCRIPT]
    assert not adapter.is_fixed_safety_reply(fence)  # cleared once the reply ends
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
    assert chunks[-1].final
    ready = [r.getMessage() for r in caplog.records if "fixed safety speech ready" in r.getMessage()]
    assert len(ready) == 1 and "source=synthesis" in ready[0]
    assert any("fixed safety speech attempt failed" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert CHILD_TEXT not in record.getMessage()
        assert SPOKEN_SCRIPT not in record.getMessage()


@pytest.mark.asyncio
async def test_a_later_failure_replays_the_approved_script_from_cache(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider outage must not leave the child in silence after one good synthesis."""

    first = SubtitledSpeech()
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), first)
    identity, fence = _generation("fixed-safety-cache-warm")
    [warm] = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]
    assert warm.pcm_s16le

    second = _UnavailableSpeech()
    cold = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), second)
    identity, fence = _generation("fixed-safety-cache-cold")
    with caplog.at_level(logging.INFO):
        chunks = [chunk async for chunk in cold.generate_reply(identity, CHILD_TEXT, fence)]

    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
    ready = [r.getMessage() for r in caplog.records if "fixed safety speech ready" in r.getMessage()]
    assert len(ready) == 1 and "source=cache" in ready[0]
    assert any("replayed from cache" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_without_cache_a_failed_script_fails_loudly() -> None:
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), _UnavailableSpeech())
    identity, fence = _generation("fixed-safety-no-cache")

    with pytest.raises(RuntimeError, match="fixed safety script produced no audio"):
        [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]
