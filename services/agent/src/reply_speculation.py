"""Reply speculation mixin: the reply request sent before the turn commits.

``ReplySpeculationMixin`` is composed into ``ReplyPipeline``.  The speculative
reply sends the request the committed turn is predicted to send; the commit's
``stream_reply`` adopts it only when it asks exactly what the committed turn
asks for.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterable
from typing import TYPE_CHECKING, Any, cast

from services.agent.src.agent import plan_is_local_safe, plan_matches_mode_policy
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.llm_types import ChatContext
from services.agent.src.orchestration.context_snapshot_manager import (
    MemoryCapsule,
    PersonaCapsule,
)
from services.agent.src.reply_warmup import _compact_sentence, _memory_capsule
from services.agent.src.response_planner_client import ResponsePlan
from services.agent.src.speculative_reply import (
    SPECULATIVE_REPLY_TASK_NAME,
    Outcome,
    SpeculationKey,
    SpeculativeReply,
    log_outcome,
)
from services.agent.src.speculative_speech import speech_head_start
from services.common.crisis_policy import route_crisis

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.context_manager import ChatMessage
    from services.agent.src.orchestration.context_snapshot_manager import ContextSnapshot
    from services.agent.src.reply_warmup import _WarmedMemory
    from services.common.response_depth import ResponseDepthPolicy

logger = logging.getLogger(__name__)


def _speculation_change(
    current: SpeculativeReply,
    key: SpeculationKey,
    memory: asyncio.Task[Any] | None,
    chat_ctx: ChatContext | None,
) -> str:
    """Which input of a speculative request for the same sentence differs, as field names."""

    changed = [
        name
        for name, before, after in (
            ("speaker", current.key.speaker, key.speaker),
            ("fence", current.key.fence, key.fence),
            ("policy", current.key.policy, key.policy),
        )
        if before != after
    ]
    if current.memory is not memory:
        changed.append("memory")
    if chat_ctx is not None and current.messages not in (None, chat_ctx.to_openai_messages()):
        changed.append("messages")
    return ",".join(changed) or "none"


def _plan_capsules(
    plan: ResponsePlan, device_memory: MemoryCapsule | None
) -> tuple[MemoryCapsule, PersonaCapsule]:
    """The memory and persona capsules a turn's context snapshot is frozen with under ``plan``."""

    memory = (
        device_memory
        if device_memory is not None and plan_is_local_safe(plan)
        else _memory_capsule(plan.grounded_items)
    )
    persona = PersonaCapsule(
        version_id=plan.provenance.persona_version_id,
        version_number=plan.provenance.persona_version_number,
        prompt_fragment="\n".join(
            item.content for item in plan.grounded_items if item.kind == "persona_trait"
        ),
    )
    return memory, persona


def _memory_result(task: asyncio.Task[MemoryCapsule | None] | None) -> MemoryCapsule | None:
    """What a finished device-memory fetch released; nothing for one still running or cancelled."""

    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return None
    return task.result()


