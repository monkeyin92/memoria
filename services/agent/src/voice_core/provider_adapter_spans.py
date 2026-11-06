"""Speech-span and PCM streaming mixin for the existing voice provider adapter.

``ExistingVoiceProviderSpanMixin`` owns the provider speech stream, timed text
spans and PCM chunk extraction.  The adapter dataclass owns config, metrics
and the stream registries, declared here for type checking only.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.orchestration.handlers import SpeechSynthesisRequest

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from services.agent.src.orchestration.handlers import SpeechSynthesisHandler
    from services.agent.src.voice_core.provider_adapter import (
        ExistingVoiceProviderConfig,
    )


class ExistingVoiceProviderSpanMixin:
    """Stream reply phrases and project the provider's timed text spans."""

    if TYPE_CHECKING:
        config: ExistingVoiceProviderConfig
        _active_speech_streams: dict[GenerationFence, Any]
        _generation_output_ends: dict[GenerationFence, int]
        _fixed_safety_fences: set[GenerationFence]

        speech_synthesis: SpeechSynthesisHandler


    async def _provider_pcm_chunks(
        self,
        spoken: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[bytes, None]:
        """Yield the provider's actual asynchronous PCM chunks when available."""

        from services.common.labelled_lines import labelled_catalogue

        labelled = labelled_catalogue().by_text(spoken)
        if labelled is not None:
            logger.info(
                "labelled line pcm content_id=%s ai_generated=%s session=%s turn=%s",
                labelled.aigc["content_id"],
                labelled.aigc["ai_generated"],
                fence.session_id,
                fence.turn_id,
            )
            yield labelled.pcm_s16le
            return

        request = SpeechSynthesisRequest(
            phrases=(spoken,),
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
                pushed = push_text(spoken)
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

    async def _heard_text_spans(
        self,
        fence: GenerationFence,
        stream: Any,
        *,
        max_audio_end_sample: int,
    ) -> tuple[Any, ...]:
        if fence in self._fixed_safety_fences:
            return ()
        return await self._provider_timed_text_spans(
            stream,
            max_audio_end_sample=max_audio_end_sample,
        )

    def is_fixed_safety_reply(self, fence: GenerationFence) -> bool:
        """Whether this generation speaks an approved fixed safety script.

        The media session asks before the first frame reaches the device: a reply
        that is a crisis line must not be superseded by a wordless user turn (C1,
        2026-10-21).  The fence is marked when the phrase stream yields the script,
        which is before any text reaches the speech provider.
        """

        return fence in self._fixed_safety_fences

    async def interrupted_timed_text_spans(self, fence: GenerationFence) -> tuple[Any, ...]:
        """Return only the verified timed prefix available at interruption."""

        stream = self._active_speech_streams.get(fence)
        audio_end = self._generation_output_ends.get(fence, 0)
        if stream is None or audio_end <= 0:
            return ()
        return await self._heard_text_spans(fence, stream, max_audio_end_sample=audio_end)
