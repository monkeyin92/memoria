"""Rule fast path and semantic fallback for conversation-close routing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from services.agent.src.classifier_inflight import resolve_shared, start_shared
from services.agent.src.orchestration.interruption_guard import (
    _conversation_close_compact,
    is_completion_ack_only,
    is_conversation_close_only,
    is_interrupt_command_only,
)


def rule_conversation_close_only(text: str) -> bool:
    """Deterministic fast path for obvious session-close phrases."""

    return is_conversation_close_only(text)


def lexical_playback_control_only(text: str) -> bool:
    """「停」「等等」「好了」: stop the reply, never end the conversation.

    The semantic classifier has read a bare 「停」 as a farewell. A close
    verdict routes the utterance to END_SESSION, which needs the target-
    speaker gate and closes the session, while the stop command bypasses the
    gate and only interrupts playback. Lexical control phrases win.
    """

    return is_interrupt_command_only(text) or is_completion_ack_only(text)


def conversation_close_cache_key(text: str) -> str:
    return _conversation_close_compact(text)


async def resolve_conversation_close_needed(
    text: str,
    *,
    cache: dict[str, bool],
    semantic_resolver: Callable[[str], Awaitable[bool]] | None = None,
    log_wait: bool = True,
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
    if lexical_playback_control_only(text):
        cache[compact] = False
        return False
    if semantic_resolver is None:
        cache[compact] = False
        return False
    return await resolve_shared(cache, compact, semantic_resolver, text, log_wait=log_wait)


def start_conversation_close_verdict(
    text: str,
    *,
    cache: dict[str, bool],
    semantic_resolver: Callable[[str], Awaitable[bool]] | None,
) -> None:
    """Start the semantic verdict for a sentence that is about to be committed, and leave it in flight.

    The commit joins this call instead of asking the classifier itself once the end-of-speech grace is over
    (TODOLIST N-14 7).  Nothing is started where ``resolve_conversation_close_needed`` would not call the
    classifier: a cached verdict, a rule hit, a stop word or a completion acknowledgement.
    """

    compact = conversation_close_cache_key(text)
    if (
        semantic_resolver is None
        or not compact
        or compact in cache
        or rule_conversation_close_only(text)
        or lexical_playback_control_only(text)
    ):
        return
    start_shared(cache, compact, semantic_resolver, text)


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
