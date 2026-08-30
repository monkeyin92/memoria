"""Provider-neutral values and factory contracts for Media Voice sessions."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.speech_timeline import ASRResult


@dataclass(frozen=True, slots=True)
class MediaTextSpan:
    """Provider-aligned text and its authoritative audio interval."""

    text: str
    audio_start_sample: int
    audio_end_sample: int

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("media text span must not be empty")
        if self.audio_start_sample < 0 or self.audio_end_sample <= self.audio_start_sample:
            raise ValueError("media text span audio range must be positive")


@dataclass(frozen=True, slots=True)
class MediaReplyChunk:
    """One provider-produced PCM chunk bound to a generation fence."""

    pcm_s16le: bytes
    source_start_sample: int
    text: str = ""
    assistant_text_delta: str | None = None
    first: bool = False
    final: bool = False
    text_audio_start_sample: int | None = None
    text_audio_end_sample: int | None = None
    text_spans: tuple[MediaTextSpan, ...] = ()

    def __post_init__(self) -> None:
        if not self.pcm_s16le or len(self.pcm_s16le) % 2:
            raise ValueError("media reply PCM must be non-empty 16-bit audio")
        if self.source_start_sample < 0:
            raise ValueError("media reply sample range must be non-negative")
        if (self.text_audio_start_sample is None) != (self.text_audio_end_sample is None):
            raise ValueError("text audio bounds must be provided together")
        text_audio_start = self.text_audio_start_sample
        text_audio_end = self.text_audio_end_sample
        if text_audio_start is not None and text_audio_end is not None:
            if text_audio_start < 0:
                raise ValueError("text audio start must be non-negative")
            if text_audio_end <= text_audio_start:
                raise ValueError("text audio range must be positive")
        previous_end = -1
        for span in self.text_spans:
            if span.audio_start_sample < previous_end:
                raise ValueError("media text spans must be ordered")
            previous_end = span.audio_end_sample

    @property
    def frame_samples(self) -> int:
        return len(self.pcm_s16le) // 2


class MediaVoiceProvider(Protocol):
    """Provider-neutral adapter implemented by FunASR/Qwen/Doubao wiring."""

    async def ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]: ...

    def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]: ...

    async def close(self, identity: SessionIdentity) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderAudioTaskSnapshot:
    """PCM that the active ASR task actually accepted at its provider boundary."""

    task_epoch: int
    task_sample_origin: int
    audio_start_sample: int | None
    audio_end_sample: int | None
    send_count: int
    encoding: str = "pcm_s16le"
    sample_rate_hz: int = 16_000
    channels: int = 1
    observed_sample_count: int = 0
    peak_abs: int | None = None
    rms: float | None = None
    all_zero: bool | None = None
    clipping_detected: bool | None = None

    def __post_init__(self) -> None:
        if self.task_epoch < 1:
            raise ValueError("provider ASR task epoch must be positive")
        if self.task_sample_origin < 0 or self.send_count < 0:
            raise ValueError("provider ASR audio metadata must be non-negative")
        if self.encoding != "pcm_s16le":
            raise ValueError("provider ASR audio encoding must be pcm_s16le")
        if self.sample_rate_hz <= 0 or self.channels != 1:
            raise ValueError("provider ASR audio must be positive-rate mono PCM")
        if self.observed_sample_count < 0:
            raise ValueError("provider ASR PCM statistics must be non-negative")
        if self.peak_abs is not None and not 0 <= self.peak_abs <= 32_768:
            raise ValueError("provider ASR PCM peak is invalid")
        if self.rms is not None and (not math.isfinite(self.rms) or not 0 <= self.rms <= 32_768):
            raise ValueError("provider ASR PCM RMS is invalid")
        if (self.audio_start_sample is None) != (self.audio_end_sample is None):
            raise ValueError("provider ASR audio bounds must be provided together")
        if self.audio_start_sample is None:
            if (
                self.send_count != 0
                or self.observed_sample_count != 0
                or self.peak_abs is not None
                or self.rms is not None
                or self.all_zero is not None
                or self.clipping_detected is not None
            ):
                raise ValueError("provider ASR audio evidence requires an audio range")
            return
        if self.audio_start_sample < self.task_sample_origin:
            raise ValueError("provider ASR audio cannot precede the task origin")
        if self.audio_end_sample is None or self.audio_end_sample <= self.audio_start_sample:
            raise ValueError("provider ASR audio range must be positive")
        if self.send_count < 1:
            raise ValueError("provider ASR audio range requires a send count")
        if self.observed_sample_count == 0 and (
            self.peak_abs is not None
            or self.rms is not None
            or self.all_zero is not None
            or self.clipping_detected is not None
        ):
            raise ValueError("provider ASR PCM statistics require observed samples")

    @property
    def audio_samples(self) -> int:
        if self.audio_start_sample is None or self.audio_end_sample is None:
            return 0
        return self.audio_end_sample - self.audio_start_sample


ProviderFactory = Callable[[SessionIdentity], MediaVoiceProvider]
RuntimeFactory = Callable[[str], DuplexRuntime]


@dataclass(frozen=True, slots=True)
class MediaSessionResources:
    """One session-scoped runtime and provider built from one authority context."""

    runtime: DuplexRuntime
    provider: MediaVoiceProvider


class DelegationOutputState(StrEnum):
    """Per-generation ownership state for delegated realtime output."""

    PENDING = "pending"
    OWNED = "owned"
    RELEASED = "released"
    COMPLETED = "completed"


class OutputDispatchStatus(StrEnum):
    """Bounded lifecycle states for one fenced output dispatch."""

    STARTED = "started"
    QUEUED = "queued"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class OutputDispatchResult:
    """Structured outcome retained even when no provider/PCM event follows."""

    fence: GenerationFence
    status: OutputDispatchStatus
    reason: str
    emitted_audio: bool = False

    def __post_init__(self) -> None:
        if not self.reason or len(self.reason) > 64:
            raise ValueError("output dispatch reason must be a short non-empty string")

    def __bool__(self) -> bool:
        return self.status in {
            OutputDispatchStatus.STARTED,
            OutputDispatchStatus.QUEUED,
            OutputDispatchStatus.COMPLETED,
        }


@dataclass(slots=True)
class DelegationOutputClaim:
    """Fence-bound handoff between the normal reply and deep delegation."""

    fence: GenerationFence
    _state: DelegationOutputState = field(
        default=DelegationOutputState.PENDING,
        init=False,
    )
    _initial_decision: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _normal_reply_observed: bool = field(default=False, init=False)
    _local_reply_reserved: bool = field(default=False, init=False)

    @property
    def state(self) -> DelegationOutputState:
        return self._state

    @property
    def normal_reply_observed(self) -> bool:
        return self._normal_reply_observed

    def observe_normal_reply(self) -> None:
        self._normal_reply_observed = True

    async def wait_initial_decision(self) -> DelegationOutputState:
        await self._initial_decision.wait()
        return self._state

    def acquire(self) -> bool:
        if self._state is not DelegationOutputState.PENDING:
            return False
        self._state = DelegationOutputState.OWNED
        self._initial_decision.set()
        return True

    def release(self) -> bool:
        if self._state not in {
            DelegationOutputState.PENDING,
            DelegationOutputState.OWNED,
        }:
            return False
        self._state = DelegationOutputState.RELEASED
        self._initial_decision.set()
        return True

    def complete(self) -> bool:
        if self._state is not DelegationOutputState.OWNED:
            return False
        self._state = DelegationOutputState.COMPLETED
        self._initial_decision.set()
        return True

    def reserve_local_reply(self) -> bool:
        if (
            self._state is not DelegationOutputState.RELEASED
            or self._local_reply_reserved
        ):
            return False
        self._local_reply_reserved = True
        return True


def owned_delegation_holds_turn(
    claims: Mapping[GenerationFence, DelegationOutputClaim],
    fence: GenerationFence,
) -> bool:
    """True while an OWNED same-turn claim is still waiting to speak."""

    return any(
        claim.state is DelegationOutputState.OWNED
        and claim_fence.session_id == fence.session_id
        and claim_fence.turn_id == fence.turn_id
        for claim_fence, claim in claims.items()
    )


def same_turn_followup_output_pending(
    claims: Mapping[GenerationFence, DelegationOutputClaim],
    output_work: Mapping[str, OutputWork],
    fence: GenerationFence,
) -> bool:
    """True while filler playback must not return the device to listening.

    The OWNED claim is marked COMPLETED as soon as the tool result is
    enqueued, so successor work on the same turn also holds the floor.
    """

    if owned_delegation_holds_turn(claims, fence):
        return True
    return any(
        work.fence.session_id == fence.session_id and work.fence.turn_id == fence.turn_id
        for work in output_work.values()
    )


@dataclass(frozen=True, slots=True)
class OutputOwnerLease:
    intent: Any
    fence: GenerationFence
    task: asyncio.Task[Any]


@dataclass(frozen=True, slots=True)
class OutputWork:
    """One fenced source that the Registry may render through its sole owner."""

    intent: Any
    fence: GenerationFence
    conversation_text: str | None = None

    @property
    def intent_id(self) -> str:
        return str(self.intent.intent_id)

SessionFactory = Callable[
    [SessionIdentity],
    MediaSessionResources | Awaitable[MediaSessionResources],
]
