"""Adapters for the existing remote ASR/LLM/TTS providers.

The media registry intentionally depends on a small provider protocol.  This
module is the concrete, provider-facing adapter used by a deployment that
wants to reuse Memoria's existing FunASR, language-model and Doubao handler
objects.  It does not create a second orchestration stack or embed a vendor
SDK in the media bridge.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.orchestration.handlers import (
    LanguageModelHandler,
    LanguageModelRequest,
    SpeechSynthesisHandler,
    SpeechSynthesisRequest,
)
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.providers.funasr_protocol import (
    FunASRServerEvent,
    sentence_to_asr_result,
)
from services.agent.src.providers.funasr_stt import FunASRSession
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.config import AgentSettings
    from services.agent.src.voice_core.media_session import MediaReplyChunk


@dataclass(frozen=True, slots=True)
class ExistingVoiceProviderConfig:
    """Bounds applied while adapting the existing provider handlers."""

    sample_rate: int = 16_000
    output_sample_rate: int = 24_000
    output_frame_ms: int = 20
    max_events_per_audio_frame: int = 64
    max_asr_task_history: int = 8
    max_asr_result_history: int = 256
    max_generation_history: int = 256

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.output_sample_rate <= 0:
            raise ValueError("provider sample rates must be positive")
        if self.output_frame_ms <= 0:
            raise ValueError("provider output frame duration must be positive")
        if self.output_sample_rate * self.output_frame_ms % 1000:
            raise ValueError("provider output frame duration must produce whole samples")
        if self.output_sample_rate * self.output_frame_ms // 1000 <= 0:
            raise ValueError("provider output frame duration must produce samples")
        if self.max_events_per_audio_frame <= 0:
            raise ValueError("max_events_per_audio_frame must be positive")
        if self.max_asr_task_history <= 0 or self.max_asr_result_history <= 0:
            raise ValueError("ASR metadata history bounds must be positive")
        if self.max_generation_history <= 0:
            raise ValueError("max_generation_history must be positive")


ASRSessionFactory = Callable[[], FunASRSession]


@dataclass(slots=True)
class ExistingVoiceProviderAdapter:
    """Bridge the already-existing remote provider handlers to media-v1."""

    asr_session_factory: ASRSessionFactory
    language_model: LanguageModelHandler
    speech_synthesis: SpeechSynthesisHandler
    config: ExistingVoiceProviderConfig = field(default_factory=ExistingVoiceProviderConfig)
    owns_speech_synthesis: bool = False
    _asr: FunASRSession | None = field(default=None, init=False)
    _stream_epoch: int = field(default=0, init=False)
    _asr_task_contexts: dict[str, tuple[int, int, int]] = field(
        default_factory=dict,
        init=False,
    )
    _asr_task_order: deque[str] = field(default_factory=deque, init=False)
    _sentence_revisions: dict[tuple[int, int, str, int], int] = field(
        default_factory=dict,
        init=False,
    )
    _sentence_revision_order: deque[tuple[int, int, str, int]] = field(
        default_factory=deque,
        init=False,
    )
    _generation_started: set[GenerationFence] = field(default_factory=set, init=False)
    _generation_cancel_events: dict[GenerationFence, asyncio.Event] = field(
        default_factory=dict,
        init=False,
    )
    _cancelled_generations: set[GenerationFence] = field(default_factory=set, init=False)
    _generation_history: deque[GenerationFence] = field(default_factory=deque, init=False)
    _generation_eviction_floor: tuple[int, int, int] | None = field(
        default=None,
        init=False,
    )
    _active_speech_streams: dict[GenerationFence, Any] = field(default_factory=dict, init=False)
    _generation_output_ends: dict[GenerationFence, int] = field(
        default_factory=dict,
        init=False,
    )
    _output_sample: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)

    async def _ensure_asr(self, stream_epoch: int) -> FunASRSession:
        if self._closed:
            raise RuntimeError("media provider is closed")
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self._asr is None:
            self._asr = self.asr_session_factory()
            await self._asr.connect()
            self._stream_epoch = stream_epoch
            self._remember_asr_task(self._asr, stream_epoch)
        elif stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        elif stream_epoch > self._stream_epoch:
            await self._asr.reconnect_with_replay()
            self._stream_epoch = stream_epoch
            self._asr_task_contexts.clear()
            self._asr_task_order.clear()
            self._sentence_revisions.clear()
            self._sentence_revision_order.clear()
            self._remember_asr_task(self._asr, stream_epoch)
        return self._asr

    def _remember_asr_task(self, asr: FunASRSession, stream_epoch: int) -> None:
        task_id = str(getattr(asr, "task_id", "") or "")
        if task_id:
            if task_id not in self._asr_task_contexts:
                self._asr_task_order.append(task_id)
            self._asr_task_contexts[task_id] = (
                stream_epoch,
                max(1, asr.task_epoch),
                asr.task_sample_origin,
            )
            while len(self._asr_task_order) > self.config.max_asr_task_history:
                self._asr_task_contexts.pop(self._asr_task_order.popleft(), None)

    async def ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> tuple[ASRResult, ...]:
        """Send one absolute-range PCM frame and drain bounded ASR results."""

        if frame.identity != identity:
            raise ValueError("provider frame identity does not match session")
        if len(frame.payload) % 2:
            raise ValueError("provider input must be 16-bit PCM")
        asr = await self._ensure_asr(identity.stream_epoch)
        self._remember_asr_task(asr, identity.stream_epoch)
        await asr.send_pcm(
            frame.payload,
            capture_start_sample=frame.capture_start_sample,
        )
        # ``send_pcm`` may transparently reconnect and advance the provider
        # task. Remember both sides so queued late events retain their own
        # task epoch and absolute sample origin.
        self._remember_asr_task(asr, identity.stream_epoch)
        results: list[ASRResult] = []
        for _ in range(self.config.max_events_per_audio_frame):
            try:
                event = asr.events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if event.event == "task-failed":
                raise RuntimeError(event.error_message or "FunASR task failed")
            if event.event != "result-generated" or event.sentence is None:
                continue
            result = self._map_asr_event(asr, event, identity.stream_epoch)
            if result is not None:
                results.append(result)
        return tuple(results)

    def _map_asr_event(
        self,
        asr: FunASRSession,
        event: FunASRServerEvent,
        stream_epoch: int,
    ) -> ASRResult | None:
        sentence = event.sentence
        if sentence is None:
            return None
        if not event.task_id:
            return None
        sentence_id = str(sentence.sentence_id)
        task_context = self._asr_task_contexts.get(event.task_id)
        if task_context is None:
            current_task_id = str(getattr(asr, "task_id", "") or "")
            if event.task_id and event.task_id != current_task_id:
                # Its task context was safely evicted. Never re-associate a
                # very late result with the newest provider task.
                return None
            self._remember_asr_task(asr, stream_epoch)
            task_context = (
                stream_epoch,
                max(1, asr.task_epoch),
                asr.task_sample_origin,
            )
        event_stream_epoch, task_epoch, sample_offset = task_context
        revision_key = (
            event_stream_epoch,
            task_epoch,
            sentence_id,
            sentence.begin_ms,
        )
        if revision_key not in self._sentence_revisions:
            self._sentence_revision_order.append(revision_key)
        revision = self._sentence_revisions.get(revision_key, 0) + 1
        self._sentence_revisions[revision_key] = revision
        while len(self._sentence_revision_order) > self.config.max_asr_result_history:
            self._sentence_revisions.pop(self._sentence_revision_order.popleft(), None)
        mapped = sentence_to_asr_result(
            sentence,
            task_epoch=task_epoch,
            sample_rate=self.config.sample_rate,
            revision=revision,
            stream_epoch=event_stream_epoch,
            sample_offset=sample_offset,
        )
        # Interval dedup, replay and revision semantics are owned by
        # ASRStreamSupervisor.  The adapter only maps provider events onto the
        # absolute sample clock and assigns a per-task revision; returning the
        # mapped value unchanged keeps partial/final contracts in one place.
        return mapped

    @property
    def output_frame_samples(self) -> int:
        """Number of PCM samples in one fixed 20 ms output frame."""

        return self.config.output_sample_rate * self.config.output_frame_ms // 1000

    async def _provider_pcm_chunks(
        self,
        phrase: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[bytes, None]:
        """Yield the provider's actual asynchronous PCM chunks when available."""

        request = SpeechSynthesisRequest(
            phrases=(phrase,),
            cancellation=cancellation,
            cancel_event=cancel_event,
        )
        stream_factory = getattr(self.speech_synthesis, "stream", None)
        if callable(stream_factory):
            bind_fence = getattr(self.speech_synthesis, "bind_fence", None)
            if callable(bind_fence):
                bound = bind_fence(fence)
                if inspect.isawaitable(bound):
                    await bound
            stream = stream_factory()
            if inspect.isawaitable(stream):
                stream = await stream
            try:
                push_text = getattr(stream, "push_text", None)
                end_input = getattr(stream, "end_input", None)
                if not callable(push_text) or not callable(end_input):
                    raise RuntimeError("speech provider stream cannot accept text")
                pushed = push_text(phrase)
                if inspect.isawaitable(pushed):
                    await pushed
                ended = end_input()
                if inspect.isawaitable(ended):
                    await ended
                async for pcm in self._async_speech_pcm(stream, cancel_event):
                    yield pcm
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    closed = close()
                    if inspect.isawaitable(closed):
                        await closed
            return

        # Compatibility for the existing orchestration handler contract. New
        # provider adapters should expose ``stream()`` so first audio is not
        # delayed until a whole phrase buffer has completed.
        result: Any = self.speech_synthesis.synthesize(request)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "__aiter__"):
            async for pcm in self._async_speech_pcm(result, cancel_event):
                yield pcm
            return
        pcm = self._speech_chunk_pcm(result)
        if pcm:
            yield pcm

    async def _async_speech_pcm(
        self,
        stream: Any,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        async for chunk in self._cancel_aware_items(stream, cancel_event):
            pcm = self._speech_chunk_pcm(chunk)
            if pcm:
                yield pcm

    async def _cancel_aware_items(
        self,
        stream: Any,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[Any]:
        iterator = aiter(stream)
        while not cancel_event.is_set():

            async def read_next() -> Any:
                return await anext(iterator)

            next_chunk = asyncio.create_task(read_next())
            cancelled = asyncio.create_task(cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    (next_chunk, cancelled),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                next_chunk.cancel()
                cancelled.cancel()
                await asyncio.gather(next_chunk, cancelled, return_exceptions=True)
                raise
            if cancelled in done:
                next_chunk.cancel()
                await asyncio.gather(next_chunk, return_exceptions=True)
                return
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                return
            yield chunk

    def _speech_chunk_pcm(self, chunk: Any) -> bytes:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            return bytes(chunk)
        pcm = getattr(chunk, "pcm", None)
        if pcm is not None:
            return bytes(pcm)
        frame = getattr(chunk, "frame", None)
        if frame is None:
            return b""
        sample_rate = getattr(frame, "sample_rate", self.config.output_sample_rate)
        if sample_rate != self.config.output_sample_rate:
            raise RuntimeError("speech provider returned an unexpected sample rate")
        return bytes(getattr(frame, "data", b"") or b"")

    async def _provider_timed_text_spans(
        self,
        stream: Any,
        *,
        max_audio_end_sample: int | None = None,
    ) -> tuple[Any, ...]:
        """Read exact provider subtitle timing, or return no ledger facts.

        LLM phrase arrival and PCM arrival have unrelated clocks. Only a
        provider-supplied timed transcript may map assistant text into the
        actual-heard ledger; absent or malformed timing deliberately yields no
        text spans instead of a plausible but false boundary.
        """

        from services.agent.src.voice_core.media_session import MediaTextSpan

        getter = getattr(stream, "timed_transcript", None)
        if not callable(getter):
            return ()
        timed = getter()
        if inspect.isawaitable(timed):
            timed = await timed
        if not isinstance(timed, (tuple, list)):
            return ()
        alignment_getter = getattr(stream, "timed_transcript_alignment", None)
        alignment = alignment_getter() if callable(alignment_getter) else None
        if alignment == "degraded":
            return ()
        spans: list[MediaTextSpan] = []
        previous_end = 0
        for item in timed:
            text = getattr(item, "text", None)
            start_time = getattr(item, "start_time", None)
            end_time = getattr(item, "end_time", None)
            if (
                not isinstance(text, str)
                or not text
                or not isinstance(start_time, (int, float))
                or not isinstance(end_time, (int, float))
            ):
                return ()
            start = round(start_time * self.config.output_sample_rate)
            end = round(end_time * self.config.output_sample_rate)
            if start < previous_end or end <= start:
                return ()
            if max_audio_end_sample is not None and end > max_audio_end_sample:
                break
            spans.append(MediaTextSpan(text, start, end))
            previous_end = end
        if not spans:
            return ()
        # A live/pending snapshot grows as subtitles arrive and may legally
        # trail the already-generated PCM. The loop above already keeps only
        # spans fully inside the generated audio, so a missing subtitle tail
        # must not discredit the safely covered prefix. Only an alignment that
        # claims to be final but is neither ok nor scaled gets the end-gap
        # consistency check (and scaling) below.
        if max_audio_end_sample is not None and alignment not in ("ok", "scaled", "pending"):
            timed_end = spans[-1].audio_end_sample
            difference = abs(max_audio_end_sample - timed_end)
            if difference > self.config.output_sample_rate * 0.3:
                return ()
            if difference > self.config.output_sample_rate * 0.12:
                factor = max_audio_end_sample / timed_end
                spans = [
                    MediaTextSpan(
                        span.text,
                        round(span.audio_start_sample * factor),
                        round(span.audio_end_sample * factor),
                    )
                    for span in spans
                ]
        return tuple(spans)

    async def interrupted_timed_text_spans(self, fence: GenerationFence) -> tuple[Any, ...]:
        """Return only the verified timed prefix available at interruption."""

        stream = self._active_speech_streams.get(fence)
        audio_end = self._generation_output_ends.get(fence, 0)
        if stream is None or audio_end <= 0:
            return ()
        return await self._provider_timed_text_spans(
            stream,
            max_audio_end_sample=audio_end,
        )

    async def _synthesize_phrase(
        self,
        phrase: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
        *,
        first: bool,
        final: bool,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Stream one phrase as bounded, fixed-size PCM frames."""

        if cancel_event.is_set():
            return
        from services.agent.src.voice_core.media_session import MediaReplyChunk

        frame_samples = self.output_frame_samples
        frame_bytes = frame_samples * 2
        buffered = bytearray()
        held_frame: bytes | None = None
        output_sample = source_start_sample
        provider_pcm = self._provider_pcm_chunks(
            phrase,
            fence,
            cancellation,
            cancel_event,
        )
        try:
            async for pcm in provider_pcm:
                if cancel_event.is_set():
                    return
                buffered.extend(pcm)
                while len(buffered) >= frame_bytes:
                    next_frame = bytes(buffered[:frame_bytes])
                    del buffered[:frame_bytes]
                    if held_frame is not None:
                        yield MediaReplyChunk(
                            pcm_s16le=held_frame,
                            source_start_sample=output_sample,
                            assistant_text_delta=(
                                phrase if output_sample == source_start_sample else ""
                            ),
                            first=first and output_sample == source_start_sample,
                        )
                        if cancel_event.is_set():
                            return
                        output_sample += frame_samples
                        self._output_sample = output_sample
                    held_frame = next_frame
        finally:
            await provider_pcm.aclose()
        if cancel_event.is_set():
            return
        if len(buffered) % 2:
            raise RuntimeError("speech provider returned invalid 16-bit PCM")
        if buffered:
            if held_frame is not None:
                yield MediaReplyChunk(
                    pcm_s16le=held_frame,
                    source_start_sample=output_sample,
                    assistant_text_delta=(phrase if output_sample == source_start_sample else ""),
                    first=first and output_sample == source_start_sample,
                )
                if cancel_event.is_set():
                    return
                output_sample += frame_samples
                self._output_sample = output_sample
            held_frame = bytes(buffered) + b"\x00" * (frame_bytes - len(buffered))
        if held_frame is None:
            raise RuntimeError("speech provider returned no valid PCM")
        phrase_audio_end = output_sample + frame_samples
        yield MediaReplyChunk(
            pcm_s16le=held_frame,
            source_start_sample=output_sample,
            text=phrase,
            assistant_text_delta=(phrase if output_sample == source_start_sample else ""),
            first=first and output_sample == source_start_sample,
            final=final,
            text_audio_start_sample=source_start_sample,
            text_audio_end_sample=phrase_audio_end,
        )
        self._output_sample = phrase_audio_end

    async def _stream_incremental_generation(
        self,
        user_text: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Feed every completed LLM phrase into one live TTS session.

        Doubao's bidirectional protocol is generation-scoped: reopening it for
        every phrase adds avoidable first-audio latency and can change the
        voice between adjacent phrases.  Text-to-audio boundaries are not
        exposed until the provider's final subtitle alignment, so this seam
        deliberately records one conservative whole-generation ledger span;
        an interruption can never over-claim a partly rendered phrase.
        """

        from services.agent.src.voice_core.media_session import MediaReplyChunk

        bind_fence = getattr(self.speech_synthesis, "bind_fence", None)
        if callable(bind_fence):
            bound = bind_fence(fence)
            if inspect.isawaitable(bound):
                await bound
        stream_factory = self.speech_synthesis.stream  # type: ignore[attr-defined]
        stream = stream_factory()
        if inspect.isawaitable(stream):
            stream = await stream
        push_text = getattr(stream, "push_text", None)
        end_input = getattr(stream, "end_input", None)
        if not callable(push_text) or not callable(end_input):
            raise RuntimeError("speech provider stream cannot accept text")
        self._active_speech_streams[fence] = stream
        self._generation_output_ends[fence] = 0

        phrases: list[str] = []
        pending_announcements: deque[str] = deque()
        segmenter = PhraseSegmenter(fence=fence)
        first_phrase_ready = asyncio.Event()

        async def push_phrase(phrase: str) -> None:
            if cancel_event.is_set() or not phrase:
                return
            phrases.append(phrase)
            pending_announcements.append(phrase)
            pushed = push_text(phrase)
            if inspect.isawaitable(pushed):
                await pushed
            first_phrase_ready.set()

        async def produce_text() -> None:
            llm_stream = self.language_model.stream(
                LanguageModelRequest(user_text=user_text, cancellation=cancellation)
            )
            try:
                async for token in self._cancel_aware_items(llm_stream, cancel_event):
                    if not token:
                        continue
                    for segment in segmenter.push_token(token):
                        await push_phrase(segment.text)
                for segment in segmenter.flush(end_of_stream=True):
                    await push_phrase(segment.text)
            finally:
                close_llm = getattr(llm_stream, "aclose", None)
                if callable(close_llm):
                    closed_llm = close_llm()
                    if inspect.isawaitable(closed_llm):
                        await closed_llm
                first_phrase_ready.set()
                ended = end_input()
                if inspect.isawaitable(ended):
                    await ended

        producer = asyncio.create_task(
            produce_text(),
            name=f"media-tts-text-{fence.session_id}-{fence.generation_id}",
        )

        def take_announcement() -> str:
            announcement = "".join(pending_announcements)
            pending_announcements.clear()
            return announcement

        frame_samples = self.output_frame_samples
        frame_bytes = frame_samples * 2
        buffered = bytearray()
        held_frame: bytes | None = None
        output_sample = 0

        try:
            # Start consuming PCM only after at least one complete phrase was
            # accepted by the live provider.  This keeps subtitle publication
            # aligned with the first playable frame without waiting for LLM
            # end-of-stream or a second phrase.
            await first_phrase_ready.wait()
            if cancel_event.is_set():
                return
            if producer.done() and producer.exception() is not None:
                await producer
            async for pcm in self._async_speech_pcm(stream, cancel_event):
                if cancel_event.is_set():
                    return
                buffered.extend(pcm)
                while len(buffered) >= frame_bytes:
                    next_frame = bytes(buffered[:frame_bytes])
                    del buffered[:frame_bytes]
                    if held_frame is not None:
                        announcement = take_announcement()
                        next_output_sample = output_sample + frame_samples
                        self._generation_output_ends[fence] = next_output_sample
                        yield MediaReplyChunk(
                            pcm_s16le=held_frame,
                            source_start_sample=output_sample,
                            assistant_text_delta=announcement,
                            first=output_sample == 0,
                        )
                        output_sample = next_output_sample
                        self._output_sample = output_sample
                    held_frame = next_frame
            if cancel_event.is_set():
                return
            await producer
            if cancel_event.is_set():
                return
            if len(buffered) % 2:
                raise RuntimeError("speech provider returned invalid 16-bit PCM")
            if buffered:
                if held_frame is not None:
                    announcement = take_announcement()
                    next_output_sample = output_sample + frame_samples
                    self._generation_output_ends[fence] = next_output_sample
                    yield MediaReplyChunk(
                        pcm_s16le=held_frame,
                        source_start_sample=output_sample,
                        assistant_text_delta=announcement,
                        first=output_sample == 0,
                    )
                    output_sample = next_output_sample
                    self._output_sample = output_sample
                held_frame = bytes(buffered) + b"\x00" * (frame_bytes - len(buffered))
            if held_frame is None:
                raise RuntimeError("speech provider returned no valid PCM")
            complete_text = "".join(phrases)
            if not complete_text:
                if cancel_event.is_set():
                    return
                raise RuntimeError("language model returned no speakable text")
            audio_end = output_sample + frame_samples
            announcement = take_announcement()
            timed_text_spans = await self._provider_timed_text_spans(
                stream,
                max_audio_end_sample=audio_end,
            )
            yield MediaReplyChunk(
                pcm_s16le=held_frame,
                source_start_sample=output_sample,
                assistant_text_delta=announcement,
                first=output_sample == 0,
                final=True,
                text_spans=timed_text_spans,
            )
            self._output_sample = audio_end
            self._generation_output_ends[fence] = audio_end
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            close = getattr(stream, "aclose", None)
            if callable(close):
                closed = close()
                if inspect.isawaitable(closed):
                    await closed
            self._active_speech_streams.pop(fence, None)
            self._generation_output_ends.pop(fence, None)

    async def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Stream LLM phrases through the existing TTS handler."""

        if self._closed:
            return
        if identity.session_id != fence.session_id:
            raise ValueError("provider generation fence does not match identity")
        # A generation is a one-shot output stream.  Retrying the same fence
        # would duplicate sequence/sample ranges in the media bridge; callers
        # must create a new authoritative generation for a retry.
        if (
            fence in self._generation_started
            or fence in self._cancelled_generations
            or self._generation_was_evicted(fence)
        ):
            return
        self._generation_started.add(fence)
        cancellation = CancellationContext.capture(fence)
        cancel_event = asyncio.Event()
        self._generation_cancel_events[fence] = cancel_event
        # Source samples are relative to one response generation; keep this
        # local so interleaved/stale generations cannot reset one another.
        output_sample = 0
        self._output_sample = 0
        try:
            if callable(getattr(self.speech_synthesis, "stream", None)):
                async for chunk in self._stream_incremental_generation(
                    user_text,
                    fence,
                    cancellation,
                    cancel_event,
                ):
                    yield chunk
                return
            segmenter = PhraseSegmenter(fence=fence)
            terminal_chunk: MediaReplyChunk | None = None
            llm_stream = self.language_model.stream(
                LanguageModelRequest(user_text=user_text, cancellation=cancellation)
            )
            try:
                async for token in self._cancel_aware_items(llm_stream, cancel_event):
                    if not token:
                        continue
                    for segment in segmenter.push_token(token):
                        async for chunk in self._synthesize_phrase(
                            segment.text,
                            fence,
                            cancellation,
                            cancel_event,
                            first=output_sample == 0,
                            final=False,
                            source_start_sample=output_sample,
                        ):
                            output_sample = chunk.source_start_sample + chunk.frame_samples
                            if terminal_chunk is not None:
                                yield terminal_chunk
                            terminal_chunk = chunk
            finally:
                close_llm = getattr(llm_stream, "aclose", None)
                if callable(close_llm):
                    closed_llm = close_llm()
                    if inspect.isawaitable(closed_llm):
                        await closed_llm
            pending = [segment.text for segment in segmenter.flush(end_of_stream=True)]
            for index, phrase in enumerate(pending):
                if cancel_event.is_set():
                    return
                async for chunk in self._synthesize_phrase(
                    phrase,
                    fence,
                    cancellation,
                    cancel_event,
                    first=output_sample == 0,
                    final=index == len(pending) - 1,
                    source_start_sample=output_sample,
                ):
                    output_sample = chunk.source_start_sample + chunk.frame_samples
                    if terminal_chunk is not None:
                        yield terminal_chunk
                    terminal_chunk = chunk
            if terminal_chunk is not None and not cancel_event.is_set():
                yield replace(terminal_chunk, final=True)
        finally:
            cancel_event.set()
            self._generation_cancel_events.pop(fence, None)
            self._remember_terminal_generation(fence)

    @staticmethod
    def _generation_order(fence: GenerationFence) -> tuple[int, int, int]:
        return (fence.turn_id, fence.generation_id, fence.tool_epoch)

    def _generation_was_evicted(self, fence: GenerationFence) -> bool:
        floor = self._generation_eviction_floor
        return floor is not None and self._generation_order(fence) <= floor

    def _remember_terminal_generation(self, fence: GenerationFence) -> None:
        if fence not in self._generation_history:
            self._generation_history.append(fence)
        while len(self._generation_history) > self.config.max_generation_history:
            evicted = self._generation_history.popleft()
            self._generation_started.discard(evicted)
            self._cancelled_generations.discard(evicted)
            order = self._generation_order(evicted)
            floor = self._generation_eviction_floor
            if floor is None or order > floor:
                self._generation_eviction_floor = order

    def cancel_generation(self, fence: GenerationFence) -> bool:
        """Request cooperative cancellation of one generation output stream."""

        event = self._generation_cancel_events.get(fence)
        if event is None:
            if self._generation_was_evicted(fence):
                return False
            if fence in self._cancelled_generations:
                return True
            # Async-generator bodies run only when first iterated.  Retain a
            # pre-start cancellation request so a caller can cancel between
            # creating the stream and scheduling its first ``anext``.
            if fence not in self._generation_started:
                self._cancelled_generations.add(fence)
                self._remember_terminal_generation(fence)
                return True
            return False
        event.set()
        self._cancelled_generations.add(fence)
        return True

    # Short alias used by transport owners that expose a generic cancellation
    # hook rather than a provider-specific method name.
    cancel = cancel_generation

    async def close(self, identity: SessionIdentity) -> None:
        _ = identity
        self._closed = True
        for cancel_event in self._generation_cancel_events.values():
            cancel_event.set()
        self._generation_cancel_events.clear()
        if self.owns_speech_synthesis:
            close_speech = getattr(self.speech_synthesis, "aclose", None)
            if callable(close_speech):
                closed_speech = close_speech()
                if inspect.isawaitable(closed_speech):
                    await closed_speech
        if self._asr is not None:
            await self._asr.aclose()
            self._asr = None


def build_existing_provider_factory(
    *,
    asr_session_factory: ASRSessionFactory,
    language_model: LanguageModelHandler,
    speech_synthesis: SpeechSynthesisHandler,
    config: ExistingVoiceProviderConfig | None = None,
) -> Callable[[SessionIdentity], ExistingVoiceProviderAdapter]:
    """Bind one existing provider set to independently fenced sessions."""

    selected_config = config or ExistingVoiceProviderConfig()

    def factory(identity: SessionIdentity) -> ExistingVoiceProviderAdapter:
        _ = identity
        return ExistingVoiceProviderAdapter(
            asr_session_factory=asr_session_factory,
            language_model=language_model,
            speech_synthesis=speech_synthesis,
            config=selected_config,
        )

    return factory


def build_production_provider_factory(
    settings: AgentSettings,
) -> Callable[[SessionIdentity], ExistingVoiceProviderAdapter]:
    """Build FunASR/Doubao around the existing full response orchestrator.

    The media bridge is a transport seam, not a second product brain.  Its
    production language-model handler must come from the same memory,
    persona, permission, tool and response-planning pipeline used by the
    established Agent path.  Missing that injection is a readiness failure;
    a system-prompt-plus-current-turn fallback would silently bypass policy.
    """

    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
    from services.agent.src.providers.funasr_stt import FunASRConfig

    orchestrated_reference = os.getenv(
        "MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY",
        "",
    ).strip()
    if not orchestrated_reference:
        raise ValueError(
            "production media bridge requires "
            "MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY from the full Agent pipeline"
        )
    module_name, separator, attribute = orchestrated_reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY must be module:callable")
    orchestrated_builder = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(orchestrated_builder):
        raise ValueError("orchestrated LLM factory is not callable")
    language_model_factory = orchestrated_builder(settings)
    if not callable(language_model_factory):
        raise ValueError("orchestrated LLM builder did not return a session factory")

    asr_config = FunASRConfig.from_env()
    tts_config = DoubaoTTSConfig.from_env()
    if getattr(settings, "environment", "development") == "production":
        doubao_auth = tts_config.api_key.strip() or (
            tts_config.app_id.strip() and tts_config.access_token.strip()
        )
        missing = [
            name
            for name, value in (
                ("DASHSCOPE_API_KEY", asr_config.api_key.strip()),
                ("DASHSCOPE_WS_URL", asr_config.ws_url.strip()),
                ("DOUBAO_TTS_AUTH", doubao_auth),
                ("DOUBAO_TTS_WS_URL", tts_config.ws_url.strip()),
                ("DOUBAO_TTS_SPEAKER", tts_config.speaker.strip()),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"production media provider missing required config: {', '.join(missing)}"
            )
        if not asr_config.ws_url.startswith("wss://") or not tts_config.ws_url.startswith("wss://"):
            raise ValueError("production media providers require WSS endpoints")
    adapter_config = ExistingVoiceProviderConfig(
        sample_rate=asr_config.sample_rate,
        output_sample_rate=tts_config.sample_rate,
    )

    def factory(identity: SessionIdentity) -> ExistingVoiceProviderAdapter:
        language_model = language_model_factory(identity)
        if not callable(getattr(language_model, "stream", None)):
            raise ValueError("orchestrated language-model handler must expose stream()")
        return ExistingVoiceProviderAdapter(
            asr_session_factory=lambda: FunASRSession(replace(asr_config)),
            language_model=language_model,
            speech_synthesis=cast(
                SpeechSynthesisHandler,
                DoubaoTTS(replace(tts_config)),
            ),
            config=adapter_config,
            owns_speech_synthesis=True,
        )

    return factory


__all__ = [
    "ExistingVoiceProviderAdapter",
    "ExistingVoiceProviderConfig",
    "build_existing_provider_factory",
    "build_production_provider_factory",
]
