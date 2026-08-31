"""Rule fast path and semantic fallback for conversation-close routing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from services.agent.src.orchestration.interruption_guard import (
    _conversation_close_compact,
    is_conversation_close_only,
)


def rule_conversation_close_only(text: str) -> bool:
    """Deterministic fast path for obvious session-close phrases."""

    return is_conversation_close_only(text)


def conversation_close_cache_key(text: str) -> str:
    return _conversation_close_compact(text)


async def resolve_conversation_close_needed(
    text: str,
    *,
    cache: dict[str, bool],
    semantic_resolver: Callable[[str], Awaitable[bool]] | None = None,
) -> bool:
    """Resolve once per normalized query; rule hits skip the classifier."""

    compact = conversation_close_cache_key(text)
    if not compact:
        return False
    cached = cache.get(compact)
    if cached is not None:
        return cached
    if rule_conversation_close_only(text):
        cache[compact] = True
        return True
    if semantic_resolver is None:
        cache[compact] = False
        return False
    needed = await semantic_resolver(text)
    cache[compact] = needed
    return needed


def conversation_close_needed(
    text: str,
    *,
    cache: dict[str, bool],
) -> bool:
    """Sync view: cached decision or rule fast path only."""

    compact = conversation_close_cache_key(text)
    if not compact:
        return False
    cached = cache.get(compact)
    if cached is not None:
        return cached
    return rule_conversation_close_only(text)
