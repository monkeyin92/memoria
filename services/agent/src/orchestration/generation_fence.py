"""GenerationFence gate: drop any async result that does not fully match."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry

T = TypeVar("T")


@dataclass
class FenceGate:
    """Single source of truth for accepting or dropping async pipeline results."""

    current: GenerationFence
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    dropped_count: int = 0
    accepted_count: int = 0
    on_drop: Callable[[str, GenerationFence, GenerationFence], None] | None = None

    def update(self, fence: GenerationFence) -> None:
        self.current = fence

    def accept(self, fence: GenerationFence, *, source: str) -> bool:
        if fence.matches(self.current):
            self.accepted_count += 1
            return True
        self.dropped_count += 1
        self.metrics.inc_stale_result_dropped(source)
        if self.on_drop is not None:
            self.on_drop(source, self.current, fence)
        return False

    def gate(self, fence: GenerationFence, value: T, *, source: str) -> T | None:
        if self.accept(fence, source=source):
            return value
        return None

    def require(self, fence: GenerationFence, *, source: str) -> None:
        if not self.accept(fence, source=source):
            raise StaleFenceError(source, self.current, fence)


class StaleFenceError(Exception):
    def __init__(
        self,
        source: str,
        expected: GenerationFence,
        actual: GenerationFence,
    ) -> None:
        super().__init__(
            f"stale fence from {source}: expected gen={expected.generation_id} "
            f"epoch={expected.tool_epoch}, got gen={actual.generation_id} "
            f"epoch={actual.tool_epoch}"
        )
        self.source = source
        self.expected = expected
        self.actual = actual
