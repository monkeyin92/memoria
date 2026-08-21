from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import GLOBAL_METRICS, MetricsRegistry
from services.agent.src.orchestration.handlers import (
    LanguageModelRequest,
    SpeechSynthesisRequest,
)
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    FunASRServerEvent,
)
from services.agent.src.voice_core.asr_stream_supervisor import ASRStreamSupervisor
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.provider_adapter import (
    ExistingVoiceProviderAdapter,
    ExistingVoiceProviderConfig,
    build_production_provider_factory,
)
from services.agent.src.voice_core.speech_timeline import ASRResult


async def _collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in stream]


class FakeASR:
    def __init__(self) -> None:
        self.task_id = "task-1"
        self.task_epoch = 1
        self.task_sample_origin = 0
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.sent: list[tuple[bytes, int]] = []
        self.reconnect_calls = 0
        self.committed_samples: list[int] = []
        self.closed = False

    async def connect(self) -> None:
        return None

    async def reconnect_with_replay(self) -> None:
        self.reconnect_calls += 1
        self.task_epoch += 1
        self.task_id = f"task-{self.task_epoch}"
        self.task_sample_origin = 320

    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        if not self.sent:
            self.task_sample_origin = capture_start_sample
        self.sent.append((pcm, capture_start_sample))
        if len(self.sent) == 1:
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id="task-1",
                    sentence=FunASRSentence(
                        sentence_id=7,
                        text="你好",
                        begin_ms=0,
                        end_ms=20,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )

    def mark_committed_sample(self, sample: int) -> None:
        self.committed_samples.append(sample)

    async def aclose(self) -> None:
        self.closed = True


class FakeLLM:
    def __init__(self) -> None:
        self.delegations: list[tuple[str, GenerationFence]] = []
        self.output_intents: list[Any] = []

    async def start_delegation(self, text: str, fence: GenerationFence) -> None:
        self.delegations.append((text, fence))

    async def accept_output_intent(self, intent: Any) -> None:
        self.output_intents.append(intent)

    def stream(self, request: LanguageModelRequest) -> AsyncIterator[str]:
        _ = request

        async def tokens() -> AsyncIterator[str]:
            yield "你好。"

        return tokens()


class MultiPhraseLLM:
    def stream(self, request: LanguageModelRequest) -> AsyncIterator[str]:
        _ = request

        async def tokens() -> AsyncIterator[str]:
            yield "第一句。第二句。"

        return tokens()


class PausedMultiPhraseLLM:
    def __init__(self, release_second: asyncio.Event) -> None:
        self.release_second = release_second

    def stream(self, request: LanguageModelRequest) -> AsyncIterator[str]:
        _ = request

        async def tokens() -> AsyncIterator[str]:
            yield "第一句。"
            await self.release_second.wait()
            yield "第二句。"

        return tokens()


class FakeTTS:
    async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
        assert request.phrases
        return SimpleNamespace(pcm=b"\x00\x00" * 8)


@pytest.mark.asyncio
async def test_existing_provider_adapter_renders_tts_and_pcm_output_sources() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("direct-output-sources", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)

    tts_intent = media_pb2.OutputIntent(tts_source="提示一下")
    tts_chunks = await _collect(
        adapter.generate_output(
            identity,
            tts_intent,
            fence,
            work_id="tts-work",
            source_start_sample=7,
        )
    )
    assert len(tts_chunks) == 1
    assert tts_chunks[0].source_start_sample == 7
    assert tts_chunks[0].frame_samples == adapter.output_frame_samples
    assert tts_chunks[0].first is True
    assert tts_chunks[0].final is True
    assert tts_chunks[0].assistant_text_delta == "提示一下"

    pcm_intent = media_pb2.OutputIntent(pcm_s16le=b"\x01\x00\x02\x00")
    pcm_chunks = await _collect(
        adapter.generate_output(
            identity,
            pcm_intent,
            fence,
            work_id="pcm-work",
            source_start_sample=adapter.output_frame_samples + 7,
        )
    )
    assert len(pcm_chunks) == 1
    assert pcm_chunks[0].source_start_sample == adapter.output_frame_samples + 7
    assert pcm_chunks[0].frame_samples == adapter.output_frame_samples
    assert pcm_chunks[0].pcm_s16le[:4] == b"\x01\x00\x02\x00"
    assert pcm_chunks[0].first is True
    assert pcm_chunks[0].final is True


@pytest.mark.asyncio
async def test_provider_forwards_committed_turn_preparation_to_the_shared_agent() -> None:
    identity = SessionIdentity("prepared-provider")
    expected = GenerationFence(identity.session_id, 1, 1, 0)

    class PreparingLLM(FakeLLM):
        async def prepare_committed_turn(self, text: str) -> GenerationFence:
            assert text == "帮我制定计划"
            return expected

    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=FakeASR,  # type: ignore[arg-type]
        language_model=PreparingLLM(),
        speech_synthesis=FakeTTS(),
    )

    assert await adapter.prepare_committed_turn(identity, "帮我制定计划") == expected


class SizedTTS:
    def __init__(self, samples: int) -> None:
        self.samples = samples

    async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
        assert request.phrases
        return SimpleNamespace(pcm=b"\x01\x00" * self.samples)


