"""Global generation fence and session identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class GenerationFence:
    """Complete fence: every async LLM/TTS/tool result must match all fields.

    ``session_epoch`` is the identity/context epoch (remediation doc PR-08):
    it advances whenever the active subject or runtime profile changes, so a
    late result produced under a previous subject can never cross the switch.
    """

    session_id: str
    turn_id: int
    generation_id: int
    tool_epoch: int
    session_epoch: int = 0

    def matches(self, other: GenerationFence) -> bool:
        return (
            self.session_id == other.session_id
            and self.turn_id == other.turn_id
            and self.generation_id == other.generation_id
            and self.tool_epoch == other.tool_epoch
            and self.session_epoch == other.session_epoch
        )

    def bump_generation(self) -> GenerationFence:
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id,
            generation_id=self.generation_id + 1,
            tool_epoch=self.tool_epoch,
            session_epoch=self.session_epoch,
        )

    def bump_turn(self) -> GenerationFence:
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id + 1,
            generation_id=self.generation_id + 1,
            tool_epoch=self.tool_epoch,
            session_epoch=self.session_epoch,
        )

    def bump_tool_epoch(self) -> GenerationFence:
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id,
            generation_id=self.generation_id + 1,
            tool_epoch=self.tool_epoch + 1,
            session_epoch=self.session_epoch,
        )

    def with_session_epoch(self, epoch: int) -> GenerationFence:
        """Return the same turn/generation under a new identity epoch."""

        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise ValueError("session epoch must be a non-negative integer")
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id,
            generation_id=self.generation_id,
            tool_epoch=self.tool_epoch,
            session_epoch=epoch,
        )


@dataclass(frozen=True, slots=True)
class CancellationContext:
    """Immutable provider context backed by the existing generation fence."""

    fence: GenerationFence

    @classmethod
    def capture(cls, fence: GenerationFence) -> CancellationContext:
        return cls(fence=fence)

    @property
    def session_id(self) -> str:
        return self.fence.session_id

    @property
    def turn_id(self) -> int:
        return self.fence.turn_id

    @property
    def generation_id(self) -> int:
        return self.fence.generation_id

    @property
    def tool_epoch(self) -> int:
        return self.fence.tool_epoch

    @property
    def session_epoch(self) -> int:
        return self.fence.session_epoch

    def is_current(self, current: GenerationFence | CancellationContext) -> bool:
        other = current.fence if isinstance(current, CancellationContext) else current
        return self.fence.matches(other)

    def is_stale(self, current: GenerationFence | CancellationContext) -> bool:
        return not self.is_current(current)


def new_session_id() -> str:
    return str(uuid4())


def new_task_id() -> str:
    return str(uuid4())
