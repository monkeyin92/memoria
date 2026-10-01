"""What the reply model really receives in a device conversation (2026-10-01 root cause).

Every device session is bound to one person and runs on the local safe plan (the planner's answer never
matches a fence with ``session_epoch >= 1``). That path used to hand the model ONLY the current words plus the
plan block: the frozen system prompt (safety floor, persona, how to talk to a child or an elder, policy
obligations) and the earlier turns of the session were deleted by ``current_user_only_chat_context``. The robot
answered like a generic assistant (numbered markdown lists for a six-year-old) and could not follow
"那明天呢" or "再讲一个".
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.agent import (
    build_local_safe_plan,
    plan_is_local_safe,
    plan_matches_mode_policy,
)
from services.agent.src.context_assembler import (
    ContextAssembler,
    current_user_only_chat_context,
    interrupted_reply_chat_context,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ResponsePlanFetch
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import ScriptedChatModel, bind_owner_speaker
from services.speaker.domain import DEVICE_BOUND_SUBJECT_REASON

SYSTEM_PROMPT = "【不可变安全底线】不要输出 Markdown。【服务模式】孩子：一到两句短话。"


class _Planner:
    """The real planner's verdict for a device fence: fine plan, wrong epoch."""

    async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
        return ResponsePlanFetch(None, "fence_mismatch")


def _device_bound(runtime: DuplexRuntime) -> None:
    decision = bind_owner_speaker(runtime)
    bound = replace(decision, reason_code=DEVICE_BOUND_SUBJECT_REASON, model_version="device-binding-v1")
    runtime._speaker_decision = bound


async def _run_second_turn(runtime: DuplexRuntime, *, planner: object = None) -> tuple[list[Any], str]:
    """Two spoken turns; returns the messages the model received for the second and the plan block."""

    captured: dict[str, Any] = {}

    async def model(safe_ctx: Any, _tools: list[Any]) -> Any:
        captured["ctx"] = safe_ctx

        async def one() -> Any:
            yield "好的。"

        return one()

    agent = ReplyPipeline(
        instructions=SYSTEM_PROMPT,
        runtime=runtime,
        response_planner_client=planner or _Planner(),  # type: ignore[arg-type]
    )
    agent.language_model = ScriptedChatModel(model)
    from services.agent.src.contracts.ids import CancellationContext
    from services.agent.src.orchestration.handlers import LanguageModelRequest

    fence = await agent.prepare_committed_turn("我今天画了一只小狗")
    _ = [t async for t in agent.stream(LanguageModelRequest(user_text="x", cancellation=CancellationContext.capture(fence)))]
    runtime.orchestrator.context.commit_assistant_heard("小狗很可爱呀，它是什么颜色的？", speaker_scope="owner")
    fence = await agent.prepare_committed_turn("是黄色的")
    _ = [t async for t in agent.stream(LanguageModelRequest(user_text="y", cancellation=CancellationContext.capture(fence)))]
    items = list(captured["ctx"].items)
    plan_block = "\n".join(m.text_content for m in items if m.role == "system" and m.text_content != SYSTEM_PROMPT)
    return items, plan_block


@pytest.mark.asyncio
async def test_a_device_bound_child_conversation_keeps_the_system_prompt_and_this_sessions_turns() -> None:
    runtime = DuplexRuntime.create(session_id="device-session-context")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False)
    _device_bound(runtime)

    items, plan_block = await _run_second_turn(runtime)

    system_messages = [m.text_content for m in items if m.role == "system"]
    assert SYSTEM_PROMPT in system_messages, "the model must see the frozen system prompt"
    conversation = [(m.role, m.text_content) for m in items if m.role != "system"]
    assert conversation == [
        ("user", "我今天画了一只小狗"),
        ("assistant", "小狗很可爱呀，它是什么颜色的？"),
        ("user", "是黄色的"),
    ], "the model must follow the conversation, not only the last sentence"
    assert "本次会话中已经听见的对话" in plan_block
    assert "持久历史" in plan_block, "persistent history and private memory stay closed"
    await runtime.close()


