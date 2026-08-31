"""Clock/date intent helpers for device media turn routing."""

from __future__ import annotations

from services.common.realtime_information import _asks_for_date, _asks_for_time


def is_clock_fact_query(query: str) -> bool:
    """Return whether a user utterance asks only for local clock/date facts."""

    return _asks_for_date(query) or _asks_for_time(query)


__all__ = ["is_clock_fact_query"]