class ReplySpeculationMixin:
    """Send the predicted reply request and adopt it only for the exact commit."""

    if TYPE_CHECKING:
        # Shared state owned by the ReplyPipeline.
        _runtime: DuplexRuntime
        _speculation: SpeculativeReply | None
        _speculative_reply: bool
        _speculative_speech: bool
        _warmed_memory: _WarmedMemory | None
        _tts_model: str
        language_model: Any

        # Core pipeline and sibling-mixin members consumed by this mixin.
        def _chat_context(self, turns: Iterable[ChatMessage]) -> ChatContext: ...

        def _last_user_text(self, turns: Iterable[ChatMessage], *, resume: bool) -> str: ...

        def _reply_chat_context(
            self,
            chat_ctx: Any,
            *,
            response_plan: ResponsePlan,
            policy: ModePolicy,
            snapshot: ContextSnapshot,
            speech_instruction: str,
            last_user: str,
            resume: bool,
            realtime: bool,
        ) -> tuple[Any, ResponseDepthPolicy]: ...

        def _local_safe_plan(
            self,
            *,
            fence: GenerationFence,
            speaker: Any,
            reason: str,
            query: str = "",
            policy: ModePolicy | None = None,
        ) -> ResponsePlan: ...

        def _prior_self_crisis_hits(self, query: str) -> int: ...

        def _device_memory_in_scope(self, speaker: Any, fence: GenerationFence) -> bool: ...

        def _drop_early_profile_refresh(self) -> None: ...

    def _speculate(self, text: str, speaker: Any, *, claim: bool) -> None:
        """Send the reply request ``text`` is predicted to commit with, unless that request is already out.

        ``claim`` says the commit of ``text`` has begun: only a claimed speculation is ended when the commit
        rejects its sentence, so one that an ASR final named for a later turn survives an earlier rejection.
        Nothing here may stop the commit.
        """

        try:
            self._start_speculation(text, speaker, claim=claim)
        except Exception:
            logger.warning(
                "speculative reply not started session_id=%s", self._runtime.session_id, exc_info=True
            )

    def _start_speculation(self, text: str, speaker: Any, *, claim: bool) -> None:
        model = self.language_model
        if not self._speculative_reply or model is None or not text.strip():
            return
        key = SpeculationKey(text, speaker, self._runtime.fence.bump_turn(), self._runtime.mode_policy)
        predicted = self._speculation_plan(key)
        memory: asyncio.Task[MemoryCapsule | None] | None = None
        if not isinstance(predicted, str) and self._device_memory_in_scope(
            speaker, self._runtime.fence
        ):
            warmed = self._warmed_memory
            if (
                warmed is None
                or warmed.sentence != _compact_sentence(text)
                or warmed.speaker != speaker
            ):
                predicted = "memory"
            else:
                memory = warmed.task
        chat_ctx: ChatContext | None = None
        if not isinstance(predicted, str) and (memory is None or memory.done()):
            chat_ctx = self._predicted_reply_context(key, predicted, memory)
            if chat_ctx is None:
                predicted = "context"
        current = self._speculation
        if isinstance(predicted, str):
            if current is not None and current.key.text == text:
                self._take_speculation().settle(predicted)
                return
            self._replace_speculation(text)
            if claim:
                log_outcome(key.fence, predicted)
            return
        plan = predicted
        if (
            current is not None
            and current.key == key
            and current.memory is memory
            and (chat_ctx is None or current.messages in (None, chat_ctx.to_openai_messages()))
        ):
            current.claimed = current.claimed or claim
            return
        if current is not None and current.key.text == text:
            logger.info(  # never the text: a robot round reads this to see why the head start was lost
                "speculative reply restart session_id=%s turn_id=%s changed=%s",
                self._runtime.session_id,
                key.fence.turn_id,
                _speculation_change(current, key, memory, chat_ctx),
            )
        self._replace_speculation(text)
        speech = (
            speech_head_start(self._runtime, key, speaker_class=plan.provenance.speaker_class)
            if self._speculative_speech
            else None
        )
        speculation = SpeculativeReply(key, claimed=claim, memory=memory, speech=speech)
        self._speculation = speculation

        def spawn(coroutine: Any) -> asyncio.Task[None]:
            return self._runtime._spawn(coroutine, name=SPECULATIVE_REPLY_TASK_NAME)

        if chat_ctx is not None:
            speculation.send(spawn, model, chat_ctx)
            return

        def build_after_memory() -> ChatContext | None:
            committed_meanwhile = self._runtime.fence.bump_turn() != key.fence
            if self._speculation is not speculation or committed_meanwhile:
                return None
            try:
                return self._predicted_reply_context(key, plan, memory)
            except Exception:
                logger.warning(
                    "speculative reply not sent session_id=%s", self._runtime.session_id, exc_info=True
                )
                return None

        speculation.send_after_memory(spawn, model, build_after_memory)

    def _speculation_plan(self, key: SpeculationKey) -> ResponsePlan | Outcome:
        """The plan the committed turn is predicted to reply under, or why it will not ask the model.

        The speculative request may carry nothing the committed turn would not send, so every route that
        never reaches the reply model is ruled out first: a crisis script, a farewell, a live lookup, a
        local direct answer, and a speaker or plan the policy refuses.
        """

        text, speaker, policy = key.text, key.speaker, key.policy
        crisis = route_crisis(text, prior_self_crisis_hits=self._prior_self_crisis_hits(text))
        if crisis.script is not None:
            return "crisis"
        if self._runtime.conversation_close_needed(text):
            return "close"
        if self._runtime.weather_turn(text) is not None or self._runtime.live_lookup_needed(text):
            return "lookup"
        enforced = self._runtime.mode_policy_enforced
        if enforced and not policy.allows_conversation(getattr(speaker, "classification", None)):
            return "plan"
        plan = self._local_safe_plan(
            fence=key.fence, speaker=speaker, reason="speculative", query=text, policy=policy
        )
        if (
            plan.direct_text is not None
            or plan.fixed_safety_script is not None
            or (enforced and not plan_matches_mode_policy(plan, policy, tts_model=self._tts_model))
        ):
            return "plan"
        return plan

    def _predicted_reply_context(
        self,
        key: SpeculationKey,
        plan: ResponsePlan,
        memory: asyncio.Task[MemoryCapsule | None] | None,
    ) -> ChatContext | None:
        """The request ``stream_reply`` will send for ``key`` if nothing but its commit happens first.

        Each input is what the commit will leave: the session context with the sentence added, the
        snapshot it freezes with the warmed memory, and the speech plan it picks for the sentence.
        """

        speaker_class = plan.provenance.speaker_class
        context = self._runtime.orchestrator.context.after_user(
            key.text, speaker_scope="owner" if speaker_class == "owner" else "public"
        )
        memory_capsule, persona_capsule = _plan_capsules(plan, _memory_result(memory))
        snapshot = self._runtime.predicted_generation_snapshot(
            context,
            speaker_class=speaker_class,
            memory_capsule=memory_capsule,
            persona_capsule=persona_capsule,
        )
        if snapshot is None:
            return None
        speech_plan = self._runtime.preview_speech_plan(key.text, turn_id=key.fence.turn_id)
        chat_ctx, _ = self._reply_chat_context(
            self._chat_context(context.turns),
            response_plan=plan,
            policy=key.policy,
            snapshot=snapshot,
            speech_instruction=speech_plan.llm_instruction,
            last_user=self._last_user_text(context.turns, resume=False),
            resume=False,
            realtime=False,
        )
        return cast(ChatContext, chat_ctx)

    def _take_speculation(self) -> SpeculativeReply:
        speculation, self._speculation = self._speculation, None
        assert speculation is not None
        return speculation

    def _replace_speculation(self, text: str) -> None:
        """End the speculative reply that a new prediction for ``text`` replaces."""

        current = self._speculation
        if current is None:
            return
        self._speculation = None
        if current.bound:
            current.settle("unused")
        elif current.key.text != text:
            current.settle("other_text")
        else:
            current.settle("restarted")

    def _bind_speculation(
        self, *, text: str, speaker: Any, fence: GenerationFence, plan: ResponsePlan
    ) -> None:
        """Hold the speculative reply for the committed turn only when it was sent for exactly this turn."""

        speculation = self._speculation
        if speculation is None:
            return
        key = speculation.key
        named_for_a_later_sentence = not speculation.claimed and key.text != text
        if named_for_a_later_sentence:
            return
        outcome: Outcome | None = None
        if key.text != text:
            outcome = "other_text"
        elif key.speaker != speaker:
            outcome = "other_speaker"
        elif key.fence != fence:
            outcome = "other_fence"
        elif key.policy != self._runtime.mode_policy_for_fence(fence):
            outcome = "other_policy"
        elif plan.fixed_safety_script is not None:
            outcome = "crisis"
        elif self._runtime.conversation_close_needed(text):
            outcome = "close"
        elif not plan_is_local_safe(plan) or plan.direct_text is not None:
            outcome = "plan"
        if outcome is None:
            speculation.bound = True
        else:
            self._take_speculation().settle(outcome)

    def _speculation_for(self, fence: GenerationFence) -> SpeculativeReply | None:
        """Take the speculative reply sent for the turn ``fence`` serves; one sent for a later turn stays."""

        speculation = self._speculation
        if speculation is None or speculation.key.fence.turn_id > fence.turn_id:
            return None
        self._speculation = None
        if speculation.key.fence != fence:
            speculation.settle("other_fence")
            return None
        return speculation

    def _settle_speculation(self, fence: GenerationFence, outcome: Outcome) -> None:
        speculation = self._speculation_for(fence)
        if speculation is not None:
            speculation.settle(outcome)

    def _adopt_speculation(
        self, fence: GenerationFence, chat_ctx: Any
    ) -> AsyncGenerator[Any, None] | None:
        """The speculative stream when it asked exactly what ``chat_ctx`` asks, else None."""

        speculation = self._speculation_for(fence)
        if speculation is None:
            return None
        if speculation.messages is None:
            speculation.settle("late")
            return None
        if not speculation.bound or speculation.messages != chat_ctx.to_openai_messages():
            speculation.settle("other_request")
            return None
        return speculation.adopt()

    def discard_speculative_reply(self, reason: str) -> None:
        """The commit that claimed the speculative reply rejected its sentence for ``reason``.

        A bound speculation belongs to a turn that was committed and still waits for its reply stream.
        """

        self._drop_early_profile_refresh()
        speculation = self._speculation
        if speculation is None or not speculation.claimed or speculation.bound:
            return
        self._speculation = None
        close = reason.startswith("conversation_end") or self._runtime.conversation_close_needed(
            speculation.key.text
        )
        speculation.settle("close" if close else "not_committed")
