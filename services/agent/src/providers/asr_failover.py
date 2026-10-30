"""Config-driven ASR failover: Volc primary, SenseVoice backup, Bailian tertiary.

``ASR_PROVIDER`` selects the engine. ``volc`` builds this chain, ``funasr``
builds the plain DashScope ``FunASRSession``. The selector is one key on
purpose: ``ASR_FAILOVER_ENABLED`` used to spell the same choice a second time,
and the two copies drifted, so the shell gate could expect one engine while the
process ran the other.

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
from typing import Any, ClassVar, assert_never

from services.agent.src.providers.asr_session import StreamingASRSession
from services.agent.src.providers.funasr_protocol import FunASRSentence, FunASRServerEvent
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.providers.sensevoice import SenseVoiceRescue, SenseVoiceRescueConfig
from services.agent.src.providers.volc_asr import VolcASRConfig, VolcASRSession
from services.common.voice_engines import ASREngine

logger = logging.getLogger(__name__)

# The chain this module builds has exactly one primary. It used to be a config
# field read from ASR_PRIMARY, which could only ever hold this value.
_PRIMARY_PROVIDER = "volc"

# A healthy primary that returned no text gets one backup re-read before the
# PCM is dropped. That re-read runs inside the ingress finalize lock, so it is
# capped well below the 6 s media tail deadline; the audible fallback covers
# whatever the cap cuts off.
_HEALTHY_EMPTY_RETRY_BUDGET_S = 2.0

_FAILOVER_REASONS = frozenset(
    {
        "connect_failed",
        "final_result_timeout",
        "provider_error",
        "breaker_open",
    }
)


@dataclass(frozen=True, slots=True)
class ASRFailoverConfig:
    """Environment-driven knobs for the Volcengine ASR chain.

    There is no switch here. ``ASR_PROVIDER=volc`` is what builds this chain,
    and ``ASR_FAILOVER_ENABLED`` / ``ASR_PRIMARY`` used to repeat that decision
    inside this file. They are rejected upstream by ``reject_replaced_keys``,
    so a deployment still carrying one fails the split instead of silently
    running a different engine than the selector asked for.
    """

    ENV_KEYS: ClassVar[frozenset[str]] = frozenset({
        "ASR_BACKUP",
        "ASR_TERTIARY",
        "ASR_FAILOVER_FINAL_RESULT_TIMEOUT_MS",
        "ASR_FAILOVER_CONNECT_TIMEOUT_MS",
        "ASR_BREAKER_FAILURES",
        "ASR_BREAKER_OPEN_S",
        "ASR_BACKUP_TIMEOUT_S",
        "ASR_BACKUP_MAX_AUDIO_S",
    })

    backup: str = "sensevoice"
    tertiary: str = "dashscope_funasr"
    # How long the primary gets, from the negative packet to its final, before
    # the segment goes to the backup. The backup re-recognises the whole
    # segment and cannot start until the audio ends, so a verdict before
    # ``rotate_task`` saves nothing. Measured on the offline A/B: final p50
    # 0.09 s, p90 0.18 s, against a first partial p50 of 3.40 s.
    final_result_timeout_s: float = 1.0
    connect_timeout_s: float = 2.0
    breaker_failures: int = 3
    breaker_open_s: float = 30.0
    backup_timeout_s: float = 5.0
    max_segment_s: float = 12.0
    post_finish_tail_grace_s: float = 0.25

    def __post_init__(self) -> None:
        if self.backup not in {"sensevoice", "none"}:
            raise ValueError("ASR_BACKUP must be sensevoice or none")
        if self.tertiary not in {"dashscope_funasr", "none"}:
            raise ValueError("ASR_TERTIARY must be dashscope_funasr or none")
        if self.backup == "none" and self.tertiary == "none":
            raise ValueError("ASR failover needs at least one backup or tertiary provider")
        if not 0 < self.final_result_timeout_s <= 30:
            raise ValueError("ASR final-result timeout must be within (0, 30] seconds")
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
            backup=e.get("ASR_BACKUP", "sensevoice").strip().lower() or "sensevoice",
            tertiary=e.get("ASR_TERTIARY", "dashscope_funasr").strip().lower()
            or "dashscope_funasr",
            final_result_timeout_s=float(
                e.get("ASR_FAILOVER_FINAL_RESULT_TIMEOUT_MS", "1000")
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
        self._primary_final_seen = False
        # Set when the negative packet goes out, cleared when the final lands,
        # so ``asr_finalize_ms`` measures the wait the budget actually bounds.
        self._finalize_at: float | None = None
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
        self._vad_start_sample: int | None = None
        self._recovering = False
        self._task_start_sample: int | None = None
        self._task_end_sample: int | None = None
        self._task_send_count = 0
        self._task_samples = 0
        self._task_peak_abs = 0
        self._task_square_sum = 0.0
        self._backup_sentence_id = 0
        self._last_segment_had_speech = False
        self._last_segment_provider_failed = False
        self._last_segment_backup_retried = False

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
    def last_segment_had_speech(self) -> bool:
        return self._last_segment_had_speech

    @property
    def last_segment_provider_failed(self) -> bool:
        return self._last_segment_provider_failed

    @property
    def last_segment_backup_retried(self) -> bool:
        return self._last_segment_backup_retried

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

    def note_vad_start(self, sample: int) -> None:
        """Record the bridge's VAD onset for backup slicing and replay."""

        if sample < 0:
            raise ValueError("VAD start sample must be non-negative")
        self._vad_start_sample = sample

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
            _PRIMARY_PROVIDER,
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

    def _record_primary_success(self) -> None:
        self._breaker.record_success()
        self._probe_lease = None

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
                        "provider": _PRIMARY_PROVIDER,
                        "reason": reason,
                    },
                )
        logger.info(
            "asr failover primary degraded provider=%s reason=%s",
            _PRIMARY_PROVIDER,
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
        primary = self._primary
        if primary is not None and not self._degraded:
            try:
                await primary.send_pcm(pcm, capture_start_sample=start)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._degrade("provider_error")
                await self._recover_primary_mid_segment()
            else:
                await self._drain_primary()
                if self._primary is not None:
                    self._task_epoch = max(self._task_epoch, self._primary.task_epoch)
                    self.task_id = self._primary.task_id or self.task_id
                    self._task_sample_origin = self._primary.task_sample_origin

    async def _recover_primary_mid_segment(self) -> bool:
        """Reopen the primary inside a live segment and replay its audio.

        A provider task can die between two VAD edges. Waiting for ``rotate_task``
        sends the rest of the sentence to a dead task, so recover on the first
        failure and replay everything not yet committed. The segment buffer is
        not appended to here; only the adapter's ingest path owns it.
        """

        if self._closed or self._recovering or self._rotation_pending:
            return False
        start = self._segment_pcm_start
        if start is None or not self._segment_pcm:
            self._rotation_pending = True
            return False
        end = start + len(self._segment_pcm) // 2
        vad_start = self._vad_start_sample if self._vad_start_sample is not None else start
        replay_start = max(start, vad_start, self._last_committed_sample)
        if replay_start >= end:
            self._rotation_pending = True
            return False
        replay_pcm = bytes(self._segment_pcm[(replay_start - start) * 2 :])
        self._recovering = True
        try:
            self._degraded = False
            self._degrade_reason = None
            self._rotation_pending = False
            self._task_epoch += 1
            self._task_sample_origin = replay_start
            self._reset_task_evidence()
            if not await self._open_primary(segment_is_healthy=True):
                self._rotation_pending = True
                return False
            primary = self._primary
            assert primary is not None
            try:
                await primary.send_pcm(replay_pcm, capture_start_sample=replay_start)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._degrade("provider_error")
                self._rotation_pending = True
                return False
            await self._drain_primary()
            primary = self._primary
            if self._degraded or primary is None:
                self._rotation_pending = True
                return False
            self.task_id = primary.task_id or self.task_id
            self._task_epoch = max(self._task_epoch, primary.task_epoch)
            self._task_sample_origin = primary.task_sample_origin
            self._record_task_pcm(replay_pcm, start=replay_start, end=end)
            logger.info(
                "asr failover primary recovered task_epoch=%s replay_samples=%s",
                self._task_epoch,
                len(replay_pcm) // 2,
            )
            return True
        finally:
            self._recovering = False

    def _start_segment_if_needed(self, start: int) -> None:
        if self._segment_start is not None:
            return
        self._last_segment_had_speech = False
        self._last_segment_provider_failed = False
        self._last_segment_backup_retried = False
        self._segment_start = start
        self._segment_end = start
        self._segment_pcm_start = start
        if self._primary is None:
            self._task_sample_origin = start

    async def _arm_next_task(self, start: int) -> None:
        """Reset per-segment state and retry the primary for the new segment."""

        self._rotation_pending = False
        self._primary_final_seen = False
        self._finalize_at = None
        self._task_epoch += 1
        self._segment_epoch += 1
        self._task_sample_origin = start
        self._reset_task_evidence()
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

    def _reset_task_evidence(self) -> None:
        self._task_start_sample = None
        self._task_end_sample = None
        self._task_send_count = 0
        self._task_samples = 0
        self._task_peak_abs = 0
        self._task_square_sum = 0.0

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
            await self._recover_primary_mid_segment()
            return True
        if self._degraded:
            return False
        sentence = event.sentence
        if event.event == "result-generated" and sentence is not None:
            if sentence.text.strip():
                self._record_primary_success()
            if sentence.sentence_end and sentence.text.strip():
                self._primary_final_seen = True
                if self.metrics is not None and self._finalize_at is not None:
                    self.metrics.observe_media_metric(
                        "asr_finalize_ms",
                        (self._clock() - self._finalize_at) * 1000.0,
                        labels={"provider": _PRIMARY_PROVIDER},
                    )
                    self._finalize_at = None
        self.events.put_nowait(event)
        return False

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        if self._closed:
            return
        primary = self._primary
        if primary is not None and not self._degraded:
            await self._drain_primary()
            primary = self._primary
        if primary is not None and not self._degraded:
            # The budget runs from the negative packet, not from speech onset.
            # The backup re-recognises the whole segment and cannot start until
            # the audio ends, so judging a primary that is still streaming buys
            # nothing. Cancelling the rotation is safe: the primary aborts its
            # own task in a finally block.
            self._finalize_at = self._clock()
            try:
                await asyncio.wait_for(
                    primary.rotate_task(require_consumed=require_consumed),
                    timeout=self.config.final_result_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                await self._degrade("final_result_timeout")
            except Exception:
                await self._degrade("provider_error")
            else:
                await self._drain_primary()
                if not self._degraded:
                    # A connected primary that returned a task boundary is a
                    # healthy segment even when the transcript is empty.
                    self._record_primary_success()
        # A healthy primary that returns an empty transcript still has real
        # audio.  Re-recognise it on the backup chain before dropping the
        # segment, otherwise a silent vendor final becomes a turn with no
        # reply while the PCM is discarded here.
        pcm, _ = self._voiced_segment_pcm()
        self._last_segment_had_speech = bool(pcm) and self._has_speech_energy(pcm)
        self._last_segment_provider_failed = self._degraded
        self._last_segment_backup_retried = False
        if self._degraded or self._primary is None:
            if self._primary_final_seen:
                self._emit_task_finished()
            else:
                self._last_segment_backup_retried = True
                await self._emit_backup_result()
        elif not self._primary_final_seen:
            self._last_segment_backup_retried = True
            budget_s = min(self.config.backup_timeout_s, _HEALTHY_EMPTY_RETRY_BUDGET_S)
            try:
                await asyncio.wait_for(
                    self._emit_backup_result(emit_finished=False),
                    timeout=budget_s,
                )
            except TimeoutError:
                logger.info(
                    "asr failover empty retry timed out budget_s=%s",
                    budget_s,
                )
        self._rotation_pending = True
        self._finalize_at = None
        self._reset_segment_evidence()

    async def _emit_backup_result(self, *, emit_finished: bool = True) -> bool:
        pcm, begin_ms = self._voiced_segment_pcm()
        samples = len(pcm) // 2
        if not pcm or not self._has_speech_energy(pcm):
            if emit_finished:
                self._emit_task_finished()
            return False
        text = await self._transcribe_backup(pcm)
        if text is None:
            if self.metrics is not None:
                self.metrics.inc_media_metric("asr_both_failed_total")
            logger.info(
                "asr failover both providers failed segment_ms=%s reason=%s",
                round(samples * 1000 / 16_000),
                self._degrade_reason or "unknown",
            )
            if emit_finished:
                self._emit_task_finished()
            return False
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
        if emit_finished:
            self._emit_task_finished()
        if self.metrics is not None:
            self.metrics.inc_media_metric(
                "asr_failover_total",
                labels={"provider": "backup", "reason": "recovered"},
            )
        return True

    def _voiced_segment_pcm(self) -> tuple[bytes, int]:
        """The utterance after VAD onset, capped to the backup budget.

        A degraded segment spans the whole provider task, which begins before
        the child speaks. Prefer the bridge's VAD onset, fall back to the first
        speech-energy frame, then keep only the last ``max_segment_s`` seconds.
        The returned begin offset keeps the synthetic final on the absolute
        clock.
        """

        start = self._segment_pcm_start
        if start is None:
            return b"", 0
        voiced = self._vad_start_sample
        if voiced is None:
            voiced = self._segment_voiced_start
        speech_start = start if voiced is None else max(start, voiced)
        offset = speech_start - start
        pcm = self._segment_pcm[offset * 2 :]
        max_samples = int(self.config.max_segment_s * 16_000)
        if len(pcm) // 2 > max_samples:
            dropped = len(pcm) // 2 - max_samples
            pcm = pcm[dropped * 2 :]
            offset += dropped
            logger.info(
                "asr failover backup truncated from_samples=%s to_samples=%s cap_s=%s",
                len(self._segment_pcm) // 2 - (speech_start - start),
                max_samples,
                self.config.max_segment_s,
            )
        return bytes(pcm), round(offset * 1000 / 16_000)

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
        self._vad_start_sample = None

    async def reset_for_gap(self) -> None:
        """Drop an idle primary task before the first frame after a capture gap.

        The next ``send_pcm`` arms a fresh task. No backup result is emitted:
        the audio before the gap belongs to an earlier utterance or silence.
        """

        if self._closed:
            return
        self._release_probe()
        primary = self._primary
        self._primary = None
        if primary is not None:
            with contextlib.suppress(Exception):
                await primary.aclose()
        self._degraded = False
        self._degrade_reason = None
        self._rotation_pending = True
        self._finalize_at = None
        self._reset_segment_evidence()
        logger.info("asr failover reset for upstream gap task_epoch=%s", self._task_epoch)

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
    asr_provider: ASREngine,
    funasr_config: FunASRConfig,
    metrics: Any = None,
    env: dict[str, str] | None = None,
) -> Callable[[], StreamingASRSession]:
    """Build the session the selector names, failing the deploy on bad config.

    ``funasr`` is the plain DashScope session. ``volc`` is this chain, and its
    backups are resolved here so a missing SenseVoice endpoint or a missing
    Volc credential fails startup instead of every device session. A new
    engine value cannot reach here without a type error, because ``match``
    ends in ``assert_never``.
    """

    match asr_provider:
        case "funasr":
            return lambda: FunASRSession(replace(funasr_config), metrics=metrics)
        case "volc":
            pass
        case _ as unreachable:
            assert_never(unreachable)
    settings = ASRFailoverConfig.from_env(env)
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
