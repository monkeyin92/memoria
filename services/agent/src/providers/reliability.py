"""Small per-provider circuit breaker used at network boundaries."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal


class CircuitOpenError(RuntimeError):
    """Raised while a provider circuit is open."""


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    open_seconds: float = 30.0
    half_open_successes: int = 2
    state: Literal["closed", "open", "half_open"] = "closed"
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float | None = None

    def before_request(self, *, health_probe: bool = False) -> None:
        if self.state != "open":
            return
        elapsed = time.monotonic() - (self.opened_at or 0.0)
        if elapsed >= self.open_seconds or health_probe:
            self.state = "half_open"
            self.consecutive_successes = 0
            return
        raise CircuitOpenError("provider circuit is open")

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.state == "half_open":
            self.consecutive_successes += 1
            if self.consecutive_successes < self.half_open_successes:
                return
        self.state = "closed"
        self.consecutive_successes = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        if self.state == "half_open" or self.consecutive_failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