@pytest.mark.asyncio
async def test_without_a_device_binding_the_fallback_still_sees_only_the_current_words() -> None:
    """The fail-closed rule is unchanged for every other owner decision."""

    runtime = DuplexRuntime.create(session_id="owner-fallback-context")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False)
    bind_owner_speaker(runtime)  # reason_code authenticated_text_input, not a device binding

    items, plan_block = await _run_second_turn(runtime)

    conversation = [(m.role, m.text_content) for m in items if m.role != "system"]
    assert conversation == [("user", "是黄色的")]
    assert "仅依据当前用户这一轮内容回答" in plan_block
    await runtime.close()


def _chat_context() -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content=SYSTEM_PROMPT)
    ctx.add_message(role="user", content="旧问题")
    ctx.add_message(role="assistant", content="旧回答")
    ctx.add_message(role="user", content="现在的问题")
    return ctx


def test_current_user_only_context_can_keep_the_system_prompt_and_nothing_else() -> None:
    kept = current_user_only_chat_context(_chat_context(), keep_instructions=True)
    assert [(m.role, m.text_content) for m in kept.items] == [
        ("system", SYSTEM_PROMPT),
        ("user", "现在的问题"),
    ]
    stripped = current_user_only_chat_context(_chat_context())
    assert [(m.role, m.text_content) for m in stripped.items] == [("user", "现在的问题")]


def test_the_forced_user_only_path_keeps_the_prompt_for_the_owner_but_not_for_a_guest() -> None:
    plan = build_local_safe_plan(
        policy=_companion_policy(),
        fence=GenerationFence("s", 1, 1, 0, 1),
        speaker=SimpleNamespace(classification="owner", reason_code="owner_match"),
        reason="test",
        tts_model="seed-tts-2.0",
        query="你好",
    )
    assembler = ContextAssembler()
    owner = assembler.assemble(
        chat_ctx=_chat_context(), heard_assistant=[], speaker_class="owner", response_plan=plan,
        force_current_user_only=True,
    )
    assert [m.role for m in owner.items] == ["system", "user", "system"]
    assert owner.items[0].text_content == SYSTEM_PROMPT
    guest = assembler.assemble(
        chat_ctx=_chat_context(), heard_assistant=[], speaker_class="guest", response_plan=plan,
        force_current_user_only=True,
    )
    assert [m.role for m in guest.items] == ["user", "system"]


def test_resuming_an_interrupted_owner_reply_keeps_the_system_prompt() -> None:
    ctx = _chat_context()
    owner = interrupted_reply_chat_context(ctx, ["旧回答"], include_previous_user=True)
    assert owner.items[0].text_content == SYSTEM_PROMPT
    guest = interrupted_reply_chat_context(_chat_context(), ["旧回答"], include_previous_user=False)
    assert all(m.text_content != SYSTEM_PROMPT for m in guest.items)


def _companion_policy() -> Any:
    runtime = DuplexRuntime.create(session_id="policy-only")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False)
    return runtime.mode_policy


@pytest.mark.parametrize("audience", (None, "student_minor", "senior_companion"))
def test_every_audience_variant_of_the_self_introduction_passes_plan_admission(audience: str | None) -> None:
    """The introduction a child or an elder hears is fixed text; admission compares it against the allowed set,
    so a variant missing there would have refused the plan and left 'what is your name' without an answer."""

    from services.common.companion_response_safety import fixed_companion_reply

    policy = _companion_policy()
    companion = __import__("services.common.companions", fromlist=["companion_definition"]).companion_definition(policy.companion_style_id)
    reply = fixed_companion_reply(
        query="你叫什么名字？",
        is_companion=True,
        display_name=companion.display_name,
        style_description=companion.style_description,
        audience=audience,
    )
    plan = build_local_safe_plan(
        policy=policy,
        fence=GenerationFence("s", 1, 1, 0, 1),
        speaker=SimpleNamespace(classification="owner", reason_code="owner_match", model_version="m", profile_id=None, template_version=None),
        reason="test",
        tts_model="seed-tts-2.0",
        query="你叫什么名字？",
    )
    assert plan_is_local_safe(plan)
    plan = replace(plan, direct_text=reply)
    assert plan_matches_mode_policy(plan, policy, tts_model="seed-tts-2.0")
