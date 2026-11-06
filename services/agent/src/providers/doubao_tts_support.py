"""Doubao TTS support records, error vocabulary, and auth latch.

``doubao_tts`` owns the websocket protocol, the connection pool and the
synthesis stream.  This module owns the bounded pieces they share: the pooled
connection record, the synthesized-result record, the provider error classes,
the process-wide credential latch, the session-context builder, and the PCM
continuity guard.  They live together so the pool, the stream and the public
adapter errors agree on one set of records.

``DoubaoTTSConfig`` stays in ``doubao_tts`` because the provider adapter and the
TTS factory import it from there; this module only reads it for typing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus
from websockets.protocol import State

from services.agent.src.contracts.events import TimedWord
from services.agent.src.providers.doubao_protocol import ServerMessage
from services.agent.src.providers.doubao_voice_catalog import DOUBAO_TTS_MODEL
from services.agent.src.providers.generation_budget import BeforeAudioError
from services.agent.src.providers.provider_errors import APIError

if TYPE_CHECKING:
    from services.agent.src.providers.doubao_tts import DoubaoTTSConfig


@dataclass
class PooledConnection:
    ws: ClientConnection
    conn_id: str
    resource_id: str = DOUBAO_TTS_MODEL
    in_use: bool = False
    burst: bool = False
    failed: bool = False
    closed: bool = False
    session_id: str = ""
    cancel_sent: bool = False

    @property
    def live(self) -> bool:
        """False once either side closed the socket.

        Doubao expires idle sockets server-side with a 1000 close frame. The
        local flags stay untouched then, so the transport state is the only
        evidence that a queued connection can still carry a session.
        """

        return not self.closed and not self.failed and self.ws.state is State.OPEN


@dataclass(frozen=True, slots=True)
class SynthesizeResult:
    pcm: bytes
    words: tuple[TimedWord, ...]
    task_id: str
    alignment_status: str
    discarded: bool = False


class DoubaoBeforeAudioError(BeforeAudioError, RuntimeError):
    """Raised only while this attempt produced no PCM, so a retry may change voice."""


class DoubaoFirstAudioTimeoutError(BeforeAudioError, TimeoutError):
    """Raised when the first-packet budget is spent before any PCM arrived."""


class DoubaoTimestampError(RuntimeError):
    """Post-audio timestamp failure: terminal for the attempt (never retried).

    Doubao delivers words alongside the audio stream; when the finished stream
    cannot be aligned the attempt is treated as a provider failure.  There is no
    alignment-only retry here, so a sentence with audio is never re-synthesized.
    """


class DoubaoPCMContinuityError(ValueError):
    pass


class DoubaoAuthRejectedError(APIError):
    """Doubao refused this process's credentials, and will keep refusing them.

    Deliberately not an ``APIStatusError``: nothing here came from a completed
    request, and a retryable marker would make the stream loop replay the
    sentence against an account that cannot speak it.
    """

    def __init__(self, message: str = "Doubao credentials were rejected") -> None:
        super().__init__(message, retryable=False)


#: Handshake rejections that mean the account, not the transport. 403 is the
#: shape a revoked or unentitled Volcengine account returns, and 401 the shape
#: a wrong key returns; both keep failing identically until an operator fixes
#: the console, so retrying them inside one sentence only delays the floor.
_AUTH_REJECTION_STATUS = frozenset({401, 403})


class DoubaoAuthLatch:
    """Process-wide memory that Doubao refused this process's credentials.

    The pool's own ``CircuitBreaker`` cannot serve this purpose. A device
    session builds its own ``DoubaoTTS`` and pool, so three consecutive
    failures never accumulate across sessions, and every new session pays three
    handshakes and a first-audio timeout before the child hears the floor line.
    Production did exactly that for a day: ``TTS pool warm failed ...
    error=InvalidStatus`` on every session start while the account answered 403.

    The latch is deliberately narrower than a breaker. Only an authentication
    rejection sets it, because that is the class of failure where another
    attempt cannot help. Transport failures keep the per-pool breaker, and the
    half-open attempt after ``open_s`` is the operator's evidence that the
    console change took effect without a restart.

    State is keyed by resource because the two Doubao resources are granted
    separately (``seed-tts-2.0`` and ``seed-icl-2.0``). A personal voice the
    account is not entitled to must not silence the companion voice that works.
    """

    def __init__(self, *, open_s: float = 300.0, clock: Callable[[], float] = monotonic) -> None:
        if open_s <= 0:
            raise ValueError("Doubao auth latch open seconds must be positive")
        self._open_s = open_s
        self._clock = clock
        self._opened_at: dict[str, float] = {}

    def configure(self, *, open_s: float) -> None:
        """Apply the deployed window, in the shape ``PROCESS_TTS_BREAKER`` uses.

        A window already running keeps its start timestamp, so changing the
        value mid-window neither restarts nor hides the suppression; the next
        rejection is the one that uses the new length.
        """

        if open_s <= 0:
            raise ValueError("Doubao auth latch open seconds must be positive")
        self._open_s = open_s

    def is_open(self, resource_id: str) -> bool:
        """True while a fresh attempt on this resource is known to fail."""

        opened_at = self._opened_at.get(resource_id)
        if opened_at is None:
            return False
        if self._clock() - opened_at >= self._open_s:
            # The window elapsed: let exactly this next attempt through. A
            # rejection closes the window again, a success clears it.
            self._opened_at.pop(resource_id, None)
            return False
        return True

    def record_rejection(self, resource_id: str) -> None:
        self._opened_at[resource_id] = self._clock()

    def record_success(self, resource_id: str) -> None:
        self._opened_at.pop(resource_id, None)


#: One latch per process. The credentials live in the process env, so a
#: rejection is a fact about the whole process rather than about one pool.
PROCESS_DOUBAO_AUTH_LATCH = DoubaoAuthLatch()


def _auth_rejection(exc: BaseException) -> bool:
    """Whether a failed Doubao handshake was refused for its credentials."""

    return isinstance(exc, InvalidStatus) and exc.response.status_code in _AUTH_REJECTION_STATUS


def _session_context_texts(config: DoubaoTTSConfig) -> tuple[str, ...]:
    """Build bounded StartSession context; TaskRequest remains synthesis-only."""
    if not config.style_control_enabled or config.resource_id != DOUBAO_TTS_MODEL:
        return ()
    instruction = ""
    if config.instruction and (instruction := config.instruction.strip()):
        instruction = instruction[:200]
    reference = next(
        (value.strip()[:320] for value in reversed(config.reference_contexts) if value.strip()),
        "",
    )
    parts: list[str] = []
    if instruction:
        parts.append(f"语音要求：{instruction}")
    if reference:
        parts.append(f"引用上文（只理解语境和承接情绪，不要朗读）：{reference}")
    # The public API exposes one context_texts array without role/order semantics.
    # Keep the first version to one short, explicit item until acoustic canaries pass.
    combined = "\n".join(parts)
    return (combined,) if combined else ()


class _PcmContinuityGuard:
    def __init__(self) -> None:
        self._pending = bytearray()
        self.pcm_bytes = 0
        self.chunk_count = 0
        self.odd_chunk_count = 0
        self.min_chunk_bytes: int | None = None
        self.max_chunk_bytes = 0

    def add(self, payload: bytes) -> bytes:
        size = len(payload)
        self.pcm_bytes += size
        self.chunk_count += 1
        self.odd_chunk_count += size % 2
        self.min_chunk_bytes = (
            size if self.min_chunk_bytes is None else min(self.min_chunk_bytes, size)
        )
        self.max_chunk_bytes = max(self.max_chunk_bytes, size)
        self._pending.extend(payload)
        even_size = len(self._pending) & ~1
        if not even_size:
            return b""
        emitted = bytes(self._pending[:even_size])
        del self._pending[:even_size]
        return emitted

    def finish(self) -> None:
        if self._pending:
            raise DoubaoPCMContinuityError(
                f"Doubao PCM total length is odd ({self.pcm_bytes} bytes)"
            )

    def summary(self) -> dict[str, int]:
        return {
            "pcm_bytes": self.pcm_bytes,
            "chunk_count": self.chunk_count,
            "odd_chunk_count": self.odd_chunk_count,
            "min_chunk_bytes": self.min_chunk_bytes or 0,
            "max_chunk_bytes": self.max_chunk_bytes,
        }


def _error_message(message: ServerMessage) -> str:
    return f"Doubao error {message.error_code or message.event}"
