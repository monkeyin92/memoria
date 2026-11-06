"""Reply realtime delegation mixin: public fresh-information lookups.

``ReplyDelegationMixin`` is composed into ``ReplyPipeline``.  The pipeline owns
the turn protocol and the response plan; this module owns the delegated
realtime-search tool, the per-fence delegation cache, and the stream that plays
a committed lookup's answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.delegation_coordinator import (
    DelegationRequest,
    SideEffectPolicy,
    TaskHandle,
)
from services.agent.src.orchestration.task_manager import ToolSpec
from services.common.story_grounding import (
    build_ip_story_search_query,
    extract_story_ip,
)

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.response_planner_client import ResponsePlannerClient

logger = logging.getLogger(__name__)

_TURN_CONTEXT_WAIT_S = 2.5


class ReplyDelegationMixin:
    """Run public realtime lookups as delegated tool work bound to the turn's fence."""

    if TYPE_CHECKING:
        # Shared state owned by the ReplyPipeline.
        _runtime: DuplexRuntime
        _response_planner_client: ResponsePlannerClient | None
        _realtime_search_resolver: Any
        _realtime_tool_registered: bool
        _realtime_delegation_lock: asyncio.Lock
        _realtime_delegations: dict[GenerationFence, tuple[str, TaskHandle]]
        _context_ready_by_fence: dict[GenerationFence, asyncio.Event]

        # Core pipeline members consumed by this mixin.
        def _realtime_sources_available(self) -> bool: ...

    async def _forced_realtime_search_stream(
        self,
        *,
        query: str,
    ) -> AsyncGenerator[str, None]:
        """Resolve one public fresh-information request without chat history."""

        fence = self._runtime.fence
        handle = await self._get_or_start_realtime_delegation(query=query, fence=fence)
        if handle is None:
            return

        coordinator = self._runtime.orchestrator.delegation
        try:
            async for _event in coordinator.events(handle):
                pass
        finally:
            async with self._realtime_delegation_lock:
                if self._realtime_delegations.get(fence) == (query, handle):
                    self._realtime_delegations.pop(fence, None)
        intent = coordinator.output_intent(
            handle,
            current_fence=self._runtime.fence,
            current_task_epoch=handle.request.task_epoch,
            current_context_version=coordinator.current_context_version(fence.session_id),
            relevant=self._realtime_delegation_relevant(query=query, fence=fence),
        )
        if intent is not None and intent.tts_source.strip():
            spoken = coordinator.admit_output_intent(
                intent,
                current_fence=self._runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=self._runtime.output_floor_allows_assistant,
            )
            if spoken is not None:
                try:
                    yield spoken
                finally:
                    coordinator.complete_output_intent(
                        intent,
                        current_fence=self._runtime.fence,
                        current_context_version=coordinator.current_context_version(
                            fence.session_id
                        ),
                        floor_allows_output=self._runtime.output_floor_allows_assistant,
                        reason="deep_result_emitted",
                    )

    def _register_realtime_delegation_tool(self) -> bool:
        if self._realtime_tool_registered or self._realtime_search_resolver is None:
            return self._realtime_tool_registered
        manager = self._runtime.orchestrator.task_manager

        async def _resolve(
            arguments: dict[str, Any],
            cancel: asyncio.Event,
        ) -> Any:
            resolver = self._realtime_search_resolver
            if resolver is None or cancel.is_set():
                return None
            return await resolver.resolve(query=str(arguments["query"]))

        manager.register(
            ToolSpec(
                name="realtime_search",
                description="public realtime information lookup",
                input_schema={"type": "object", "required": ["query"]},
                cancellable=True,
                idempotent=True,
                timeout_s=20.0,
                side_effect_policy=SideEffectPolicy.READ_ONLY.value,
            ),
            _resolve,
        )
        self._realtime_tool_registered = True
        return True

    def _realtime_delegation_relevant(
        self,
        *,
        query: str,
        fence: GenerationFence,
    ) -> bool:
        pending = self._runtime.pending_realtime_request
        return bool(
            self._runtime.fence.matches(fence)
            and (
                pending is None
                or pending.query == query
                or (
                    (ip := extract_story_ip(pending.query)) is not None
                    and query == build_ip_story_search_query(ip)
                )
            )
        )

    async def _start_committed_delegation(
        self,
        text: str,
        fence: GenerationFence,
    ) -> None:
        await self._runtime.resolve_live_lookup_needed(text)
        delegation_query = text
        ip = extract_story_ip(text)
        if ip is not None:
            delegation_query = build_ip_story_search_query(ip)
        if self._response_planner_client is None:
            if self._runtime.live_lookup_needed(text) and self._realtime_sources_available():
                await self._get_or_start_realtime_delegation(query=delegation_query, fence=fence)
            return
        ready = self._context_ready_by_fence.setdefault(fence, asyncio.Event())
        try:
            await asyncio.wait_for(ready.wait(), timeout=_TURN_CONTEXT_WAIT_S)
        except TimeoutError:
            return
        finally:
            self._context_ready_by_fence.pop(fence, None)
        if not self._runtime.fence.matches(fence):
            return
        if self._runtime.live_lookup_needed(text) and self._realtime_sources_available():
            await self._get_or_start_realtime_delegation(query=delegation_query, fence=fence)

    async def _get_or_start_realtime_delegation(
        self,
        *,
        query: str,
        fence: GenerationFence,
    ) -> TaskHandle | None:
        if not self._can_start_realtime_delegation(fence):
            return None
        if not self._register_realtime_delegation_tool():
            return None
        coordinator = self._runtime.orchestrator.delegation
        async with self._realtime_delegation_lock:
            existing = self._realtime_delegations.get(fence)
            if existing is not None and existing[0] == query:
                return existing[1]
            self._realtime_delegations = {
                bound_fence: delegated
                for bound_fence, delegated in self._realtime_delegations.items()
                if self._runtime.fence.matches(bound_fence)
            }
            task_epoch = coordinator.next_task_epoch(fence.session_id)
            handle = await coordinator.delegate(
                DelegationRequest(
                    tool_name="realtime_search",
                    arguments={"query": query},
                    fence=fence,
                    task_epoch=task_epoch,
                    context_version=self._runtime.orchestrator.context_version_for_fence(fence),
                    expires_at_ms=int(time.time() * 1_000) + 20_000,
                    side_effect_policy=SideEffectPolicy.READ_ONLY,
                    committed=True,
                    relevance=lambda: self._realtime_delegation_relevant(
                        query=query,
                        fence=fence,
                    ),
                )
            )
            self._realtime_delegations[fence] = (query, handle)
            return handle

    def _can_start_realtime_delegation(self, fence: GenerationFence) -> bool:
        if not self._runtime.fence.matches(fence):
            return False
        if self._runtime.mode_policy_enforced:
            try:
                snapshot = self._runtime.context_snapshot_for_fence(fence)
                policy = self._runtime.mode_policy_for_fence(fence)
            except ValueError:
                return False
            if not policy.allows_conversation(snapshot.speaker_class):
                logger.warning(
                    "realtime delegation blocked by frozen conversation policy "
                    "session_id=%s turn_id=%s",
                    fence.session_id,
                    fence.turn_id,
                )
                return False
        return True
