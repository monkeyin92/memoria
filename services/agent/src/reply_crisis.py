"""Reply crisis mixin: local crisis plans and fixed safety scripts.

``ReplyCrisisMixin`` is composed into ``ReplyPipeline``.  A crisis turn keeps
the deterministic local reply: the planner request still runs, for the guardian
notification, but it never gates the answer.  A plan that lands late raises the
protective script only when it is stronger than the local one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from services.agent.src import response_plan_sidecar
from services.agent.src.agent import build_local_safe_plan, plan_is_local_safe
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.providers.weather_places import is_city_name_report
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponsePlannerClient,
)
from services.common.crisis_policy import more_protective_local_script, route_crisis
from services.common.fixed_safety_scripts import FixedSafetyScript
from services.common.realtime_information import mentions_weather

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.response_plan_cache import ResponsePlanCache

logger = logging.getLogger(__name__)

_CRISIS_PLAN_GRACE_S = 0.08


@dataclass(slots=True)
class _LateCrisisPlan:
    """A control-api plan still in the background, and the script it raised."""

    started: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    script: FixedSafetyScript | None = None


class ReplyCrisisMixin:
    """Keep the local crisis reply and the fixed safety script a turn must play."""

    if TYPE_CHECKING:
        # Shared state owned by the ReplyPipeline.
        _runtime: DuplexRuntime
        _response_planner_client: ResponsePlannerClient | None
        _response_plans: ResponsePlanCache
        _late_crisis: dict[GenerationFence, _LateCrisisPlan]
        _block_soothing_on_next_reply: bool
        _tts_model: str

        # Core pipeline members consumed by this mixin.
        def _realtime_sources_available(self) -> bool: ...

        async def _fetch_response_plan(
            self,
            *,
            text: str,
            speaker: Any,
            fence: GenerationFence,
        ) -> ResponsePlanFetch: ...

    def _note_fixed_safety_script(self, display: str) -> None:
        self._runtime.note_fixed_safety_spoken(display)
        self._block_soothing_on_next_reply = True

    def _prior_user_texts(self, query: str) -> list[str]:
        context = getattr(getattr(self._runtime, "orchestrator", None), "context", None)
        turns = getattr(context, "turns", ()) or ()
        user_texts = [
            str(turn.content)
            for turn in turns
            if getattr(turn, "role", None) == "user" and getattr(turn, "content", "")
        ]
        if user_texts and user_texts[-1] == query:
            user_texts = user_texts[:-1]
        return user_texts

    def _prior_user_text(self, query: str) -> str:
        user_texts = self._prior_user_texts(query)
        return user_texts[-1] if user_texts else ""

    def _prior_self_crisis_hits(self, query: str) -> int:
        return sum(
            1
            for text in self._prior_user_texts(query)
            if route_crisis(text).action == "crisis_support"
        )

    def _local_safe_plan(
        self,
        *,
        fence: GenerationFence,
        speaker: Any,
        reason: str,
        query: str = "",
        policy: ModePolicy | None = None,
    ) -> ResponsePlan:
        prior = self._prior_user_text(query)
        return build_local_safe_plan(
            policy=policy or self._runtime.mode_policy_for_fence(fence),
            fence=fence,
            speaker=speaker,
            reason=reason,
            tts_model=self._tts_model,
            query=query,
            weather_lookup_available=self._realtime_sources_available(),
            city_followup=bool(query)
            and is_city_name_report(query)
            and mentions_weather(prior),
        )

    async def _crisis_plan_or_local(
        self,
        *,
        text: str,
        speaker: Any,
        fence: GenerationFence,
    ) -> tuple[ResponsePlan | None, str]:
        """Give the planner a short head start, then keep the local crisis reply.

        The deterministic crisis reply is already decided locally. The planner
        request still runs, because control-api records the guardian
        notification, but it may never gate the reply: on a long session that
        request times out and used to leave only an ordinary fallback.
        """

        client = self._response_planner_client
        script = route_crisis(
            text, prior_self_crisis_hits=self._prior_self_crisis_hits(text)
        ).script
        if client is None or not self._runtime.profile_permits(
            fence, capability="memory_recall_private"
        ):
            return None, "crisis_local_route"
        task = self._runtime._spawn(
            self._fetch_response_plan(text=text, speaker=speaker, fence=fence),
            name="crisis-plan-notification",
            durable=True,
        )
        if not response_plan_sidecar.applies(
            self._response_planner_client, self._runtime, fence
        ):
            self._watch_late_crisis_plan(task, text=text, fence=fence)
        done, _ = await asyncio.wait({task}, timeout=_CRISIS_PLAN_GRACE_S)
        if done:
            try:
                fetched = task.result()
            except Exception:
                logger.warning(
                    "crisis plan request failed; keeping local route session_id=%s turn_id=%s",
                    self._runtime.session_id,
                    fence.turn_id,
                    exc_info=True,
                )
                return None, "request_exception"
            return fetched.plan, fetched.reason
        logger.info(
            "crisis_local_route template_id=%s session_id=%s turn_id=%s",
            script.template_id if script is not None else "none",
            self._runtime.session_id,
            fence.turn_id,
        )
        return None, "crisis_local_route"

    def _watch_late_crisis_plan(
        self,
        task: asyncio.Task[Any],
        *,
        text: str,
        fence: GenerationFence,
    ) -> None:
        """Keep a background plan only when it raises the protective script.

        The reply may already be speaking. Nothing here reads or logs the child's
        words; the state holds the script the media bridge must play instead.
        """

        state = _LateCrisisPlan(started=time.monotonic())
        self._late_crisis[fence] = state

        def _resolved(done: asyncio.Task[Any]) -> None:
            if done.cancelled():
                return
            try:
                fetched = done.result()
            except Exception:
                fetched = None
            planned = getattr(getattr(fetched, "plan", None), "fixed_safety_script", None)
            prior_hits = self._prior_self_crisis_hits(text)
            local = route_crisis(text, prior_self_crisis_hits=prior_hits).script
            script = (
                planned
                if local is None
                else more_protective_local_script(
                    text, planned, prior_self_crisis_hits=prior_hits
                )
            )
            latency_ms = (time.monotonic() - state.started) * 1000.0
            if script is not None:
                state.script = script
                state.event.set()
            logger.info(
                "late_crisis_plan hit=%s category=%s latency_ms=%.1f "
                "session_id=%s turn_id=%s",
                "true" if script is not None else "false",
                script.template_id if script is not None else "none",
                latency_ms,
                fence.session_id,
                fence.turn_id,
            )

        task.add_done_callback(_resolved)

    def _with_local_crisis_floor(self, plan: ResponsePlan, query: str) -> ResponsePlan:
        script = more_protective_local_script(
            query,
            plan.fixed_safety_script,
            prior_self_crisis_hits=self._prior_self_crisis_hits(query),
        )
        if script is None:
            return plan
        if not plan_is_local_safe(plan):
            planned = plan.fixed_safety_script
            logger.warning(
                "crisis_route_raised_over_planner planned=%s local=%s session_id=%s turn_id=%s",
                planned.template_id if planned is not None else "none",
                script.template_id,
                self._runtime.session_id,
                plan.fence.turn_id,
            )
        return replace(plan, direct_text=script.display)

    def fixed_safety_planned(self, fence: GenerationFence) -> bool:
        """Whether this turn's reply is an approved fixed safety script.

        ``prepare_turn`` caches the plan before the media session emits
        ``generation.started``, so the session can mark that start protected on
        the device before any audio arrives.
        """

        return self._fixed_safety_script_for_turn(fence) is not None

    def _fixed_safety_script_for_turn(self, fence: GenerationFence) -> FixedSafetyScript | None:
        """A crisis script already decided for this turn on any generation.

        Prerecorded crisis audio is not an LLM reply and must not be dropped
        when an internal output handoff moves the reply to a new generation.
        """

        for plan in reversed(self._response_plans.same_turn(fence)):
            script = plan.fixed_safety_script
            if script is not None:
                return script
        for late_fence, state in reversed(tuple(self._late_crisis.items())):
            if (
                state.script is not None
                and late_fence.session_id == fence.session_id
                and late_fence.turn_id == fence.turn_id
                and late_fence.tool_epoch == fence.tool_epoch
                and late_fence.session_epoch == fence.session_epoch
            ):
                return state.script
        return None
