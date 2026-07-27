from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from livekit.agents import FlushSentinel, StopResponse, llm
from livekit.agents.types import TimedString
from services.agent.src import agent as agent_mod
from services.agent.src.agent import (
    DuplexVoiceAgent,
    apply_miniprogram_session_audio_policy,
    should_enable_legacy_speaker_verifier,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict
from services.agent.src.response_planner_client import (
    ResponseGroundedItem,
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.common.miniprogram_gateway_ticket import MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


async def _text_source(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


def _plan_for_fence(
    fence: GenerationFence,
    *,
    instructions: str,
    direct_text: str | None = None,
    grounded_items: tuple[ResponseGroundedItem, ...] = (),
    speaker_class: str = "owner",
) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions=instructions,
        direct_text=direct_text,
        epistemic_status="fact",
        epistemic_reason_codes=("grounded_manifest",),
        grounded_items=grounded_items,
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion",
            profile_id="warm_companion",
            model="seed-tts-2.0",
        ),
        provenance=ResponseProvenance(
            planner_policy_version="digital-self-response-planner-v2",
            interaction_mode="companion",
            mode_policy_version="test-policy",
            digital_self_version_id=None,
            manifest_sha256=None,
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=None,
            relationship_profile_version=None,
            speaker_class=speaker_class,  # type: ignore[arg-type]
            speaker_reason_code=("owner_match" if speaker_class == "owner" else "owner_mismatch"),
            speaker_profile_id=("profile-owner-001" if speaker_class == "owner" else None),
            speaker_model_version="campplus-test",
            speaker_template_version=1,
            source_refs=(),
            epistemic_status="fact",
            epistemic_reason_codes=("grounded_manifest",),
            disclosures=(),
        ),
    )


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


def test_miniprogram_audio_policy_disables_only_the_gateway_warmup() -> None:
    web_kwargs: dict[str, Any] = {}
    miniprogram_kwargs: dict[str, Any] = {}

    assert not apply_miniprogram_session_audio_policy(web_kwargs, "")
    assert "aec_warmup_duration" not in web_kwargs
    assert not apply_miniprogram_session_audio_policy(web_kwargs, "memoria.miniprogram.aec.v2")
    assert apply_miniprogram_session_audio_policy(
        miniprogram_kwargs,
        MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    )
    assert miniprogram_kwargs["aec_warmup_duration"] is None
    assert agent_mod.AgentSession(**web_kwargs)._aec_warmup_remaining == 3.0
    assert agent_mod.AgentSession(**miniprogram_kwargs)._aec_warmup_remaining == 0.0


@pytest.mark.asyncio
async def test_agent_llm_node_uses_heard_history_and_phrase_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("当前问题")
    runtime.orchestrator.context.commit_assistant_heard("实际听到的旧回复")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._current_speaker_class = "owner"
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
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
    )
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
        yield llm.ChatChunk(
            id="usage",
            usage=llm.CompletionUsage(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
        )

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        settings: Any,
    ) -> AsyncIterator[Any]:
        captured.update(ctx=safe_ctx, tools=tools, settings=settings)
        return _stream()

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))
    output = [item async for item in agent.llm_node(chat_ctx, [], None)]

    assert [
        message.text_content for message in captured["ctx"].messages() if message.role != "system"
    ] == [
        "旧问题",
        "实际听到的旧回复",
        "当前问题",
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "【控制计划】" in system_text
    assert "claim-1" in system_text
    assert "杭州" in system_text
    assert "人格胶囊" not in system_text
    assert "经确认的人生记忆" not in system_text
    assert "可以，我先帮你看一下。" in output
    assert not any(isinstance(item, FlushSentinel) for item in output)
    assert runtime.orchestrator.active_llm_task is None


@pytest.mark.asyncio
async def test_agent_fetches_response_plan_once_per_committed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()

    class Message:
        def __init__(self, text: str) -> None:
            self._text = text

        def text_content(self) -> str:
            return self._text

    fetch_calls: list[dict[str, object]] = []

    async def classify(_pcm: bytes, _sample_rate: int) -> Any:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fetch_calls.append(dict(kwargs))
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    runtime.fence,
                    instructions="【控制计划】只使用当前已确认上下文。",
                ),
                reason="ok",
            )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        agent_mod.Agent.default, "llm_node", staticmethod(lambda *_args: _text_source("好。"))
    )  # type: ignore[arg-type]

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("当前问题"))

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] == runtime.session_id
    assert fetch_calls[0]["query"] == "当前问题"
    assert fetch_calls[0]["fence"] == runtime.fence
    assert runtime.response_provenance_for(runtime.fence) is None
    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]
    assert runtime.response_provenance_for(runtime.fence) is not None


