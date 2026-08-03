"""Strict generation and response-lease fencing for provider callbacks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from services.agent.src.contracts.ids import GenerationFence

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ResponseLease:
    """Cancellation handle bound to exactly one generation fence."""

    fence: GenerationFence
    cancel: Callable[[], None]


@dataclass(slots=True)
class GenerationController:
    """Own the authoritative fence and reject every stale provider result.

    The controller is intentionally synchronous: callers invoke it from their
    event-loop critical section, while provider work remains cancellable by the
    lease callback.  ``accept`` requires an exact match across session, turn,
    generation and tool epoch; a partial match is never accepted.
    """

    session_id: str
    _current: GenerationFence = field(init=False)
    _response: ResponseLease | None = field(default=None, init=False)
    _stale_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        self._current = GenerationFence(
            session_id=self.session_id,
            turn_id=0,
            generation_id=0,
            tool_epoch=0,
        )

    @property
    def current(self) -> GenerationFence:
        return self._current

    @property
    def fence(self) -> GenerationFence:
        """Alias matching the existing orchestration terminology."""

        return self._current

    @property
    def response(self) -> ResponseLease | None:
        return self._response

    @property
    def stale_count(self) -> int:
        return self._stale_count

    def start_turn(self, *, speaker_scope: str | None = None) -> GenerationFence:
        """Start a new logical turn; the optional scope is caller metadata only."""

        _ = speaker_scope
        return self._replace(self._current.bump_turn())

    def start_generation(self) -> GenerationFence:
        return self._replace(self._current.bump_generation())

    def bump_tool_epoch(self) -> GenerationFence:
        return self._replace(self._current.bump_tool_epoch())

    def advance(self, fence: GenerationFence) -> GenerationFence:
        """Install an authoritative externally-created fence.

        Media Edge and Voice Core both receive generation controls.  The
        receiver must accept only a monotonic complete fence; it must not
        derive a replacement generation from a local ``+ 1`` guess.
        """

        if fence.session_id != self.session_id:
            raise ValueError("generation fence belongs to another session")
        current = self._current
        if (
            fence.turn_id < current.turn_id
            or (
                fence.turn_id == current.turn_id
                and fence.generation_id < current.generation_id
            )
            or (
                fence.turn_id == current.turn_id
                and fence.generation_id == current.generation_id
                and fence.tool_epoch < current.tool_epoch
            )
        ):
            self._stale_count += 1
            raise ValueError("generation fence must advance monotonically")
        return self._replace(fence)

    def accept(self, fence: GenerationFence) -> bool:
        """Return true only for the current complete fence."""

        accepted = fence.matches(self._current)
        if not accepted:
            self._stale_count += 1
        return accepted

    def gate(self, fence: GenerationFence, value: T) -> T | None:
        return value if self.accept(fence) else None

    def install_response(
        self,
        fence: GenerationFence,
        cancel: Callable[[], None],
    ) -> ResponseLease | None:
        """Install a response only for the current fence.

        The previous lease is cancelled before the new one is stored.  A stale
        install is rejected and cannot disturb the active response.
        """

        if not self.accept(fence):
            return None
        previous = self._response
        if previous is not None:
            previous.cancel()
        lease = ResponseLease(fence=fence, cancel=cancel)
        self._response = lease
        return lease

    def clear_response(self, fence: GenerationFence) -> bool:
        """Clear only the response lease that owns ``fence``."""

        lease = self._response
        if lease is None or not lease.fence.matches(fence):
            self._stale_count += 1
            return False
        self._response = None
        return True

    def cancel(self, fence: GenerationFence | None = None) -> GenerationFence | None:
        """Cancel the current response and advance generation atomically.

        A stale cancellation is ignored.  The returned fence is authoritative;
        callers must use it rather than deriving a generation with ``+ 1``.
        """

        expected = fence or self._current
        if not self.accept(expected):
            return None
        return self._replace(self._current.bump_generation())

    def _replace(self, fence: GenerationFence) -> GenerationFence:
        if fence.session_id != self.session_id:
            raise ValueError("generation fence belongs to another session")
        lease = self._response
        if lease is not None and not lease.fence.matches(fence):
            lease.cancel()
            self._response = None
        self._current = fence
        return fence


__all__ = ["GenerationController", "ResponseLease"]
