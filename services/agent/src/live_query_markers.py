"""Agent-scoped live-query markers for component-only deploys."""

from __future__ import annotations

from services.common.companion_response_safety import companion_safety_decision
from services.common.realtime_information import (
    _normalized,
    requires_realtime_lookup,
)

_AGENT_EXTRA_LIVE_MARKERS = (
    "动车",
    "列车",
)


def requires_live_media_lookup(query: str) -> bool:
    """Return whether a device utterance needs a fresh live lookup."""

    if requires_realtime_lookup(query):
        return True
    compact = _normalized(query)
    return (
        bool(compact)
        and companion_safety_decision(query) == "none"
        and any(marker in compact for marker in _AGENT_EXTRA_LIVE_MARKERS)
    )


__all__ = ["requires_live_media_lookup"]
