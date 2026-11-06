"""Reply plan mixin: response-plan fetch, cache, authorization and context readiness.

``ReplyPlanMixin`` is composed into ``ReplyPipeline``.  The pipeline owns the
turn protocol; this module owns the plan cache stamping, the response-planner
tool, the delegated plan fetch and the per-fence context-ready barrier.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from services.agent.src import response_plan_sidecar
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.context_snapshot_manager import (
    ContextSnapshot,
    ContextSnapshotDraft,
    ContextTurn,
    PersonaCapsule,
    scope_context_snapshot_draft,
)
from services.agent.src.orchestration.delegation_coordinator import (
    DelegationRequest,
    SideEffectPolicy,
)
from services.agent.src.orchestration.task_manager import ToolSpec
from services.agent.src.reply_delegation import _TURN_CONTEXT_WAIT_S
from services.agent.src.reply_warmup import _memory_capsule, _PlanRequest, _recall_context
from services.agent.src.response_plan_cache import ResponsePlanCache
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponsePlannerClient,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.reply_crisis import _LateCrisisPlan

logger = logging.getLogger(__name__)

media_pb2: Any = _media_pb2


class ReplyPlanMixin:
    """Fetch, cache and authorize the turn's response plan and hold its context ready."""

    if TYPE_CHECKING:
        # Shared state owned by the ReplyPipeline.
        _runtime: DuplexRuntime
        _response_plans: ResponsePlanCache
        _response_planner_client: ResponsePlannerClient | None
        _response_planner_tool_registered: bool
        _context_ready_by_fence: dict[GenerationFence, asyncio.Event]
        _late_crisis: dict[GenerationFence, _LateCrisisPlan]

        # Sibling-mixin methods consumed by this mixin.
        def _drop_early_plan(self) -> None: ...

        def _take_early_plan(
            self, request: _PlanRequest
        ) -> asyncio.Task[ResponsePlanFetch] | None: ...

        def _watch_late_crisis_plan(
            self,
            task: asyncio.Task[Any],
            *,
            text: str,
            fence: GenerationFence,
        ) -> None: ...

    def _current_authorizing_profile_id(self) -> str | None:
        """The ``runtime_profile_id`` currently authorizing this session, if any.

        Defensive lookups: unit-test doubles may not wire the full orchestrator
        chain, in which case there is no authorizing profile to stamp.
        """
        profiles = getattr(getattr(self._runtime, "orchestrator", None), "runtime_profiles", None)
        current = getattr(profiles, "current", None)
        profile = getattr(current, "profile", None)
        profile_id = getattr(profile, "runtime_profile_id", None)
        return profile_id if isinstance(profile_id, str) and profile_id else None

    def cache_response_plan(self, plan: ResponsePlan) -> None:
        """Cache ``plan`` stamped with the profile authorizing this session now."""

        self._response_plans.store(
            plan,
            authorizing_profile_id=self._current_authorizing_profile_id(),
        )

    def response_plan_authorized(self, plan: ResponsePlan) -> bool:
        """Whether ``plan``'s authorizing profile is still the current one."""

        return self._response_plans.authorized(
            plan,
            current_profile_id=self._current_authorizing_profile_id(),
        )

    def rebind_response_plan(
        self,
        previous: GenerationFence,
        current: GenerationFence,
    ) -> None:
        """Move the cached plan and late-crisis state to a successor generation.

        The media output owner advances the fence on an internal handoff. The
        admitted reply work keeps its per-turn answer, so its plan key must
        move too; otherwise the next LLM request fails the exact-plan gate.
        """

        if not self._response_plans.rebind(previous, current):
            return
        late = self._late_crisis.pop(previous, None)
        if late is not None:
            self._late_crisis[current] = late
        logger.info(
            "response_plan_rebound session_id=%s turn_id=%s generation_id=%s previous_generation_id=%s",
            current.session_id,
            current.turn_id,
            current.generation_id,
            previous.generation_id,
        )

    def _register_response_planner_tool(self) -> bool:
        if self._response_planner_tool_registered or self._response_planner_client is None:
            return self._response_planner_tool_registered
        client = self._response_planner_client

        async def _fetch(arguments: dict[str, Any], cancel: asyncio.Event) -> Any:
            if cancel.is_set():
                return ResponsePlanFetch(None, "cancelled")
            return await client.fetch(
                session_id=str(arguments["session_id"]),
                query=str(arguments["query"]),
                fence=cast(GenerationFence, arguments["fence"]),
                speaker_decision=arguments["speaker_decision"],
                recall_context=cast(tuple[str, ...], arguments.get("recall_context", ())),
                utterance_intent=str(arguments.get("utterance_intent", "chat")),
            )

        self._runtime.orchestrator.task_manager.register(
            ToolSpec(
                name="response_planner",
                description="build the fenced response and grounding control plan",
                input_schema={"type": "object", "required": ["query"]},
                cancellable=True,
                idempotent=True,
                timeout_s=2.0,
                contains_sensitive_data=True,
                side_effect_policy=SideEffectPolicy.READ_ONLY.value,
                required_capability="memory_recall_private",
            ),
            _fetch,
        )
        self._response_planner_tool_registered = True
        return True

    async def _build_prefetched_context_snapshot(
        self,
        base: ContextSnapshot,
        committed_turns: tuple[ContextTurn, ...],
    ) -> ContextSnapshotDraft:
        client = self._response_planner_client
        prefetch = getattr(client, "prefetch_context", None)
        query = self._runtime.context_prefetch_text
        policy = self._runtime.mode_policy
        speaker_decision = self._runtime.current_speaker_decision
        speaker_class = speaker_decision.classification
        fallback = scope_context_snapshot_draft(
            ContextSnapshotDraft(
                recent_committed_turns=committed_turns,
                memory_capsule=base.memory_capsule,
                persona_capsule=base.persona_capsule,
                relationship_policy=policy,
                tool_permission=base.tool_permission,
                speaker_class=speaker_class,
                summary=base.summary,
            )
        )
        if not callable(prefetch) or not query or not self._runtime.profile_permits(self._runtime.fence, capability="memory_recall_private"):
            return fallback
        fetched = await prefetch(session_id=self._runtime.session_id, query=query, speaker_decision=speaker_decision)
        if not fetched.available or self._runtime.current_speaker_decision != speaker_decision:
            return fallback
        memory = _memory_capsule(fetched.grounded_items)
        persona = PersonaCapsule(
            version_id=fetched.persona_version_id,
            version_number=fetched.persona_version_number,
            prompt_fragment="\n".join(
                item.content for item in fetched.grounded_items if item.kind == "persona_trait"
            ),
        )
        return scope_context_snapshot_draft(
            ContextSnapshotDraft(
                recent_committed_turns=committed_turns,
                memory_capsule=memory,
                persona_capsule=persona,
                relationship_policy=policy,
                tool_permission=base.tool_permission,
                speaker_class=speaker_class,
                summary=base.summary,
            )
        )

    async def _fetch_response_plan(
        self,
        *,
        text: str,
        speaker: Any,
        fence: GenerationFence,
    ) -> ResponsePlanFetch:
        if not self._register_response_planner_tool() or not self._runtime.profile_permits(
            fence, capability="memory_recall_private"
        ):
            self._drop_early_plan()
            return ResponsePlanFetch(None, "no_verified_runtime_profile")
        request = _PlanRequest(
            text=text,
            speaker=speaker,
            fence=fence,
            recall_context=self._recall_context_for_fence(fence, speaker=speaker),
            utterance_intent=self._runtime.route_user_turn(text).intent,
        )
        if response_plan_sidecar.applies(self._response_planner_client, self._runtime, fence):
            self._drop_early_plan()
            fetch, task = response_plan_sidecar.spawn_task(
                self._runtime,
                self._response_planner_client,
                session_id=self._runtime.session_id,
                text=text,
                fence=fence,
                speaker=speaker,
                recall_context=request.recall_context,
                utterance_intent=request.utterance_intent,
            )
            self._watch_late_crisis_plan(task, text=text, fence=fence)
            return fetch
        coordinator = self._runtime.orchestrator.delegation
        context_version = self._runtime.orchestrator.context_version_for_fence(fence)
        early = self._take_early_plan(request)
        if early is not None:
            fetched = await early
            # What ``accept_control_result`` checks for a delegated request: the turn and the context the
            # plan was asked for are still the current ones.
            if (
                self._runtime.fence.matches(fence)
                and coordinator.current_context_version(fence.session_id) == context_version
            ):
                return fetched
            return ResponsePlanFetch(None, "delegation_rejected")
        handle = await coordinator.delegate(
            DelegationRequest(
                tool_name="response_planner",
                arguments={
                    "session_id": self._runtime.session_id,
                    "query": text,
                    "fence": fence,
                    "speaker_decision": speaker,
                    "recall_context": request.recall_context,
                    "utterance_intent": request.utterance_intent,
                },
                fence=fence,
                task_epoch=coordinator.next_task_epoch(fence.session_id),
                context_version=context_version,
                expires_at_ms=int(time.time() * 1_000) + 2_000,
                side_effect_policy=SideEffectPolicy.READ_ONLY,
                committed=True,
                relevance=lambda: self._runtime.fence.matches(fence),
                output_kind=media_pb2.OUTPUT_INTENT_KIND_UNSPECIFIED,
            )
        )
        async for _event in coordinator.events(handle):
            pass
        accepted = coordinator.accept_control_result(
            handle,
            current_fence=self._runtime.fence,
            current_task_epoch=handle.request.task_epoch,
            current_context_version=coordinator.current_context_version(fence.session_id),
            relevant=self._runtime.fence.matches(fence),
        )
        return (
            accepted
            if isinstance(accepted, ResponsePlanFetch)
            else ResponsePlanFetch(None, "delegation_rejected")
        )

    def _recall_context_for_fence(
        self,
        fence: GenerationFence,
        *,
        speaker: Any,
    ) -> tuple[str, ...]:
        """Keep only recent owner utterances from the fence's frozen snapshot."""

        try:
            snapshot = self._runtime.context_snapshot_for_fence(fence)
        except (RuntimeError, ValueError):
            return ()
        return _recall_context(snapshot, speaker=speaker)


    def _mark_context_ready(self, fence: GenerationFence) -> None:
        event = self._context_ready_by_fence.setdefault(fence, asyncio.Event())
        event.set()

    async def _wait_for_turn_context(self, fence: GenerationFence) -> None:
        """Hold a delegated answer until its turn's context is frozen.

        Device soak 2026-11-01 (run 205917, 4 of 12 「那明天呢？」 before any city): asking for the city needs
        no lookup, so the answer was ready before ``prepare_turn`` froze the turn's context.  Its output
        intent carried the context version from before the freeze, the freeze moved the version on, and
        the reply was dropped as superseded at its first audio.  A prepare that never marks the turn
        ready leaves the old timing after the bound.
        """

        ready = self._context_ready_by_fence.setdefault(fence, asyncio.Event())
        try:
            await asyncio.wait_for(ready.wait(), timeout=_TURN_CONTEXT_WAIT_S)
        except TimeoutError:
            pass
        finally:
            self._context_ready_by_fence.pop(fence, None)