@pytest.mark.asyncio
async def test_companion_voice_turn_reaches_llm_when_guest_filter_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="companion-voice-turn")
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=False,
            owner_evidence=False,
            tools=False,
            voice_profile=False,
            shadow_low_sensitivity_persona=False,
        )
    )
    runtime.set_reject_non_owner_voice(False)
    runtime.set_target_speaker_focus(True)
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_male_yangguangqingnian_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )

    class Message:
        def text_content(self) -> str:
            return "你好"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=0.42,
            quality_score=0.95,
            reason_code="shadow_guest_candidate",
            model_version="campplus-test",
            template_version=1,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    runtime.fence,
                    instructions="只回答当前问题。",
                    speaker_class="uncertain",
                ),
                reason="ok",
            )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
        llm_provider="qwen",
        llm_model="qwen-plus",
        tts_provider="doubao",
        tts_model="seed-tts-2.0",
    )
    monkeypatch.setattr(
        agent_mod.Agent.default,
        "llm_node",
        staticmethod(lambda *_args: _text_source("你好，我在。")),
    )  # type: ignore[arg-type]

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())
    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output
    assert runtime.generation_voice_for(runtime.fence) is not None
    assert runtime.response_provenance_for(runtime.fence) is not None


@pytest.mark.asyncio
async def test_agent_drops_response_plan_when_fence_changes_during_fetch() -> None:
    runtime = DuplexRuntime.create()

    class Message:
        def text_content(self) -> str:
            return "会过期的问题"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    fetch_calls = 0

    class ResponsePlannerStub:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            nonlocal fetch_calls
            fetch_calls += 1
            requested_fence = kwargs["fence"]
            assert isinstance(requested_fence, GenerationFence)
            await runtime.orchestrator.bump_tool_epoch_on_condition_change()
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    requested_fence,
                    instructions="这份计划已经过期。",
                ),
                reason="ok",
            )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    assert fetch_calls == 1
    assert agent._response_plan_by_fence == {}


@pytest.mark.asyncio
async def test_planner_failure_fallback_is_current_turn_only_and_disables_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="fallback-current-only")
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=False,
            shadow_low_sensitivity_persona=False,
        )
    )

    class Message:
        def text_content(self) -> str:
            return "只回答现在这个问题"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            return ResponsePlanFetch(plan=None, reason="http_503")

    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "安全回答。"

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="旧私人问题：保险号码是多少？")
    chat_ctx.add_message(role="assistant", content="旧私人回答：号码是 123456。")
    chat_ctx.add_message(role="user", content="只回答现在这个问题")
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(chat_ctx, Message())
    assert [item async for item in agent.llm_node(chat_ctx, ["private-tool"], None)] == [
        "安全回答。"
    ]

    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].messages()
        if message.role != "system"
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert conversation == [("user", "只回答现在这个问题")]
    assert "不得读取、引用或推断历史对话" in system_text
    assert "123456" not in system_text
    assert captured["tools"] == []
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["source_refs"] == []


@pytest.mark.asyncio
async def test_agent_uses_direct_response_text_without_calling_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("直接回答")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="【控制计划】",
        direct_text="可以，直接说这一句。",
    )
    called = False

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        nonlocal called
        called = True
        yield "不应触发"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert called is False
    assert "".join(str(item) for item in output) == "可以，直接说这一句。"
    assert runtime.response_provenance_for(runtime.fence) is not None


@pytest.mark.asyncio
async def test_cascade_response_fails_closed_without_generation_voice_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("必须带声音来源")

    class TTSWithoutBoundVoice:
        def bind_fence(self, _fence: GenerationFence) -> None:
            pass

    runtime.tts = TTSWithoutBoundVoice()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="仅生成有完整审计来源的回答。",
    )
    called = False

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        nonlocal called
        called = True
        yield "不应播放"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output == []
    assert called is False
    assert runtime.generation_voice_for(runtime.fence) is None
    assert runtime.response_provenance_for(runtime.fence) is None


@pytest.mark.asyncio
async def test_legacy_turn_stops_before_planning_when_generation_voice_cannot_bind() -> None:
    runtime = DuplexRuntime.create(session_id="legacy-bind-failure")
    runtime.set_mode_policy(
        ModePolicy(
            mode="legacy",
            policy_version="s9-v1",
            companion_style_id=None,
            style_version=None,
            references=(
                ("fallback_voice_profile_id", "bright_peer"),
                ("fallback_voice_provider", "volcengine_doubao"),
                ("fallback_voice_model", "seed-tts-2.0"),
                ("fallback_voice_resource_id", "seed-tts-2.0"),
                ("legacy_voice_allowed", False),
            ),
            capabilities=(("conversation", True),),
            companion_style=None,
        )
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="wrong-designed-profile",
        current_model="seed-tts-2.0",
        current_voice="wrong-speaker",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    class PlannerStub:
        called = False

        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            self.called = True
            raise AssertionError("voice binding must fail before response planning")

    class Message:
        def text_content(self) -> str:
            return "请继续说。"

    planner = PlannerStub()
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=planner,  # type: ignore[arg-type]
    )

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    assert planner.called is False
    assert runtime.generation_voice_for(runtime.fence) is None


def test_self_preview_selected_fallback_binds_exact_generation_voice_snapshot() -> None:
    runtime = DuplexRuntime.create(session_id="self-preview-selected-fallback")
    runtime.set_mode_policy(
        ModePolicy(
            mode="self_preview",
            policy_version="s8-v1",
            companion_style_id=None,
            style_version=None,
            references=tuple(
                sorted(
                    {
                        "fallback_voice_profile_id": "bright_peer",
                        "fallback_voice_provider": "volcengine_doubao",
                        "fallback_voice_model": "seed-tts-2.0",
                        "fallback_voice_resource_id": "seed-tts-2.0",
                    }.items()
                )
            ),
            capabilities=(),
            companion_style=None,
        )
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="bright_peer",
        current_model="seed-tts-2.0",
        current_voice="zh_female_tianmeitaozi_uranus_bigtts",
        current_voice_kind="designed",
    )
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    assert agent._bind_current_tts_voice(runtime.fence)
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert snapshot is not None
    assert snapshot.profile_id == "bright_peer"
    assert snapshot.resource_id == "seed-tts-2.0"


