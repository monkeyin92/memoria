"""StepFun primary with a one-way Doubao backup per media session.

The wrapper keeps the state machine out of either vendor adapter. A session
starts on StepFun; the first failure moves it to Doubao and it never moves
back. A failure before the first PCM of a pushed sentence replays that whole
sentence on Doubao. A failure after PCM already reached the caller drops the
rest of that sentence and starts the next one on Doubao, so no sentence is
stitched across engines or replayed halfway.
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

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.provider_errors import APIConnectOptions
from services.agent.src.providers.stepfun_tts import BracketInstructionFilter
from services.agent.src.providers.tts_stream import SynthesizedAudio
from services.common.tts_voice_policy import is_clone_or_icl_voice

logger = logging.getLogger(__name__)

STEPFUN_PRIMARY_PROVIDER = "stepfun_primary"
_NO_RETRY_CONNECT_OPTIONS = APIConnectOptions(max_retry=0)


@dataclass
class StepFunFailoverConfig:
    """Process-level circuit thresholds for the StepFun primary."""

    failures: int = 3
    open_s: float = 30.0
    auth_open_s: float = 3600.0
    pool_size: int = 2

    def __post_init__(self) -> None:
        if self.failures < 1:
            raise ValueError("StepFun breaker failures must be positive")
        if self.open_s <= 0 or self.auth_open_s <= 0:
            raise ValueError("StepFun breaker open seconds must be positive")
        if self.pool_size < 1:
            raise ValueError("StepFun failover pool_size must be positive")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StepFunFailoverConfig:
        import os

        values = dict(os.environ) if env is None else env
        return cls(
            failures=int(values.get("STEPFUN_TTS_BREAKER_FAILURES", "3")),
            open_s=float(values.get("STEPFUN_TTS_BREAKER_OPEN_S", "30")),
            auth_open_s=float(values.get("STEPFUN_TTS_AUTH_BREAKER_OPEN_S", "3600")),
            pool_size=int(values.get("STEPFUN_TTS_POOL_SIZE", "2")),
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
        auth_open_s: float = 3600.0,
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

    def allow(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            elapsed = self._clock() - (self._opened_at or 0.0)
            if elapsed < self._current_open_s:
                return False
            self._state = "half_open"
        if self._probe_in_flight:
            return False
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


class _StreamEnd:
    pass


_STREAM_END = _StreamEnd()


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
            use_primary = not self._owner.degraded and self._owner.breaker.allow()
            if not use_primary:
                self._owner.mark_degraded("breaker_open")
            audio_started = False
            while True:
                try:
                    stream = await self._open_stream(use_primary)
                except Exception as exc:
                    if not use_primary:
                        self._events.put_nowait(_Failure(exc))
                        self._events.put_nowait(_STREAM_END)
                        return
                    self._owner.record_failure(exc)
                    self._owner.mark_degraded(type(exc).__name__)
                    use_primary = False
                    continue
                self._current_stream = stream
                try:
                    pushed = stream.push_text(unit)
                    if inspect.isawaitable(pushed):
                        await pushed
                    ended = stream.end_input()
                    if inspect.isawaitable(ended):
                        await ended
                    async for event in stream:
                        if not audio_started:
                            audio_started = True
                            self._owner._report_sentence(
                                index=index,
                                use_primary=use_primary,
                            )
                        self._events.put_nowait(event)
                except asyncio.CancelledError:
                    await self._close_stream(stream)
                    raise
                except Exception as exc:
                    await self._close_stream(stream)
                    if not use_primary:
                        self._events.put_nowait(_Failure(exc))
                        self._events.put_nowait(_STREAM_END)
                        return
                    self._owner.record_failure(exc)
                    self._owner.mark_degraded(type(exc).__name__)
                    logger.warning(
                        "StepFun TTS failed; switching sentence to Doubao "
                        "sentence_index=%s audio_started=%s error_type=%s auth=%s",
                        index,
                        audio_started,
                        type(exc).__name__,
                        getattr(exc, "auth_failure", False),
                    )
                    if audio_started:
                        # The rest of this sentence is not replayed. The next
                        # complete pushed sentence starts on Doubao.
                        break
                    use_primary = False
                    continue
                else:
                    if use_primary:
                        self._owner.record_success()
                    await self._close_stream(stream)
                    break
            index += 1
            self._current_stream = None

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

    @property
    def provider(self) -> str:
        return "stepfun"

    @property
    def model(self) -> str:
        return str(getattr(self.primary, "model", "stepaudio-3-tts"))

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

    def record_failure(self, exc: BaseException) -> None:
        auth = bool(getattr(exc, "auth_failure", False))
        self.breaker.record_failure(auth=auth)
        if auth:
            logger.warning(
                "StepFun TTS authentication/quota failure; long circuit open "
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

    def bind_fence(self, fence: GenerationFence) -> None:
        self._fence = fence
        for provider in (self.primary, self.backup):
            bind = getattr(provider, "bind_fence", None)
            if callable(bind):
                bind(fence)

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
            logger.warning(
                "tts_failover_kept_configured_backup_voice reason=clone_or_icl_voice"
            )
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
        fence = self._fence
        callback = self._sentence_callback
        if fence is None or callback is None:
            return
        provider = self.primary if use_primary else self.backup
        engine = str(getattr(provider, "provider", "stepfun" if use_primary else "volcengine_doubao"))
        model = str(getattr(provider, "current_model", getattr(provider, "model", "")))
        voice = str(
            getattr(
                provider,
                "current_voice",
                getattr(provider, "voice", ""),
            )
        )
        try:
            callback(fence, index, engine, model, voice, not use_primary)
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
]
