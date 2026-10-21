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
import logging
import os
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from services.agent.src.asr_text_debug_log import (
    AsrTextDebugSubjects,
    asr_text_debug_line,
    excerpt,
    subject_tag,
)
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
from services.agent.src.voice_core.media_session_types import ProviderAudioTaskSnapshot
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.common.fixed_safety_scripts import (
    FixedSafetyScript,
    SpokenPhrase,
    log_fixed_safety_playout,
)

if TYPE_CHECKING:
    from services.agent.src.config import AgentSettings
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.media_session import MediaReplyChunk

logger = logging.getLogger(__name__)

# An approved fixed safety script is spoken from one whole-sentence synthesis so a
# provider fault can never truncate it mid-script, and the exact audio is kept per
# voice for the process: a later failure replays it instead of leaving the child in
# silence (C1, 2026-10-21 03:51:53Z: the first Doubao attempt failed and the retry
# only produced audio 1.9 s later).
_FIXED_SAFETY_SYNTHESIS_ATTEMPTS = 2
_FIXED_SAFETY_CACHE_MAX_ENTRIES = 6
_FIXED_SAFETY_PCM_CACHE: dict[tuple[str, str], bytes] = {}


def _remember_fixed_safety_pcm(key: tuple[str, str], pcm: bytes) -> None:
    """Keep the most recently rendered approved script audio, bounded.

    Only fixed-safety copy ever lands here (no child audio, no model text), and
    the key carries the bound voice so a different voice re-synthesizes.
    """

    _FIXED_SAFETY_PCM_CACHE[key] = pcm
    while len(_FIXED_SAFETY_PCM_CACHE) > _FIXED_SAFETY_CACHE_MAX_ENTRIES:
        _FIXED_SAFETY_PCM_CACHE.pop(next(iter(_FIXED_SAFETY_PCM_CACHE)))


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
    fixed_safety_opening_budget_s: float = 5.0

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
        if self.fixed_safety_opening_budget_s <= 0:
            raise ValueError("fixed safety opening budget must be positive")


ASRSessionFactory = Callable[[], FunASRSession]


