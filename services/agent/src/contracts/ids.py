"""Global generation fence and session identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class GenerationFence:
    """Complete fence: every async LLM/TTS/tool result must match all fields."""

    session_id: str
    turn_id: int
    generation_id: int
    tool_epoch: int

    def matches(self, other: GenerationFence) -> bool:
        return (
            self.session_id == other.session_id
            and self.turn_id == other.turn_id
            and self.generation_id == other.generation_id
            and self.tool_epoch == other.tool_epoch
        )

    def bump_generation(self) -> GenerationFence:
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id,
            generation_id=self.generation_id + 1,
            tool_epoch=self.tool_epoch,
        )

    def bump_turn(self) -> GenerationFence:
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id + 1,
            generation_id=self.generation_id + 1,
            tool_epoch=self.tool_epoch,
        )

    def bump_tool_epoch(self) -> GenerationFence:
        return GenerationFence(
            session_id=self.session_id,
            turn_id=self.turn_id,
            generation_id=self.generation_id + 1,
            tool_epoch=self.tool_epoch + 1,
        )


def new_session_id() -> str:
    return str(uuid4())


def new_task_id() -> str:
    return str(uuid4())
