from __future__ import annotations

from typing import Final

_UPLINK_SAMPLE_RATE: Final = 16_000


def speech_end_monotonic(
    *,
    received_at: float,
    event_sample: int,
    voiced_end_sample: int | None,
) -> float:
    """When the child's last voiced sample was spoken, on ``received_at``'s monotonic clock.

    A device reports VAD end at ``event_sample``, a hangover after ``voiced_end_sample``.
    Uplink latency is not subtracted, so the result is never earlier than the speech end.
    """

    if voiced_end_sample is None or voiced_end_sample >= event_sample:
        return received_at
    return received_at - (event_sample - voiced_end_sample) / _UPLINK_SAMPLE_RATE


__all__ = ["speech_end_monotonic"]
