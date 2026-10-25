"""Config-driven ASR failover: Volc primary, SenseVoice backup, Bailian tertiary.

The switch is off by default. When ``ASR_FAILOVER_ENABLED`` is false the
factory returns the existing DashScope ``FunASRSession`` unchanged, so a
deployment that does not opt in keeps the current behaviour byte for byte.

Failover is per VAD segment, never concatenated: if the primary errors or
misses its first-result budget, the whole segment PCM is re-recognised offline
by the deployed SenseVoice sidecar; if that also fails, the segment is replayed
through Bailian ``fun-asr-realtime``. Degradation lasts for that segment only.
The next segment tries the primary again whenever the process breaker allows
it, so one quiet moment cannot pin a whole conversation to the backup.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import logging
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any, ClassVar

from services.agent.src.providers.asr_session import StreamingASRSession
from services.agent.src.providers.funasr_protocol import FunASRSentence, FunASRServerEvent
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.providers.sensevoice import SenseVoiceRescue, SenseVoiceRescueConfig
from services.agent.src.providers.volc_asr import VolcASRConfig, VolcASRSession

logger = logging.getLogger(__name__)

_FAILOVER_REASONS = frozenset(
    {
        "connect_failed",
        "first_result_timeout",
        "provider_error",
        "breaker_open",
    }
)


@dataclass(frozen=True, slots=True)
class ASRFailoverConfig:
    """Environment-driven knobs for the optional ASR failover chain.

    ENV_KEYS includes ASR_FAILOVER_ENABLED, ASR_PRIMARY, and
    ASR_FAILOVER_FIRST_RESULT_TIMEOUT_MS because failover code still reads them.
    They are rejected upstream by reject_replaced_keys before reaching from_env.
    """

    ENV_KEYS: ClassVar[frozenset[str]] = frozenset({
        "ASR_FAILOVER_ENABLED",
        "ASR_PRIMARY",
        "ASR_BACKUP",
        "ASR_TERTIARY",
        "ASR_FAILOVER_FIRST_RESULT_TIMEOUT_MS",
        "ASR_FAILOVER_CONNECT_TIMEOUT_MS",
        "ASR_BREAKER_FAILURES",
        "ASR_BREAKER_OPEN_S",
        "ASR_BACKUP_TIMEOUT_S",
        "ASR_BACKUP_MAX_AUDIO_S",
    })

    enabled: bool = False
    primary: str = "volc"
    backup: str = "sensevoice"
    tertiary: str = "dashscope_funasr"
    first_result_timeout_s: float = 1.5
    connect_timeout_s: float = 2.0
    breaker_failures: int = 3
    breaker_open_s: float = 30.0
    backup_timeout_s: float = 5.0
    max_segment_s: float = 12.0
    post_finish_tail_grace_s: float = 0.25

    def __post_init__(self) -> None:
        if self.primary not in {"volc"}:
            raise ValueError("ASR_PRIMARY must be volc when failover is enabled")
        if self.backup not in {"sensevoice", "none"}:
            raise ValueError("ASR_BACKUP must be sensevoice or none")
        if self.tertiary not in {"dashscope_funasr", "none"}:
            raise ValueError("ASR_TERTIARY must be dashscope_funasr or none")
        if self.enabled and self.backup == "none" and self.tertiary == "none":
            raise ValueError("ASR failover needs at least one backup or tertiary provider")
        if not 0 < self.first_result_timeout_s <= 30:
            raise ValueError("ASR first-result timeout must be within (0, 30] seconds")
        if not 0 < self.connect_timeout_s <= 30:
            raise ValueError("ASR connect timeout must be within (0, 30] seconds")
        if self.breaker_failures < 1:
            raise ValueError("ASR breaker failures must be positive")
        if self.breaker_open_s <= 0:
            raise ValueError("ASR breaker open seconds must be positive")
        if not 0 < self.backup_timeout_s <= 30:
            raise ValueError("ASR backup timeout must be within (0, 30] seconds")
        if not 0 < self.max_segment_s <= 300:
            raise ValueError("ASR backup segment cap must be within (0, 300] seconds")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ASRFailoverConfig:
        from services.agent.src.providers.volc_asr import _DeclaredEnv

        e = _DeclaredEnv(env if env is not None else dict(os.environ), cls.ENV_KEYS)
        return cls(
            enabled=e.get("ASR_FAILOVER_ENABLED", "false").strip().lower() == "true",
            primary=e.get("ASR_PRIMARY", "volc").strip().lower() or "volc",
            backup=e.get("ASR_BACKUP", "sensevoice").strip().lower() or "sensevoice",
            tertiary=e.get("ASR_TERTIARY", "dashscope_funasr").strip().lower()
            or "dashscope_funasr",
            first_result_timeout_s=float(
                e.get("ASR_FAILOVER_FIRST_RESULT_TIMEOUT_MS", "1500")
            )
            / 1000.0,
            connect_timeout_s=float(e.get("ASR_FAILOVER_CONNECT_TIMEOUT_MS", "2000"))
            / 1000.0,
            breaker_failures=int(e.get("ASR_BREAKER_FAILURES", "3")),
            breaker_open_s=float(e.get("ASR_BREAKER_OPEN_S", "30")),
            backup_timeout_s=float(e.get("ASR_BACKUP_TIMEOUT_S", "5")),
            max_segment_s=float(e.get("ASR_BACKUP_MAX_AUDIO_S", "12")),
        )


class ProcessASRBreaker:
    """Process-wide primary breaker that merges concurrent probes.

    The existing session ``CircuitBreaker`` raises while open and only lets one
    probe through after the window. Sharing it across sessions would make one
    session's timeout block every other session. This breaker never raises; it
    returns ``False`` so the caller can send the segment to backup, and it
    allows exactly one half-open probe at a time.

    A probe lease is bounded. A session that notices the open window but dies
    without resolving the probe (device disconnect, cancelled handshake) must
    not disable the primary for the whole process, so the lease expires on its
    own and ``release_probe`` frees it as soon as the owner is closed.

    A lease carries an owner token so a session that never held it cannot
    release it by reporting an unrelated verdict.
    """

    def __init__(
        self,
        *,
        failures: int = 3,
        open_s: float = 30.0,
        probe_timeout_s: float | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.failures = failures
        self.open_s = open_s
        self.probe_timeout_s = open_s if probe_timeout_s is None else probe_timeout_s
        self._clock = clock
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._probe_expires_at: float | None = None
        self._probe_token = 0
        self._probe_owner: int | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def probe_in_flight(self) -> bool:
        return self._probe_in_flight

    @property
    def probe_owner(self) -> int | None:
        """Token of the lease handed out by the last successful ``allow()``."""

        return self._probe_owner if self._probe_in_flight else None

    def allow(self) -> bool:
        if self._state == "closed":
            self._probe_owner = None
            return True
        now = self._clock()
        if self._state == "open":
            elapsed = now - (self._opened_at or 0.0)
            if elapsed < self.open_s:
                return False
            self._state = "half_open"
        if self._probe_in_flight:
            if self._probe_expires_at is not None and now >= self._probe_expires_at:
                # The probing session never reported back; take the lease.
                self._probe_in_flight = False
                self._probe_expires_at = None
                self._probe_owner = None
            else:
                return False
        self._probe_in_flight = True
        self._probe_expires_at = now + self.probe_timeout_s
        self._probe_token += 1
        self._probe_owner = self._probe_token
        return True

    def release_probe(self, owner: int | None = None) -> None:
        """Drop a half-open lease whose owner closed without a verdict.

        Without an owner this clears whatever lease is outstanding, which is
        only correct for a deliberate reset; a session must pass the token it
        received from ``allow()``.
        """

        if owner is not None and owner != self._probe_owner:
            return
        self._probe_in_flight = False
        self._probe_expires_at = None
        self._probe_owner = None

    def record_success(self) -> None:
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = None
        self.release_probe()

    def record_failure(self, *, owner: int | None = None) -> None:
        self.release_probe(owner)
        self._consecutive_failures += 1
        if self._state == "half_open" or self._consecutive_failures >= self.failures:
            self._state = "open"
            self._opened_at = self._clock()


class FailoverASRSession:
    """Wrap a streaming primary and whole-segment offline fallbacks."""

    def __init__(
        self,
        *,
        config: ASRFailoverConfig,
        primary_factory: Callable[[], StreamingASRSession],
        sensevoice: SenseVoiceRescue | None = None,
        tertiary_factory: Callable[[], StreamingASRSession] | None = None,
        breaker: ProcessASRBreaker | None = None,
        metrics: Any = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.config = config
        self.metrics = metrics
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.task_id: str | None = None
        self._primary_factory = primary_factory
        self._sensevoice = sensevoice
        self._tertiary_factory = tertiary_factory
        self._breaker = breaker or ProcessASRBreaker(
            failures=config.breaker_failures,
            open_s=config.breaker_open_s,
        )
        self._clock = clock
        self._primary: StreamingASRSession | None = None
        self._closed = False
        self._probe_lease: int | None = None
        self._degraded = False
        self._degrade_reason: str | None = None
        self._primary_first_result = False
        self._primary_final_seen = False
        # First frame of this segment that carries speech energy. Silence never
        # starts the first-result clock: a child who pauses before speaking
        # must not be mistaken for a silent provider.
        self._speech_onset_at: float | None = None
        self._task_epoch = 0
        self._segment_epoch = 1
        self._task_sample_origin = 0
        self._last_sent_sample = 0
        self._last_committed_sample = 0
        self._rotation_pending = False
        self._segment_start: int | None = None
        self._segment_end: int | None = None
        self._segment_pcm = bytearray()
        self._segment_pcm_start: int | None = None
        self._segment_voiced_start: int | None = None
        self._task_start_sample: int | None = None
        self._task_end_sample: int | None = None
        self._task_send_count = 0
        self._task_samples = 0
        self._task_peak_abs = 0
        self._task_square_sum = 0.0
        self._backup_sentence_id = 0

    @property
    def task_epoch(self) -> int:
        return self._task_epoch

    @property
    def segment_epoch(self) -> int:
        return self._segment_epoch

    @property
    def rotation_pending(self) -> bool:
        return self._rotation_pending

    @property
    def task_sample_origin(self) -> int:
        return self._task_sample_origin

    @property
    def task_audio_start_sample(self) -> int | None:
        return self._task_start_sample

    @property
    def task_audio_end_sample(self) -> int | None:
        return self._task_end_sample

    @property
    def task_audio_send_count(self) -> int:
        return self._task_send_count

    @property
    def task_pcm_sample_count(self) -> int:
        return self._task_samples

    @property
    def task_pcm_peak_abs(self) -> int | None:
        return self._task_peak_abs if self._task_samples else None

    @property
    def task_pcm_rms(self) -> float | None:
        if not self._task_samples:
            return None
        return math.sqrt(self._task_square_sum / self._task_samples)

    @property
    def last_sent_sample(self) -> int:
        return self._last_sent_sample

    @property
    def degraded(self) -> bool:
        """Whether the segment in progress is going to the backup chain."""

        return self._degraded

    @property
    def degrade_reason(self) -> str | None:
        return self._degrade_reason

    def mark_committed_sample(self, sample: int) -> None:
        if sample < 0:
            raise ValueError("committed sample must be non-negative")
        self._last_committed_sample = max(self._last_committed_sample, sample)
        if self._primary is not None:
            self._primary.mark_committed_sample(sample)

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("ASR failover session is closed")
        self._ensure_task_identity()
        await self._open_primary(segment_is_healthy=False)

    async def _open_primary(self, *, segment_is_healthy: bool) -> bool:
        """Connect the primary when the breaker allows it.

        Returns whether a primary is available. ``segment_is_healthy`` marks a
        fresh segment, where a successful connect means the previous
        degradation no longer applies.
        """

        if not self._breaker.allow():
            await self._degrade("breaker_open", count_failure=False)
            return False
        if self._breaker.state == "half_open":
            # Hold the lease so ``aclose`` can hand it back if this session
            # dies before the primary reports a verdict.
            self._probe_lease = self._breaker.probe_owner
        primary: StreamingASRSession | None = None
        try:
            primary = self._primary_factory()
            await asyncio.wait_for(primary.connect(), timeout=self.config.connect_timeout_s)
            if self._last_committed_sample:
                primary.mark_committed_sample(self._last_committed_sample)
        except asyncio.CancelledError:
            self._release_probe()
            if primary is not None:
                with contextlib.suppress(Exception):
                    await primary.aclose()
            raise
        except Exception:
            self._probe_lease = None
            if primary is not None:
                with contextlib.suppress(Exception):
                    await primary.aclose()
            await self._degrade("connect_failed")
            return False
        self._primary = primary
        self.task_id = primary.task_id or self._ensure_task_identity()
        self._task_epoch = max(self._task_epoch, primary.task_epoch)
        self._task_sample_origin = primary.task_sample_origin
        if segment_is_healthy:
            self._degraded = False
            self._degrade_reason = None
        logger.info(
            "asr failover primary connected provider=%s task_epoch=%s",
            self.config.primary,
            self._task_epoch,
        )
        return True

    def _ensure_task_identity(self) -> str:
        if not self.task_id:
            self.task_id = f"failover-{uuid.uuid4()}"
        return self.task_id

    def _release_probe(self) -> None:
        lease = self._probe_lease
        if lease is not None:
            self._probe_lease = None
            self._breaker.release_probe(lease)

    async def _degrade(self, reason: str, *, count_failure: bool = True) -> None:
        if self._degraded:
            return
        if reason not in _FAILOVER_REASONS:
            raise ValueError(f"unknown ASR failover reason: {reason}")
        self._degraded = True
        self._degrade_reason = reason
        lease = self._probe_lease
        self._probe_lease = None
        if count_failure:
            self._breaker.record_failure(owner=lease)
        primary = self._primary
        self._primary = None
        if primary is not None:
            # Primary interims that have not crossed the adapter boundary are
            # discarded; the segment is re-recognised as one whole sentence.
            while True:
                try:
                    primary.events.get_nowait()
                except asyncio.QueueEmpty:
                    break
            with contextlib.suppress(Exception):
                await primary.aclose()
        if self.metrics is not None:
            with contextlib.suppress(Exception):
                self.metrics.inc_media_metric(
                    "asr_failover_total",
                    labels={
                        "provider": self.config.primary,
                        "reason": reason,
                    },
                )
        logger.info(
            "asr failover primary degraded provider=%s reason=%s",
            self.config.primary,
            reason,
        )

    async def send_pcm(
        self,
        pcm: bytes,
        *,
        capture_start_sample: int | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("ASR failover session is not connected")
        if not pcm or len(pcm) % 2:
            raise ValueError("ASR failover PCM must be non-empty 16-bit samples")
        start = self._last_sent_sample if capture_start_sample is None else capture_start_sample
        if start < self._last_sent_sample:
            raise ValueError("capture sample position moved backwards")
        end = start + len(pcm) // 2
        if self._rotation_pending:
            await self._arm_next_task(start)
        self._start_segment_if_needed(start)
        self._segment_pcm.extend(pcm)
        self._record_task_pcm(pcm, start=start, end=end)
        if self._segment_voiced_start is None and self._has_speech_energy(pcm):
            self._segment_voiced_start = start
        self._last_sent_sample = end
        if self._speech_onset_at is None and self._segment_voiced_start is not None:
            self._speech_onset_at = self._clock()
        primary = self._primary
        if primary is not None and not self._degraded:
            try:
                await primary.send_pcm(pcm, capture_start_sample=start)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._degrade("provider_error")
            else:
                await self._drain_primary()
                if self._primary is not None:
                    self._task_epoch = max(self._task_epoch, self._primary.task_epoch)
                    self.task_id = self._primary.task_id or self.task_id
                    self._task_sample_origin = self._primary.task_sample_origin
        if self._degraded or primary is None:
            return
        if (
            self._speech_onset_at is not None
            and not self._primary_first_result
            and self._clock() - self._speech_onset_at
            >= self.config.first_result_timeout_s
        ):
            await self._degrade("first_result_timeout")

    def _start_segment_if_needed(self, start: int) -> None:
        if self._segment_start is not None:
            return
        self._segment_start = start
        self._segment_end = start
        self._segment_pcm_start = start
        if self._primary is None:
            self._task_sample_origin = start

    async def _arm_next_task(self, start: int) -> None:
        """Reset per-segment state and retry the primary for the new segment."""

        self._rotation_pending = False
        self._speech_onset_at = None
        self._primary_first_result = False
        self._primary_final_seen = False
        self._task_epoch += 1
        self._segment_epoch += 1
        self._task_sample_origin = start
        self._task_start_sample = None
        self._task_end_sample = None
        self._task_send_count = 0
        self._task_samples = 0
        self._task_peak_abs = 0
        self._task_square_sum = 0.0
        if self._primary is not None and not self._degraded:
            self.task_id = self._primary.task_id or self.task_id
            self._task_epoch = max(self._task_epoch, self._primary.task_epoch)
            self._task_sample_origin = self._primary.task_sample_origin
            return
        # The previous segment failed over. That verdict covered that segment
        # only, so retry the primary now; the breaker decides whether it runs.
        self._degraded = False
        self._degrade_reason = None
        if await self._open_primary(segment_is_healthy=True):
            primary = self._primary
            assert primary is not None
            self.task_id = primary.task_id or self.task_id
            self._task_epoch = max(self._task_epoch, primary.task_epoch)
            self._task_sample_origin = start
        else:
            self.task_id = f"failover-{uuid.uuid4()}"

    def _record_task_pcm(self, pcm: bytes, *, start: int, end: int) -> None:
        if self._task_start_sample is None:
            self._task_start_sample = start
        self._task_end_sample = end
        self._task_send_count += 1
        samples = len(pcm) // 2
        self._task_samples += samples
        peak = audioop.max(pcm, 2)
        rms = audioop.rms(pcm, 2)
        self._task_peak_abs = max(self._task_peak_abs, peak)
        self._task_square_sum += float(rms * rms * samples)
        self._segment_end = end

    async def _drain_primary(self) -> None:
        primary = self._primary
        if primary is None:
            return
        while True:
            try:
                event = primary.events.get_nowait()
            except asyncio.QueueEmpty:
                return
            if await self._handle_primary_event(event):
                return

    async def _handle_primary_event(self, event: FunASRServerEvent) -> bool:
        if event.event == "task-failed":
            await self._degrade("provider_error")
            return True
        if self._degraded:
            return False
        sentence = event.sentence
        if event.event == "result-generated" and sentence is not None:
            if sentence.text.strip():
                self._primary_first_result = True
            if sentence.sentence_end and sentence.text.strip():
                self._primary_final_seen = True
                if self.metrics is not None and self._speech_onset_at is not None:
                    self.metrics.observe_media_metric(
                        "asr_first_result_ms",
                        (self._clock() - self._speech_onset_at) * 1000.0,
                        labels={"provider": self.config.primary},
                    )
        self.events.put_nowait(event)
        return False

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        if self._closed:
            return
        primary = self._primary
        if primary is not None and not self._degraded:
            await self._drain_primary()
        if (
            primary is not None
            and not self._degraded
            and self._speech_onset_at is not None
            and not self._primary_first_result
            and self._clock() - self._speech_onset_at
            >= self.config.first_result_timeout_s
        ):
            await self._degrade("first_result_timeout")
            primary = None
        if primary is not None and not self._degraded:
            try:
                await primary.rotate_task(require_consumed=require_consumed)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._degrade("provider_error")
            else:
                await self._drain_primary()
                if not self._degraded:
                    # A connected primary that returned a task boundary is a
                    # healthy segment even when the transcript is empty.
                    self._breaker.record_success()
                    self._probe_lease = None
        if self._degraded or self._primary is None:
            if self._primary_final_seen:
                self._emit_task_finished()
            else:
                await self._emit_backup_result()
        self._rotation_pending = True
        self._speech_onset_at = None
        self._reset_segment_evidence()

    async def _emit_backup_result(self) -> None:
        pcm, begin_ms = self._voiced_segment_pcm()
        samples = len(pcm) // 2
        if not pcm:
            self._emit_task_finished()
            return
        if samples > int(self.config.max_segment_s * 16_000):
            logger.info(
                "asr failover backup skipped: segment too long samples=%s cap_s=%s",
                samples,
                self.config.max_segment_s,
            )
            if self.metrics is not None:
                self.metrics.inc_media_metric(
                    "asr_failover_total",
                    labels={"provider": "backup", "reason": "segment_too_long"},
                )
            self._emit_task_finished()
            return
        if not self._has_speech_energy(pcm):
            self._emit_task_finished()
            return
        text = await self._transcribe_backup(pcm)
        if text is None:
            if self.metrics is not None:
                self.metrics.inc_media_metric("asr_both_failed_total")
            logger.info(
                "asr failover both providers failed segment_ms=%s reason=%s",
                round(samples * 1000 / 16_000),
                self._degrade_reason or "unknown",
            )
            self._emit_task_finished()
            return
        self._backup_sentence_id += 1
        duration_ms = round(samples * 1000 / 16_000)
        self.events.put_nowait(
            FunASRServerEvent(
                event="result-generated",
                task_id=self._ensure_task_identity(),
                sentence=FunASRSentence(
                    sentence_id=self._backup_sentence_id,
                    text=text,
                    begin_ms=begin_ms,
                    end_ms=begin_ms + duration_ms,
                    sentence_end=True,
                    heartbeat=False,
                    words=(),
                    word_timing_valid=False,
                ),
            )
        )
        self._emit_task_finished()
        if self.metrics is not None:
            self.metrics.inc_media_metric(
                "asr_failover_total",
                labels={"provider": "backup", "reason": "recovered"},
            )

    def _voiced_segment_pcm(self) -> tuple[bytes, int]:
        """The segment from its first speech frame, not from the task start.

        A degraded segment spans the whole provider task, which begins before
        the child speaks. Sending that leading silence to the offline decoder
        both wastes the budget and can push a normal utterance past
        ``max_segment_s``, where it would be dropped without any reply. The
        returned begin offset keeps the synthetic final on the absolute clock.
        """

        start = self._segment_pcm_start
        if start is None:
            return b"", 0
        voiced = self._segment_voiced_start
        if voiced is None or voiced <= start:
            return bytes(self._segment_pcm), 0
        return (
            bytes(self._segment_pcm[(voiced - start) * 2 :]),
            round((voiced - start) * 1000 / 16_000),
        )

    def _has_speech_energy(self, pcm: bytes) -> bool:
        rms = audioop.rms(pcm, 2)
        peak = audioop.max(pcm, 2)
        sensevoice = self._sensevoice
        if sensevoice is not None:
            return sensevoice.accepts_energy(rms=rms, peak_abs=peak)
        return rms >= 100 or peak >= 350

    async def _transcribe_backup(self, pcm: bytes) -> str | None:
        sensevoice = self._sensevoice
        if sensevoice is not None and self.config.backup == "sensevoice":
            try:
                text = await asyncio.wait_for(
                    sensevoice.transcribe(pcm, sample_rate=16_000, language="zh"),
                    timeout=self.config.backup_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.info("asr failover sensevoice backup failed")
                text = None
            if text and sensevoice.accepts_text(text):
                return text.strip()
        if self._tertiary_factory is not None and self.config.tertiary == "dashscope_funasr":
            return await self._tertiary_transcribe(pcm)
        return None

    async def _tertiary_transcribe(self, pcm: bytes) -> str | None:
        assert self._tertiary_factory is not None
        session = self._tertiary_factory()
        try:
            await asyncio.wait_for(
                session.connect(),
                timeout=self.config.connect_timeout_s,
            )
            await asyncio.wait_for(
                session.send_pcm(pcm, capture_start_sample=0),
                timeout=self.config.backup_timeout_s,
            )
            await asyncio.wait_for(
                session.rotate_task(require_consumed=False),
                timeout=self.config.backup_timeout_s,
            )
            return self._latest_final(session.events)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info("asr failover tertiary backup failed")
            return None
        finally:
            with contextlib.suppress(Exception):
                await session.aclose()

    def _latest_final(self, events: asyncio.Queue[FunASRServerEvent]) -> str | None:
        text: str | None = None
        while True:
            try:
                event = events.get_nowait()
            except asyncio.QueueEmpty:
                return text
            if (
                event.event == "result-generated"
                and event.sentence is not None
                and event.sentence.sentence_end
                and event.sentence.text.strip()
            ):
                text = event.sentence.text.strip()

    def _emit_task_finished(self) -> None:
        self.events.put_nowait(
            FunASRServerEvent(
                event="task-finished",
                task_id=self._ensure_task_identity(),
            )
        )

    def _reset_segment_evidence(self) -> None:
        self._segment_pcm.clear()
        self._segment_start = None
        self._segment_end = None
        self._segment_pcm_start = None
        self._segment_voiced_start = None

    async def reconnect_with_replay(self) -> None:
        if self._closed or self._degraded or self._primary is None:
            return
        try:
            await self._primary.reconnect_with_replay()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._degrade("provider_error")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A half-open probe that this session took but never resolved would
        # otherwise leave ``allow()`` returning False for the whole process.
        self._release_probe()
        primary = self._primary
        self._primary = None
        if primary is not None:
            with contextlib.suppress(Exception):
                await primary.aclose()
        self._reset_segment_evidence()


def build_asr_session_factory(
    *,
    funasr_config: FunASRConfig,
    metrics: Any = None,
    env: dict[str, str] | None = None,
) -> Callable[[], StreamingASRSession]:
    """Return the configured factory; the default remains the existing session."""

    settings = ASRFailoverConfig.from_env(env)
    if not settings.enabled:
        return lambda: FunASRSession(replace(funasr_config), metrics=metrics)
    environment = env or dict(os.environ)
    volc_config = VolcASRConfig.from_env(environment)
    sensevoice: SenseVoiceRescue | None = None
    if settings.backup == "sensevoice":
        url = environment.get("SENSEVOICE_URL", "").strip()
        if not url:
            raise ValueError("ASR_BACKUP=sensevoice requires SENSEVOICE_URL")
        rescue_config = replace(
            SenseVoiceRescueConfig.from_env(environment),
            timeout_s=settings.backup_timeout_s,
            max_audio_s=settings.max_segment_s,
        )
        sensevoice = SenseVoiceRescue(rescue_config)
    # Fail the deploy, not every device session: without credentials the
    # primary can only ever fail its handshake.
    volc_config.auth_headers()
    tertiary_factory: Callable[[], StreamingASRSession] | None = None
    if settings.tertiary == "dashscope_funasr":
        tertiary_config = replace(funasr_config, rescue_config=None)
        tertiary_factory = lambda: FunASRSession(  # noqa: E731
            replace(tertiary_config),
            metrics=metrics,
        )
    breaker = ProcessASRBreaker(
        failures=settings.breaker_failures,
        open_s=settings.breaker_open_s,
    )

    def factory() -> StreamingASRSession:
        return FailoverASRSession(
            config=settings,
            primary_factory=lambda: VolcASRSession(volc_config, metrics=metrics),
            sensevoice=sensevoice,
            tertiary_factory=tertiary_factory,
            breaker=breaker,
            metrics=metrics,
        )

    return factory


__all__ = [
    "ASRFailoverConfig",
    "FailoverASRSession",
    "ProcessASRBreaker",
    "build_asr_session_factory",
]
