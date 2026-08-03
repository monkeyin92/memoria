"""Machine-readable SLO and automatic rollback gates for media rollout."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaSLO:
    first_audio_p95_ms: float = 1_200.0
    interrupt_stop_p95_ms: float = 250.0
    session_failure_rate: float = 0.02
    max_stale_generation: int = 0
    max_stale_asr_final: int = 0

    def __post_init__(self) -> None:
        if self.first_audio_p95_ms <= 0 or self.interrupt_stop_p95_ms <= 0:
            raise ValueError("latency SLOs must be positive")
        if not 0.0 <= self.session_failure_rate <= 1.0:
            raise ValueError("session failure SLO must be in [0, 1]")
        if self.max_stale_generation < 0 or self.max_stale_asr_final < 0:
            raise ValueError("stale-event SLOs must be non-negative")


@dataclass(frozen=True, slots=True)
class SLOReport:
    passed: bool
    rollback_required: bool
    failures: tuple[str, ...]


def evaluate_slo(observed: Mapping[str, float], *, slo: MediaSLO | None = None) -> SLOReport:
    policy = slo or MediaSLO()
    failures: list[str] = []
    checks = (
        ("first_audio_p95_ms", policy.first_audio_p95_ms, "first_audio_p95_ms"),
        ("interrupt_stop_p95_ms", policy.interrupt_stop_p95_ms, "interrupt_stop_p95_ms"),
        ("session_failure_rate", policy.session_failure_rate, "session_failure_rate"),
    )
    for metric, limit, label in checks:
        value = observed.get(metric)
        if value is None:
            failures.append(f"missing_{label}")
        elif value > limit:
            failures.append(f"{label}>{limit:g}")
    for metric, limit in (
        ("stale_generation_total", policy.max_stale_generation),
        ("stale_asr_final_total", policy.max_stale_asr_final),
    ):
        if metric not in observed:
            failures.append(f"missing_{metric}")
            continue
        value = observed[metric]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or not float(value).is_integer()
        ):
            failures.append(f"invalid_{metric}")
            continue
        if value > limit:
            failures.append(f"{metric}>0")
    return SLOReport(
        passed=not failures,
        rollback_required=bool(failures),
        failures=tuple(failures),
    )


__all__ = ["MediaSLO", "SLOReport", "evaluate_slo"]