@pytest.mark.asyncio
async def test_agent_adds_only_the_canonical_response_plan_system_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("帮我安排一个十五分钟的英语口语训练")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="只回答当前训练安排。",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="帮我安排一个十五分钟的英语口语训练")
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        _tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        captured["ctx"] = safe_ctx
        return _text_source("嗯，好，我先理一下。")

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(chat_ctx, [], None)]
    system_messages = [
        message for message in captured["ctx"].messages() if message.role == "system"
    ]
    assert len(system_messages) == 1
    assert "只回答当前训练安排" in system_messages[0].text_content
    assert "short" not in system_messages[0].text_content.lower()
    assert [message.text_content for message in chat_ctx.messages()] == [
        "帮我安排一个十五分钟的英语口语训练"
    ]


@pytest.mark.asyncio
async def test_agent_llm_node_drops_token_after_fence_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("问题")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="仅回答当前问题。",
    )

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield llm.ChatChunk(id="empty")
        yield "这段旧回答不应出现。"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))
    stream = agent.llm_node(llm.ChatContext.empty(), [], None)
    first = await anext(stream)
    assert isinstance(first, llm.ChatChunk)
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    assert [item async for item in stream] == []


@pytest.mark.asyncio
async def test_non_preemptive_turn_commits_fence_before_first_llm_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=False,
            owner_evidence=False,
            tools=False,
            voice_profile=False,
            shadow_low_sensitivity_persona=False,
        )
    )
    await runtime.orchestrator.ready()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "你好"

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield "你好，有什么可以帮你的吗？"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())
    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

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
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    private_text = "我的私人保险资料只应进入加密档案"

    class Message:
        def text_content(self) -> str:
            return private_text

    caplog.set_level(logging.INFO, logger="services.agent.src.agent")
    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "services.agent.src.agent"
    ]
    assert private_text not in "\n".join(messages)
    assert any(f"text_len={len(private_text)}" in message for message in messages)


@pytest.mark.asyncio
async def test_one_physical_speech_epoch_cannot_commit_more_than_once() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def __init__(self, text: str, *, anchored: bool) -> None:
            self._text = text
            self.metrics: dict[str, object] = (
                {"started_speaking_at": 1.0, "stopped_speaking_at": 2.0} if anchored else {}
            )

        def text_content(self) -> str:
            return self._text

    runtime.on_user_voice_started()
    await agent.on_user_turn_completed(
        llm.ChatContext.empty(),
        Message("第一句话", anchored=True),
    )

    runtime.on_user_voice_started()
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(
            llm.ChatContext.empty(),
            Message("迟到片段", anchored=False),
        )

    assert runtime.fence.turn_id == 1
    assert [turn.content for turn in runtime.orchestrator.context.turns] == ["第一句话"]

    runtime.on_user_voice_started()
    await agent.on_user_turn_completed(
        llm.ChatContext.empty(),
        Message("真正的新讲话", anchored=True),
    )
    await asyncio.sleep(0)
    assert runtime.fence.turn_id == 2
    assert [
        event["text"]
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "user"
        and event.get("final") is True
    ] == ["第一句话", "真正的新讲话"]


@pytest.mark.asyncio
async def test_post_playback_backchannel_is_ignored_as_echo_tail() -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("介绍一下")
    runtime.update_pending_assistant_text("你好。")
    await runtime.on_playback_started()
    await runtime.on_assistant_reply_completed("你好。")
    fence = runtime.fence
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "是。"

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    assert runtime.fence.matches(fence)
    assert (
        runtime.orchestrator.metrics.get("guarded_user_input_total", {"reason": "backchannel"}) == 1
    )


@pytest.mark.asyncio
async def test_post_playback_english_assistant_echo_is_ignored() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("教我英语")
    runtime.update_pending_assistant_text("It's a good idea.")
    await runtime.on_playback_started()
    await runtime.on_assistant_reply_completed("It's a good idea.")
    fence = runtime.fence
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "it's"

    runtime.on_user_voice_started()
    runtime.observe_user_transcript("it's", final=True)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    assert runtime.fence.matches(fence)
    assert (
        runtime.orchestrator.metrics.get("guarded_user_input_total", {"reason": "assistant_echo"})
        == 1
    )


@pytest.mark.asyncio
async def test_delayed_post_playback_assistant_echo_never_publishes_as_user() -> None:
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("妈妈，你看")
    reply = "你好！很高兴见到你。今天过得怎么样？"
    runtime.update_pending_assistant_text(reply)
    await runtime.on_playback_started()
    await runtime.on_assistant_reply_completed(reply)
    runtime._last_playback_completed_ns = time.monotonic_ns() - 6_500_000_000

    runtime.on_user_voice_started()
    assert runtime.observe_user_transcript(reply, final=True) == "accept"
    message = llm.ChatMessage(role="user", content=[reply])
    message.metrics["started_speaking_at"] = 1.0
    message.metrics["stopped_speaking_at"] = 2.0
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), message)
    await asyncio.sleep(0)

    assert not any(
        event.get("type") == "transcript_delta"
        and event.get("speaker") == "user"
        and event.get("final") is True
        for event in published
    )
    assert (
        runtime.orchestrator.metrics.get("guarded_user_input_total", {"reason": "assistant_echo"})
        == 1
    )


