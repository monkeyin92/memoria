"""Agent production wiring: response-plan gating, reply streaming, budget and alignment."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src import reply_pipeline as pipeline_mod
from services.agent.src import reply_warmup
from services.agent.src.agent import plan_is_local_safe
from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.duplex_runtime import (
    DuplexRuntime,
)
from services.agent.src.llm_types import StopResponse
from services.agent.src.orchestration.context_snapshot_manager import (
    ContextSnapshotDraft,
    ContextTurn,
    MemoryCapsule,
    MemoryCapsuleEntry,
    PersonaCapsule,
)
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.orchestration.prosody import SpeechPlan
from services.agent.src.orchestration.speaker_verify import (
    should_enable_legacy_speaker_verifier,
)
from services.agent.src.orchestration.task_manager import ToolSpec
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponseGroundedItem,
    ResponsePlanFetch,
)
from services.agent.tests.unit.agent_wiring_support import (
    _plan_for_fence,
    _text_source,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
    bind_owner_speaker,
    commit_media_turn,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


@pytest.mark.asyncio
async def test_agent_prepares_and_streams_a_media_turn_through_the_response_plan() -> None:
    runtime = DuplexRuntime.create(session_id="media-agent-session")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    bind_owner_speaker(runtime)

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            speaker = kwargs["speaker_decision"]
            assert isinstance(speaker, SpeakerDecision)
            plan = _plan_for_fence(
                kwargs["fence"],  # type: ignore[arg-type]
                instructions="只回答已授权内容。",
                direct_text="这是经过完整响应计划的回答。",
            )
            return ResponsePlanFetch(
                replace(
                    plan,
                    provenance=replace(
                        plan.provenance,
                        speaker_reason_code=speaker.reason_code,
                        speaker_profile_id=speaker.profile_id,
                        speaker_model_version=speaker.model_version,
                        speaker_template_version=speaker.template_version,
                    ),
                ),
                "ok",
            )

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )

    fence = await agent.prepare_committed_turn("请回答")
    output = [
        token
        async for token in agent.stream(
            LanguageModelRequest(
                user_text="请回答",
                cancellation=CancellationContext.capture(fence),
            )
        )
    ]

    assert output == ["这是经过完整响应计划的回答。"]
    await runtime.close()


@pytest.mark.asyncio
async def test_response_plan_receives_bounded_owner_recall_context_only() -> None:
    runtime = DuplexRuntime.create(session_id="response-plan-recall-context")
    bind_owner_policy(runtime, voice_profile=False)
    bind_owner_speaker(runtime)
    fresh_snapshot = runtime.orchestrator.context_snapshots.rebind_identity(
        runtime.session_id,
        ContextSnapshotDraft(
            recent_committed_turns=tuple(
                ContextTurn("user", f"第{index}轮提到的安排", "owner") for index in range(1, 7)
            ),
            relationship_policy=runtime.mode_policy,
            tool_permission=True,
            speaker_class="owner",
        ),
    )
    runtime.orchestrator.bind_context_version(runtime.fence, fresh_snapshot.version)
    observed: dict[str, object] = {}

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            observed.update(kwargs)
            speaker = kwargs["speaker_decision"]
            assert isinstance(speaker, SpeakerDecision)
            plan = _plan_for_fence(kwargs["fence"], instructions="按历史上下文回答。")  # type: ignore[arg-type]
            return ResponsePlanFetch(
                replace(
                    plan,
                    provenance=replace(
                        plan.provenance,
                        speaker_reason_code=speaker.reason_code,
                        speaker_profile_id=speaker.profile_id,
                        speaker_model_version=speaker.model_version,
                        speaker_template_version=speaker.template_version,
                    ),
                ),
                "ok",
            )

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    result = await agent._fetch_response_plan(
        text="现在还记得之前的安排吗？",
        speaker=runtime.current_speaker_decision,
        fence=runtime.fence,
    )

    assert result.available
    assert observed["recall_context"] == (
        "第3轮提到的安排",
        "第4轮提到的安排",
        "第5轮提到的安排",
        "第6轮提到的安排",
    )
    uncertain = replace(
        runtime.current_speaker_decision,
        classification="uncertain",
        reason_code="owner_mismatch",
        profile_id=None,
        permissions=permissions_for_speaker("uncertain"),
    )
    assert agent._recall_context_for_fence(runtime.fence, speaker=uncertain) == ()
    await runtime.close()


@pytest.mark.asyncio
async def test_media_agent_streams_the_configured_llm_without_a_livekit_session() -> None:
    runtime = DuplexRuntime.create(session_id="standalone-media-agent")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    bind_owner_speaker(runtime)

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            speaker = kwargs["speaker_decision"]
            assert isinstance(speaker, SpeakerDecision)
            plan = _plan_for_fence(
                kwargs["fence"],  # type: ignore[arg-type]
                instructions="简洁回答。",
            )
            return ResponsePlanFetch(
                replace(
                    plan,
                    provenance=replace(
                        plan.provenance,
                        speaker_reason_code=speaker.reason_code,
                        speaker_profile_id=speaker.profile_id,
                        speaker_model_version=speaker.model_version,
                        speaker_template_version=speaker.template_version,
                    ),
                ),
                "ok",
            )

    class TokenStream:
        async def __aenter__(self) -> TokenStream:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def __aiter__(self) -> AsyncIterator[str]:
            return _text_source("第一句。", "第二句。")

    class StandaloneLLM:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, **_kwargs: object) -> TokenStream:
            self.calls += 1
            return TokenStream()

    model = StandaloneLLM()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
        language_model=model,
    )

    fence = await agent.prepare_committed_turn("请回答")
    output = [
        token
        async for token in agent.stream(
            LanguageModelRequest("请回答", CancellationContext.capture(fence))
        )
    ]

    assert output == ["第一句。", "第二句。"]
    assert model.calls == 1
    await runtime.close()


def test_formal_speaker_authority_disables_legacy_session_enrollment() -> None:
    settings = SimpleNamespace(
        speaker_verify_enabled=True,
        speaker_authority_enabled=True,
    )

    assert not should_enable_legacy_speaker_verifier(settings, offline=False)
    assert should_enable_legacy_speaker_verifier(
        SimpleNamespace(
            speaker_verify_enabled=True,
            speaker_authority_enabled=False,
        ),
        offline=False,
    )
    assert not should_enable_legacy_speaker_verifier(settings, offline=True)


@pytest.mark.asyncio
async def test_stream_reply_uses_heard_history_and_phrase_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    runtime.orchestrator.context.commit_assistant_heard(
        "实际听到的旧回复",
        speaker_scope="public",
    )
    bind_owner_speaker(runtime)
    await runtime.on_turn_committed("当前问题")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    snapshot = await runtime.freeze_context_capsules_for_generation(
        runtime.fence,
        memory_capsule=MemoryCapsule(
            (
                MemoryCapsuleEntry(
                    item_id="claim-1",
                    kind="memory_claim",
                    content="已确认资料：他在杭州读过书。",
                    source_refs=("event-1",),
                    confidence=0.9,
                    sharing_scope="private",
                ),
            )
        ),
        persona_capsule=PersonaCapsule(),
    )
    assert snapshot is not None
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="【控制计划】只使用当前已确认上下文；不要复述未听到的旧回答。",
        grounded_items=(
            ResponseGroundedItem(
                kind="memory_claim",
                item_id="claim-1",
                content="已确认资料：他在杭州读过书。",
                use_as="fact",
                source_event_ids=("event-1",),
                confidence=0.9,
                sharing_scope="private",
            ),
        ),
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="旧问题")
    chat_ctx.add_message(role="assistant", content="未听到的完整旧回复")
    chat_ctx.add_message(role="user", content="当前问题")
    captured: dict[str, Any] = {}

    async def _stream() -> AsyncIterator[Any]:
        yield llm.ChatChunk(
            id="content",
            delta=llm.ChoiceDelta(content="可以，我先帮你看一下。"),
        )
        # A metadata-only chunk (the provider's usage report) carries no text.
        yield llm.ChatChunk(id="usage")

    async def fake_llm_node(
        safe_ctx: Any,
        tools: list[Any],
    ) -> AsyncIterator[Any]:
        captured.update(ctx=safe_ctx, tools=tools)
        return _stream()

    agent.language_model = ScriptedChatModel(fake_llm_node)
    output = [item async for item in agent.stream_reply(chat_ctx)]

    assert [
        message.text_content for message in captured["ctx"].items if message.role != "system"
    ] == [
        "旧问题",
        "实际听到的旧回复",
        "当前问题",
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].items if message.role == "system"
    )
    assert "【控制计划】" in system_text
    assert "claim-1" in system_text
    assert "杭州" in system_text
    assert "人格胶囊" not in system_text
    assert "经确认的人生记忆" not in system_text
    assert "可以，我先帮你看一下。" in output
    assert all(isinstance(item, str) for item in output)
    assert runtime.orchestrator.active_llm_task is None


def test_high_risk_tool_cannot_register_without_explicit_confirmation() -> None:
    runtime = DuplexRuntime.create(session_id="high-risk-tool-blocked")

    async def handler(
        _arguments: dict[str, Any],
        _cancel: asyncio.Event,
    ) -> str:
        return "should not run"

    with pytest.raises(PermissionError):
        runtime.orchestrator.task_manager.register(
            ToolSpec(
                name="send_message",
                description="发送消息",
                input_schema={"type": "object"},
                cancellable=True,
                idempotent=False,
                timeout_s=1,
                side_effect_policy="high_risk",
            ),
            handler,
        )


@pytest.mark.asyncio
async def test_response_plan_capsules_are_frozen_for_the_current_generation() -> None:
    runtime = DuplexRuntime.create(session_id="response-plan-context-snapshot")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    runtime._speaker_class = "owner"

    class Planner:
        task_names: list[str] = []

        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
            current = asyncio.current_task()
            assert current is not None
            self.task_names.append(current.get_name())
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    fence,
                    instructions="依据已核验资料回答。",
                    grounded_items=(
                        ResponseGroundedItem(
                            kind="memory_claim",
                            item_id="memory-1",
                            content="喜欢桂花。",
                            use_as="fact",
                            source_event_ids=("event-1",),
                            confidence=0.95,
                            sharing_scope="private",
                        ),
                        ResponseGroundedItem(
                            kind="persona_trait",
                            item_id="persona-1",
                            content="日常表达偏好短句。",
                            use_as="style",
                            source_event_ids=("event-2",),
                            confidence=0.9,
                            sharing_scope="private",
                        ),
                    ),
                ),
                reason="ok",
            )

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    first = await agent.prepare_turn(
        text="你还记得我喜欢什么吗？",
        speaker=SimpleNamespace(),
        input_modality="text",
    )
    snapshot = runtime.orchestrator.context_snapshots.current(runtime.session_id)

    assert runtime.orchestrator.context_version_for_fence(first) == snapshot.version
    assert snapshot.version > 0
    assert snapshot.memory_capsule.entries[0].content == "喜欢桂花。"
    assert snapshot.persona_capsule.prompt_fragment == "日常表达偏好短句。"
    assert snapshot.tool_permission is True
    assert runtime.orchestrator.task_manager.tasks == {}
    # The plan was asked for once, ahead of the commit, and the commit took that answer.
    assert Planner.task_names == [reply_warmup.EARLY_RESPONSE_PLAN_TASK_NAME]
    await runtime.close()


@pytest.mark.asyncio
async def test_snapshot_build_failure_keeps_call_on_safe_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="snapshot-build-fallback")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    await runtime.on_turn_committed("seed", input_modality="text")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    runtime._activate_pending_context_snapshot()
    manager = runtime.orchestrator.context_snapshots
    base_version = manager.current(runtime.session_id).version
    manager.max_snapshot_chars = manager.current(runtime.session_id).size_chars + 1

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
            return ResponsePlanFetch(
                _plan_for_fence(
                    fence,
                    instructions="使用过大的 grounding。",
                    grounded_items=(
                        ResponseGroundedItem(
                            kind="memory_claim",
                            item_id="large",
                            content="这段资料会超过快照大小限制。",
                            use_as="fact",
                            source_event_ids=("event-large",),
                            confidence=1.0,
                            sharing_scope="private",
                        ),
                    ),
                ),
                "ok",
            )

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    fence = await agent.prepare_turn(
        text="当前问题",
        speaker=SimpleNamespace(classification="owner"),
        input_modality="text",
    )
    plan = agent.response_plans.get(fence)

    assert plan_is_local_safe(plan)
    assert runtime.orchestrator.context_version_for_fence(fence) == base_version
    await runtime.close()


@pytest.mark.asyncio
async def test_a_plan_the_planner_cannot_match_to_the_epoch_answers_with_the_fallback_plan() -> None:
    """2026-10-01: every device session has session_epoch >= 1, but the planner contract echoes only
    turn/generation/tool_epoch, so the client reports ``fence_mismatch`` for a plan that is for this very
    turn. That raised StopResponse: when the result was accepted in time (5 of 7 first questions in a quiet
    room) the question got no answer and the conversation was closed by turn_prepare_timeout. The turn
    was never stale (the runtime-fence check decides that), so it answers with the local safe plan."""

    runtime = DuplexRuntime.create(session_id="planner-epoch-mismatch")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)

    class Planner:
        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            return ResponsePlanFetch(None, "fence_mismatch")

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    fence = await agent.prepare_turn(
        text="你好呀，你叫什么名字？",
        speaker=SimpleNamespace(classification="owner"),
        input_modality="text",
    )

    assert runtime.fence.matches(fence)
    assert plan_is_local_safe(agent.response_plans.get(fence))
    await runtime.close()


@pytest.mark.asyncio
async def test_a_turn_that_went_stale_while_the_plan_was_fetched_is_still_dropped() -> None:
    """The fallback above must not weaken the real staleness check: a new turn during the fetch drops it."""

    runtime = DuplexRuntime.create(session_id="planner-stale-turn")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)

    class Planner:
        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            await runtime.on_turn_committed("用户已经说了下一句", input_modality="text")
            return ResponsePlanFetch(None, "fence_mismatch")

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    with pytest.raises(StopResponse):
        await agent.prepare_turn(
            text="第一句",
            speaker=SimpleNamespace(classification="owner"),
            input_modality="text",
        )
    await runtime.close()


@pytest.mark.asyncio
async def test_agent_adds_only_the_canonical_response_plan_system_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("帮我安排一个十五分钟的英语口语训练")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="只回答当前训练安排。",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="帮我安排一个十五分钟的英语口语训练")
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        safe_ctx: Any,
        _tools: list[Any],
    ) -> AsyncIterator[Any]:
        captured["ctx"] = safe_ctx
        return _text_source("嗯，好，我先理一下。")

    agent.language_model = ScriptedChatModel(fake_llm_node)

    assert [item async for item in agent.stream_reply(chat_ctx)]
    system_messages = [
        message for message in captured["ctx"].items if message.role == "system"
    ]
    assert len(system_messages) == 1
    assert "只回答当前训练安排" in system_messages[0].text_content
    assert "四到十二个字" in system_messages[0].text_content
    assert "short" not in system_messages[0].text_content.lower()
    assert [message.text_content for message in chat_ctx.items] == [
        "帮我安排一个十五分钟的英语口语训练"
    ]


@pytest.mark.asyncio
async def test_stream_reply_drops_token_after_fence_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("问题")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="仅回答当前问题。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield llm.ChatChunk(id="empty")
        await runtime.orchestrator.bump_tool_epoch_on_condition_change()
        yield "这段旧回答不应出现。"

    agent.language_model = ScriptedChatModel(fake_llm_node)
    assert [item async for item in agent.stream_reply(llm.ChatContext.empty())] == []


@pytest.mark.asyncio
async def test_non_preemptive_turn_commits_fence_before_first_llm_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False, shadow_low_sensitivity_persona=False)
    await runtime.orchestrator.ready()
    agent = ReplyPipeline(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "你好"

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield "你好，有什么可以帮你的吗？"

    agent.language_model = ScriptedChatModel(fake_llm_node)

    await commit_media_turn(agent, Message())
    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert runtime.fence.turn_id == 1
    assert runtime.fence.generation_id == 1
    assert "你好，有什么可以帮你的吗？" in output
    assert runtime.orchestrator.fence_gate is not None
    assert runtime.orchestrator.fence_gate.dropped_count == 0


@pytest.mark.asyncio
async def test_committed_private_transcript_never_enters_agent_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    private_text = "我的私人保险资料只应进入加密档案"

    class Message:
        def text_content(self) -> str:
            return private_text

    caplog.set_level(logging.INFO, logger="services.agent.src.reply_pipeline")
    await commit_media_turn(agent, Message())

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "services.agent.src.reply_pipeline"
    ]
    assert private_text not in "\n".join(messages)
    assert any(f"text_len={len(private_text)}" in message for message in messages)


@pytest.mark.asyncio
async def test_reply_budget_stops_after_three_spoken_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("介绍一下")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="简洁介绍。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in (
            "第一句。",
            "第二句。",
            "第三句。",
            "第四句。",
            "第五句。",
            "第六句。",
            "第七句。",
            "第八句。",
            "第九句。",
        ):
            yield sentence

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == [
        "第一句。",
        "第二句。",
        "第三句。",
        "第四句。",
        "第五句。",
        "第六句。",
        "第七句。",
        "第八句。",
    ]


@pytest.mark.asyncio
async def test_controlled_turn_reply_budget_stops_after_three_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed("介绍一下")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="简洁介绍。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == ["第一句。", "第二句。", "第三句。"]


@pytest.mark.asyncio
async def test_controlled_turn_keeps_short_budget_for_supportive_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    runtime.speech_plan = SpeechPlan(
        voice_emotion="neutral",
        rate=1.0,
        delivery_mode="supportive",
    )
    await runtime.on_turn_committed("介绍一下")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="简洁介绍。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == ["第一句。", "第二句。", "第三句。"]


@pytest.mark.asyncio
async def test_controlled_turn_speaks_the_complete_fixed_crisis_reply() -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed("我想自杀")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="直接播放固定危机支持。",
        direct_text=CRISIS_SELF_SCRIPT.display,
    ))

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == [CRISIS_SELF_SCRIPT]


@pytest.mark.asyncio
async def test_fixed_safety_script_outranks_a_pending_realtime_resume() -> None:
    runtime = DuplexRuntime.create(session_id="fixed-safety-outranks-realtime")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    searched: list[str] = []

    class SearchResolver:
        async def resolve(self, *, query: str) -> str | None:
            searched.append(query)
            return None if len(searched) == 1 else "南京今天多云，最高气温三十二度。"

    agent._realtime_search_resolver = SearchResolver()
    agent._realtime_search_model = "realtime-search-model"
    agent._llm_model = "chat-model"
    _ = [item async for item in agent.stream_reply(chat_ctx)]
    runtime.orchestrator.context.commit_assistant_heard("我不知道。", speaker_scope="public")
    chat_ctx.add_message(role="assistant", content="我不知道。")
    await runtime.on_turn_committed("人呢？")
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="直接播放固定危机支持。",
        direct_text=CRISIS_SELF_SCRIPT.display,
        speaker_class="uncertain",
    ))
    chat_ctx.add_message(role="user", content="人呢？")

    output = [item async for item in agent.stream_reply(chat_ctx)]

    assert output == [CRISIS_SELF_SCRIPT]
    assert len(searched) == 1
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["llm_model"] == "chat-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", ("给我一个方案", "给我一份计划", "列出步骤"))
async def test_controlled_turn_keeps_short_budget_for_ordinary_planning_terms(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed(user_text)
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="简洁回答。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == ["第一句。", "第二句。", "第三句。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", ("给我详细方案", "请朗读这段", "请继续"))
async def test_controlled_turn_allows_explicit_longform_requests(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed(user_text)
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="按用户要求展开。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == ["第一句。", "第二句。", "第三句。", "第四句。"]


@pytest.mark.asyncio
async def test_reply_budget_truncates_an_oversized_first_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("详细介绍")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="详细介绍。",
    ))

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield "这" * 140 + "。"

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output
    assert sum(ch.isalnum() for text in output for ch in text) <= pipeline_mod.MAX_VOICE_REPLY_CHARS


class _Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, name: str, handler: Any) -> None:
        self.handlers.setdefault(name, []).append(handler)

    def off(self, name: str, handler: Any) -> None:
        self.handlers[name].remove(handler)

    def emit(self, name: str, event: Any) -> None:
        for handler in tuple(self.handlers.get(name, ())):
            handler(event)


class _FakePool:
    def __init__(self) -> None:
        self.discarded: list[Any] = []

    async def warm(self) -> None:
        raise RuntimeError("warm unavailable")

    async def discard_active_connection(self, fence: Any) -> None:
        self.discarded.append(fence)


class _FakeTTS:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.bound: list[Any] = []
        self.alignment_callback: Any | None = None
        self.closed = False
        self.plans: list[dict[str, Any]] = []

    def bind_fence(self, fence: Any) -> None:
        self.bound.append(fence)

    def apply_speech_plan(self, **kwargs: Any) -> None:
        self.plans.append(kwargs)

    def set_alignment_callback(self, callback: Any) -> None:
        self.alignment_callback = callback

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_agent_alignment_callback_rejects_a_stale_runtime_fence() -> None:
    fake_tts = _FakeTTS()
    runtime = DuplexRuntime.create(tts=fake_tts)  # type: ignore[arg-type]
    ReplyPipeline(instructions="test", runtime=runtime)
    callback = fake_tts.alignment_callback
    assert callback is not None

    stale = runtime.fence
    runtime.heard_tracker.expect_utterance(stale)
    callback(stale, "old-task", "started")
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    callback(stale, "old-task", "degraded")

    assert runtime.heard_tracker.alignment_degraded is False


class _FakeAudio(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pause_count = 0
        self.resume_count = 0

    def pause(self) -> None:
        self.pause_count += 1

    def resume(self) -> None:
        self.resume_count += 1


class _RecordingInterruption(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__(min_words=0)
        self.history: list[int] = []

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "min_words":
            self.history.append(int(value))
        super().__setitem__(key, value)


class _FakeSession(_Emitter):
    last: _FakeSession | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.output = SimpleNamespace(audio=_FakeAudio())
        self.history = llm.ChatContext.empty()
        self.history.add_message(role="assistant", content="实际听到")
        self.started: tuple[Any, Any, Any] | None = None
        self.generated: list[str] = []
        self.interrupt_count = 0
        self.clear_user_turn_count = 0
        self.said: list[str] = []
        self.options = SimpleNamespace(interruption=_RecordingInterruption())
        _FakeSession.last = self

    async def start(self, *, room: Any, agent: Any, room_options: Any) -> None:
        self.started = (room, agent, room_options)

    def interrupt(self, *, force: bool = False) -> asyncio.Future[None]:
        self.interrupt_count += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    def say(self, text: str, **_kwargs: Any) -> Any:
        self.said.append(text)

        async def _wait_for_playout() -> None:
            return None

        return SimpleNamespace(wait_for_playout=_wait_for_playout)

    def clear_user_turn(self) -> None:
        self.clear_user_turn_count += 1

    async def generate_reply(self, *, instructions: str) -> None:
        self.generated.append(instructions)