class FakeStreamingSpeech:
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        release_after_first: asyncio.Event | None = None,
    ) -> None:
        self.chunks = chunks
        self.release_after_first = release_after_first
        self.bound_fences: list[GenerationFence] = []
        self.phrases: list[str] = []
        self.completed = False
        self.closed = False

    def bind_fence(self, fence: GenerationFence) -> None:
        self.bound_fences.append(fence)

    def stream(self) -> FakeStreamingSpeech:
        return self

    def push_text(self, text: str) -> None:
        self.phrases.append(text)

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            for index, chunk in enumerate(self.chunks):
                yield SimpleNamespace(frame=SimpleNamespace(data=chunk, sample_rate=24_000))
                if index == 0 and self.release_after_first is not None:
                    await self.release_after_first.wait()
            self.completed = True

        return events()

    async def aclose(self) -> None:
        self.closed = True


class TimedStreamingSpeech(FakeStreamingSpeech):
    def timed_transcript(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(text="你好。", start_time=0.0, end_time=0.02),)


class PrefixAndTailTimedStreamingSpeech(FakeStreamingSpeech):
    def timed_transcript(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(text="已播放。", start_time=0.0, end_time=0.02),
            SimpleNamespace(text="未播放。", start_time=0.02, end_time=0.04),
        )


class PendingTimedStreamingSpeech(FakeStreamingSpeech):
    def timed_transcript(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(text="你好", start_time=0.0, end_time=0.5),)

    def timed_transcript_alignment(self) -> str:
        return "pending"


class PhraseDrivenStreamingSpeech:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any | None] = asyncio.Queue()
        self.phrases: list[str] = []
        self.closed = False

    def push_text(self, text: str) -> None:
        self.phrases.append(text)
        samples = 960 if len(self.phrases) == 1 else 480
        self.queue.put_nowait(
            SimpleNamespace(frame=SimpleNamespace(data=b"\x01\x00" * samples, sample_rate=24_000))
        )

    def end_input(self) -> None:
        self.queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            while True:
                item = await self.queue.get()
                if item is None:
                    return
                yield item

        return events()

    async def aclose(self) -> None:
        self.closed = True

    def timed_transcript(self) -> tuple[SimpleNamespace, ...]:
        if len(self.phrases) != 2:
            return ()
        return (
            SimpleNamespace(text=self.phrases[0], start_time=0.0, end_time=0.04),
            SimpleNamespace(text=self.phrases[1], start_time=0.04, end_time=0.06),
        )


class PhraseDrivenTTS:
    def __init__(self) -> None:
        self.stream_calls = 0
        self.stream_instance = PhraseDrivenStreamingSpeech()

    def stream(self) -> PhraseDrivenStreamingSpeech:
        self.stream_calls += 1
        return self.stream_instance


class ReconnectingASR(FakeASR):
    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        self.sent.append((pcm, capture_start_sample))
        call = len(self.sent)
        if call == 2:
            self.task_id = "task-2"
            self.task_epoch = 2
            self.task_sample_origin = 320
        elif call == 3:
            self.task_id = "task-3"
            self.task_epoch = 3
            self.task_sample_origin = 0
        sentence = FunASRSentence(
            sentence_id=7,
            text="重连结果",
            begin_ms=0,
            end_ms=20,
            sentence_end=True,
            heartbeat=False,
            words=(),
        )
        await self.events.put(
            FunASRServerEvent(
                event="result-generated",
                task_id=self.task_id,
                sentence=sentence,
            )
        )
        if call == 1:
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id=self.task_id,
                    sentence=sentence,
                )
            )


class ExpandingReplayASR(FakeASR):
    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        self.sent.append((pcm, capture_start_sample))
        call = len(self.sent)
        if call == 2:
            self.task_id = "expanded-task"
            self.task_epoch = 2
            self.task_sample_origin = 0
        await self.events.put(
            FunASRServerEvent(
                event="result-generated",
                task_id=self.task_id,
                sentence=FunASRSentence(
                    sentence_id=1,
                    text="你好" if call == 1 else "你好世界",
                    begin_ms=0,
                    end_ms=20 if call == 1 else 40,
                    sentence_end=True,
                    heartbeat=False,
                    words=(),
                ),
            )
        )


class CorrectingExpandedASR(FakeASR):
    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        self.sent.append((pcm, capture_start_sample))
        call = len(self.sent)
        if call >= 2:
            self.task_id = "expanded-task"
            self.task_epoch = 2
            self.task_sample_origin = 0
        text, end_ms = (("你好", 20), ("你好世界", 40), ("你好世间", 40))[call - 1]
        await self.events.put(
            FunASRServerEvent(
                event="result-generated",
                task_id=self.task_id,
                sentence=FunASRSentence(
                    sentence_id=1,
                    text=text,
                    begin_ms=0,
                    end_ms=end_ms,
                    sentence_end=True,
                    heartbeat=False,
                    words=(),
                ),
            )
        )


class LongRunningASR(FakeASR):
    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        self.sent.append((pcm, capture_start_sample))
        call = len(self.sent)
        self.task_id = f"long-task-{call}"
        self.task_epoch = call
        self.task_sample_origin = (call - 1) * 320
        await self.events.put(
            FunASRServerEvent(
                event="result-generated",
                task_id=self.task_id,
                sentence=FunASRSentence(
                    sentence_id=1,
                    text=f"结果{call}",
                    begin_ms=0,
                    end_ms=20,
                    sentence_end=True,
                    heartbeat=False,
                    words=(),
                ),
            )
        )


class CorrectingASR(FakeASR):
    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        self.sent.append((pcm, capture_start_sample))
        for text in ("你好", "你好呀"):
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id=self.task_id,
                    sentence=FunASRSentence(
                        sentence_id=7,
                        text=text,
                        begin_ms=0,
                        end_ms=20,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )


