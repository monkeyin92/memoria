"""StepFun primary with a Doubao backup for ordinary media replies.

The wrapper keeps the state machine out of either vendor adapter. A session
starts on StepFun; a failed unit is re-spoken whole on the backup, and the next
unit returns to StepFun unless the process breaker is open. A single failure
never pins the session to Doubao.

An approved fixed-safety script is normally served by the pre-synthesized clip
before this wrapper is reached. If that clip is missing or fails its manifest
hash, only StepFun is a candidate: the four shipped approved clips are the
media adapter's next rung, and Doubao never speaks a crisis turn. A fixed-safety
take is buffered whole, so a drop after part of the line is discarded before it
reaches the caller. If StepFun fails there is no runtime audio to approve, and
the adapter falls back to its sha256-verified clip instead of leaving the child
in silence. Only a complete take is ever forwarded, so a half line can never
join a whole line.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from websockets.exceptions import ConnectionClosed

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.provider_errors import (
    TTS_UNAVAILABLE_STANDBY_REASON,
    APIConnectOptions,
    TTSUnavailableError,
)
from services.agent.src.providers.stepfun_tts import BracketInstructionFilter
from services.agent.src.providers.tts_stream import SynthesizedAudio
from services.common.fixed_safety_scripts import fixed_safety_echo_reference
from services.common.tts_defaults import STEPFUN_TTS_MODEL
from services.common.tts_voice_policy import is_clone_or_icl_voice

logger = logging.getLogger(__name__)

STEPFUN_PRIMARY_PROVIDER = "stepfun_primary"
_NO_RETRY_CONNECT_OPTIONS = APIConnectOptions(max_retry=0)


def _env_bool(values: dict[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass
class StepFunFailoverConfig:
    """Process-level circuit thresholds and the fixed-safety opening budget.

    A crisis line must reach the child inside five seconds. StepFun gets at most
    ``fixed_safety_primary_first_audio_s`` and the attempts share
    ``fixed_safety_budget_s``. This budget applies only to the live fallback
    used when the approved stored clip is unavailable. Doubao is not a
    candidate for that unit.

    ``fixed_safety_primary_first`` is accepted for deployed configuration
    compatibility and has no effect: a crisis unit always starts on StepFun and
    then moves to the shipped approved clip, never to Doubao.
    """

    failures: int = 3
    open_s: float = 30.0
    auth_open_s: float = 300.0
    pool_size: int = 2
    fixed_safety_budget_s: float = 4.5
    fixed_safety_primary_first_audio_s: float = 1.5
    fixed_safety_primary_first: bool = True

    def __post_init__(self) -> None:
        if self.failures < 1:
            raise ValueError("StepFun breaker failures must be positive")
        if self.open_s <= 0 or self.auth_open_s <= 0:
            raise ValueError("StepFun breaker open seconds must be positive")
        if self.pool_size < 1:
            raise ValueError("StepFun failover pool_size must be positive")
        for name in (
            "fixed_safety_budget_s",
            "fixed_safety_primary_first_audio_s",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"StepFun failover {name} must be positive")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StepFunFailoverConfig:
        import os

        values = dict(os.environ) if env is None else env
        return cls(
            failures=int(values.get("STEPFUN_TTS_BREAKER_FAILURES", "3")),
            open_s=float(values.get("STEPFUN_TTS_BREAKER_OPEN_S", "30")),
            auth_open_s=float(values.get("STEPFUN_TTS_AUTH_BREAKER_OPEN_S", "300")),
            pool_size=int(values.get("STEPFUN_TTS_POOL_SIZE", "2")),
            fixed_safety_budget_s=float(values.get("STEPFUN_TTS_FIXED_SAFETY_BUDGET_S", "4.5")),
            fixed_safety_primary_first_audio_s=float(
                values.get("STEPFUN_TTS_FIXED_SAFETY_PRIMARY_FIRST_AUDIO_S", "1.5")
            ),
            fixed_safety_primary_first=_env_bool(
                values, "STEPFUN_TTS_FIXED_SAFETY_PRIMARY_FIRST", True
            ),
        )


class TTSFailoverBreaker:
    """Process-wide StepFun breaker in the shape of ``ProcessASRBreaker``.

    It never raises; ``allow`` returns ``False`` so a session sends the next
    sentence to Doubao. One half-open probe at a time keeps a recovering
    provider from receiving every session at once. Authentication and balance
    failures open a much longer window than transient transport failures.
    """

    def __init__(
        self,
        *,
        failures: int = 3,
        open_s: float = 30.0,
        auth_open_s: float = 300.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.failures = failures
        self.open_s = open_s
        self.auth_open_s = auth_open_s
        self._clock = clock
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._current_open_s = open_s
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def configure(self, config: StepFunFailoverConfig) -> None:
        """Apply environment thresholds to the process-wide singleton."""

        self.failures = config.failures
        self.open_s = config.open_s
        self.auth_open_s = config.auth_open_s
        if self._state == "closed":
            self._current_open_s = config.open_s

    def available(self) -> bool:
        """Whether a unit could start now without claiming the half-open probe."""

        if self._state == "closed":
            return True
        if self._state == "open":
            elapsed = self._clock() - (self._opened_at or 0.0)
            if elapsed < self._current_open_s:
                return False
        return not self._probe_in_flight

    def allow(self) -> bool:
        if not self.available():
            return False
        if self._state == "open":
            self._state = "half_open"
        if self._state == "half_open":
            self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = None
        self._current_open_s = self.open_s
        self._probe_in_flight = False

    def record_failure(self, *, auth: bool = False) -> None:
        self._probe_in_flight = False
        self._consecutive_failures += 1
        if auth or self._state == "half_open" or self._consecutive_failures >= self.failures:
            self._state = "open"
            self._opened_at = self._clock()
            self._current_open_s = self.auth_open_s if auth else self.open_s


PROCESS_TTS_BREAKER = TTSFailoverBreaker()


@dataclass(slots=True)
class _Failure:
    error: BaseException


@dataclass(slots=True)
class _UnitProgress:
    """First-audio state visible to the caller even when the attempt raises."""

    audio_started: bool = False


class _StreamEnd:
    pass


_STREAM_END = _StreamEnd()


def is_benign_stream_close(exc: BaseException) -> bool:
    """True for a normal close or a cancellation caused by stop/barge-in.

    ``websockets`` reports a server-side clean close as ``ConnectionClosedOK``
    (code 1000). The provider task can also surface it while a stop tears the
    socket down. Neither is a provider outage, and moving the session to the
    backup for it is what silenced every later turn in the 2026-10-22 field run.
    """

    if isinstance(exc, asyncio.CancelledError):
        return True
    return isinstance(exc, ConnectionClosed) and exc.code == 1000


@dataclass(slots=True)
class _FailoverPool:
    primary: Any
    backup: Any

    async def warm(self, size: int | None = None) -> None:
        calls: list[Any] = []
        for provider in (self.primary, self.backup):
            pool = getattr(provider, "pool", None)
            warm = getattr(pool, "warm", None)
            if callable(warm):
                calls.append(warm(size))
        if not calls:
            return
        results = await asyncio.gather(*calls, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        calls: list[Any] = []
        for provider in (self.primary, self.backup):
            pool = getattr(provider, "pool", None)
            discard = getattr(pool, "discard_active_connection", None)
            if callable(discard):
                calls.append(discard(fence))
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)

    async def aclose(self) -> None:
        await asyncio.gather(
            *(provider.aclose() for provider in (self.primary, self.backup)),
            return_exceptions=True,
        )

    @property
    def available_approx(self) -> int:
        total = 0
        for provider in (self.primary, self.backup):
            value = getattr(getattr(provider, "pool", None), "available_approx", 0)
            total += int(value) if isinstance(value, int) else 0
        return total


class StepFunFailoverStream:
    """Proxy one reply's pushed sentences across the StepFun→Doubao boundary."""

    def __init__(
        self,
        owner: StepFunPrimaryTTS,
        *,
        conn_options: APIConnectOptions,
    ) -> None:
        self._owner = owner
        self._conn_options = conn_options
        self._input: asyncio.Queue[str | _StreamEnd] = asyncio.Queue()
        self._events: asyncio.Queue[SynthesizedAudio | _Failure | _StreamEnd] = asyncio.Queue()
        self._bracket_filter = BracketInstructionFilter()
        self._task = asyncio.create_task(self._run(), name="stepfun-tts-failover")
        self._current_stream: Any | None = None
        self._closed = False

    def push_text(self, token: str) -> None:
        if self._closed or not token:
            return
        cleaned = self._bracket_filter.feed(token)
        if cleaned:
            self._input.put_nowait(cleaned)

    def end_input(self) -> None:
        if self._closed:
            return
        self._input.put_nowait(_STREAM_END)

    async def _run(self) -> None:
        try:
            await self._run_units()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._events.put_nowait(_Failure(exc))
            self._events.put_nowait(_STREAM_END)

    async def _run_units(self) -> None:
        index = 0
        while True:
            item = await self._input.get()
            if isinstance(item, _StreamEnd):
                self._events.put_nowait(_STREAM_END)
                return
            unit = item
            if not unit.strip():
                continue
            fixed_safety_unit = fixed_safety_echo_reference(unit) is not None
            candidates = self._engine_candidates(fixed_safety_unit)
            deadline = (
                monotonic() + self._owner._config.fixed_safety_budget_s
                if fixed_safety_unit
                else None
            )
            if fixed_safety_unit:
                self._owner.trace(
                    "tts_fixed_safety_live_fallback",
                    "ok",
                    {
                        "chars": len(unit),
                        "primary_first": candidates[0],
                    },
                )
            first_engine: tuple[Any, bool] | None = None
            progress = _UnitProgress()
            attempt = 0
            while attempt < len(candidates):
                use_primary = candidates[attempt]
                has_next = attempt + 1 < len(candidates)
                if deadline is not None and monotonic() >= deadline:
                    self._put_failure(TimeoutError("fixed safety opening budget exhausted"))
                    return
                # A fixed-safety attempt is held until it finishes, so a drop
                # after part of the line never reaches the caller. The adapter
                # sees one whole take from one engine, or nothing.
                buffered: list[SynthesizedAudio] | None = [] if fixed_safety_unit else None
                # Non-fixed units keep one progress marker across engines so a
                # partial StepFun take still reports the engine that opened the
                # unit. A fixed-safety attempt starts clean because its partial
                # take was discarded before this retry.
                attempt_progress = _UnitProgress() if fixed_safety_unit else progress
                try:
                    stream = await self._open_stream(use_primary)
                except Exception as exc:
                    if is_benign_stream_close(exc):
                        self._events.put_nowait(_STREAM_END)
                        return
                    if not has_next:
                        if not use_primary:
                            self._owner.record_backup_failure()
                        self._put_failure(
                            self._exhaustion_error(exc, fixed_safety_unit)
                        )
                        return
                    self._record_engine_failure(
                        use_primary,
                        exc,
                        index=index,
                        audio_started=attempt_progress.audio_started,
                        fixed_safety_unit=fixed_safety_unit,
                        chars=len(unit),
                    )
                    # `self._owner.primary` still names the engine whose reopen
                    # failed; without this the unit would claim the backup spoke
                    # from its first packet.
                    if use_primary and first_engine is None:
                        first_engine = (self._owner.primary, False)
                    attempt += 1
                    continue
                self._current_stream = stream
                try:
                    await self._consume_unit(
                        stream,
                        unit=unit,
                        use_primary=use_primary,
                        index=index,
                        progress=attempt_progress,
                        buffered=buffered,
                        first_audio_timeout=self._first_audio_timeout(
                            fixed_safety_unit,
                            deadline,
                        ),
                    )
                except asyncio.CancelledError:
                    await self._close_stream(stream)
                    raise
                except Exception as exc:
                    await self._close_stream(stream)
                    if buffered is not None:
                        # Doubao dropped mid-line. The child has not heard it
                        # yet, so discard the partial take and speak the whole
                        # approved line from scratch on StepFun.
                        buffered.clear()
                    if is_benign_stream_close(exc):
                        if use_primary and attempt_progress.audio_started:
                            self._owner.record_success()
                        self._events.put_nowait(_STREAM_END)
                        return
                    if not has_next:
                        if not use_primary:
                            self._owner.record_backup_failure()
                        self._put_failure(
                            self._exhaustion_error(exc, fixed_safety_unit)
                        )
                        return
                    self._record_engine_failure(
                        use_primary,
                        exc,
                        index=index,
                        audio_started=attempt_progress.audio_started,
                        fixed_safety_unit=fixed_safety_unit,
                        chars=len(unit),
                    )
                    if use_primary and attempt_progress.audio_started and first_engine is None:
                        first_engine = (self._owner.primary, True)
                    attempt += 1
                    continue
                await self._close_stream(stream)
                if fixed_safety_unit and not attempt_progress.audio_started:
                    # A cleanly closed stream that produced no PCM is an outage
                    # like any other; the child still needs the approved line.
                    if has_next:
                        self._record_engine_failure(
                            use_primary,
                            RuntimeError("TTS returned no audio"),
                            index=index,
                            audio_started=False,
                            fixed_safety_unit=True,
                            chars=len(unit),
                        )
                        attempt += 1
                        continue
                    if not use_primary:
                        self._owner.record_backup_failure()
                    self._put_failure(RuntimeError("fixed safety script produced no audio"))
                    return
                if buffered is not None:
                    self._flush_buffered_unit(
                        buffered,
                        use_primary=use_primary,
                        index=index,
                    )
                if use_primary:
                    self._owner.record_success()
                break
            if first_engine is not None:
                self._owner._last_engine = first_engine[0]
            index += 1
            self._current_stream = None

    def _engine_candidates(self, fixed_safety_unit: bool) -> tuple[bool, ...]:
        """Engine order for one unit; ``True`` means the StepFun primary.

        A fixed-safety unit has exactly one candidate. Doubao never speaks a
        crisis turn: a StepFun failure raises and the media adapter moves to
        its sha256-verified approved clip. Every other unit starts on StepFun
        and may use Doubao for that unit only; the next unit retries StepFun
        as soon as the breaker allows, so a single failure is not sticky.
        """

        if fixed_safety_unit:
            return (True,)
        if self._owner.primary_allowed():
            self._owner.clear_degraded("breaker_closed")
            return (True, False)
        self._owner.mark_degraded("breaker_open")
        return (False,)

    def _first_audio_timeout(
        self,
        fixed_safety_unit: bool,
        deadline: float | None,
    ) -> float | None:
        """Seconds allowed before StepFun must deliver its first packet."""

        if not fixed_safety_unit:
            return None
        per_engine = self._owner._config.fixed_safety_primary_first_audio_s
        if deadline is None:
            return per_engine
        return max(0.0, min(per_engine, deadline - monotonic()))

    async def _consume_unit(
        self,
        stream: Any,
        *,
        unit: str,
        use_primary: bool,
        index: int,
        progress: _UnitProgress,
        buffered: list[SynthesizedAudio] | None,
        first_audio_timeout: float | None,
    ) -> None:
        """Push one unit and forward or buffer its events.

        ``buffered`` is set only for an approved fixed-safety unit. Nothing is
        released until the attempt succeeds, so a provider drop after part of
        the line cannot reach the caller as a truncated take.
        """

        pushed = stream.push_text(unit)
        if inspect.isawaitable(pushed):
            await pushed
        ended = stream.end_input()
        if inspect.isawaitable(ended):
            await ended
        iterator = aiter(stream)
        while True:
            try:
                if not progress.audio_started and first_audio_timeout is not None:
                    event = await asyncio.wait_for(
                        anext(iterator),
                        timeout=first_audio_timeout,
                    )
                else:
                    event = await anext(iterator)
            except StopAsyncIteration:
                return
            if not progress.audio_started:
                progress.audio_started = True
                if buffered is None:
                    # Remember the engine that speaks this unit; the media
                    # adapter reports it as sentence provenance.
                    self._owner._last_engine = (
                        self._owner.primary if use_primary else self._owner.backup
                    )
                    self._owner._report_sentence(index=index, use_primary=use_primary)
            if buffered is not None:
                buffered.append(event)
            else:
                self._events.put_nowait(event)

    def _flush_buffered_unit(
        self,
        buffered: list[SynthesizedAudio],
        *,
        use_primary: bool,
        index: int,
    ) -> None:
        """Release one complete fixed-safety take and report its engine once."""

        if not buffered:
            return
        self._owner._last_engine = self._owner.primary if use_primary else self._owner.backup
        self._owner._report_sentence(index=index, use_primary=use_primary)
        for event in buffered:
            self._events.put_nowait(event)

    def _record_engine_failure(
        self,
        use_primary: bool,
        exc: BaseException,
        *,
        index: int,
        audio_started: bool,
        fixed_safety_unit: bool,
        chars: int,
    ) -> None:
        if use_primary:
            self._owner.record_failure(exc)
            # One failed unit may use Doubao, but it must not pin the session
            # there. Only an open breaker keeps later units on the backup.
            if not self._owner.primary_allowed():
                self._owner.mark_degraded("breaker_open")
            logger.warning(
                "StepFun TTS failed; switching sentence to Doubao "
                "sentence_index=%s audio_started=%s error_type=%s auth=%s",
                index,
                audio_started,
                type(exc).__name__,
                getattr(exc, "auth_failure", False),
            )
            return
        # The backup's failure must not pin the rest of the session to it. The
        # next unit starts on StepFun again, so a Doubao 403 cannot leave every
        # later turn silent after a one-off StepFun cancellation.
        self._owner.record_backup_failure()
        logger.warning(
            "Doubao TTS failed%s sentence_index=%s audio_started=%s error_type=%s",
            " for an approved safety script" if fixed_safety_unit else "",
            index,
            audio_started,
            type(exc).__name__,
        )
        if fixed_safety_unit:
            self._owner.trace(
                "tts_fixed_safety_stepfun_fallback",
                "degraded",
                {"reason": type(exc).__name__, "chars": chars},
            )

    def _put_failure(self, exc: BaseException) -> None:
        self._events.put_nowait(_Failure(exc))
        self._events.put_nowait(_STREAM_END)

    @staticmethod
    def _exhaustion_error(exc: BaseException, fixed_safety_unit: bool) -> BaseException:
        if fixed_safety_unit:
            return exc
        return TTSUnavailableError(f"all TTS engines failed: {type(exc).__name__}: {exc}")

    async def _open_stream(self, use_primary: bool) -> Any:
        provider = self._owner.primary if use_primary else self._owner.backup
        bind = getattr(provider, "bind_fence", None)
        if callable(bind) and self._owner.fence is not None:
            result = bind(self._owner.fence)
            if inspect.isawaitable(result):
                await result
        stream = provider.stream(conn_options=self._conn_options)
        if inspect.isawaitable(stream):
            stream = await stream
        return stream

    async def _close_stream(self, stream: Any) -> None:
        close = getattr(stream, "aclose", None)
        if callable(close):
            with contextlib.suppress(Exception):
                result = close()
                if inspect.isawaitable(result):
                    await result
        if self._current_stream is stream:
            self._current_stream = None

    def timed_transcript(self) -> tuple[Any, ...]:
        # A reply may span several per-sentence streams. Returning one
        # stream's words would mislabel the whole reply, so the adapter uses
        # its whole-reply ledger span instead.
        return ()

    def timed_transcript_alignment(self) -> str:
        return "unavailable"

    def __aiter__(self) -> AsyncIterator[SynthesizedAudio]:
        return self

    async def __anext__(self) -> SynthesizedAudio:
        item = await self._events.get()
        if isinstance(item, _StreamEnd):
            raise StopAsyncIteration
        if isinstance(item, _Failure):
            raise item.error
        return item

    async def aclose(self) -> None:
        self._closed = True
        self._task.cancel()
        await asyncio.wait((self._task,))
        stream, self._current_stream = self._current_stream, None
        if stream is not None:
            await self._close_stream(stream)

    async def __aenter__(self) -> StepFunFailoverStream:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"StepFunFailoverStream(units={self._input.qsize()})"


