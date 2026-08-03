"""Adapters for the existing remote ASR/LLM/TTS providers.

The media registry intentionally depends on a small provider protocol.  This
module is the concrete, provider-facing adapter used by a deployment that
wants to reuse Memoria's existing FunASR, language-model and Doubao handler
objects.  It does not create a second orchestration stack or embed a vendor
SDK in the media bridge.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    from services.agent.src.voice_core.media_session import MediaReplyChunk


@dataclass(frozen=True, slots=True)
class ExistingVoiceProviderConfig:
    """Bounds applied while adapting the existing provider handlers."""

    sample_rate: int = 16_000
    output_sample_rate: int = 24_000
    output_frame_ms: int = 20
    max_events_per_audio_frame: int = 64

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


ASRSessionFactory = Callable[[], FunASRSession]


@dataclass(slots=True)
class ExistingVoiceProviderAdapter:
    """Bridge the already-existing remote provider handlers to media-v1."""

    asr_session_factory: ASRSessionFactory
    language_model: LanguageModelHandler
    speech_synthesis: SpeechSynthesisHandler
    config: ExistingVoiceProviderConfig = field(default_factory=ExistingVoiceProviderConfig)
    _asr: FunASRSession | None = field(default=None, init=False)
    _stream_epoch: int = field(default=0, init=False)
    _sentence_revisions: dict[str, int] = field(default_factory=dict, init=False)
    _final_sentence_ids: set[str] = field(default_factory=set, init=False)
    _generation_started: set[GenerationFence] = field(default_factory=set, init=False)
    _generation_cancel_events: dict[GenerationFence, asyncio.Event] = field(
        default_factory=dict,
        init=False,
    )
    _cancelled_generations: set[GenerationFence] = field(default_factory=set, init=False)
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
        elif stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        elif stream_epoch > self._stream_epoch:
            await self._asr.reconnect_with_replay()
            self._stream_epoch = stream_epoch
            # FunASR sentence ids are scoped to a task/connection.  Keeping a
            # final-id set across a reconnect can suppress a valid sentence
            # that happens to reuse the same provider id.
            self._sentence_revisions.clear()
            self._final_sentence_ids.clear()
        return self._asr

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
        await asr.send_pcm(
            frame.payload,
            capture_start_sample=frame.capture_start_sample,
        )
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
        sentence_id = str(sentence.sentence_id)
        if sentence.sentence_end and sentence_id in self._final_sentence_ids:
            return None
        revision = self._sentence_revisions.get(sentence_id, 0) + 1
        self._sentence_revisions[sentence_id] = revision
        mapped = sentence_to_asr_result(
            sentence,
            task_epoch=max(1, asr.task_epoch),
            sample_rate=self.config.sample_rate,
            revision=revision,
            stream_epoch=stream_epoch,
            sample_offset=asr.task_sample_origin,
        )
        if sentence.sentence_end:
            self._final_sentence_ids.add(sentence_id)
        return ASRResult(
            task_epoch=mapped.task_epoch,
            sentence_id=mapped.sentence_id,
            revision=mapped.revision,
            capture_start_sample=mapped.capture_start_sample,
            capture_end_sample=mapped.capture_end_sample,
            text=mapped.text,
            is_final=mapped.is_final,
            confidence=mapped.confidence,
            provider_begin_ms=mapped.provider_begin_ms,
            provider_end_ms=mapped.provider_end_ms,
            stream_epoch=mapped.stream_epoch,
        )

    @property
    def output_frame_samples(self) -> int:
        """Number of PCM samples in one fixed 20 ms output frame."""

        return self.config.output_sample_rate * self.config.output_frame_ms // 1000

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
        """Synthesize one phrase and yield bounded, fixed-size PCM frames.

        Existing ``SpeechSynthesisHandler`` implementations return a PCM
        buffer for a phrase.  The adapter deliberately never forwards that
        buffer as one media frame: it slices it into 20 ms/24 kHz frames and
        yields one frame at a time, giving cancellation and downstream
        backpressure a scheduling point between frames.  A short tail is
        zero-padded so every emitted frame has the negotiated fixed size.
        """

        if cancel_event.is_set():
            return
        result = await self.speech_synthesis.synthesize(
            SpeechSynthesisRequest(
                phrases=(phrase,),
                cancellation=cancellation,
                cancel_event=cancel_event,
            )
        )
        pcm = bytes(getattr(result, "pcm", b"") or b"")
        if not pcm or len(pcm) % 2:
            raise RuntimeError("speech provider returned no valid PCM")
        from services.agent.src.voice_core.media_session import MediaReplyChunk

        frame_samples = self.output_frame_samples
        frame_bytes = frame_samples * 2
        phrase_frame_count = (len(pcm) + frame_bytes - 1) // frame_bytes
        phrase_audio_end = source_start_sample + phrase_frame_count * frame_samples
        output_sample = source_start_sample
        for offset in range(0, len(pcm), frame_bytes):
            if cancel_event.is_set():
                return
            frame_pcm = pcm[offset : offset + frame_bytes]
            if len(frame_pcm) < frame_bytes:
                frame_pcm += b"\x00" * (frame_bytes - len(frame_pcm))
            is_first = first and offset == 0
            is_final = final and offset + frame_bytes >= len(pcm)
            # ``text`` is attached to the first frame of a phrase.  The
            # media-session span writer can use the frame range metadata to
            # map that text across all frames without duplicating it.
            chunk = MediaReplyChunk(
                pcm_s16le=frame_pcm,
                source_start_sample=output_sample,
                text=phrase if is_first else "",
                first=is_first,
                final=is_final,
                text_audio_start_sample=(source_start_sample if is_first else None),
                text_audio_end_sample=(phrase_audio_end if is_first else None),
            )
            output_sample += frame_samples
            self._output_sample = output_sample
            yield chunk
            # Async-generator suspension is the backpressure boundary.  The
            # explicit yield-to-loop also lets a cancellation request arrive
            # before the next expensive provider call.
            await asyncio.sleep(0)

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
        if fence in self._generation_started or fence in self._cancelled_generations:
            return
        self._generation_started.add(fence)
        cancellation = CancellationContext.capture(fence)
        cancel_event = asyncio.Event()
        self._generation_cancel_events[fence] = cancel_event
        # Source samples are relative to one response generation; keep this
        # local so interleaved/stale generations cannot reset one another.
        output_sample = 0
        self._output_sample = 0
        segmenter = PhraseSegmenter(fence=fence)
        pending: list[str] = []
        try:
            async for token in self.language_model.stream(
                LanguageModelRequest(user_text=user_text, cancellation=cancellation)
            ):
                if cancel_event.is_set():
                    return
                if not token:
                    continue
                for segment in segmenter.push_token(token):
                    pending.append(segment.text)
                    # Keep one phrase in hand so a stream that ends directly
                    # after punctuation can mark its final PCM frame.  Once
                    # the next phrase arrives, the older one is known to be
                    # non-final and can flow immediately.
                    while len(pending) > 1:
                        phrase = pending.pop(0)
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
                            yield chunk
            for segment in segmenter.flush(end_of_stream=True):
                pending.append(segment.text)
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
                    yield chunk
        finally:
            # Keep the event in the map for a late cancellation call; this is
            # cheap bounded metadata compared with PCM and makes cancellation
            # idempotent after the provider has completed.
            cancel_event.set()

    def cancel_generation(self, fence: GenerationFence) -> bool:
        """Request cooperative cancellation of one generation output stream."""

        event = self._generation_cancel_events.get(fence)
        if event is None:
            # Async-generator bodies run only when first iterated.  Retain a
            # pre-start cancellation request so a caller can cancel between
            # creating the stream and scheduling its first ``anext``.
            if fence not in self._generation_started:
                self._cancelled_generations.add(fence)
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


__all__ = [
    "ExistingVoiceProviderAdapter",
    "ExistingVoiceProviderConfig",
    "build_existing_provider_factory",
]