class FakeProductionLLMStream:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> AsyncIterator[Any]:
        async def chunks() -> AsyncIterator[Any]:
            yield SimpleNamespace(delta=SimpleNamespace(content="生产回复。"))

        return chunks()

    async def aclose(self) -> None:
        self.closed = True


class FakeProductionLLM:
    init_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).init_kwargs = kwargs

    def chat(self, **kwargs: Any) -> FakeProductionLLMStream:
        assert kwargs["chat_ctx"].items
        return FakeProductionLLMStream()


class FakeProductionTTS:
    instances: list[FakeProductionTTS] = []

    def __init__(self, config: Any, *, metrics: Any = None) -> None:
        self.config = config
        self.metrics = metrics
        self.fence: GenerationFence | None = None
        self.closed = False
        type(self).instances.append(self)

    def bind_fence(self, fence: GenerationFence) -> None:
        self.fence = fence

    def stream(self) -> FakeStreamingSpeech:
        return FakeStreamingSpeech((b"\x01\x00" * 8,))

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_existing_provider_adapter_maps_asr_and_streams_existing_handlers() -> None:
    asr = FakeASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("adapter-session", stream_epoch=1)
    results = await adapter.ingest_audio(
        identity,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=320,
            payload=b"\x00\x00" * 320,
        ),
    )
    assert len(results) == 1
    assert results[0].text == "你好"
    assert results[0].capture_end_sample == 320
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    chunks = [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]
    assert len(chunks) == 1
    assert chunks[0].text == "你好。"
    assert chunks[0].source_start_sample == 0
    assert chunks[0].frame_samples == 480
    assert chunks[0].first is True
    assert chunks[0].final is True
    assert chunks[0].text_audio_start_sample == 0
    assert chunks[0].text_audio_end_sample == 480
    assert chunks[0].assistant_text_delta == "你好。"
    await adapter.close(identity)
    assert asr.closed


@pytest.mark.asyncio
async def test_existing_provider_adapter_rebuilds_funasr_for_new_stream_epoch() -> None:
    sessions: list[FakeASR] = []

    def build_asr() -> FakeASR:
        session = FakeASR()
        sessions.append(session)
        return session

    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, build_asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    first_identity = SessionIdentity("adapter-stream-reset", stream_epoch=1)
    first = await adapter.ingest_audio(
        first_identity,
        AudioFrame(first_identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    first_task_epoch = first[0].task_epoch

    second_identity = SessionIdentity(first_identity.session_id, stream_epoch=2)
    await adapter.reset_for_stream_epoch(second_identity)
    await sessions[0].events.put(
        FunASRServerEvent(
            event="result-generated",
            task_id=sessions[0].task_id,
            sentence=FunASRSentence(
                sentence_id=99,
                text="迟到旧结果",
                begin_ms=0,
                end_ms=20,
                sentence_end=True,
                heartbeat=False,
                words=(),
            ),
        )
    )
    second = await adapter.ingest_audio(
        second_identity,
        AudioFrame(second_identity, 0, 0, 320, b"\x01\x00" * 320),
    )

    assert len(sessions) == 2
    assert sessions[0].closed is True
    assert sessions[0].reconnect_calls == 0
    assert sessions[1].sent == [(b"\x01\x00" * 320, 0)]
    assert [(result.text, result.stream_epoch) for result in second] == [("你好", 2)]
    assert second[0].task_epoch > first_task_epoch
    assert sessions[0].events.qsize() == 1


@pytest.mark.asyncio
async def test_existing_provider_adapter_aligns_replacement_task_to_external_floor() -> None:
    sessions: list[FakeASR] = []

    def build_asr() -> FakeASR:
        session = FakeASR()
        sessions.append(session)
        return session

    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, build_asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    first_identity = SessionIdentity("adapter-task-floor", stream_epoch=1)
    first = await adapter.ingest_audio(
        first_identity,
        AudioFrame(first_identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    await adapter.reset_for_stream_epoch(
        SessionIdentity(first_identity.session_id, stream_epoch=2),
    )

    adapter.set_asr_task_epoch_floor(8)
    second_identity = SessionIdentity(first_identity.session_id, stream_epoch=2)
    second = await adapter.ingest_audio(
        second_identity,
        AudioFrame(second_identity, 0, 0, 320, b"\x01\x00" * 320),
    )

    assert first[0].task_epoch == 1
    assert second[0].task_epoch == 9
    assert adapter.current_asr_task_epoch == 9


@pytest.mark.asyncio
async def test_existing_provider_adapter_recovers_failed_session_at_absolute_sample() -> None:
    sessions: list[FakeASR] = []

    def build_asr() -> FakeASR:
        session = FakeASR()
        sessions.append(session)
        return session

    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, build_asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("adapter-provider-recovery", stream_epoch=1)
    first = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )

    await adapter.recover_after_failure(identity)
    await adapter.reset_after_discontinuity(identity, capture_start_sample=320)
    second = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 1, 320, 320, b"\x01\x00" * 320),
    )

    assert len(sessions) == 2
    assert sessions[0].closed is True
    assert sessions[1].reconnect_calls == 0
    assert sessions[1].committed_samples == [320]
    assert (second[0].capture_start_sample, second[0].capture_end_sample) == (320, 640)
    assert second[0].task_epoch > first[0].task_epoch


@pytest.mark.asyncio
async def test_existing_provider_adapter_rotates_funasr_at_vad_boundary() -> None:
    class SegmentASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.rotation_calls = 0

        async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
            self.sent.append((pcm, capture_start_sample))

        async def rotate_task(self, *, require_consumed: bool = True) -> None:
            self.rotation_calls += 1
            assert require_consumed is False
            previous_task_id = self.task_id
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id=previous_task_id,
                    sentence=FunASRSentence(
                        sentence_id=9,
                        text="第二问",
                        begin_ms=0,
                        end_ms=20,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )
            await self.events.put(
                FunASRServerEvent(event="task-finished", task_id=previous_task_id)
            )
            self.task_epoch += 1
            self.task_id = f"task-{self.task_epoch}"
            self.task_sample_origin = 320

    asr = SegmentASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("adapter-vad-boundary", stream_epoch=1)
    assert not await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )

    results = await adapter.finalize_speech_segment(identity)

    assert [(result.text, result.task_epoch) for result in results] == [("第二问", 1)]
    assert adapter.current_asr_task_epoch == 2
    assert asr.task_id == "task-2"
    assert await adapter.finalize_speech_segment(identity) == ()
    assert asr.rotation_calls == 1


