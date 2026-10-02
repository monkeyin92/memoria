"""Semantic turn-classifier runtime mixin: live lookup and conversation close.

``DuplexRuntimeClassifierMixin`` is composed into ``DuplexRuntime``; the verdict caches and the installed
resolvers stay owned by the runtime dataclass and are only declared here for type checking.  Both caches are
``ClassifierCache``: a call in flight is shared, so the end-of-speech commit joins the verdict that was started
when the ASR final arrived instead of asking the cloud classifier a second time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable


class DuplexRuntimeClassifierMixin:
    _live_lookup_cache: dict[str, bool]
    _live_lookup_semantic_resolver: Callable[[str], Awaitable[bool]] | None
    _conversation_close_cache: dict[str, bool]
    _conversation_close_semantic_resolver: Callable[[str], Awaitable[bool]] | None

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

    async def resolve_live_lookup_needed(self, query: str) -> bool:
        from services.agent.src.live_lookup_router import resolve_live_lookup_needed

        return await resolve_live_lookup_needed(
            query,
            cache=self._live_lookup_cache,
            semantic_resolver=self._live_lookup_semantic_resolver,
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
            )
        return live_lookup_needed(query, cache=self._live_lookup_cache)

    def set_conversation_close_semantic_resolver(
        self,
        resolver: Callable[[str], Awaitable[bool]] | None,
    ) -> None:
        self._conversation_close_semantic_resolver = resolver

    async def resolve_conversation_close_needed(self, text: str) -> bool:
        from services.agent.src.conversation_close_router import (
            resolve_conversation_close_needed,
        )

        return await resolve_conversation_close_needed(
            text,
            cache=self._conversation_close_cache,
            semantic_resolver=self._conversation_close_semantic_resolver,
        )

    def conversation_close_needed(self, text: str) -> bool:
        from services.agent.src.conversation_close_router import conversation_close_needed

        return conversation_close_needed(text, cache=self._conversation_close_cache)
