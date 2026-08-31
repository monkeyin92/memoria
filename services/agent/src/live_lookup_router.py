"""Keyword fast path and semantic fallback for live-information routing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from services.common.companion_response_safety import companion_safety_decision
from services.common.realtime_information import (
    _normalized,
    requires_realtime_lookup,
)

_AGENT_EXTRA_LIVE_MARKERS = (
    "动车",
    "列车",
)


def keyword_requires_live_media_lookup(query: str) -> bool:
    """Deterministic fast path for obvious live-information turns."""

    if requires_realtime_lookup(query):
        return True
    compact = _normalized(query)
    return (
        bool(compact)
        and companion_safety_decision(query) == "none"
        and any(marker in compact for marker in _AGENT_EXTRA_LIVE_MARKERS)
    )


def live_lookup_cache_key(query: str) -> str:
    return _normalized(query)


async def resolve_live_lookup_needed(
    query: str,
    *,
    cache: dict[str, bool],
    semantic_resolver: Callable[[str], Awaitable[bool]] | None = None,
) -> bool:
    """Resolve once per normalized query; keyword hits skip the classifier."""

    compact = live_lookup_cache_key(query)
    if not compact:
        return False
    if companion_safety_decision(query) != "none":
        cache[compact] = False
        return False
    cached = cache.get(compact)
    if cached is not None:
        return cached
    if keyword_requires_live_media_lookup(query):
        cache[compact] = True
        return True
    if semantic_resolver is None:
        cache[compact] = False
        return False
    needed = await semantic_resolver(query)
    cache[compact] = needed
    return needed


def live_lookup_needed(
    query: str,
    *,
    cache: dict[str, bool],
) -> bool:
    """Sync view: cached decision or keyword fast path only."""

    compact = live_lookup_cache_key(query)
    if not compact:
        return False
    if companion_safety_decision(query) != "none":
        return False
    cached = cache.get(compact)
    if cached is not None:
        return cached
    return keyword_requires_live_media_lookup(query)