@pytest.mark.asyncio
async def test_playback_echo_final_is_removed_before_the_next_real_turn_is_committed() -> None:
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("你好呀")
    runtime.update_pending_assistant_text("你好呀！很高兴见到你。")
    await runtime.on_playback_started()

    echo = runtime.observe_user_transcript("你好呀！ 我告现你。", final=True)
    await runtime.on_assistant_reply_completed("你好呀！很高兴见到你。")
    runtime.on_user_voice_started()
    real = runtime.observe_user_transcript("介绍一下南京。", final=True)

    message = llm.ChatMessage(
        role="user",
        content=["你好呀！ 我告现你。 介绍一下南京。"],
    )
    message.metrics["started_speaking_at"] = 1.0
    message.metrics["stopped_speaking_at"] = 2.0
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), message)
    await asyncio.sleep(0)

    user_finals = [
        event["text"]
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "user"
        and event.get("final") is True
    ]
    assert echo == "ignore"
    assert real == "accept"
    assert message.text_content == "介绍一下南京。"
    assert runtime.orchestrator.context.turns[-1].content == "介绍一下南京。"
    assert user_finals == ["介绍一下南京。"]


@pytest.mark.asyncio
async def test_queued_turn_callbacks_consume_only_their_own_speech_epoch() -> None:
    """Later VAD epochs may arrive while LiveKit serializes completed-turn hooks."""

    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    runtime.update_pending_assistant_text("你好呀！很高兴见到你。")
    await runtime.on_playback_started()

    assert runtime.observe_user_transcript("你好呀！ 我告现你。", final=True) == "ignore"
    runtime._was_speaking = False

    raw_turns = (
        "你好呀！ 我告现你。 介绍一下南京。",
        "南京有哪些景点？",
        "夫子庙晚上几点关门？",
    )
    canonical_turns = (
        "介绍一下南京。",
        "南京有哪些景点？",
        "夫子庙晚上几点关门？",
    )
    for text in canonical_turns:
        runtime.on_user_voice_started()
        assert runtime.observe_user_transcript(text, final=True) == "accept"

    class Message:
        def __init__(self, text: str) -> None:
            self.content = [text]
            self.metrics = {"started_speaking_at": 1.0, "stopped_speaking_at": 2.0}

        def text_content(self) -> str:
            return "".join(self.content)

    messages = [Message(text) for text in raw_turns]
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    for message in messages:
        await agent.on_user_turn_completed(llm.ChatContext.empty(), message)
    await asyncio.sleep(0)

    assert [message.text_content() for message in messages] == list(canonical_turns)
    assert [turn.content for turn in runtime.orchestrator.context.turns] == list(canonical_turns)
    assert [
        event["text"]
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "user"
        and event.get("final") is True
    ] == list(canonical_turns)


@pytest.mark.asyncio
async def test_queued_control_turn_cannot_clear_the_following_speech_epoch() -> None:
    cleared: list[str] = []
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_user_turn_clearer(lambda: cleared.append("clear"))
    await runtime.orchestrator.ready()

    runtime.on_user_voice_started()
    assert runtime.observe_user_transcript("等一下", final=True) == "accept"
    runtime.on_user_voice_started()
    assert runtime.observe_user_transcript("介绍一下南京。", final=True) == "accept"

    class Message:
        def __init__(self, text: str) -> None:
            self.content = [text]
            self.metrics = {"started_speaking_at": 1.0, "stopped_speaking_at": 2.0}

        def text_content(self) -> str:
            return "".join(self.content)

    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("等一下"))
    await agent.on_user_turn_completed(
        llm.ChatContext.empty(),
        Message("介绍一下南京。"),
    )

    assert cleared == []
    assert [turn.content for turn in runtime.orchestrator.context.turns] == ["介绍一下南京。"]


@pytest.mark.asyncio
async def test_agent_semantic_review_suppresses_polluted_sticky_final() -> None:
    resolved: list[tuple[str, str, str]] = []

    async def _resolve(
        final_text: str,
        sticky_text: str,
        assistant_text: str,
    ) -> InterruptSemanticVerdict:
        resolved.append((final_text, sticky_text, assistant_text))
        return InterruptSemanticVerdict.CONTROL_ONLY

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_interrupt_semantic_resolver(_resolve)
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("根据提供的数据和指示来协助。")
    runtime.on_user_voice_started()
    runtime.observe_user_transcript("停一下，你叫什么名字？", final=False)

    class Message:
        def __init__(self, text: str) -> None:
            self.content = [text]
            self.metrics = {
                "started_speaking_at": 1.0,
                "stopped_speaking_at": 2.0,
            }

        def text_content(self) -> str:
            return "".join(self.content)

    final_text = "份停听一下能是据提供的数据和指示来协助。"
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message(final_text))

    assert resolved == [
        (final_text, "停一下，你叫什么名字", "根据提供的数据和指示来协助。")
    ]
    assert runtime.orchestrator.context.turns == []
    await runtime.close()