@dataclass(slots=True)
class ExistingVoiceProviderAdapter:
    """Bridge the already-existing remote provider handlers to media-v1."""

    asr_session_factory: ASRSessionFactory
    language_model: LanguageModelHandler
    speech_synthesis: SpeechSynthesisHandler
    config: ExistingVoiceProviderConfig = field(default_factory=ExistingVoiceProviderConfig)
    metrics: MetricsRegistry | None = None
    owns_speech_synthesis: bool = False
    owns_language_model: bool = False
    # Test-period transcript logging, off unless ASR_TEXT_DEBUG_LOG_SUBJECTS
    # lists the session's subject or device. Never changes what is committed.
    asr_text_debug_subjects: AsrTextDebugSubjects | None = None
    _asr: FunASRSession | None = field(default=None, init=False)
    _asr_text_debug: AsrTextDebugSubjects | None = field(default=None, init=False)
    _stream_epoch: int = field(default=0, init=False)
    _asr_task_epoch_offset: int = field(default=0, init=False)
    _asr_task_epoch_floor: int = field(default=0, init=False)
    # The provider session is intentionally replaceable on transport failure.
    # Keep the last exported global task epoch separately from the live ASR
    # object: recovery can briefly clear ``_asr`` before the replacement task
    # is connected, and the next task must still be newer than the Voice Core
    # supervisor's already-observed epoch.
    _last_global_asr_task_epoch: int = field(default=0, init=False)
    _audio_since_finalize: bool = field(default=False, init=False)
    _asr_task_contexts: dict[str, tuple[int, int, int]] = field(
        default_factory=dict,
        init=False,
    )
    _asr_task_order: deque[str] = field(default_factory=deque, init=False)
    # begin_ms is mutable provider content, not sentence identity. Keeping it
    # out of this key makes revisions monotonic when a provider corrects the
    # sentence start in the same task.
    _sentence_revisions: dict[tuple[int, int, str], int] = field(
        default_factory=dict,
        init=False,
    )
    _sentence_revision_order: deque[tuple[int, int, str]] = field(
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
    _generation_eviction_floor: tuple[int, int, int, int] | None = field(
        default=None,
        init=False,
    )
    _active_speech_streams: dict[GenerationFence, Any] = field(default_factory=dict, init=False)
    _fixed_safety_fences: set[GenerationFence] = field(default_factory=set, init=False)
    _generation_output_ends: dict[GenerationFence, int] = field(
        default_factory=dict,
        init=False,
    )
    # Direct intent sources are independent pieces of work inside one logical
    # generation. They cannot share the conversation reply's one-shot key.
    _output_work_started: set[tuple[GenerationFence, str]] = field(
        default_factory=set,
        init=False,
    )
    _output_work_history: deque[tuple[GenerationFence, str]] = field(
        default_factory=deque,
        init=False,
    )
    _output_work_cancel_events: dict[tuple[GenerationFence, str], asyncio.Event] = field(
        default_factory=dict,
        init=False,
    )
    _output_sample: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)

    def prewarm(self) -> Any:
        """Use the provider's native fast-model prewarm without starting a reply."""

        prewarm = getattr(self.language_model, "prewarm", None)
        return prewarm() if callable(prewarm) else None

    @property
    def supports_delegation(self) -> bool:
        return (
            bool(getattr(self.language_model, "supports_delegation", True))
            and callable(getattr(self.language_model, "start_delegation", None))
            and callable(getattr(self.language_model, "accept_output_intent", None))
        )

    @property
    def current_asr_task_epoch(self) -> int:
        """Return the provider task that has successfully connected."""

        return self._global_asr_task_epoch(self._asr) if self._asr is not None else 0

    @property
    def asr_rotation_pending(self) -> bool:
        """Whether the next provider task is deliberately waiting for PCM."""

        return bool(
            self._asr is not None
            and getattr(self._asr, "rotation_pending", False)
        )

    @property
    def current_asr_audio_task_snapshot(self) -> ProviderAudioTaskSnapshot | None:
        """Expose provider-confirmed PCM without leaking the concrete ASR session."""

        asr = self._asr
        if asr is None:
            return None
        task_epoch = self._global_asr_task_epoch(asr)
        task_sample_origin = int(asr.task_sample_origin)
        audio_start = getattr(asr, "task_audio_start_sample", None)
        audio_end = getattr(asr, "task_audio_end_sample", None)
        send_count = int(getattr(asr, "task_audio_send_count", 0))
        if audio_start is None or audio_end is None or send_count < 1:
            audio_start = None
            audio_end = None
            send_count = 0
        return ProviderAudioTaskSnapshot(
            task_epoch=task_epoch,
            task_sample_origin=task_sample_origin,
            audio_start_sample=audio_start,
            audio_end_sample=audio_end,
            send_count=send_count,
            encoding="pcm_s16le",
            sample_rate_hz=self.config.sample_rate,
            channels=1,
            observed_sample_count=int(getattr(asr, "task_pcm_sample_count", 0)),
            peak_abs=getattr(asr, "task_pcm_peak_abs", None),
            rms=getattr(asr, "task_pcm_rms", None),
            all_zero=(
                getattr(asr, "task_pcm_peak_abs", None) == 0
                if int(getattr(asr, "task_pcm_sample_count", 0)) > 0
                else None
            ),
            clipping_detected=(
                getattr(asr, "task_pcm_peak_abs", None) in (32_767, 32_768)
                if int(getattr(asr, "task_pcm_sample_count", 0)) > 0
                else None
            ),
        )

    @property
    def supports_turn_preparation(self) -> bool:
        return callable(getattr(self.language_model, "prepare_committed_turn", None))

    def start_delegation(self, text: str, fence: GenerationFence) -> Any:
        """Start deep work through the injected full orchestration handler."""

        starter = getattr(self.language_model, "start_delegation", None)
        if not callable(starter):
            raise RuntimeError("orchestrated language-model handler has no delegation seam")
        return starter(text, fence)

    def accept_output_intent(self, intent: Any) -> Any:
        acceptor = getattr(self.language_model, "accept_output_intent", None)
        if not callable(acceptor):
            raise RuntimeError("orchestrated language-model handler has no output-intent seam")
        return acceptor(intent)

    def warm_committed_turn(self, identity: SessionIdentity, text: str) -> None:
        """Tell the shared Agent which sentence is about to be committed, while its grace still runs.

        The Agent starts what its preparation would otherwise wait for (the bound person's memory); the
        later ``prepare_committed_turn`` uses it only for the same sentence.  A language model without
        the seam simply prepares as before.
        """

        warm = getattr(self.language_model, "warm_committed_turn", None)
        if callable(warm):
            warm(text)

    async def prepare_committed_turn(
        self,
        identity: SessionIdentity,
        text: str,
    ) -> GenerationFence:
        """Let the shared Agent freeze policy, context and response plan once."""

        prepare = getattr(self.language_model, "prepare_committed_turn", None)
        if not callable(prepare):
            raise RuntimeError("orchestrated language-model handler cannot prepare a turn")
        prepared = prepare(text)
        fence = await prepared if inspect.isawaitable(prepared) else prepared
        if not isinstance(fence, GenerationFence) or fence.session_id != identity.session_id:
            raise RuntimeError("orchestrated language-model handler returned an invalid fence")
        return fence

    def _global_asr_task_epoch(self, asr: FunASRSession) -> int:
        value = self._asr_task_epoch_offset + max(1, int(asr.task_epoch))
        self._last_global_asr_task_epoch = max(self._last_global_asr_task_epoch, value)
        return value

    def set_asr_task_epoch_floor(self, task_epoch: int) -> None:
        """Align a replacement provider with the Voice Core task fence."""

        if isinstance(task_epoch, bool) or task_epoch < 0:
            raise ValueError("ASR task epoch floor must be a non-negative integer")
        self._asr_task_epoch_floor = max(
            self._asr_task_epoch_floor,
            task_epoch,
            self._last_global_asr_task_epoch,
        )
        self._asr_task_epoch_offset = max(
            self._asr_task_epoch_offset,
            self._asr_task_epoch_floor,
        )

    def _clear_asr_event_context(self) -> None:
        self._asr_task_contexts.clear()
        self._asr_task_order.clear()
        self._sentence_revisions.clear()
        self._sentence_revision_order.clear()

    async def _reset_asr_session(self, stream_epoch: int) -> None:
        """Discard provider state so ``stream_epoch`` owns a new sample origin."""

        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        previous = self._asr
        if previous is not None:
            self._asr_task_epoch_floor = max(
                self._asr_task_epoch_floor,
                self._global_asr_task_epoch(previous),
            )
            close = getattr(previous, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        self._asr = None
        self._stream_epoch = stream_epoch
        self._asr_task_epoch_floor = max(
            self._asr_task_epoch_floor,
            self._last_global_asr_task_epoch,
        )
        self._asr_task_epoch_offset = self._asr_task_epoch_floor
        self._audio_since_finalize = False
        self._clear_asr_event_context()

    async def reset_for_stream_epoch(self, identity: SessionIdentity) -> None:
        """Fence a transport epoch before its first PCM frame is accepted."""

        stream_epoch = identity.stream_epoch
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        if stream_epoch == self._stream_epoch:
            return
        await self._reset_asr_session(stream_epoch)

    async def recover_after_failure(self, identity: SessionIdentity) -> None:
        """Replace a failed provider task before the next frame is processed."""

        if identity.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self._stream_epoch and identity.stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        await self._reset_asr_session(identity.stream_epoch)

    async def _ensure_asr(
        self, stream_epoch: int, identity: SessionIdentity | None = None
    ) -> FunASRSession:
        if self._closed:
            raise RuntimeError("media provider is closed")
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self._asr is None:
            if stream_epoch < self._stream_epoch:
                raise ValueError("ASR stream epoch moved backwards")
            if stream_epoch > self._stream_epoch:
                await self._reset_asr_session(stream_epoch)
            self._asr = self.asr_session_factory()
            await self._asr.connect()
            self._stream_epoch = stream_epoch
            self._remember_asr_task(self._asr, stream_epoch)
        elif stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        elif stream_epoch > self._stream_epoch:
            await self._reset_asr_session(stream_epoch)
            return await self._ensure_asr(stream_epoch, identity)
        # Every provider session the bridge creates follows the whitelist, so
        # a factory the deployment built keeps logging the same sessions.
        self._asr_text_debug = self._asr_text_debug_for(identity)
        self._asr.asr_text_debug = self._asr_text_debug
        if identity is not None:
            # The provider logs its own finals without this adapter around it.
            self._asr.asr_text_debug_session_id = identity.session_id
            self._asr.asr_text_debug_subject_ids = (
                identity.subject_id,
                identity.device_id,
            )
        return self._asr

    def _asr_text_debug_for(self, identity: SessionIdentity | None) -> AsrTextDebugSubjects:
        """The subjects whose ASR text this adapter's provider session may log."""

        subjects = self.asr_text_debug_subjects
        if subjects is None or identity is None:
            return AsrTextDebugSubjects()
        return subjects.for_session(identity.subject_id, identity.device_id)

    def _log_asr_text_debug_decision(
        self,
        identity: SessionIdentity,
        result: ASRResult,
        *,
        source: str,
    ) -> None:
        """The final as the bridge normalized it, next to the playback-stop verdict.

        The line also covers an empty final (``text_len=0``): a 「停」 that
        FunASR dropped never reaches the stop guards visible at all.
        """

        # The mapped line stands for the audio this adapter delivered, so it
        # is written where the audio runs, never only where a provider was made.
        subjects = self.asr_text_debug_subjects
        if subjects is None or not subjects.enabled:
            return
        boundary = getattr(identity, "last_playback_end_sample", None)
        origin = boundary or 0
        start_ms = (result.capture_start_sample - origin) * 1000 // self.config.sample_rate
        end_ms = (result.capture_end_sample - origin) * 1000 // self.config.sample_rate
        line = asr_text_debug_line(
            subjects,
            identity.subject_id,
            identity.device_id,
            source=source,
            session_id=identity.session_id,
            subject_sha256_8=subject_tag(identity.subject_id),
            device_sha256_8=subject_tag(identity.device_id),
            stream_epoch=result.stream_epoch,
            task_epoch=result.task_epoch,
            sentence_id=result.sentence_id,
            final=result.is_final,
            empty=not result.text.strip(),
            text_len=len(result.text),
            begin_ms=result.provider_begin_ms if result.provider_begin_ms is not None else start_ms,
            end_ms=result.provider_end_ms if result.provider_end_ms is not None else end_ms,
            begin_sample=result.capture_start_sample,
            end_sample=result.capture_end_sample,
            playback_boundary_sample=boundary,
            text=excerpt(result.text) if result.text else None,
        )
        if line is not None:
            logger.info(line)

    def _remember_asr_task(self, asr: FunASRSession, stream_epoch: int) -> None:
        task_id = str(getattr(asr, "task_id", "") or "")
        if task_id:
            if task_id not in self._asr_task_contexts:
                self._asr_task_order.append(task_id)
            self._asr_task_contexts[task_id] = (
                stream_epoch,
                self._global_asr_task_epoch(asr),
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
        asr = await self._ensure_asr(identity.stream_epoch, identity)
        self._remember_asr_task(asr, identity.stream_epoch)
        await asr.send_pcm(
            frame.payload,
            capture_start_sample=frame.capture_start_sample,
        )
        self._audio_since_finalize = True
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
                raise RuntimeError(f"FunASR task failed error_code={event.error_code or 'unknown'}")
            if event.event != "result-generated" or event.sentence is None:
                continue
            result = self._map_asr_event(asr, event, identity.stream_epoch)
            if result is not None:
                results.append(result)
        return tuple(results)

    async def finalize_speech_segment(
        self,
        identity: SessionIdentity,
    ) -> tuple[ASRResult, ...]:
        """Close one VAD task and arm the next task for its first PCM frame."""

        if self._asr is None or not self._audio_since_finalize:
            return ()
        asr = await self._ensure_asr(identity.stream_epoch, identity)
        previous_task_id = str(getattr(asr, "task_id", "") or "")
        if not previous_task_id:
            raise RuntimeError("FunASR segment boundary has no active task id")
        self._remember_asr_task(asr, identity.stream_epoch)
        await asr.rotate_task(require_consumed=False)
        self._remember_asr_task(asr, identity.stream_epoch)

        results: list[ASRResult] = []
        boundary_seen = False
        final_seen = False
        tail_deadline: float | None = None
        tail_grace_s = float(
            getattr(getattr(asr, "config", None), "post_finish_tail_grace_s", 0.0)
        )
        # Audio ingestion continuously drains provider events. The larger
        # history bound is a fail-closed ceiling for the small tail that can
        # remain between the final PCM frame and task-finished.
        for _ in range(self.config.max_asr_result_history):
            if asr.events.empty():
                if not boundary_seen or final_seen:
                    break
                loop = asyncio.get_running_loop()
                if tail_deadline is None:
                    tail_deadline = loop.time() + tail_grace_s
                remaining = tail_deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    event = await asyncio.wait_for(asr.events.get(), timeout=remaining)
                except TimeoutError:
                    break
            else:
                event = asr.events.get_nowait()
            if event.event == "task-failed":
                raise RuntimeError(f"FunASR task failed error_code={event.error_code or 'unknown'}")
            if event.event == "result-generated" and event.sentence is not None:
                result = self._map_asr_event(asr, event, identity.stream_epoch)
                if result is not None:
                    self._log_asr_text_debug_decision(identity, result, source="segment_final")
                    results.append(result)
                    if event.task_id == previous_task_id and result.is_final:
                        final_seen = True
            if event.event == "task-finished" and event.task_id == previous_task_id:
                boundary_seen = True
                tail_deadline = (
                    asyncio.get_running_loop().time()
                    + tail_grace_s
                )
        if not boundary_seen:
            raise RuntimeError("FunASR task boundary event was not available to the media adapter")
        self._audio_since_finalize = False
        return tuple(results)

    async def reset_after_discontinuity(
        self,
        identity: SessionIdentity,
        *,
        capture_start_sample: int,
    ) -> None:
        """Fence a dropped input interval before accepting newer audio."""

        if capture_start_sample < 0:
            raise ValueError("discontinuity sample must be non-negative")
        had_active_asr = self._asr is not None
        asr = await self._ensure_asr(identity.stream_epoch)
        asr.mark_committed_sample(capture_start_sample)
        # Failure recovery has already discarded the poisoned provider task. A
        # fresh session adopts the next frame's absolute sample origin on first
        # send and must not immediately reconnect a second time.
        if had_active_asr:
            await asr.reconnect_with_replay()
        self._audio_since_finalize = False
        self._clear_asr_event_context()
        self._remember_asr_task(asr, identity.stream_epoch)

    async def pause_asr_for_playback(self, identity: SessionIdentity) -> None:
        """Close the current ASR task when playback starts to avoid idle timeout.

        Half-duplex playback stops capture, so FunASR would idle-timeout after
        23 seconds. Rotating the task here prevents that; a new task starts
        when capture resumes. interrupt_assist callers must not invoke this.
        """

        if self._asr is None or not self._audio_since_finalize:
            return
        asr = await self._ensure_asr(identity.stream_epoch)
        previous_task_id = str(getattr(asr, "task_id", "") or "")
        if not previous_task_id:
            return
        self._remember_asr_task(asr, identity.stream_epoch)
        await asr.rotate_task(require_consumed=False)
        self._remember_asr_task(asr, identity.stream_epoch)
        self._audio_since_finalize = False
        logger.info(
            "Rotated ASR task on playback start: task_id=%s stream_epoch=%s",
            previous_task_id,
            identity.stream_epoch,
        )

    def _map_asr_event(
        self,
        asr: FunASRSession,
        event: FunASRServerEvent,
        stream_epoch: int,
    ) -> ASRResult | None:
        sentence = event.sentence
        if sentence is None:
            return None
        if sentence.heartbeat and not sentence.text:
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
                logger.warning(
                    "media ASR event dropped: task context evicted task_id=%s "
                    "current_task_id=%s stream_epoch=%s sentence_end=%s",
                    event.task_id,
                    current_task_id,
                    stream_epoch,
                    sentence.sentence_end,
                )
                return None
            self._remember_asr_task(asr, stream_epoch)
            task_context = (
                stream_epoch,
                self._global_asr_task_epoch(asr),
                asr.task_sample_origin,
            )
        event_stream_epoch, task_epoch, sample_offset = task_context
        revision_key = (event_stream_epoch, task_epoch, sentence_id)
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
        if not mapped.is_final and self.metrics is not None:
            latest_sample = max(
                mapped.capture_end_sample,
                int(getattr(asr, "last_sent_sample", mapped.capture_end_sample)),
            )
            self.metrics.set_media_metric(
                "asr_partial_age_ms",
                (latest_sample - mapped.capture_end_sample) * 1000.0 / self.config.sample_rate,
            )
        return mapped

    @property
    def output_frame_samples(self) -> int:
        """Number of PCM samples in one fixed 20 ms output frame."""

        return self.config.output_sample_rate * self.config.output_frame_ms // 1000

    @property
    def output_sample_rate(self) -> int:
        """PCM sample rate of every output frame on the downlink."""

        return self.config.output_sample_rate

    def _record_tts_frame_age(self, received_at: float | None) -> None:
        if received_at is not None and self.metrics is not None:
            self.metrics.set_media_metric(
                "tts_frame_age_ms",
                (monotonic() - received_at) * 1000.0,
            )

    async def _reply_phrases(
        self,
        user_text: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[SpokenPhrase, None]:
        segmenter = PhraseSegmenter(fence=fence)
        llm_stream = self.language_model.stream(
            LanguageModelRequest(user_text=user_text, cancellation=cancellation)
        )
        from services.common.labelled_lines import labelled_catalogue

        catalogue = labelled_catalogue()
        try:
            async for item in self._cancel_aware_items(llm_stream, cancel_event):
                if isinstance(item, FixedSafetyScript):
                    self._fixed_safety_fences.add(fence)
                    log_fixed_safety_playout(logger, item)
                    for phrase in item.phrases:
                        if cancel_event.is_set():
                            return
                        yield phrase
                    continue
                if not item:
                    continue
                if isinstance(item, str) and catalogue.by_text(item) is not None:
                    for segment in segmenter.flush(end_of_stream=True):
                        if cancel_event.is_set():
                            return
                        yield SpokenPhrase.verbatim(segment.text)
                    segmenter.reset(fence)
                    yield SpokenPhrase.verbatim(item.strip())
                    continue
                for segment in segmenter.push_token(item):
                    yield SpokenPhrase.verbatim(segment.text)
        finally:
            close_llm = getattr(llm_stream, "aclose", None)
            if callable(close_llm):
                closed_llm = close_llm()
                if inspect.isawaitable(closed_llm):
                    await closed_llm
        for segment in segmenter.flush(end_of_stream=True):
            if cancel_event.is_set():
                return
            yield SpokenPhrase.verbatim(segment.text)

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

    async def _synthesize_phrase(
        self,
        phrase: SpokenPhrase,
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

        display = phrase.display
        frame_samples = self.output_frame_samples
        frame_bytes = frame_samples * 2
        buffered = bytearray()
        held_frame: bytes | None = None
        held_frame_received_at: float | None = None
        output_sample = source_start_sample
        provider_pcm = self._provider_pcm_chunks(
            phrase.spoken,
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
                        self._record_tts_frame_age(held_frame_received_at)
                        yield MediaReplyChunk(
                            pcm_s16le=held_frame,
                            source_start_sample=output_sample,
                            assistant_text_delta=(
                                display if output_sample == source_start_sample else ""
                            ),
                            first=first and output_sample == source_start_sample,
                        )
                        if cancel_event.is_set():
                            return
                        output_sample += frame_samples
                        self._output_sample = output_sample
                    held_frame = next_frame
                    held_frame_received_at = monotonic()
        finally:
            await provider_pcm.aclose()
        if cancel_event.is_set():
            return
        if len(buffered) % 2:
            raise RuntimeError("speech provider returned invalid 16-bit PCM")
        if buffered:
            if held_frame is not None:
                self._record_tts_frame_age(held_frame_received_at)
                yield MediaReplyChunk(
                    pcm_s16le=held_frame,
                    source_start_sample=output_sample,
                    assistant_text_delta=(display if output_sample == source_start_sample else ""),
                    first=first and output_sample == source_start_sample,
                )
                if cancel_event.is_set():
                    return
                output_sample += frame_samples
                self._output_sample = output_sample
            held_frame = bytes(buffered) + b"\x00" * (frame_bytes - len(buffered))
            held_frame_received_at = monotonic()
        if held_frame is None:
            raise RuntimeError("speech provider returned no valid PCM")
        phrase_audio_end = output_sample + frame_samples
        self._record_tts_frame_age(held_frame_received_at)
        yield MediaReplyChunk(
            pcm_s16le=held_frame,
            source_start_sample=output_sample,
            text=display,
            assistant_text_delta=(display if output_sample == source_start_sample else ""),
            first=first and output_sample == source_start_sample,
            final=final,
            text_audio_start_sample=source_start_sample,
            text_audio_end_sample=phrase_audio_end,
        )
        self._output_sample = phrase_audio_end

    async def _emit_labelled_line(
        self,
        line: Any,
        *,
        display: str,
        fence: GenerationFence,
        source_start_sample: int,
        first: bool,
        final: bool,
    ) -> AsyncIterator[MediaReplyChunk]:
        from services.agent.src.voice_core.media_session import MediaReplyChunk, MediaTextSpan

        frame_samples = self.output_frame_samples
        audio_start = source_start_sample
        last: MediaReplyChunk | None = None
        for pcm, start, is_first, is_final, text_delta in line.frame_pcm(
            source_start_sample=source_start_sample,
            frame_samples=frame_samples,
            announce_text=True,
        ):
            audio_end = start + frame_samples
            chunk = MediaReplyChunk(
                pcm_s16le=pcm,
                source_start_sample=start,
                text=display if is_final else "",
                assistant_text_delta=text_delta,
                first=first and is_first,
                final=False,
                text_audio_start_sample=audio_start if is_final else None,
                text_audio_end_sample=audio_end if is_final else None,
                text_spans=(
                    (MediaTextSpan(display, audio_start, audio_end),) if is_final else ()
                ),
            )
            if last is not None:
                yield last
            last = chunk
        if last is None:
            raise RuntimeError(f"labelled line {line.line_id} produced no PCM frames")
        yield replace(last, final=final)
        self._output_sample = last.source_start_sample + frame_samples
        self._generation_output_ends[fence] = self._output_sample

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
        exposed until the provider's final subtitle alignment; without that
        alignment this seam records one conservative whole-generation ledger
        span, so an interruption can never over-claim a partly rendered phrase.
        """

        from services.agent.src.voice_core.media_session import MediaReplyChunk, MediaTextSpan
        from services.common.labelled_lines import labelled_catalogue

        catalogue = labelled_catalogue()
        reply = self._reply_phrases(user_text, fence, cancellation, cancel_event)
        reply_iter = aiter(reply)
        try:
            first_phrase = await anext(reply_iter)
        except StopAsyncIteration:
            await reply.aclose()
            return

        if fence in self._fixed_safety_fences:
            script_phrases = [first_phrase]
            try:
                async for phrase in reply_iter:
                    script_phrases.append(phrase)
            finally:
                await reply.aclose()
            async for chunk in self._stream_fixed_safety_script(
                script_phrases,
                fence,
                cancellation,
                cancel_event,
            ):
                yield chunk
            return

        sample_origin = 0
        if catalogue.prefix_candidate(first_phrase.spoken):
            labelled_phrases: list[SpokenPhrase] = [first_phrase]
            try:
                async for phrase in reply_iter:
                    labelled_phrases.append(phrase)
            finally:
                await reply.aclose()
            joined = "".join(item.spoken for item in labelled_phrases)
            joined_line = catalogue.by_text(joined)
            if joined_line is not None:
                logger.info(
                    "labelled line reply content_id=%s ai_generated=%s "
                    "session=%s turn=%s",
                    joined_line.aigc["content_id"],
                    joined_line.aigc["ai_generated"],
                    fence.session_id,
                    fence.turn_id,
                )
                self._generation_output_ends[fence] = 0
                async for chunk in self._emit_labelled_line(
                    joined_line,
                    display=joined_line.text,
                    fence=fence,
                    source_start_sample=sample_origin,
                    first=True,
                    final=True,
                ):
                    yield chunk
                return
            if all(catalogue.by_text(item.spoken) is not None for item in labelled_phrases):
                self._generation_output_ends[fence] = 0
                for index, phrase in enumerate(labelled_phrases):
                    line = catalogue.by_text(phrase.spoken)
                    assert line is not None
                    logger.info(
                        "labelled line reply content_id=%s ai_generated=%s "
                        "session=%s turn=%s",
                        line.aigc["content_id"],
                        line.aigc["ai_generated"],
                        fence.session_id,
                        fence.turn_id,
                    )
                    async for chunk in self._emit_labelled_line(
                        line,
                        display=phrase.display,
                        fence=fence,
                        source_start_sample=sample_origin,
                        first=index == 0,
                        final=index + 1 == len(labelled_phrases),
                    ):
                        sample_origin = chunk.source_start_sample + chunk.frame_samples
                        yield chunk
                return

            async def _replay_all() -> AsyncGenerator[SpokenPhrase, None]:
                for phrase in labelled_phrases:
                    yield phrase

            reply = _replay_all()
            reply_iter = aiter(reply)
            first_phrase = await anext(reply_iter)

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
        first_phrase_ready = asyncio.Event()
        trailing_labelled: list[tuple[Any, str]] = []

        async def push_phrase(phrase: SpokenPhrase) -> None:
            if cancel_event.is_set() or not phrase.spoken:
                return
            labelled = labelled_catalogue().by_text(phrase.spoken)
            if labelled is not None:
                phrases.append(phrase.display)
                trailing_labelled.append((labelled, phrase.display))
                first_phrase_ready.set()
                return
            phrases.append(phrase.display)
            pending_announcements.append(phrase.display)
            pushed = push_text(phrase.spoken)
            if inspect.isawaitable(pushed):
                await pushed
            first_phrase_ready.set()

        async def produce_text() -> None:
            try:
                await push_phrase(first_phrase)
                async for phrase in reply_iter:
                    await push_phrase(phrase)
            finally:
                await reply.aclose()
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
        held_frame_received_at: float | None = None
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
                        self._record_tts_frame_age(held_frame_received_at)
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
                    held_frame_received_at = monotonic()
            if cancel_event.is_set():
                return
            await producer
            if cancel_event.is_set():
                return
            if len(buffered) % 2:
                raise RuntimeError("speech provider returned invalid 16-bit PCM")
            if buffered:
                if held_frame is not None:
                    self._record_tts_frame_age(held_frame_received_at)
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
                held_frame_received_at = monotonic()
            complete_text = "".join(phrases)
            if held_frame is None and not trailing_labelled:
                if cancel_event.is_set():
                    return
                if not complete_text:
                    raise RuntimeError("language model returned no speakable text")
                raise RuntimeError("speech provider returned no valid PCM")
            if held_frame is not None:
                audio_end = output_sample + frame_samples
                announcement = take_announcement()
                timed_text_spans = await self._heard_text_spans(
                    fence,
                    stream,
                    max_audio_end_sample=audio_end,
                )
                if not timed_text_spans:
                    # No usable provider timing (none exposed, a degraded
                    # alignment or a malformed timeline): the whole reply is one
                    # ledger span over all of its audio.  Only an exact watermark
                    # at the last sample promotes it, so a fully played reply
                    # still becomes heard history while a stopped or
                    # approximately acknowledged one claims nothing.
                    alignment = getattr(stream, "timed_transcript_alignment", None)
                    logger.info(
                        "media reply without provider text timing; one whole-reply "
                        "ledger span session=%s turn_id=%s generation_id=%s alignment=%s",
                        fence.session_id,
                        fence.turn_id,
                        fence.generation_id,
                        alignment() if callable(alignment) else "unavailable",
                    )
                    tts_text = "".join(
                        part for part in phrases
                        if labelled_catalogue().by_text(part) is None
                    ) or complete_text
                    timed_text_spans = (MediaTextSpan(tts_text, 0, audio_end),)
                self._record_tts_frame_age(held_frame_received_at)
                yield MediaReplyChunk(
                    pcm_s16le=held_frame,
                    source_start_sample=output_sample,
                    assistant_text_delta=announcement,
                    first=output_sample == 0,
                    final=not trailing_labelled,
                    text_spans=timed_text_spans,
                )
                output_sample = audio_end
                self._output_sample = audio_end
                self._generation_output_ends[fence] = audio_end
            for index, (labelled, display) in enumerate(trailing_labelled):
                async for chunk in self._emit_labelled_line(
                    labelled,
                    display=display,
                    fence=fence,
                    source_start_sample=output_sample,
                    first=output_sample == 0,
                    final=index + 1 == len(trailing_labelled),
                ):
                    output_sample = chunk.source_start_sample + chunk.frame_samples
                    yield chunk
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

    async def _stream_fixed_safety_script(
        self,
        phrases: list[SpokenPhrase],
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Speak one approved safety script from a whole-sentence, cached synthesis.

        The crisis line is the one reply a child must hear in full.  Streaming it
        phrase by phrase starts sooner, but a provider fault after the first phrase
        leaves the rest unheard and cannot be retried without repeating words.  One
        whole-sentence request is synthesized before the first frame, retried when
        the provider fails, cached per voice, and replayed from that cache if the
        provider stays down (C1, 2026-10-21 03:51:53Z).  Nothing reaches the device
        until a whole take exists, so a Doubao drop after part of the line needs no
        device stop: the discarded partial never played, and the device starts the
        replacement once from sample zero.
        """

        spoken = "".join(phrase.spoken for phrase in phrases)
        pcm, source = await self._fixed_safety_script_pcm(
            spoken,
            fence,
            cancellation,
            cancel_event,
        )
        if cancel_event.is_set() or not pcm:
            return
        logger.info(
            "fixed safety speech ready source=%s text_len=%d bytes=%d session=%s turn=%s",
            source,
            len(spoken),
            len(pcm),
            fence.session_id,
            fence.turn_id,
        )
        async for chunk in self._emit_fixed_safety_pcm(
            pcm,
            display="".join(phrase.display for phrase in phrases),
            fence=fence,
        ):
            yield chunk

    async def _fixed_safety_script_pcm(
        self,
        spoken: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> tuple[bytes, str]:
        """Synthesize the whole approved script inside the five-second opening budget.

        The script is pushed to the bound speech engine as one unit, so a
        provider that loses the connection mid-unit still delivers the full line
        (the wrapper buffers each engine's take, drops a partial one, and speaks
        the whole text again on StepFun). The wrapper's fixed-safety cascade
        tries Doubao and then StepFun inside its own allowance; the deadline here
        bounds every synthesis attempt so a cold, unreachable provider cannot
        exceed the five-second crisis budget. A cached approved take is replayed
        immediately after the first failed attempt, because waiting for a second
        cascade would spend the budget the child's line needs. Only a complete
        take is cached, so a half line can never join a whole one.
        """

        key = (self._fixed_safety_voice_key(), spoken)
        cached = _FIXED_SAFETY_PCM_CACHE.get(key)
        deadline = monotonic() + self.config.fixed_safety_opening_budget_s
        failure: Exception | None = None
        for attempt in range(1, _FIXED_SAFETY_SYNTHESIS_ATTEMPTS + 1):
            if cancel_event.is_set():
                return b"", "cancelled"
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                pcm = await asyncio.wait_for(
                    self._synthesize_whole_script_once(
                        spoken,
                        fence,
                        cancellation,
                        cancel_event,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                pcm = b""
                logger.warning(
                    "fixed safety speech attempt exceeded the opening budget "
                    "attempt=%d/%d text_len=%d session=%s",
                    attempt,
                    _FIXED_SAFETY_SYNTHESIS_ATTEMPTS,
                    len(spoken),
                    fence.session_id,
                )
            if pcm:
                _remember_fixed_safety_pcm(key, pcm)
                return pcm, "synthesis"
            failure = RuntimeError("speech provider returned no audio")
            logger.warning(
                "fixed safety speech attempt produced no audio attempt=%d/%d text_len=%d "
                "session=%s",
                attempt,
                _FIXED_SAFETY_SYNTHESIS_ATTEMPTS,
                len(spoken),
                fence.session_id,
            )
            if cached is not None:
                break
        if cached is not None:
            logger.warning(
                "fixed safety speech replayed from cache text_len=%d bytes=%d session=%s",
                len(spoken),
                len(cached),
                fence.session_id,
            )
            return cached, "cache"
        raise RuntimeError("fixed safety script produced no audio") from failure

    async def _synthesize_whole_script_once(
        self,
        spoken: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> bytes:
        """One full attempt, with the engine that spoke it logged for provenance."""

        pcm = bytearray()
        try:
            async for chunk in self._provider_pcm_chunks(
                spoken,
                fence,
                cancellation,
                cancel_event,
            ):
                pcm.extend(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "fixed safety speech attempt failed text_len=%d error=%s session=%s",
                len(spoken),
                type(exc).__name__,
                fence.session_id,
            )
            return b""
        report = getattr(self.speech_synthesis, "report_sentence_from", None)
        if callable(report) and pcm:
            chosen = cast(Any, self.speech_synthesis).last_engine()
            report(chosen, index=0, switched=getattr(chosen, "provider", "") != "stepfun")
        return bytes(pcm)

    def _fixed_safety_voice_key(self) -> str:
        return "|".join(
            str(getattr(self.speech_synthesis, attribute, "") or "")
            for attribute in ("current_model", "current_voice", "current_voice_profile_id")
        )

    async def _emit_fixed_safety_pcm(
        self,
        pcm: bytes,
        *,
        display: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        from services.agent.src.voice_core.media_session import MediaReplyChunk, MediaTextSpan

        frame_samples = self.output_frame_samples
        frame_bytes = frame_samples * 2
        output_sample = 0
        offset = 0
        total = len(pcm)
        while offset < total:
            frame = pcm[offset : offset + frame_bytes]
            offset += frame_bytes
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            audio_end = output_sample + frame_samples
            is_last = offset >= total
            yield MediaReplyChunk(
                pcm_s16le=frame,
                source_start_sample=output_sample,
                text=display if is_last else "",
                assistant_text_delta=display if output_sample == 0 else "",
                first=output_sample == 0,
                final=is_last,
                text_audio_start_sample=0 if is_last else None,
                text_audio_end_sample=audio_end if is_last else None,
                text_spans=(MediaTextSpan(display, 0, audio_end),) if is_last else (),
            )
            output_sample = audio_end
            self._output_sample = output_sample
            self._generation_output_ends[fence] = output_sample

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

    from services.agent.src.observability.metrics import GLOBAL_METRICS
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
    asr_text_debug_subjects = AsrTextDebugSubjects.from_env()
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
        if not callable(getattr(language_model, "start_delegation", None)):
            raise ValueError("orchestrated language-model handler must expose start_delegation()")
        if not callable(getattr(language_model, "accept_output_intent", None)):
            raise ValueError(
                "orchestrated language-model handler must expose accept_output_intent()"
            )
        return ExistingVoiceProviderAdapter(
            asr_session_factory=lambda: FunASRSession(
                replace(asr_config),
                metrics=GLOBAL_METRICS,
                asr_text_debug_session_id=identity.session_id,
                asr_text_debug_subject_ids=(identity.subject_id, identity.device_id),
            ),
            language_model=language_model,
            asr_text_debug_subjects=asr_text_debug_subjects,
            speech_synthesis=cast(
                SpeechSynthesisHandler,
                DoubaoTTS(replace(tts_config), metrics=GLOBAL_METRICS),
            ),
            config=adapter_config,
            metrics=GLOBAL_METRICS,
            owns_speech_synthesis=True,
        )

    return factory


__all__ = [
    "ExistingVoiceProviderAdapter",
    "ExistingVoiceProviderConfig",
    "build_existing_provider_factory",
    "build_production_provider_factory",
]