@pytest.mark.asyncio
async def test_existing_provider_adapter_keeps_late_final_on_old_lazy_task_origin() -> None:
    class LazyRotationASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.rotation_pending = False

        async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
            self.sent.append((pcm, capture_start_sample))
            if self.rotation_pending:
                self.rotation_pending = False
                self.task_id = "task-2"
                self.task_epoch = 2
                self.task_sample_origin = capture_start_sample
                # This final was emitted by task-1 but arrived after the lazy
                # handoff. The adapter must retain task-1's origin (0).
                await self.events.put(
                    FunASRServerEvent(
                        event="result-generated",
                        task_id="task-1",
                        sentence=FunASRSentence(
                            sentence_id=11,
                            text="迟到旧任务",
                            begin_ms=0,
                            end_ms=20,
                            sentence_end=True,
                            heartbeat=False,
                            words=(),
                        ),
                    )
                )

        async def rotate_task(self, *, require_consumed: bool = True) -> None:
            assert require_consumed is False
            previous_task_id = self.task_id
            self.rotation_pending = True
            await self.events.put(
                FunASRServerEvent(event="task-finished", task_id=previous_task_id)
            )

    asr = LazyRotationASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("adapter-lazy-late-final", stream_epoch=1)

    await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    assert await adapter.finalize_speech_segment(identity) == ()
    assert adapter.asr_rotation_pending is True

    results = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 1, 320, 320, b"\x01\x00" * 320),
    )

    assert [(item.text, item.task_epoch, item.capture_start_sample, item.capture_end_sample)
            for item in results] == [("迟到旧任务", 1, 0, 320)]


@pytest.mark.asyncio
async def test_existing_provider_adapter_waits_for_final_after_task_finished() -> None:
    class InvertedTailASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.config = SimpleNamespace(post_finish_tail_grace_s=0.1)

        async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
            self.sent.append((pcm, capture_start_sample))

        async def rotate_task(self, *, require_consumed: bool = True) -> None:
            assert require_consumed is False
            await self.events.put(
                FunASRServerEvent(event="task-finished", task_id=self.task_id)
            )

            async def emit_late_final() -> None:
                await asyncio.sleep(0.01)
                await self.events.put(
                    FunASRServerEvent(
                        event="result-generated",
                        task_id=self.task_id,
                        sentence=FunASRSentence(
                            sentence_id=12,
                            text="边界后尾包",
                            begin_ms=0,
                            end_ms=20,
                            sentence_end=True,
                            heartbeat=False,
                            words=(),
                        ),
                    )
                )

            asyncio.create_task(emit_late_final())

    asr = InvertedTailASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("adapter-inverted-tail", stream_epoch=1)
    await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )

    results = await adapter.finalize_speech_segment(identity)

    assert [(item.text, item.task_epoch) for item in results] == [("边界后尾包", 1)]


@pytest.mark.asyncio
async def test_existing_provider_adapter_records_partial_sample_age() -> None:
    class PartialASR(FakeASR):
        last_sent_sample = 640

        async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
            self.sent.append((pcm, capture_start_sample))
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id=self.task_id,
                    sentence=FunASRSentence(
                        sentence_id=7,
                        text="你",
                        begin_ms=0,
                        end_ms=20,
                        sentence_end=False,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )

    metrics = MetricsRegistry()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, PartialASR),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
        metrics=metrics,
    )
    identity = SessionIdentity("partial-age", stream_epoch=1)

    await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 640, b"\x00\x00" * 640),
    )

    assert metrics.get("asr_partial_age_ms") == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_existing_provider_adapter_records_tts_frame_queue_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src.voice_core import provider_adapter as adapter_module

    ticks = iter((20.0, 20.015))
    monkeypatch.setattr(adapter_module, "monotonic", lambda: next(ticks))
    metrics = MetricsRegistry()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, FakeASR),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(8)),
        metrics=metrics,
    )
    identity = SessionIdentity("tts-frame-age", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)

    assert [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]

    assert metrics.get("tts_frame_age_ms") == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_existing_provider_adapter_splits_pcm_and_deduplicates_generation() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(481)),
    )
    identity = SessionIdentity("framed-adapter", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)

    chunks = [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]
    assert [chunk.source_start_sample for chunk in chunks] == [0, 480]
    assert [chunk.frame_samples for chunk in chunks] == [480, 480]
    assert chunks[0].first is True
    assert chunks[0].final is False
    assert chunks[0].text == ""
    assert chunks[0].assistant_text_delta == "你好。"
    assert chunks[1].text == "你好。"
    assert chunks[1].assistant_text_delta == ""
    assert chunks[1].text_audio_start_sample == 0
    assert chunks[1].text_audio_end_sample == 960
    assert chunks[1].final is True

    # Replaying the same authoritative fence must not duplicate sequence or
    # sample ranges.  A new fence gets a fresh local sample clock.
    assert [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)] == []
    next_fence = GenerationFence(identity.session_id, 2, 2, 0)
    next_chunks = [chunk async for chunk in adapter.generate_reply(identity, "hi", next_fence)]
    assert next_chunks[0].source_start_sample == 0