@pytest.mark.asyncio
async def test_playback_echo_interim_is_removed_from_a_later_cumulative_final() -> None:
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("你好呀")
    runtime.update_pending_assistant_text("你好呀！很高兴见到你。")
    await runtime.on_playback_started()

    echo = runtime.observe_user_transcript("你好呀！ 我告现你。", final=False)
    await runtime.on_assistant_reply_completed("你好呀！很高兴见到你。")
    runtime.on_user_voice_started()
    combined = runtime.observe_user_transcript(
        "你好呀！ 我告现你。 介绍一下南京。",
        final=True,
    )

    message = llm.ChatMessage(
        role="user",
        content=["你好呀！ 我告现你。 介绍一下南京。"],
    )
    message.metrics["started_speaking_at"] = 1.0
    message.metrics["stopped_speaking_at"] = 2.0
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), message)
    await asyncio.sleep(0)

    user_finals = [
        event["text"]
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "user"
        and event.get("final") is True
    ]
    assert echo == "wait"
    assert combined == "accept"
    assert message.text_content == "介绍一下南京。"
    assert runtime.orchestrator.context.turns[-1].content == "介绍一下南京。"
    assert user_finals == ["介绍一下南京。"]


@pytest.mark.asyncio
async def test_short_playback_prefix_does_not_remove_a_matching_real_question() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("介绍南京")
    runtime.update_pending_assistant_text("南京不是一座只有历史的城市。")
    await runtime.on_playback_started()

    assert runtime.observe_user_transcript("南京", final=False) == "wait"
    await runtime.on_assistant_reply_completed("南京不是一座只有历史的城市。")
    runtime.on_user_voice_started()
    assert runtime.observe_user_transcript("南京有哪些景点？", final=True) == "accept"

    assert runtime.consume_canonical_user_turn("南京有哪些景点？") == "南京有哪些景点？"


@pytest.mark.asyncio
async def test_production_echo_trace_is_quarantined_while_assistant_is_speaking() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("你充当我的英语培训师")
    runtime.update_pending_assistant_text(
        "好的，我们先说一句简单的英语，比如 Good morning, how are you?"
    )
    await runtime.on_playback_started()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    fence = runtime.fence

    class Message:
        def __init__(self, text: str) -> None:
            self._text = text

        def text_content(self) -> str:
            return self._text

    for text in ("그.", "佢。", "啊！", "Good morning."):
        runtime.on_user_voice_started(now_ns=1_000_000_000)
        runtime.observe_user_transcript(text, final=True, now_ns=1_300_000_000)
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(llm.ChatContext.empty(), Message(text))

    assert runtime.fence.matches(fence)
    assert [turn.content for turn in runtime.orchestrator.context.turns] == ["你充当我的英语培训师"]
    assert (
        runtime.orchestrator.metrics.get(
            "guarded_user_input_total", {"reason": "non_target_language"}
        )
        == 2
    )
    assert (
        runtime.orchestrator.metrics.get("guarded_user_input_total", {"reason": "backchannel"}) == 1
    )
    assert (
        runtime.orchestrator.metrics.get("guarded_user_input_total", {"reason": "assistant_echo"})
        == 1
    )


@pytest.mark.asyncio
async def test_explicit_language_switch_allows_requested_script() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def __init__(self, text: str) -> None:
            self._text = text

        def text_content(self) -> str:
            return self._text

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("请教我韩语"))
    runtime.update_pending_assistant_text("可以，我们开始。")
    await runtime.on_playback_started()
    runtime.on_user_voice_started(now_ns=1_000_000_000)
    decision = runtime.observe_user_transcript("그.", final=True, now_ns=1_300_000_000)

    assert decision == "accept"
    await runtime.on_real_interrupt(cause="accepted_test_input")
    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("그."))
    assert runtime.orchestrator.context.turns[-1].content == "그."


@pytest.mark.asyncio
async def test_reply_budget_stops_after_three_spoken_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("介绍一下")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="简洁介绍。",
    )

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

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

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
async def test_reply_budget_truncates_an_oversized_first_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("详细介绍")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="详细介绍。",
    )

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield "这" * 140 + "。"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output
    assert sum(ch.isalnum() for text in output for ch in text) <= agent_mod.MAX_VOICE_REPLY_CHARS