class StepFunPrimaryTTS:
    """Session-scoped StepFun primary with a permanent Doubao backup."""

    def __init__(
        self,
        *,
        primary: Any,
        backup: Any,
        config: StepFunFailoverConfig | None = None,
        breaker: TTSFailoverBreaker | None = None,
    ) -> None:
        self.primary = primary
        self.backup = backup
        self._config = config or StepFunFailoverConfig()
        self.breaker = breaker or TTSFailoverBreaker(
            failures=self._config.failures,
            open_s=self._config.open_s,
            auth_open_s=self._config.auth_open_s,
        )
        self._degraded = False
        self._degrade_reason = ""
        self._force_backup = False
        self._fence: GenerationFence | None = None
        self._trace_callback: Callable[[str, str, dict[str, Any] | None], None] | None = None
        self._sentence_callback: (
            Callable[[GenerationFence, int, str, str, str, bool], None] | None
        ) = None
        self._last_engine: Any | None = None

    @property
    def provider(self) -> str:
        return "stepfun"

    @property
    def model(self) -> str:
        return str(getattr(self.primary, "model", STEPFUN_TTS_MODEL))

    @property
    def sample_rate(self) -> int:
        return int(getattr(self.primary, "sample_rate", 24000))

    @property
    def pool(self) -> _FailoverPool:
        return _FailoverPool(self.primary, self.backup)

    @property
    def fence(self) -> GenerationFence | None:
        return self._fence

    @property
    def degraded(self) -> bool:
        return self._degraded or self._force_backup

    @property
    def degrade_reason(self) -> str:
        if self._degrade_reason:
            return self._degrade_reason
        return "personal_voice" if self._force_backup else ""

    def mark_degraded(self, reason: str) -> None:
        if self._force_backup:
            return
        if not self._degraded:
            self._degraded = True
            self._degrade_reason = reason
            self.trace(
                "tts_primary_failover",
                "degraded",
                {"reason": reason},
            )

    def clear_degraded(self, reason: str) -> None:
        """Return to StepFun once the breaker allows it again."""

        if self._force_backup or not self._degraded:
            return
        self._degraded = False
        self._degrade_reason = ""
        self.trace("tts_primary_recovered", "ok", {"reason": reason})

    def primary_allowed(self) -> bool:
        """Whether the next ordinary unit may start on StepFun."""

        if self._force_backup:
            return False
        return self.breaker.allow()

    def primary_available(self) -> bool:
        """Whether StepFun could start now, without claiming a half-open probe."""

        if self._force_backup:
            return False
        return self.breaker.available()

    def record_failure(self, exc: BaseException) -> None:
        auth = bool(getattr(exc, "auth_failure", False))
        self.breaker.record_failure(auth=auth)
        if auth:
            logger.warning(
                "StepFun TTS authentication/quota failure; auth circuit open "
                "error_type=%s breaker_state=%s",
                type(exc).__name__,
                self.breaker.state,
            )
            self.trace(
                "tts_primary_auth_failure",
                "error",
                {"error_type": type(exc).__name__},
            )

    def record_success(self) -> None:
        self.breaker.record_success()

    def record_backup_failure(self) -> None:
        """Forget a primary downgrade once the backup also failed this unit."""

        self.clear_degraded("backup_failed")

    def bind_fence(self, fence: GenerationFence) -> None:
        self._fence = fence
        self._last_engine = None
        for provider in (self.primary, self.backup):
            bind = getattr(provider, "bind_fence", None)
            if callable(bind):
                bind(fence)

    def last_engine(self) -> Any:
        """The engine the most recent unit actually came from, or the bound default."""

        if self._last_engine is not None:
            return self._last_engine
        return self.bound_engine()

    def bound_engine(self) -> Any:
        """The engine a pushed unit starts on, given degradation and voice binding."""

        if self._degraded or not self.primary_available():
            return self.backup
        return self.primary

    def set_trace_callback(
        self,
        callback: Callable[[str, str, dict[str, Any] | None], None],
    ) -> None:
        self._trace_callback = callback
        for provider in (self.primary, self.backup):
            setter = getattr(provider, "set_trace_callback", None)
            if callable(setter):
                setter(callback)

    def set_tts_sentence_callback(
        self,
        callback: Callable[[GenerationFence, int, str, str, str, bool], None],
    ) -> None:
        self._sentence_callback = callback

    def set_alignment_callback(self, callback: Callable[[GenerationFence, str, str], None]) -> None:
        setter = getattr(self.backup, "set_alignment_callback", None)
        if callable(setter):
            setter(callback)

    def set_voice_fallback_callback(
        self,
        callback: Callable[[GenerationFence, str, str, str, str], None],
    ) -> None:
        setter = getattr(self.backup, "set_voice_fallback_callback", None)
        if callable(setter):
            setter(callback)

    def trace(
        self,
        name: str,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self._trace_callback is not None:
            self._trace_callback(name, status, detail)

    def apply_speech_plan(
        self,
        *,
        emotion: str,
        rate: float,
        instruction: str = "",
        pitch: int = 0,
        loudness_rate: int = 0,
        reference_contexts: tuple[str, ...] = (),
        fence: GenerationFence | None = None,
    ) -> None:
        for provider in (self.primary, self.backup):
            apply_plan = getattr(provider, "apply_speech_plan", None)
            if callable(apply_plan):
                apply_plan(
                    emotion=emotion,
                    rate=rate,
                    instruction=instruction,
                    pitch=pitch,
                    loudness_rate=loudness_rate,
                    reference_contexts=reference_contexts,
                    fence=fence,
                )

    def apply_voice_profile(
        self,
        *,
        model: str,
        voice: str,
        profile_id: str | None = None,
        provider: str | None = None,
        voice_kind: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        # A personal clone is a Doubao-only voice contract. StepFun must not
        # speak a different voice under that identity, and this branch's backup
        # keeps its configured designed voice instead of adopting the clone.
        if is_clone_or_icl_voice(
            voice=voice,
            voice_kind=voice_kind,
            resource_id=resource_id,
            provider=provider,
        ):
            self._force_backup = voice_kind == "personal"
            logger.warning("tts_failover_kept_configured_backup_voice reason=clone_or_icl_voice")
            return
        apply_profile = getattr(self.backup, "apply_voice_profile", None)
        if callable(apply_profile):
            apply_profile(
                model=model,
                voice=voice,
                profile_id=profile_id,
                provider=provider,
                voice_kind=voice_kind,
                resource_id=resource_id,
            )

    def configure_personal_fallback(self, **kwargs: Any) -> None:
        configure = getattr(self.backup, "configure_personal_fallback", None)
        if callable(configure):
            configure(**kwargs)

    def clear_personal_fallback(self) -> None:
        clear = getattr(self.backup, "clear_personal_fallback", None)
        if callable(clear):
            clear()

    def use_baseline_voice(self) -> None:
        self._force_backup = False
        for provider in (self.primary, self.backup):
            baseline = getattr(provider, "use_baseline_voice", None)
            if callable(baseline):
                baseline()

    @property
    def current_model(self) -> str:
        return str(getattr(self.backup, "current_model", "seed-tts-2.0"))

    @property
    def current_voice(self) -> str:
        return str(getattr(self.backup, "current_voice", ""))

    @property
    def current_voice_profile_id(self) -> str | None:
        value = getattr(self.backup, "current_voice_profile_id", None)
        return value if isinstance(value, str) else None

    @property
    def current_voice_kind(self) -> str:
        return str(getattr(self.backup, "current_voice_kind", "designed"))

    @property
    def current_instruction(self) -> str | None:
        value = getattr(self.primary, "current_instruction", None)
        return value if isinstance(value, str) else None

    @property
    def current_rate(self) -> float:
        return 1.0

    def _report_sentence(self, *, index: int, use_primary: bool) -> None:
        self.report_sentence_from(
            self.primary if use_primary else self.backup,
            index=index,
            switched=not use_primary,
        )

    def report_sentence_from(self, provider: Any, *, index: int, switched: bool) -> None:
        """Record which engine actually spoke one unit of this generation.

        The fixed-safety whole-script path synthesizes outside this stream and
        reports the engine it used, so per-sentence provenance stays complete.
        """

        fence = self._fence
        callback = self._sentence_callback
        if fence is None or callback is None:
            return
        engine = str(getattr(provider, "provider", "volcengine_doubao" if switched else "stepfun"))
        model = str(getattr(provider, "current_model", getattr(provider, "model", "")))
        voice = str(
            getattr(
                provider,
                "current_voice",
                getattr(provider, "voice", ""),
            )
        )
        try:
            callback(fence, index, engine, model, voice, bool(switched))
        except Exception:
            logger.warning("TTS sentence provenance callback failed", exc_info=True)

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = _NO_RETRY_CONNECT_OPTIONS,
    ) -> StepFunFailoverStream:
        return StepFunFailoverStream(self, conn_options=conn_options)

    async def warm(self) -> None:
        await self.pool.warm()

    async def aclose(self) -> None:
        await self.pool.aclose()

    def __repr__(self) -> str:
        return (
            "StepFunPrimaryTTS("
            f"degraded={self._degraded!r}, reason={self._degrade_reason!r}, "
            f"breaker={self.breaker.state!r})"
        )


__all__ = [
    "PROCESS_TTS_BREAKER",
    "STEPFUN_PRIMARY_PROVIDER",
    "StepFunFailoverConfig",
    "StepFunFailoverStream",
    "StepFunPrimaryTTS",
    "TTSFailoverBreaker",
    "TTSUnavailableError",
    "TTS_UNAVAILABLE_STANDBY_REASON",
]