@pytest.mark.asyncio
async def test_existing_provider_adapter_cancellation_stops_pending_frames() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(1_000)),
    )
    identity = SessionIdentity("cancel-adapter", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)
    first = await anext(stream)
    assert first.first is True
    assert adapter.cancel_generation(fence)
    assert [chunk async for chunk in stream] == []

    pre_start_fence = GenerationFence(identity.session_id, 2, 2, 0)
    pre_start_stream = adapter.generate_reply(identity, "hi", pre_start_fence)
    assert adapter.cancel_generation(pre_start_fence)
    assert [chunk async for chunk in pre_start_stream] == []


@pytest.mark.asyncio
async def test_existing_provider_adapter_maps_every_phrase_to_its_audio_span() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, MultiPhraseLLM()),
        speech_synthesis=cast(Any, SizedTTS(8)),
    )
    identity = SessionIdentity("phrase-map", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)

    chunks = [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]

    assert [chunk.text for chunk in chunks] == ["第一句。", "第二句。"]
    assert [chunk.assistant_text_delta for chunk in chunks] == ["第一句。", "第二句。"]
    assert [(chunk.text_audio_start_sample, chunk.text_audio_end_sample) for chunk in chunks] == [
        (0, 480),
        (480, 960),
    ]
    assert [chunk.first for chunk in chunks] == [True, False]
    assert [chunk.final for chunk in chunks] == [False, True]


