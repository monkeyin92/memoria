"""Semantic turn-classifier runtime mixin: live lookup and conversation close.

``DuplexRuntimeClassifierMixin`` is composed into ``DuplexRuntime``; the verdict caches and the installed
resolvers stay owned by the runtime dataclass and are only declared here for type checking.  Both caches are
``ClassifierCache``: a call in flight is shared, so the end-of-speech commit joins the verdict that was started
when the ASR final arrived instead of asking the cloud classifier a second time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.agent.src.orchestration.orchestrator import Orchestrator
    from services.agent.src.weather_thread import WeatherTurn


class DuplexRuntimeClassifierMixin:
    session_id: str
    orchestrator: Orchestrator
    _live_lookup_cache: dict[str, bool]
    _live_lookup_semantic_resolver: Callable[[str], Awaitable[bool]] | None
    _conversation_close_cache: dict[str, bool]
    _conversation_close_semantic_resolver: Callable[[str], Awaitable[bool]] | None
    _turn_verdict_key: str | None
    _turn_verdict_serial: int

    def begin_turn_verdicts(self) -> None:
        """Open a verdict scope for the next turn; a settled verdict answers only the turn that asked."""

        self._turn_verdict_serial += 1
        self._turn_verdict_key = f"{self.session_id}:{self._turn_verdict_serial}"

    async def close_classifier_calls(self) -> None:
        """Cancel the verdict calls still in flight; nothing may outlive the runtime."""

        for cache in (self._live_lookup_cache, self._conversation_close_cache):
            aclose = getattr(cache, "aclose", None)
            if aclose is not None:
                await aclose()

    def set_live_lookup_semantic_resolver(
        self,
        resolver: Callable[[str], Awaitable[bool]] | None,
    ) -> None:
        self._live_lookup_semantic_resolver = resolver

    def weather_turn(self, query: str) -> WeatherTurn | None:
        """The session weather thread's reading of ``query``."""

        return self.orchestrator.context.weather.read(query)

    async def resolve_lookup_owns_reply(self, query: str) -> bool:
        """Whether a live lookup answers this committed turn.

        A weather follow-up such as 「那明天呢」 counts here only. Endpoint pinning
        and rejected-final recovery keep the text-only verdict, so a short
        fragment is never cut early or merged into a neighbouring turn.
        """

        return self.weather_turn(query) is not None or await self.resolve_live_lookup_needed(query)

    async def resolve_live_lookup_needed(self, query: str) -> bool:
        from services.agent.src.live_lookup_router import resolve_live_lookup_needed

        return await resolve_live_lookup_needed(
            query,
            cache=self._live_lookup_cache,
            semantic_resolver=self._live_lookup_semantic_resolver,
            turn_key=self._turn_verdict_key,
        )

    def live_lookup_needed(self, query: str, *, start_verdict: bool = False) -> bool:
        """The cached or keyword decision; ``start_verdict`` also starts the semantic one in the background."""

        from services.agent.src.live_lookup_router import (
            live_lookup_needed,
            start_live_lookup_verdict,
        )

        if start_verdict:
            start_live_lookup_verdict(
                query,
                cache=self._live_lookup_cache,
                semantic_resolver=self._live_lookup_semantic_resolver,
                turn_key=self._turn_verdict_key,
            )
        return live_lookup_needed(query, cache=self._live_lookup_cache)

    def set_conversation_close_semantic_resolver(
        self,
        resolver: Callable[[str], Awaitable[bool]] | None,
    ) -> None:
        self._conversation_close_semantic_resolver = resolver

    async def resolve_conversation_close_needed(self, text: str, *, log_wait: bool = True) -> bool:
        """The verdict; ``log_wait=False`` for a background evaluation that no commit waits on."""

        from services.agent.src.conversation_close_router import (
            resolve_conversation_close_needed,
        )

        return await resolve_conversation_close_needed(
            text,
            cache=self._conversation_close_cache,
            semantic_resolver=self._conversation_close_semantic_resolver,
            log_wait=log_wait,
            turn_key=self._turn_verdict_key,
        )

    def start_conversation_close_verdict(self, text: str) -> None:
        """Start the semantic close verdict in the background; the turn commit joins it."""

        from services.agent.src.conversation_close_router import start_conversation_close_verdict

        start_conversation_close_verdict(
            text,
            cache=self._conversation_close_cache,
            semantic_resolver=self._conversation_close_semantic_resolver,
            turn_key=self._turn_verdict_key,
        )

    def conversation_close_needed(self, text: str) -> bool:
        from services.agent.src.conversation_close_router import conversation_close_needed

        return conversation_close_needed(text, cache=self._conversation_close_cache)
