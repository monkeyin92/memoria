from __future__ import annotations

import asyncio
import logging
import re
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
from services.agent.src.providers.tts_failover import (
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

    assert speech.pushed == [phrase.spoken for phrase in CRISIS_SELF_SCRIPT.phrases]
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
    assert _spans(chunks[-1]) == [(CRISIS_SELF_SCRIPT.display, 0, FRAME * 4)]
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
    first_spoken = CRISIS_SELF_SCRIPT.phrases[0].spoken
    speech.cancel = lambda: adapter.cancel_generation(fence)
    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    spoken = "".join(speech.pushed)
    assert speech.pushed == [first_spoken]
    assert "好的" not in spoken
    assert "嗯，你说" not in spoken
    for phrase in CRISIS_SELF_SCRIPT.phrases[1:]:
        assert phrase.spoken not in spoken
        assert phrase.display not in "".join(chunk.assistant_text_delta for chunk in chunks)
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


class _FixedScriptPrimaryStream:
    def __init__(self, *, fail_after_first_chunk: bool) -> None:
        self.texts: list[str] = []
        self._fail_after_first_chunk = fail_after_first_chunk
        self._yielded = False

    def push_text(self, text: str) -> None:
        self.texts.append(text)

    def end_input(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def __aiter__(self) -> _FixedScriptPrimaryStream:
        return self

    async def __anext__(self) -> Any:
        if self._fail_after_first_chunk and not self._yielded:
            self._yielded = True
            return SimpleNamespace(frame=SimpleNamespace(data=b"\x01\x00" * FRAME, sample_rate=24_000))
        raise ConnectionError("stepfun primary unavailable")


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
    provider = "stepfun"
    model = "stepaudio-3-tts"
    sample_rate = 24_000

    def __init__(self, *, fail_after_first_chunk: bool) -> None:
        self.streams: list[_FixedScriptPrimaryStream] = []
        self._fail_after_first_chunk = fail_after_first_chunk

    def bind_fence(self, fence: GenerationFence) -> None:
        del fence

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> _FixedScriptPrimaryStream:
        del conn_options
        stream = _FixedScriptPrimaryStream(
            fail_after_first_chunk=self._fail_after_first_chunk
        )
        self.streams.append(stream)
        return stream

    async def aclose(self) -> None:
        return None

    @property
    def received(self) -> list[str]:
        return [text for stream in self.streams for text in stream.texts]


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


def _failover_speech(fail_after_first_chunk: bool) -> tuple[StepFunPrimaryTTS, Any, Any]:
    primary = _FixedScriptPrimary(fail_after_first_chunk=fail_after_first_chunk)
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
    assert all(text in spoken for text in received)
    assert all(not any(character.isdigit() for character in text) for text in received)
    assert any(character.isdigit() for character in CRISIS_SELF_SCRIPT.display)


@pytest.mark.asyncio
async def test_fixed_safety_script_reaches_both_engines_as_spoken_on_first_packet_switch() -> None:
    wrapper, primary, backup = _failover_speech(fail_after_first_chunk=False)
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), wrapper)
    identity, fence = _generation("fixed-safety-failover-first-packet")

    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    _assert_spoken_only(primary.received)
    _assert_spoken_only(backup.received)
    assert backup.received == [phrase.spoken for phrase in CRISIS_SELF_SCRIPT.phrases]
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display


@pytest.mark.asyncio
async def test_fixed_safety_script_reaches_both_engines_as_spoken_on_mid_sentence_switch() -> None:
    wrapper, primary, backup = _failover_speech(fail_after_first_chunk=True)
    adapter = _adapter(ReplyLLM(CRISIS_SELF_SCRIPT), wrapper)
    identity, fence = _generation("fixed-safety-failover-mid-sentence")

    chunks = [chunk async for chunk in adapter.generate_reply(identity, CHILD_TEXT, fence)]

    _assert_spoken_only(primary.received)
    _assert_spoken_only(backup.received)
    assert primary.received == [CRISIS_SELF_SCRIPT.phrases[0].spoken]
    assert backup.received == [phrase.spoken for phrase in CRISIS_SELF_SCRIPT.phrases[1:]]
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