@pytest.mark.asyncio
async def test_agent_tts_and_transcription_nodes_gate_and_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("问题")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def fake_tts_node(
        _agent: Any,
        text: AsyncIterator[str],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        async def frames() -> AsyncIterator[Any]:
            async for _ in text:
                yield SimpleNamespace(data=b"")
                yield SimpleNamespace(data=b"\x01\x02")

        return frames()

    async def fake_transcription_node(
        _agent: Any,
        _text: AsyncIterator[Any],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        yield TimedString("你", start_time=0.0, end_time=0.1)
        yield SimpleNamespace(text="坏", start_time=object(), end_time=object())
        yield "普通"

    monkeypatch.setattr(agent_mod.Agent.default, "tts_node", staticmethod(fake_tts_node))
    monkeypatch.setattr(
        agent_mod.Agent.default,
        "transcription_node",
        staticmethod(fake_transcription_node),
    )
    frames = [frame async for frame in agent.tts_node(_text_source("回答。"), None)]
    assert runtime.heard_tracker.observe_alignment(runtime.fence, "current-task", "started") is True
    transcript = [delta async for delta in agent.transcription_node(_text_source("ignored"), None)]

    assert len(frames) == 2
    assert runtime.orchestrator.published_audio_generations == [runtime.fence.generation_id]
    assert runtime.heard_tracker.full_text == "回答。"
    assert [word.text for word in runtime.heard_tracker.words] == ["你"]
    assert len(transcript) == 3
    assert runtime.orchestrator.active_tts_task is None


@pytest.mark.asyncio
async def test_agent_omits_degraded_timed_suffix_from_livekit_heard_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("问题")
    runtime.heard_tracker.expect_utterance(runtime.fence)
    runtime.heard_tracker.observe_alignment(runtime.fence, "task", "started")
    runtime.heard_tracker.observe_alignment(runtime.fence, "task", "degraded")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def fake_transcription_node(
        _agent: Any,
        _text: AsyncIterator[Any],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        yield TimedString("伪精确后缀", start_time=0.0, end_time=0.4)

    monkeypatch.setattr(
        agent_mod.Agent.default,
        "transcription_node",
        staticmethod(fake_transcription_node),
    )

    transcript = [delta async for delta in agent.transcription_node(_text_source("ignored"), None)]

    assert transcript == []
    assert [word.text for word in runtime.heard_tracker.words] == ["伪精确后缀"]


def test_agent_helpers_prewarm_and_turn_handling_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert agent_mod._message_text("plain") == "plain"
    assert agent_mod._message_text(SimpleNamespace(content=["a", 1, "b"])) == "a\nb"
    assert agent_mod._message_text(SimpleNamespace(content=12)) == "12"
    assert "session.history" not in inspect.getsource(agent_mod.entrypoint)

    loaded: dict[str, Any] = {}

    def fake_load(**kwargs: Any) -> str:
        loaded.update(kwargs)
        return "vad"

    monkeypatch.setattr(agent_mod.silero.VAD, "load", fake_load)
    proc = SimpleNamespace(userdata={})
    agent_mod.prewarm(proc)
    assert proc.userdata["vad"] == "vad"
    assert loaded["min_silence_duration"] == 0.30

    monkeypatch.setenv("LIVEKIT_TURN_DETECTOR_VERSION", "v1-mini")
    options = agent_mod.build_turn_handling_options("livekit_cloud")
    assert options["interruption"]["mode"] == "adaptive"
    assert options["preemptive_generation"]["enabled"] is False
    assert (
        agent_mod.build_turn_handling_config("livekit_cloud")["preemptive_generation"]["enabled"]
        is False
    )
    monkeypatch.setenv("LIVEKIT_ADAPTIVE_INTERRUPTION", "false")
    options = agent_mod.build_turn_handling_options("livekit_cloud")
    assert options["interruption"]["mode"] == "vad"
    monkeypatch.delenv("LIVEKIT_ADAPTIVE_INTERRUPTION", raising=False)
    # Self-hosted: adaptive needs LiveKit Cloud gateway — default VAD.
    assert agent_mod.build_turn_handling_options("cn_self_hosted")["interruption"]["mode"] == "vad"
    assert (
        agent_mod.build_turn_handling_config("cn_self_hosted")["stream_speak_while_think"] is True
    )
    monkeypatch.setenv("LIVEKIT_ADAPTIVE_INTERRUPTION", "true")
    assert (
        agent_mod.build_turn_handling_config("cn_self_hosted")["interruption"]["mode"] == "adaptive"
    )

    def fail_options(_profile: str) -> Any:
        raise ValueError("bad api")

    monkeypatch.setattr(agent_mod, "build_turn_handling_options", fail_options)
    monkeypatch.setenv("ENVIRONMENT", "development")
    kwargs = agent_mod.build_session_kwargs(
        vad=None,
        stt="stt",
        llm="llm",
        tts="tts",
        profile="livekit_cloud",
        offline=False,
    )
    assert "turn_handling_config" in kwargs
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="bad api"):
        agent_mod.build_session_kwargs(
            vad=None,
            stt="stt",
            llm="llm",
            tts="tts",
            profile="livekit_cloud",
            offline=False,
        )


def test_cascade_audio_output_uses_high_quality_opus_bitrate() -> None:
    options = agent_mod.build_cascade_audio_output_options()

    assert options.sample_rate == 24000
    assert options.num_channels == 1
    assert options.track_publish_options.audio_encoding.max_bitrate == 64000


def test_self_hosted_turn_handling_filters_short_echoes_and_reads_timing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ENDPOINTING_MIN_DELAY_S",
        "ENDPOINTING_MAX_DELAY_S",
        "ENDPOINTING_ALPHA",
        "INTERRUPTION_MIN_DURATION_S",
        "FALSE_INTERRUPTION_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)

    options = agent_mod.build_turn_handling_options("cn_self_hosted")
    assert options["endpointing"] == {
        "mode": "dynamic",
        "min_delay": 0.90,
        "max_delay": 1.50,
        "alpha": 0.85,
    }
    assert options["interruption"]["min_duration"] == 0.55
    assert options["interruption"]["min_words"] == 0
    assert options["interruption"]["false_interruption_timeout"] == 1.70
    assert (
        options["interruption"]["false_interruption_timeout"] >= options["endpointing"]["min_delay"]
    )
    assert (
        agent_mod.build_turn_handling_config("cn_self_hosted")["interruption"]
        == options["interruption"]
    )

    cloud = agent_mod.build_turn_handling_config("livekit_cloud")
    assert cloud["endpointing"]["min_delay"] == 0.30
    assert cloud["interruption"]["min_duration"] == 0.25
    assert cloud["interruption"]["min_words"] == 0
    assert cloud["interruption"]["false_interruption_timeout"] == 1.20

    monkeypatch.setenv("ENDPOINTING_MIN_DELAY_S", "0.61")
    monkeypatch.setenv("ENDPOINTING_MAX_DELAY_S", "2.40")
    monkeypatch.setenv("ENDPOINTING_ALPHA", "0.75")
    monkeypatch.setenv("INTERRUPTION_MIN_DURATION_S", "0.52")
    monkeypatch.setenv("FALSE_INTERRUPTION_TIMEOUT_S", "0.93")
    overridden = agent_mod.build_turn_handling_options("cn_self_hosted")
    assert overridden["endpointing"] == {
        "mode": "dynamic",
        "min_delay": 0.61,
        "max_delay": 2.40,
        "alpha": 0.75,
    }
    assert overridden["interruption"]["min_duration"] == 0.52
    assert overridden["interruption"]["false_interruption_timeout"] == 0.93


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


class _FakeParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[dict[str, Any], bool, str]] = []

    async def publish_data(self, payload: str, *, reliable: bool, topic: str) -> None:
        self.published.append((json.loads(payload), reliable, topic))


class _FakeRoom(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.name = "voice-public-session"
        self.local_participant = _FakeParticipant()


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

    def bind_fence(self, fence: Any) -> None:
        self.bound.append(fence)

    def set_alignment_callback(self, callback: Any) -> None:
        self.alignment_callback = callback

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_agent_alignment_callback_rejects_a_stale_runtime_fence() -> None:
    fake_tts = _FakeTTS()
    runtime = DuplexRuntime.create(tts=fake_tts)  # type: ignore[arg-type]
    DuplexVoiceAgent(instructions="test", runtime=runtime)
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

    def say(self, text: str, **_kwargs: Any) -> str:
        self.said.append(text)
        return text

    def clear_user_turn(self) -> None:
        self.clear_user_turn_count += 1

    async def generate_reply(self, *, instructions: str) -> None:
        self.generated.append(instructions)


@pytest.mark.asyncio
async def test_entrypoint_routes_control_playback_and_ui_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src import mode_policy_client
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.providers import deepseek, doubao_tts, funasr_stt

    fake_tts = _FakeTTS()
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    monkeypatch.setenv("SPEAKER_VERIFY_ENABLED", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN",
        "interaction-policy-material-that-is-long-enough",
    )

    class FakeDoubao:
        @classmethod
        def from_env(cls) -> _FakeTTS:
            return fake_tts

    class FakeFun:
        @classmethod
        def from_env(cls) -> str:
            return "stt"

    class FakeDeepConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeDeepClient:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeModePolicyClient:
        def __init__(self, _config: Any) -> None:
            self.closed = False

        async def fetch(self, *, session_id: str) -> ModePolicy:
            assert session_id == "public-session"
            return ModePolicy.companion_for_test(
                policy_version="s2-v1",
                private_context=True,
                owner_evidence=True,
                tools=True,
                voice_profile=True,
                shadow_low_sensitivity_persona=True,
            )

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(doubao_tts, "DoubaoTTS", FakeDoubao)
    monkeypatch.setattr(funasr_stt, "FunASRSTT", FakeFun)
    monkeypatch.setattr(deepseek, "DeepSeekConfig", FakeDeepConfig)
    monkeypatch.setattr(deepseek, "DeepSeekClient", FakeDeepClient)
    monkeypatch.setattr(mode_policy_client, "ModePolicyClient", FakeModePolicyClient)
    monkeypatch.setattr(agent_mod.openai, "LLM", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agent_mod, "AgentSession", _FakeSession)
    monkeypatch.setattr(
        agent_mod,
        "build_session_kwargs",
        lambda **kwargs: {"turn_handling_config": {}, **kwargs},
    )

    room = _FakeRoom()
    shutdown_callbacks: list[Any] = []
    ctx = SimpleNamespace(
        room=room,
        job=SimpleNamespace(metadata=MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA),
        proc=SimpleNamespace(userdata={"vad": "vad"}),
        connect=lambda: asyncio.sleep(0),
        add_shutdown_callback=shutdown_callbacks.append,
    )
    await agent_mod.entrypoint(ctx)
    session = _FakeSession.last
    assert session is not None and session.started is not None
    assert session.kwargs["aec_warmup_duration"] is None
    assert session.generated == []
    assert session.said == ["嗨，我在呢。想聊什么就直接说吧。"]
    assert any(event[0].get("state") == "ready" for event in room.local_participant.published)
    assert any(
        event[0].get("type") == "audio_trace" and event[0].get("name") == "audio_output_attached"
        for event in room.local_participant.published
    )
    await asyncio.sleep(0)
    runtime: DuplexRuntime = ctx.proc.userdata["duplex_runtime"]
    assert runtime.session_id == "public-session"
    assert runtime.input_guard.enabled is True
    assert runtime.trusted_aec_playback_control is True
    assert runtime._interrupt_semantic_resolver is not None
    runtime._clear_control_user_turn(cause="production_wiring_test")
    assert session.clear_user_turn_count == 1

    session.emit("agent_state_changed", SimpleNamespace(new_state="thinking"))
    session.emit("user_input_transcribed", SimpleNamespace(transcript="你好", is_final=True))
    session.emit(
        "conversation_item_added",
        SimpleNamespace(item=SimpleNamespace(role="assistant", text_content="回复")),
    )
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(role="user")))
    assert session.options.interruption["min_words"] == 1000
    session.emit(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    assert session.options.interruption["min_words"] == 0
    await asyncio.sleep(0)

    await runtime.on_turn_committed("等待停止")
    room.emit("data_received", SimpleNamespace(topic="other", participant=None, data=b"{}"))
    room.emit(
        "data_received",
        SimpleNamespace(topic="voice-agent.control", participant=object(), data=b"{}"),
    )
    room.emit(
        "data_received",
        SimpleNamespace(topic="voice-agent.control", participant=None, data=b"not-json"),
    )
    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.control",
            participant=None,
            data=json.dumps({"type": "stop_response", "session_id": "wrong"}).encode(),
        ),
    )
    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.control",
            participant=None,
            data=json.dumps(
                {
                    "type": "stop_response",
                    "session_id": "public-session",
                    "reason": "user_button",
                }
            ).encode(),
        ),
    )
    await asyncio.sleep(0.02)
    assert session.interrupt_count == 1
    assert runtime.orchestrator.state is ConversationState.LISTENING

    before_recovery = runtime.fence
    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.control",
            participant=None,
            data=json.dumps({"type": "rtc_recovered", "session_id": "public-session"}).encode(),
        ),
    )
    await asyncio.sleep(0.02)
    assert session.interrupt_count == 2
    assert runtime.fence.generation_id == before_recovery.generation_id + 1

    await runtime.on_turn_committed("播放")
    runtime.update_pending_assistant_text("播放内容")
    assert session.output.audio is not None
    session.output.audio.emit("playback_started", SimpleNamespace())
    await asyncio.sleep(0.02)
    assert session.options.interruption["min_words"] == 1000
    audio_event_count = sum(
        1
        for event in room.local_participant.published
        if event[0].get("type") == "assistant_audio"
    )
    session.emit(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="그.", is_final=True),
    )
    await asyncio.sleep(0)
    assert session.output.audio.pause_count == 0
    assert session.output.audio.resume_count == 0
    new_audio_events = [
        event[0]
        for event in room.local_participant.published
        if event[0].get("type") == "assistant_audio"
    ][audio_event_count:]
    assert [event.get("action") for event in new_audio_events] == ["duck", "restore"]
    assert [event.get("gain") for event in new_audio_events] == [0.0, 1.0]
    assert 1000 in session.options.interruption.history
    assert session.options.interruption["min_words"] == 1000
    session.emit(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等等", is_final=True),
    )
    await asyncio.sleep(0.02)
    assert 0 in session.options.interruption.history
    assert session.options.interruption["min_words"] == 0
    assert session.interrupt_count == 3
    session.output.audio.emit(
        "playback_finished",
        SimpleNamespace(
            playback_position=0.5,
            interrupted=False,
            synchronized_transcript="播放内容",
        ),
    )
    session.emit(
        "conversation_item_added",
        SimpleNamespace(
            item=SimpleNamespace(
                role="assistant",
                text_content="播放内容",
                interrupted=False,
            )
        ),
    )
    await asyncio.sleep(0.02)
    # The accepted control command advanced the fence, so a late completion
    # cannot overwrite the already-heard, truncated assistant text.
    assert runtime.orchestrator.context.turns[-1].content == "播放"

    await runtime.on_turn_committed("会被语音中断")
    await session.interrupt(force=True)
    assert session.interrupt_count == 4
    assert runtime.orchestrator.state is ConversationState.USER_SPEAKING

    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.gateway-health",
            participant=object(),
            data=json.dumps(
                {
                    "type": "miniprogram_aec_ready",
                    "session_id": "public-session",
                    "failure_id": "not-a-disable",
                }
            ).encode(),
        ),
    )
    assert runtime.trusted_aec_playback_control is True

    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.gateway-health",
            participant=object(),
            data=json.dumps(
                {
                    "type": "miniprogram_aec_failed",
                    "session_id": "public-session",
                    "failure_id": "failure-1",
                }
            ).encode(),
        ),
    )
    await asyncio.sleep(0)
    assert runtime.trusted_aec_playback_control is False
    assert any(
        event == {
            "type": "miniprogram_aec_failed_ack",
            "session_id": "public-session",
            "failure_id": "failure-1",
        }
        and topic == "voice-agent.gateway-health.ack"
        for event, reliable, topic in room.local_participant.published
        if reliable
    )

    assert len(shutdown_callbacks) == 1
    await shutdown_callbacks[0]()
    assert fake_tts.closed
    assert room.handlers["data_received"] == []
