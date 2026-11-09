"""Reply/output generation mixin for the existing voice provider adapter.

``ExistingVoiceProviderGenerationMixin`` streams text and fixed-safety replies,
yields output work and owns the terminal/eviction bookkeeping.  Adapter config
and the phrase/span helpers stay in the adapter core, declared here for type
checking only.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.common.fixed_safety_scripts import SpokenPhrase


def take_presynthesized_stream(
    speech_synthesis: Any,
    fence: GenerationFence,
    first_phrase: SpokenPhrase,
    cancel_event: asyncio.Event,
) -> Any:
    """Take the provider session opened for this generation's first phrase, if one exists."""

    from services.common.labelled_lines import labelled_catalogue

    take = getattr(speech_synthesis, "take_presynthesis", None)
    spoken = first_phrase.spoken
    if not callable(take):
        return None
    sends_first = (
        bool(spoken) and not cancel_event.is_set() and labelled_catalogue().by_text(spoken) is None
    )
    return take(fence, spoken if sends_first else "")


if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session import MediaReplyChunk
    from services.agent.src.voice_core.provider_adapter import (
        ExistingVoiceProviderConfig,
    )


class ExistingVoiceProviderGenerationMixin:
    """Provider adapter seam."""

    if TYPE_CHECKING:
        config: ExistingVoiceProviderConfig
        language_model: Any
        speech_synthesis: Any
        metrics: Any
        owns_language_model: bool
        owns_speech_synthesis: bool
        _generation_started: set[GenerationFence]
        _generation_cancel_events: dict[GenerationFence, asyncio.Event]
        _cancelled_generations: set[GenerationFence]
        _generation_history: deque[GenerationFence]
        _fixed_safety_fences: set[GenerationFence]
        _output_work_started: set[tuple[GenerationFence, str]]
        _output_work_history: deque[tuple[GenerationFence, str]]
        _output_work_cancel_events: dict[tuple[GenerationFence, str], asyncio.Event]
        _output_sample: int
        _closed: bool
        _generation_eviction_floor: tuple[int, int, int, int] | None
        _prerecorded_fixed_safety: dict[GenerationFence, tuple[str, int]]
        _asr: Any

        @property
        def output_frame_samples(self) -> int: ...

        def _reply_phrases(
            self,
            user_text: str,
            fence: GenerationFence,
            cancellation: CancellationContext,
            cancel_event: asyncio.Event,
        ) -> AsyncGenerator[SpokenPhrase, None]: ...

        def _synthesize_phrase(
            self,
            phrase: SpokenPhrase,
            fence: GenerationFence,
            cancellation: CancellationContext,
            cancel_event: asyncio.Event,
            *,
            first: bool,
            final: bool,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]: ...

        def _stream_incremental_generation(
            self,
            user_text: str,
            fence: GenerationFence,
            cancellation: CancellationContext,
            cancel_event: asyncio.Event,
        ) -> AsyncIterator[MediaReplyChunk]: ...

        def _stream_fixed_safety_script(
            self,
            phrases: list[SpokenPhrase],
            fence: GenerationFence,
            cancellation: CancellationContext,
            cancel_event: asyncio.Event,
        ) -> AsyncIterator[MediaReplyChunk]: ...

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
            terminal_chunk: MediaReplyChunk | None = None
            reply = self._reply_phrases(user_text, fence, cancellation, cancel_event)
            try:
                async for phrase in reply:
                    async for chunk in self._synthesize_phrase(
                        phrase,
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
                await reply.aclose()
            if terminal_chunk is not None and not cancel_event.is_set():
                yield replace(terminal_chunk, final=True)
        finally:
            cancel_event.set()
            self._generation_cancel_events.pop(fence, None)
            self._fixed_safety_fences.discard(fence)
            self._remember_terminal_generation(fence)

    async def generate_output(
        self,
        identity: SessionIdentity,
        intent: Any,
        fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Render one admitted non-conversation OutputIntent source.

        The Registry serializes source ownership. This adapter only gives each
        source a distinct cancellation key so a deep/ack work can follow the
        conversation stream under the same generation fence.
        """

        from services.agent.src.voice_core.media_session import MediaReplyChunk

        if self._closed:
            return
        if identity.session_id != fence.session_id or source_start_sample < 0 or not work_id:
            raise ValueError("output source does not match the active media generation")
        key = (fence, work_id)
        if (
            key in self._output_work_started
            or fence in self._cancelled_generations
            or self._generation_was_evicted(fence)
        ):
            return
        self._output_work_started.add(key)
        cancel_event = asyncio.Event()
        self._output_work_cancel_events[key] = cancel_event
        cancellation = CancellationContext.capture(fence)
        try:
            source = getattr(intent, "WhichOneof", lambda _name: None)("source")
            if source == "tts_source":
                text = str(getattr(intent, "tts_source", "")).strip()
                if not text:
                    raise ValueError("output tts source must not be empty")
                async for chunk in self._synthesize_phrase(
                    SpokenPhrase.verbatim(text),
                    fence,
                    cancellation,
                    cancel_event,
                    first=True,
                    final=True,
                    source_start_sample=source_start_sample,
                ):
                    yield chunk
                return
            if source != "pcm_s16le":
                raise ValueError("output intent has no executable source")
            pcm = bytes(getattr(intent, "pcm_s16le", b""))
            if not pcm or len(pcm) % 2:
                raise ValueError("output PCM source must be non-empty 16-bit audio")
            frame_bytes = self.output_frame_samples * 2
            sample = source_start_sample
            for offset in range(0, len(pcm), frame_bytes):
                if cancel_event.is_set():
                    return
                frame = pcm[offset : offset + frame_bytes]
                final = offset + frame_bytes >= len(pcm)
                if len(frame) < frame_bytes:
                    frame += b"\x00" * (frame_bytes - len(frame))
                yield MediaReplyChunk(
                    pcm_s16le=frame,
                    source_start_sample=sample,
                    first=offset == 0,
                    final=final,
                )
                sample += self.output_frame_samples
        finally:
            cancel_event.set()
            self._output_work_cancel_events.pop(key, None)
            self._remember_output_work(key)

    def _remember_output_work(self, key: tuple[GenerationFence, str]) -> None:
        if key not in self._output_work_history:
            self._output_work_history.append(key)
        while len(self._output_work_history) > self.config.max_generation_history:
            self._output_work_started.discard(self._output_work_history.popleft())

    @staticmethod
    def _generation_order(fence: GenerationFence) -> tuple[int, int, int, int]:
        # session_epoch is the identity dimension (P0-3): the same
        # turn/generation/tool numbers under a new subject must never be
        # misjudged as already evicted by the old subject's floor.
        return (fence.session_epoch, fence.turn_id, fence.generation_id, fence.tool_epoch)

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
            self._prerecorded_fixed_safety.pop(evicted, None)
            order = self._generation_order(evicted)
            floor = self._generation_eviction_floor
            if floor is None or order > floor:
                self._generation_eviction_floor = order

    def cancel_generation(self, fence: GenerationFence) -> bool:
        """Request cooperative cancellation of one generation output stream."""

        output_cancelled = False
        for (output_fence, _work_id), output_event in tuple(
            self._output_work_cancel_events.items()
        ):
            if output_fence.matches(fence):
                output_event.set()
                output_cancelled = True
        event = self._generation_cancel_events.get(fence)
        if event is None:
            if self._generation_was_evicted(fence):
                return output_cancelled
            if fence in self._cancelled_generations:
                return True
            # Async-generator bodies run only when first iterated.  Retain a
            # pre-start cancellation request so a caller can cancel between
            # creating the stream and scheduling its first ``anext``.
            if fence not in self._generation_started:
                self._cancelled_generations.add(fence)
                self._remember_terminal_generation(fence)
                return True
            return output_cancelled
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
        for cancel_event in self._output_work_cancel_events.values():
            cancel_event.set()
        self._output_work_cancel_events.clear()
        if self.owns_speech_synthesis:
            close_speech = getattr(self.speech_synthesis, "aclose", None)
            if callable(close_speech):
                closed_speech = close_speech()
                if inspect.isawaitable(closed_speech):
                    await closed_speech
        if self.owns_language_model:
            close_language_model = getattr(self.language_model, "aclose", None)
            if callable(close_language_model):
                closed_language_model = close_language_model()
                if inspect.isawaitable(closed_language_model):
                    await closed_language_model
        if self._asr is not None:
            await self._asr.aclose()
            self._asr = None
