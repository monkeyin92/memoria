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
    max_events_per_audio_frame: int = 64

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.output_sample_rate <= 0:
            raise ValueError("provider sample rates must be positive")
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
            if sentence_id in self._final_sentence_ids:
                return None
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

    async def _synthesize_phrase(
        self,
        phrase: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
        *,
        first: bool,
        final: bool,
    ) -> MediaReplyChunk:
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

        chunk = MediaReplyChunk(
            pcm_s16le=pcm,
            source_start_sample=self._output_sample,
            text=phrase,
            first=first,
            final=final,
        )
        self._output_sample += chunk.frame_samples
        return chunk

    async def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Stream LLM phrases through the existing TTS handler."""

        _ = identity
        if self._closed:
            return
        cancellation = CancellationContext.capture(fence)
        cancel_event = asyncio.Event()
        # Source samples are relative to one response generation; the media
        # registry resets its downlink sequence at the same boundary.
        self._output_sample = 0
        segmenter = PhraseSegmenter(fence=fence)
        pending: list[str] = []
        async for token in self.language_model.stream(
            LanguageModelRequest(user_text=user_text, cancellation=cancellation)
        ):
            if not token:
                continue
            for segment in segmenter.push_token(token):
                pending.append(segment.text)
                phrase = pending.pop(0)
                yield await self._synthesize_phrase(
                    phrase,
                    fence,
                    cancellation,
                    cancel_event,
                    first=self._output_sample == 0,
                    final=False,
                )
        for segment in segmenter.flush(end_of_stream=True):
            pending.append(segment.text)
        for index, phrase in enumerate(pending):
            yield await self._synthesize_phrase(
                phrase,
                fence,
                cancellation,
                cancel_event,
                first=self._output_sample == 0,
                final=index == len(pending) - 1,
            )

    async def close(self, identity: SessionIdentity) -> None:
        _ = identity
        self._closed = True
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
