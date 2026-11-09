"""Reply warm-up mixin: plan and memory head starts before a turn commits.

``ReplyWarmupMixin`` is composed into ``ReplyPipeline``.  The turn commit used to
wait for the response plan and the device memory after the close verdict; these
methods start both while the end-of-speech grace is still running and let
``prepare_turn`` take the result only when it would send the same request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from services.agent.src import response_plan_sidecar
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.context_snapshot_manager import (
    ContextSnapshot,
    MemoryCapsule,
    MemoryCapsuleEntry,
)
from services.agent.src.response_planner_client import (
    RECALL_CONTEXT_ITEM_MAX_CHARS,
    RECALL_CONTEXT_MAX_ITEMS,
    RECALL_CONTEXT_TOTAL_MAX_CHARS,
    ResponseGroundedItem,
    ResponsePlanFetch,
    ResponsePlannerClient,
)
from services.common.crisis_policy import route_crisis

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime

logger = logging.getLogger(__name__)


# plan's tool; a memory that is still outstanding when the plan is in is dropped, never waited for.
_DEVICE_MEMORY_TIMEOUT_S = 2.0
DEVICE_MEMORY_TASK_NAME = "device-memory-prefetch"
# Stronger-than-bind owner reasons that may open device private-memory prefetch (A06).
_STRONG_OWNER_MEMORY_REASONS = frozenset({"authenticated_text_input", "owner_match"})
# A fetch started for a sentence whose turn is about to be committed (``warm_committed_turn``) is only ever
# taken by that commit; one that nobody took within this long belonged to a turn that never committed.
_WARMED_MEMORY_MAX_AGE_S = 10.0
# The response-plan request of a turn about to be committed goes out under the fence the commit will make
# (``start_committed_turn_plan``); the commit takes it only when it would send exactly the same request.
EARLY_RESPONSE_PLAN_TASK_NAME = "response-plan-head-start"


def _compact_sentence(text: str) -> str:
    """The sentence without punctuation or spaces: the final and the committed text may differ in those."""

    return "".join(character for character in text if character.isalnum())


@dataclass(slots=True)
class _WarmedMemory:
    """The device-memory fetch started for one sentence before its turn was committed."""

    sentence: str
    speaker: Any
    task: asyncio.Task[MemoryCapsule | None]
    started: float


@dataclass(frozen=True, slots=True)
class _PlanRequest:
    """What one response-plan request asks the control plane; two equal values ask the same thing."""

    text: str
    speaker: Any
    fence: GenerationFence
    recall_context: tuple[str, ...]
    utterance_intent: str


@dataclass(slots=True)
class _EarlyPlan:
    """A response-plan request sent before its turn was committed."""

    request: _PlanRequest
    task: asyncio.Task[ResponsePlanFetch]




def _recall_context(snapshot: ContextSnapshot, *, speaker: Any) -> tuple[str, ...]:
    """Keep only recent owner utterances from a frozen snapshot."""

    if getattr(speaker, "classification", None) != "owner" or snapshot.speaker_class != "owner":
        return ()
    selected: list[str] = []
    total_chars = 0
    for turn in reversed(snapshot.recent_committed_turns):
        if turn.role != "user" or turn.speaker_scope != "owner":
            continue
        text = turn.content.strip()[:RECALL_CONTEXT_ITEM_MAX_CHARS]
        if not text:
            continue
        if total_chars + len(text) > RECALL_CONTEXT_TOTAL_MAX_CHARS:
            break
        selected.append(text)
        total_chars += len(text)
        if len(selected) >= RECALL_CONTEXT_MAX_ITEMS:
            break
    selected.reverse()
    return tuple(selected)




def _memory_capsule(items: Iterable[ResponseGroundedItem]) -> MemoryCapsule:
    """The memory entries among grounded items; persona traits travel in their own capsule."""

    return MemoryCapsule(
        tuple(
            MemoryCapsuleEntry(
                item_id=item.item_id,
                kind=item.kind,
                content=item.content,
                source_refs=item.source_event_ids,
                use_as=item.use_as,
                confidence=item.confidence,
                sharing_scope=item.sharing_scope,
            )
            for item in items
            if item.kind != "persona_trait"
        )
    )




class ReplyWarmupMixin:
    """Start the response-plan and device-memory work of a turn before its commit."""

    if TYPE_CHECKING:
        # Shared state owned by ReplyPipeline.
        _runtime: DuplexRuntime
        _response_planner_client: ResponsePlannerClient | None
        _warmed_memory: _WarmedMemory | None
        _early_plan: _EarlyPlan | None
        _early_profile_refresh: tuple[str, asyncio.Task[Any]] | None

        # Core members consumed by this mixin.
        def _prior_self_crisis_hits(self, query: str) -> int: ...

        def _speculate(self, text: str, speaker: Any, *, claim: bool) -> None: ...

    def start_committed_turn_plan(self, text: str) -> None:
        """Send the response-plan request of a turn the media session is about to commit.

        The request used to leave only once the turn was committed, behind both classifier verdicts and two
        profile refreshes, and then took 0.3-0.8 s of the child's wait on its own (2026-10-28 bench:
        ``prepare_ms`` p50 983).  This one goes out under the fence the commit will make; ``prepare_turn``
        takes it only when the request it would send itself is the same, and sends its own otherwise.

        The reply request goes out here too (``_speculate``), unless the one the ASR final sent is still
        the request this turn is predicted to send.
        """

        speaker = self._runtime.current_speaker_decision
        self._start_early_plan(text, speaker)
        self._start_early_profile_refresh(text)
        self._speculate(text, speaker, claim=True)

    def _start_early_profile_refresh(self, text: str) -> None:
        """Refresh the signed profile beside the close verdict instead of after it.

        ``prepare_turn`` refreshes it before the voice bind check; on the device path that ran only once
        the commit had its close verdict, and the session-policy round trip added about 0.1 s to every
        turn (2026-11-01 device soak).  The turn-boundary refresh in ``on_turn_committed`` stays.
        """

        self._drop_early_profile_refresh()
        if self._runtime.tts is None:
            return
        task = self._runtime._spawn(
            self._runtime.refresh_runtime_profile(), name="runtime-profile-head-start"
        )
        self._early_profile_refresh = (text, task)

    def _drop_early_profile_refresh(self) -> None:
        early, self._early_profile_refresh = self._early_profile_refresh, None
        if early is not None and not early[1].done():
            early[1].cancel()

    async def _refresh_runtime_profile_for(self, text: str) -> None:
        early, self._early_profile_refresh = self._early_profile_refresh, None
        if early is not None and early[0] == text and not early[1].cancelled():
            await early[1]
            return
        if early is not None and not early[1].done():
            early[1].cancel()
        await self._runtime.refresh_runtime_profile()

    def _start_early_plan(self, text: str, speaker: Any) -> None:
        fence = self._runtime.fence.bump_turn()
        try:
            recall_context = _recall_context(
                self._runtime.prospective_context_snapshot(), speaker=speaker
            )
        except (RuntimeError, ValueError):
            recall_context = ()
        request = _PlanRequest(
            text=text,
            speaker=speaker,
            fence=fence,
            recall_context=recall_context,
            utterance_intent=self._runtime.route_user_turn(text).intent,
        )
        early = self._early_plan
        if early is not None and early.request == request:
            return
        self._drop_early_plan()
        client = self._response_planner_client
        local_crisis = route_crisis(
            text, prior_self_crisis_hits=self._prior_self_crisis_hits(text)
        ).script
        if (
            client is None
            or not text.strip()
            # A crisis turn asks the control plane its own way (``_crisis_plan_or_local``).
            or local_crisis is not None
            or not self._runtime.profile_permits(fence, capability="memory_recall_private")
            or response_plan_sidecar.applies(client, self._runtime, fence)
        ):
            return
        task = self._runtime._spawn(
            client.fetch(
                session_id=self._runtime.session_id,
                query=text,
                fence=fence,
                speaker_decision=speaker,
                recall_context=recall_context,
                utterance_intent=request.utterance_intent,
            ),
            name=EARLY_RESPONSE_PLAN_TASK_NAME,
        )
        self._early_plan = _EarlyPlan(request, task)

    def _drop_early_plan(self) -> None:
        early, self._early_plan = self._early_plan, None
        if early is not None and not early.task.done():
            early.task.cancel()

    def _take_early_plan(self, request: _PlanRequest) -> asyncio.Task[ResponsePlanFetch] | None:
        """The request ``_start_early_plan`` sent when it is exactly ``request``, or None."""

        early, self._early_plan = self._early_plan, None
        if early is None:
            return None
        sent = early.request
        if sent.fence != request.fence:
            outcome = "other_fence"
        elif sent.text != request.text:
            outcome = "other_sentence"
        elif sent.speaker != request.speaker:
            outcome = "other_speaker"
        elif sent != request:
            outcome = "other_context"
        elif early.task.cancelled():
            outcome = "cancelled"
        else:
            outcome = "used"
        logger.info(  # never the text: a robot round reads this line to see whether the head start was used
            "response plan head start session_id=%s turn_id=%s outcome=%s",
            self._runtime.session_id,
            request.fence.turn_id,
            outcome,
        )
        if outcome == "used":
            return early.task
        if not early.task.done():
            early.task.cancel()
        return None

    def _device_memory_in_scope(self, speaker: Any, fence: GenerationFence) -> bool:
        """Private device memory needs owner authority stronger than bind alone (A06)."""

        reason = getattr(speaker, "reason_code", None)
        return (
            callable(getattr(self._response_planner_client, "prefetch_context", None))
            and getattr(speaker, "classification", None) == "owner"
            and reason in _STRONG_OWNER_MEMORY_REASONS
            and self._runtime.profile_permits(fence, capability="memory_recall_private")
        )

    def warm_committed_turn(self, text: str) -> None:
        """Start the device-memory fetch of a sentence whose turn is about to be committed.

        The commit's own fetch starts after the end-of-speech grace and costs about 0.4 s of the child's
        wait (round 12, TODOLIST N-14 8).  This one runs inside the grace; ``prepare_turn`` takes it for
        the same sentence and the same speaker and fetches for itself otherwise.  Read-only and bounded
        exactly like the commit's fetch: ``/context-prefetch`` creates no turn.

        The speaker is the one the commit will hold.  A session's first sentence has no decision yet (it is
        made when the turn commits) and a device VAD start resets it, so asking for the current one found
        nobody and the fetch never started: 6 of 84 commits in round 15, 0.4 s each (N-14 10).

        The sentence's reply request goes out here as well (``_speculate``); the commit's own start keeps it
        when it is still the request the turn is predicted to send.
        """

        speaker = self._runtime.prospective_speaker_decision()
        self._warm_memory(text, speaker)
        self._speculate(text, speaker, claim=False)

    def _warm_memory(self, text: str, speaker: Any) -> None:
        sentence = _compact_sentence(text)
        warmed = self._warmed_memory
        if warmed is not None and warmed.sentence == sentence and warmed.speaker == speaker:
            return
        self._drop_warmed_memory()
        if not sentence or not self._device_memory_in_scope(speaker, self._runtime.fence):
            return
        task = self._runtime._spawn(
            self._fetch_device_memory(text=text, speaker=speaker), name=DEVICE_MEMORY_TASK_NAME
        )
        self._warmed_memory = _WarmedMemory(sentence, speaker, task, time.monotonic())

    def _drop_warmed_memory(self) -> None:
        warmed, self._warmed_memory = self._warmed_memory, None
        if warmed is not None and not warmed.task.done():
            warmed.task.cancel()

    def _take_warmed_memory(
        self, text: str, speaker: Any
    ) -> asyncio.Task[MemoryCapsule | None] | None:
        """The fetch ``warm_committed_turn`` started for exactly this sentence and speaker, or None."""

        warmed, self._warmed_memory = self._warmed_memory, None
        if warmed is None:
            return None
        if warmed.sentence != _compact_sentence(text):
            outcome = "other_sentence"
        elif warmed.speaker != speaker:
            outcome = "other_speaker"
        elif time.monotonic() - warmed.started > _WARMED_MEMORY_MAX_AGE_S:
            outcome = "stale"
        elif warmed.task.cancelled():
            outcome = "cancelled"
        else:
            outcome = "used"
        logger.info(  # never the text: a robot round reads this line to see whether the head start was used
            "device memory warm-up session_id=%s outcome=%s", self._runtime.session_id, outcome
        )
        if outcome == "used":
            return warmed.task
        if not warmed.task.done():
            warmed.task.cancel()
        return None

    async def _fetch_device_memory(self, *, text: str, speaker: Any) -> MemoryCapsule | None:
        """The memories the control plane releases for this exact speaker and sentence, or None.

        Every failure means no memory for this reply, never a blocked reply.  The control API applies
        the subject scope (a minor needs the guardian's retention consent) and returns confirmed
        memories only; the content is never logged.
        """

        client = cast(ResponsePlannerClient, self._response_planner_client)
        try:
            async with asyncio.timeout(_DEVICE_MEMORY_TIMEOUT_S):
                fetched = await client.prefetch_context(
                    session_id=self._runtime.session_id,
                    query=text,
                    speaker_decision=speaker,
                )
        except Exception as exc:
            logger.info(
                "device memory unavailable session_id=%s reason=%s",
                self._runtime.session_id,
                type(exc).__name__,
            )
            return None
        # The speaker the commit will hold, not the decision of the moment. A device VAD start only
        # resets to classification_pending; that placeholder is not another speaker (A06: bind no
        # longer predicts owner through the pending window).
        if not fetched.available:
            return None
        prospective = self._runtime.prospective_speaker_decision()
        if (
            prospective != speaker
            and getattr(prospective, "reason_code", None) != "classification_pending"
        ):
            return None
        capsule = _memory_capsule(fetched.grounded_items)
        logger.info(
            "device memory attached session_id=%s items=%s",
            self._runtime.session_id,
            len(capsule.entries),
        )
        return capsule