@pytest.mark.asyncio
async def test_existing_provider_adapter_frames_real_provider_stream_before_completion() -> None:
    release = asyncio.Event()
    speech = FakeStreamingSpeech(
        (
            b"\x01\x00" * 960,
            b"\x02\x00" * 241,
        ),
        release_after_first=release,
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("provider-stream", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    first = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert first.frame_samples == 480
    assert first.first is True
    assert first.assistant_text_delta == "你好。"
    assert not speech.completed
    release.set()
    chunks = [first, *[chunk async for chunk in stream]]
    assert [chunk.frame_samples for chunk in chunks] == [480, 480, 480]
    assert chunks[-1].text == ""
    assert chunks[-1].assistant_text_delta == ""
    assert chunks[-1].text_spans == ()
    assert chunks[-1].final is True
    assert speech.phrases == ["你好。"]
    assert speech.bound_fences == [fence]
    assert speech.closed


@pytest.mark.asyncio
async def test_existing_provider_adapter_exposes_safe_timed_prefix_during_interrupt() -> None:
    speech = TimedStreamingSpeech(
        (b"\x01\x00" * 960,),
        release_after_first=asyncio.Event(),
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("interrupt-provider-stream", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    assert (await anext(stream)).first
    spans = await adapter.interrupted_timed_text_spans(fence)

    assert [(span.text, span.audio_start_sample, span.audio_end_sample) for span in spans] == [
        ("你好。", 0, 480)
    ]
    assert adapter.cancel_generation(fence)
    await stream.aclose()


@pytest.mark.asyncio
async def test_existing_provider_adapter_cuts_unplayed_timed_text_on_interrupt() -> None:
    speech = PrefixAndTailTimedStreamingSpeech(
        # Two output frames: the first frame has been yielded, while the
        # second remains buffered behind the provider's release gate.
        (b"\x01\x00" * 960,),
        release_after_first=asyncio.Event(),
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("interrupt-prefix-cutoff", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    assert (await anext(stream)).first
    spans = await adapter.interrupted_timed_text_spans(fence)

    assert [(span.text, span.audio_start_sample, span.audio_end_sample) for span in spans] == [
        ("已播放。", 0, 480)
    ]
    assert adapter.cancel_generation(fence)
    await stream.aclose()


@pytest.mark.asyncio
async def test_existing_provider_adapter_keeps_pending_subtitle_prefix_when_pcm_leads() -> None:
    speech = PendingTimedStreamingSpeech(
        (b"\x01\x00" * 96_000,),
        release_after_first=asyncio.Event(),
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("pending-subtitle-interrupt", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    assert (await anext(stream)).first
    # Drain all 100 frames of the first chunk so the PCM watermark reaches 2s
    # while the provider stream is still blocked mid-generation and the live
    # subtitle snapshot only covers the first 0.5s. The already covered prefix
    # must survive even though the subtitle tail has not arrived yet.
    for _ in range(99):
        await anext(stream)
    spans = await adapter.interrupted_timed_text_spans(fence)

    assert [(span.text, span.audio_start_sample, span.audio_end_sample) for span in spans] == [
        ("你好", 0, 12_000)
    ]
    assert adapter.cancel_generation(fence)
    await stream.aclose()


@pytest.mark.asyncio
async def test_existing_provider_adapter_rejects_unverified_subtitle_gap_on_interrupt() -> None:
    # No alignment status is reported (``None``), so the transcript claims to
    # be final without proof. A 1.5s trailing gap against the generated PCM
    # must fail closed instead of fabricating a heard prefix.
    speech = TimedStreamingSpeech(
        (b"\x01\x00" * 96_000,),
        release_after_first=asyncio.Event(),
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("unverified-gap-interrupt", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    assert (await anext(stream)).first
    for _ in range(99):
        await anext(stream)
    spans = await adapter.interrupted_timed_text_spans(fence)

    assert spans == ()
    assert adapter.cancel_generation(fence)
    await stream.aclose()


@pytest.mark.asyncio
async def test_incremental_tts_reuses_one_stream_and_starts_before_second_phrase() -> None:
    release_second = asyncio.Event()
    speech = PhraseDrivenTTS()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, PausedMultiPhraseLLM(release_second)),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("incremental-tts", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    first = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert first.assistant_text_delta == "第一句。"
    assert speech.stream_calls == 1
    assert speech.stream_instance.phrases == ["第一句。"]
    release_second.set()
    chunks = [first, *[chunk async for chunk in stream]]
    assert speech.stream_calls == 1
    assert speech.stream_instance.phrases == ["第一句。", "第二句。"]
    # The TTS provider, rather than LLM enqueue timing, supplies the exact
    # sample ranges. The spans arrive after its final subtitle alignment.
    assert chunks[-1].text == ""
    assert [
        (span.text, span.audio_start_sample, span.audio_end_sample)
        for span in chunks[-1].text_spans
    ] == [
        ("第一句。", 0, 960),
        ("第二句。", 960, 1440),
    ]
    assert chunks[-1].final is True
    assert speech.stream_instance.closed


@pytest.mark.asyncio
async def test_existing_provider_adapter_cancellation_closes_waiting_provider_stream() -> None:
    speech = FakeStreamingSpeech(
        (b"\x01\x00" * 960,),
        release_after_first=asyncio.Event(),
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, speech),
    )
    identity = SessionIdentity("cancel-provider-stream", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)

    assert (await anext(stream)).first
    assert adapter.cancel_generation(fence)
    assert await asyncio.wait_for(_collect(stream), timeout=0.5) == []
    assert speech.closed


@pytest.mark.asyncio
async def test_existing_provider_adapter_deduplicates_asr_by_task_and_audio_range() -> None:
    asr = ReconnectingASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("asr-reconnect", stream_epoch=1)

    first = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    reconnected = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 1, 320, 320, b"\x00\x00" * 320),
    )
    replayed = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 2, 640, 320, b"\x00\x00" * 320),
    )

    # The adapter is a pure provider mapping; ASRStreamSupervisor is the
    # single interval/revision/replay authority.
    supervisor = ASRStreamSupervisor()
    first = [
        result
        for result in first
        if supervisor.accept_result(result, session_id=identity.session_id)
    ]
    reconnected = [
        result
        for result in reconnected
        if supervisor.accept_result(result, session_id=identity.session_id)
    ]
    replayed = [
        result
        for result in replayed
        if supervisor.accept_result(result, session_id=identity.session_id)
    ]
    assert [
        (result.task_epoch, result.capture_start_sample, result.capture_end_sample)
        for result in first
    ] == [(1, 0, 320)]
    assert [
        (result.task_epoch, result.capture_start_sample, result.capture_end_sample)
        for result in reconnected
    ] == [(2, 320, 640)]
    assert replayed == []


@pytest.mark.asyncio
async def test_existing_provider_adapter_replaces_expanding_replay_as_full_range() -> None:
    asr = ExpandingReplayASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("asr-expanding-replay", stream_epoch=1)

    first = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    extension = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 1, 320, 320, b"\x00\x00" * 320),
    )

    assert [(item.capture_start_sample, item.capture_end_sample, item.text) for item in first] == [
        (0, 320, "你好")
    ]
    assert [
        (item.capture_start_sample, item.capture_end_sample, item.text) for item in extension
    ] == [(0, 640, "你好世界")]
    assert extension[0].segment_id == first[0].segment_id
    supervisor = ASRStreamSupervisor()
    assert supervisor.accept_result(first[0], session_id=identity.session_id)
    assert supervisor.accept_result(extension[0], session_id=identity.session_id)
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=640,
        )
        == "你好世界"
    )


@pytest.mark.asyncio
async def test_existing_provider_adapter_revises_expanded_result_text() -> None:
    asr = CorrectingExpandedASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("asr-expanded-correction", stream_epoch=1)

    first = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    extension = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 1, 320, 320, b"\x00\x00" * 320),
    )
    correction = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 2, 640, 320, b"\x00\x00" * 320),
    )

    assert [
        (item.capture_start_sample, item.capture_end_sample, item.text) for item in correction
    ] == [(0, 640, "你好世间")]
    supervisor = ASRStreamSupervisor()
    assert supervisor.accept_result(first[0], session_id=identity.session_id)
    assert supervisor.accept_result(extension[0], session_id=identity.session_id)
    assert supervisor.accept_result(correction[0], session_id=identity.session_id)
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=640,
        )
        == "你好世间"
    )


@pytest.mark.asyncio
async def test_existing_provider_adapter_accepts_same_interval_correction() -> None:
    asr = CorrectingASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("asr-correction", stream_epoch=1)

    ingested = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )

    assert [
        (item.revision, item.capture_start_sample, item.capture_end_sample, item.text)
        for item in ingested
    ] == [(1, 0, 320, "你好"), (2, 0, 320, "你好呀")]


@pytest.mark.asyncio
async def test_existing_provider_adapter_keeps_revision_monotonic_when_begin_moves() -> None:
    class BeginCorrectingASR(FakeASR):
        async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
            self.sent.append((pcm, capture_start_sample))
            begin_ms = 10 if len(self.sent) == 1 else 0
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id=self.task_id,
                    sentence=FunASRSentence(
                        sentence_id=7,
                        text="你好",
                        begin_ms=begin_ms,
                        end_ms=20,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )

    asr = BeginCorrectingASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("asr-begin-correction", stream_epoch=1)
    first = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )
    second = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 1, 320, 320, b"\x00\x00" * 320),
    )

    assert first[0].revision == 1
    assert second[0].revision == 2
    assert first[0].capture_start_sample != second[0].capture_start_sample


@pytest.mark.asyncio
async def test_existing_provider_adapter_drops_asr_without_task_identity() -> None:
    asr = FakeASR()
    await asr.events.put(
        FunASRServerEvent(
            event="result-generated",
            task_id="",
            sentence=FunASRSentence(
                sentence_id=99,
                text="无法归属",
                begin_ms=0,
                end_ms=20,
                sentence_end=True,
                heartbeat=False,
                words=(),
            ),
        )
    )
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("missing-asr-task", stream_epoch=1)

    results = await adapter.ingest_audio(
        identity,
        AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
    )

    assert [result.text for result in results] == ["你好"]


@pytest.mark.asyncio
async def test_existing_provider_adapter_bounds_asr_metadata_during_long_reconnects() -> None:
    asr = LongRunningASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
        config=ExistingVoiceProviderConfig(
            max_asr_task_history=3,
            max_asr_result_history=4,
        ),
    )
    identity = SessionIdentity("long-asr", stream_epoch=1)

    for sequence in range(12):
        results = await adapter.ingest_audio(
            identity,
            AudioFrame(
                identity,
                sequence,
                sequence * 320,
                320,
                b"\x00\x00" * 320,
            ),
        )
        assert len(results) == 1

    assert len(adapter._asr_task_contexts) <= 3
    assert len(adapter._sentence_revisions) <= 4


@pytest.mark.asyncio
async def test_existing_provider_adapter_higher_revision_revises_output_prefix() -> None:
    class PrefixRevisingASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.sequence = (
                ("你好", 20),
                ("你好世界", 40),
                ("你号世界", 40),
            )

        async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
            self.sent.append((pcm, capture_start_sample))
            call = len(self.sent)
            if call > len(self.sequence):
                return
            text, end_ms = self.sequence[call - 1]
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id=self.task_id,
                    sentence=FunASRSentence(
                        sentence_id=1,
                        text=text,
                        begin_ms=0,
                        end_ms=end_ms,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )

    asr = PrefixRevisingASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("asr-prefix-revision", stream_epoch=1)
    supervisor = ASRStreamSupervisor()

    # 你好 -> 你好世界 -> 你号世界: the highest revision must become the
    # authoritative full-sentence result even though it rewrites an already
    # published prefix.
    emitted: list[ASRResult] = []
    for call in range(1, 4):
        results = await adapter.ingest_audio(
            identity,
            AudioFrame(identity, call, (call - 1) * 320, 320, b"\x00\x00" * 320),
        )
        accepted = [
            result
            for result in results
            if supervisor.accept_result(result, session_id=identity.session_id)
        ]
        assert len(accepted) == 1
        emitted.extend(accepted)

    assert [
        (item.revision, item.capture_start_sample, item.capture_end_sample, item.text)
        for item in emitted
    ] == [
        (1, 0, 320, "你好"),
        (2, 0, 640, "你好世界"),
        (3, 0, 640, "你号世界"),
    ]
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=640,
        )
        == "你号世界"
    )


@pytest.mark.asyncio
async def test_existing_provider_adapter_evicts_terminal_generations_without_replay() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(8)),
        config=ExistingVoiceProviderConfig(max_generation_history=2),
    )
    identity = SessionIdentity("bounded-generations", stream_epoch=1)
    fences = [
        GenerationFence(identity.session_id, generation, generation, 0)
        for generation in range(1, 5)
    ]

    for fence in fences:
        assert [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]

    assert len(adapter._generation_started) <= 2
    assert len(adapter._generation_cancel_events) == 0
    assert [chunk async for chunk in adapter.generate_reply(identity, "hi", fences[0])] == []
    assert not adapter.cancel_generation(fences[0])

    cancelled = [
        GenerationFence(identity.session_id, generation, generation, 0)
        for generation in range(5, 9)
    ]
    for fence in cancelled:
        assert adapter.cancel_generation(fence)
    assert len(adapter._cancelled_generations) <= 2
    assert [chunk async for chunk in adapter.generate_reply(identity, "hi", cancelled[0])] == []


@pytest.mark.asyncio
async def test_generation_eviction_floor_is_epoch_scoped() -> None:
    """P0-3: same turn/generation/tool numbers under a new subject (higher
    session_epoch) are never misjudged as already evicted by the old floor."""

    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(8)),
        config=ExistingVoiceProviderConfig(max_generation_history=2),
    )
    identity = SessionIdentity("epoch-scoped-evictions", stream_epoch=1)

    def fence(epoch: int, generation: int) -> GenerationFence:
        return GenerationFence(
            identity.session_id,
            turn_id=generation,
            generation_id=generation,
            tool_epoch=0,
            session_epoch=epoch,
        )

    # Old subject (epoch 1) fills the history and advances the eviction floor.
    for generation in range(1, 5):
        assert [
            chunk async for chunk in adapter.generate_reply(identity, "hi", fence(1, generation))
        ]
    assert len(adapter._generation_started) <= 2
    assert adapter._generation_eviction_floor is not None
    # New subject reuses the same turn/generation numbers at epoch 2: it must
    # be accepted, not rejected by the epoch-1 eviction floor.
    for generation in range(1, 3):
        assert [
            chunk async for chunk in adapter.generate_reply(identity, "hi", fence(2, generation))
        ]
    assert len(adapter._generation_started) <= 2


@pytest.mark.asyncio
async def test_production_provider_factory_owns_session_tts_and_uses_injected_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src.providers import doubao_tts, funasr_stt

    asr_config = funasr_stt.FunASRConfig(api_key="asr-key", ws_url="ws://asr")
    tts_config = doubao_tts.DoubaoTTSConfig(
        ws_url="ws://tts",
        api_key="tts-key",
        speaker="speaker",
    )
    monkeypatch.setattr(
        funasr_stt.FunASRConfig,
        "from_env",
        classmethod(lambda cls: asr_config),
    )
    monkeypatch.setattr(
        doubao_tts.DoubaoTTSConfig,
        "from_env",
        classmethod(lambda cls: tts_config),
    )
    monkeypatch.setattr(doubao_tts, "DoubaoTTS", FakeProductionTTS)
    monkeypatch.setenv("MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY", "fake.module:build")
    monkeypatch.setattr(
        "services.agent.src.voice_core.provider_adapter.importlib.import_module",
        lambda _name: SimpleNamespace(build=lambda _settings: lambda _identity: FakeLLM()),
    )
    FakeProductionTTS.instances.clear()
    settings = SimpleNamespace(
        environment="development",
        llm_api_key="llm-key",
        llm_base_url="https://llm.example/v1",
        llm_fast_model="fast-model",
        llm_provider="bailian_deepseek",
    )

    factory = build_production_provider_factory(cast(Any, settings))
    first_identity = SessionIdentity("production-1", stream_epoch=1)
    second_identity = SessionIdentity("production-2", stream_epoch=1)
    first = factory(first_identity)
    second = factory(second_identity)

    assert first.speech_synthesis is not second.speech_synthesis
    assert first.asr_session_factory().metrics is GLOBAL_METRICS
    assert cast(FakeProductionTTS, first.speech_synthesis).metrics is GLOBAL_METRICS
    fence = GenerationFence(first_identity.session_id, 1, 1, 0)
    await first.start_delegation("你好", fence)
    assert cast(FakeLLM, first.language_model).delegations == [("你好", fence)]
    chunks = [chunk async for chunk in first.generate_reply(first_identity, "你好", fence)]
    assert [chunk.text for chunk in chunks] == [""]
    assert [chunk.assistant_text_delta for chunk in chunks] == ["你好。"]
    assert cast(FakeProductionTTS, first.speech_synthesis).fence == fence
    await first.close(first_identity)
    assert cast(FakeProductionTTS, first.speech_synthesis).closed

    class MissingOutputIntentAcceptor:
        async def stream(self, _request: Any) -> AsyncIterator[str]:
            yield ""

        async def start_delegation(self, _text: str, _fence: GenerationFence) -> None:
            return None

    monkeypatch.setattr(
        "services.agent.src.voice_core.provider_adapter.importlib.import_module",
        lambda _name: SimpleNamespace(
            build=lambda _settings: lambda _identity: MissingOutputIntentAcceptor()
        ),
    )
    rejecting_factory = build_production_provider_factory(cast(Any, settings))
    with pytest.raises(ValueError, match="accept_output_intent"):
        rejecting_factory(SessionIdentity("production-missing-output-intent", stream_epoch=1))

    class MissingDelegationStarter:
        async def stream(self, _request: Any) -> AsyncIterator[str]:
            yield ""

        async def accept_output_intent(self, _intent: Any) -> None:
            return None

    monkeypatch.setattr(
        "services.agent.src.voice_core.provider_adapter.importlib.import_module",
        lambda _name: SimpleNamespace(
            build=lambda _settings: lambda _identity: MissingDelegationStarter()
        ),
    )
    rejecting_factory = build_production_provider_factory(cast(Any, settings))
    with pytest.raises(ValueError, match="start_delegation"):
        rejecting_factory(SessionIdentity("production-missing-delegation", stream_epoch=1))


def test_production_provider_factory_fails_closed_without_production_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src.providers import doubao_tts, funasr_stt

    monkeypatch.setattr(
        funasr_stt.FunASRConfig,
        "from_env",
        classmethod(
            lambda cls: funasr_stt.FunASRConfig(
                api_key="",
                ws_url="wss://asr.example/ws",
            )
        ),
    )
    monkeypatch.setattr(
        doubao_tts.DoubaoTTSConfig,
        "from_env",
        classmethod(
            lambda cls: doubao_tts.DoubaoTTSConfig(
                ws_url="wss://tts.example/ws",
                api_key="tts-key",
                speaker="speaker",
            )
        ),
    )
    settings = SimpleNamespace(
        environment="production",
        llm_api_key="",
        llm_base_url="https://llm.example/v1",
        llm_fast_model="fast-model",
        llm_provider="bailian_deepseek",
    )

    monkeypatch.setenv("MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY", "fake.module:build")
    monkeypatch.setattr(
        "services.agent.src.voice_core.provider_adapter.importlib.import_module",
        lambda _name: SimpleNamespace(build=lambda _settings: lambda _identity: FakeLLM()),
    )
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        build_production_provider_factory(cast(Any, settings))


def test_production_provider_factory_rejects_raw_llm_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY", raising=False)

    with pytest.raises(ValueError, match="full Agent pipeline"):
        build_production_provider_factory(cast(Any, SimpleNamespace()))
