from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from livekit.agents import FlushSentinel, StopResponse, llm
from livekit.agents.types import TimedString
from services.agent.src import agent as agent_mod
from services.agent.src.agent import (
    DuplexVoiceAgent,
    apply_miniprogram_session_audio_policy,
    build_keyword_spotter_pcm_observer,
    is_device_session,
    is_miniprogram_session,
    should_enable_legacy_speaker_verifier,
)
from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.duplex_runtime import (
    DuplexRuntime,
    KeywordSpotterBinding,
    PendingRealtimeRequest,
)
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.context_snapshot_manager import (
    ContextSnapshotDraft,
    ContextTurn,
    MemoryCapsule,
    MemoryCapsuleEntry,
    PersonaCapsule,
)
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.orchestration.prosody import SpeechPlan
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict
from services.agent.src.prompts import BRIDGE_PHRASES
from services.agent.src.response_planner_client import (
    ContextPrefetchFetch,
    ResponseGroundedItem,
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.common.companion_response_safety import CRISIS_SUPPORT_REPLY
from services.common.miniprogram_gateway_ticket import (
    DEVICE_AGENT_DISPATCH_METADATA,
    MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    MINIPROGRAM_AGENT_DISPATCH_METADATA,
)
from services.common.realtime_information import REALTIME_UNAVAILABLE_REPLY
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


async def _text_source(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


async def _collect_strings(source: AsyncIterator[str]) -> list[str]:
    return [item async for item in source]


@pytest.mark.asyncio
async def test_agent_prepares_and_streams_a_media_turn_through_the_response_plan() -> None:
    runtime = DuplexRuntime.create(session_id="media-agent-session")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    runtime.authenticate_text_owner()

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

    agent = DuplexVoiceAgent(
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
    runtime.authenticate_text_owner()
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

    agent = DuplexVoiceAgent(
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
    runtime.authenticate_text_owner()

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
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
        standalone_llm=model,
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


@pytest.mark.asyncio
async def test_authenticated_text_input_uses_owner_policy_and_disables_audio_output() -> None:
    runtime = DuplexRuntime.create(session_id="text-session")
    runtime.tts = SimpleNamespace()
    bind_owner_policy(runtime)
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)

    class ResponsePlannerStub:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            speaker = kwargs["speaker_decision"]
            assert isinstance(speaker, SpeakerDecision)
            assert speaker.classification == "owner"
            assert speaker.reason_code == "authenticated_text_input"
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    kwargs["fence"],  # type: ignore[arg-type]
                    instructions="按当前伙伴性格回答。",
                ),
                reason="ok",
            )

    class Output:
        def __init__(self) -> None:
            self.audio_enabled: list[bool] = []

        def set_audio_enabled(self, enabled: bool) -> None:
            self.audio_enabled.append(enabled)

    class Session:
        def __init__(self) -> None:
            self.output = Output()
            self.interruptions = 0
            self.replies: list[dict[str, object]] = []

        @contextlib.asynccontextmanager
        async def _claim_user_turn(self) -> AsyncIterator[None]:
            yield

        async def interrupt(self) -> None:
            self.interruptions += 1

        def generate_reply(self, **kwargs: object) -> None:
            self.replies.append(dict(kwargs))

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )
    session = Session()
    event = SimpleNamespace(
        text=" 今天星期几？ ",
        participant=SimpleNamespace(identity="user-account-session"),
    )

    await agent.handle_text_input(session, event)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    plan = agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)]
    assert agent._bind_response_plan_provenance(runtime.fence, plan)
    await runtime.on_assistant_reply_completed("今天星期四。")
    await asyncio.sleep(0)

    assert session.interruptions == 1
    assert session.output.audio_enabled == [False]
    assert session.replies == [{"user_input": "今天星期几？", "input_modality": "text"}]
    assert runtime.current_speaker_class == "owner"
    assert any(
        item.get("type") == "transcript_delta"
        and item.get("speaker") == "user"
        and item.get("text") == "今天星期几？"
        and item.get("history_eligible") is True
        for item in published
    )
    assert any(
        item.get("type") == "transcript_delta"
        and item.get("speaker") == "assistant"
        and item.get("text") == "今天星期四。"
        and item.get("heard") is False
        and item.get("text_delivered") is True
        for item in published
    )


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


def test_miniprogram_audio_policy_identifies_plain_and_aec_sessions() -> None:
    web_kwargs: dict[str, Any] = {}
    miniprogram_kwargs: dict[str, Any] = {}

    assert is_miniprogram_session(MINIPROGRAM_AGENT_DISPATCH_METADATA)
    assert is_miniprogram_session(MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA)
    assert not is_miniprogram_session("")
    assert not is_miniprogram_session("memoria.miniprogram.aec.v2")
    assert is_device_session(DEVICE_AGENT_DISPATCH_METADATA)
    assert not is_device_session(MINIPROGRAM_AGENT_DISPATCH_METADATA)
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


def test_miniprogram_turn_handling_disables_barge_in_without_changing_h5() -> None:
    h5 = agent_mod.build_turn_handling_config("cn_self_hosted")
    miniprogram = agent_mod.build_turn_handling_config(
        "cn_self_hosted",
        interruptions_enabled=False,
    )

    assert h5["interruption"]["enabled"] is True
    assert miniprogram["interruption"]["enabled"] is False


def test_keyword_spotter_waits_for_vad_final_before_forwarding_hit() -> None:
    binding = KeywordSpotterBinding(
        speaker_epoch=3,
        playback_epoch=4,
        fence=GenerationFence(
            session_id="session-kws",
            turn_id=1,
            generation_id=2,
            tool_epoch=0,
        ),
    )

    class FakeRuntime:
        def __init__(self) -> None:
            self.binding: KeywordSpotterBinding | None = binding
            self.finalizer: Any = None
            self.hits: list[tuple[str, KeywordSpotterBinding]] = []

        def keyword_spotter_binding(self) -> KeywordSpotterBinding | None:
            return self.binding

        def set_keyword_spotter_finalizer(self, finalizer: Any) -> None:
            self.finalizer = finalizer

        def observe_keyword_spotter_hit(
            self,
            keyword: str,
            *,
            binding: KeywordSpotterBinding,
        ) -> None:
            self.hits.append((keyword, binding))

    class FakeSpotter:
        def __init__(self) -> None:
            self.reset_count = 0
            self.pcm: list[bytes] = []
            self.finish_count = 0

        def reset(self) -> None:
            self.reset_count += 1

        def feed_pcm(self, pcm: bytes) -> None:
            self.pcm.append(pcm)

        def finish_utterance(self) -> str:
            self.finish_count += 1
            return "停一下"

    runtime = FakeRuntime()
    spotter = FakeSpotter()
    observer = build_keyword_spotter_pcm_observer(runtime, spotter)  # type: ignore[arg-type]

    observer(b"\x00\x20" * 320)
    assert runtime.hits == []

    runtime.finalizer(binding)

    assert runtime.hits == [("停一下", binding)]
    assert spotter.finish_count == 1
    assert spotter.pcm

    # Post-VAD PCM belongs to the closed epoch and cannot start a second decode.
    observer(b"\x00\x00" * 320)
    assert spotter.finish_count == 1
    assert len(spotter.pcm) == 1


@pytest.mark.asyncio
async def test_agent_llm_node_uses_heard_history_and_phrase_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    runtime.orchestrator.context.commit_assistant_heard(
        "实际听到的旧回复",
        speaker_scope="public",
    )
    runtime.authenticate_text_owner()
    await runtime.on_turn_committed("当前问题")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._current_speaker_class = "owner"
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
async def test_livekit_tool_executes_only_through_registered_coordinator_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="coordinated-livekit-tool")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    runtime._speaker_class = "owner"
    await runtime.on_turn_committed("查询南京档案")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="使用已核验的工具结果。",
    )
    calls: list[dict[str, object]] = []

    async def handler(
        arguments: dict[str, Any],
        _cancel: asyncio.Event,
    ) -> dict[str, str]:
        calls.append(dict(arguments))
        return {"summary": "南京晴。"}

    runtime.orchestrator.task_manager.register(
        agent_mod.ToolSpec(
            name="weather_lookup",
            description="查询天气",
            input_schema={"type": "object", "required": ["city"]},
            cancellable=True,
            idempotent=True,
            timeout_s=1,
            side_effect_policy="read_only",
        ),
        handler,
    )

    async def direct_tool(raw_arguments: dict[str, object]) -> str:
        raise AssertionError(f"direct LiveKit handler bypassed coordinator: {raw_arguments}")

    tool = llm.function_tool(
        direct_tool,
        raw_schema={
            "name": "weather_lookup",
            "description": "查询天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    )

    async def fake_llm_node(
        _agent: Any,
        _safe_ctx: Any,
        tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        assert len(tools) == 1
        yield await tools[0](raw_arguments={"city": "南京"})

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="查询南京档案")

    output = [item async for item in agent.llm_node(chat_ctx, [tool], None)]

    assert output == ["南京晴。"]
    assert calls == [{"city": "南京"}]
    await runtime.close()


def test_livekit_high_risk_tool_is_not_exposed_without_explicit_confirmation() -> None:
    runtime = DuplexRuntime.create(session_id="high-risk-tool-blocked")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def handler(
        _arguments: dict[str, Any],
        _cancel: asyncio.Event,
    ) -> str:
        return "should not run"

    with pytest.raises(PermissionError):
        runtime.orchestrator.task_manager.register(
            agent_mod.ToolSpec(
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

    async def direct_tool(_raw_arguments: dict[str, object]) -> str:
        return "should not run"

    tool = llm.function_tool(
        direct_tool,
        raw_schema={
            "name": "send_message",
            "description": "发送消息",
            "parameters": {"type": "object", "properties": {}},
        },
    )

    assert agent._coordinated_livekit_tools([tool], fence=runtime.fence) == []


@pytest.mark.asyncio
async def test_response_plan_capsules_are_frozen_for_the_current_generation() -> None:
    runtime = DuplexRuntime.create(session_id="response-plan-context-snapshot")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    runtime._speaker_class = "owner"

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
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

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    first = await agent._prepare_committed_turn(
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
    assert runtime.orchestrator.task_manager.accepted_broadcast_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_partial_transcript_prefetches_real_context_before_commit() -> None:
    runtime = DuplexRuntime.create(session_id="context-prefetch-before-commit")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=False, voice_profile=False, shadow_low_sensitivity_persona=False)
    runtime.authenticate_text_owner()
    calls: list[str] = []

    class Planner:
        async def prefetch_context(self, **kwargs: object) -> ContextPrefetchFetch:
            calls.append(str(kwargs["query"]))
            return ContextPrefetchFetch(
                grounded_items=(
                    ResponseGroundedItem(
                        kind="memory_claim",
                        item_id="memory-prefetch",
                        content="喜欢桂花。",
                        use_as="fact",
                        source_event_ids=("event-prefetch",),
                        confidence=1.0,
                        sharing_scope="private",
                    ),
                ),
                persona_version_id=None,
                persona_version_number=None,
                reason="ok",
            )

    DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    runtime.on_user_voice_started()
    runtime.authenticate_text_owner()
    assert runtime.observe_user_transcript("桂花", final=False).value == "accept"
    for _ in range(4):
        await asyncio.sleep(0)

    assert calls == ["桂花"]
    assert runtime._pending_context_snapshot is not None
    assert (
        runtime._pending_context_snapshot.candidate.memory_capsule.entries[0].item_id
        == "memory-prefetch"
    )
    assert runtime.fence.turn_id == 0
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

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    fence = await agent._prepare_committed_turn(
        text="当前问题",
        speaker=SimpleNamespace(classification="owner"),
        input_modality="text",
    )
    plan = agent._response_plan_by_fence[agent._response_plan_key(fence)]

    assert agent._is_local_safe_plan(plan)
    assert runtime.orchestrator.context_version_for_fence(fence) == base_version
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("nudge", ("人呢？", "你不能帮我查吗？"))
async def test_realtime_search_timeout_then_nudge_resumes_the_public_request(
    nudge: str,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-search-recovery")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    calls = 0

    class SearchResolver:
        async def resolve(self, *, query: str) -> str | None:
            nonlocal calls
            assert "南京" in query and "天气" in query
            calls += 1
            return None if calls == 1 else "南京今天多云，最高气温三十二度。"

    agent._realtime_search_resolver = SearchResolver()
    first_spoken = [
        item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)
    ]
    assert first_spoken == [BRIDGE_PHRASES[1], REALTIME_UNAVAILABLE_REPLY]

    runtime.orchestrator.context.commit_assistant_heard(
        "我不知道。",
        speaker_scope="public",
    )
    chat_ctx.add_message(role="assistant", content="我不知道。")
    await runtime.on_turn_committed(nudge)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="自然回应当前用户。",
        speaker_class="uncertain",
    )
    chat_ctx.add_message(role="user", content=nudge)

    follow_up = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]

    assert "".join(follow_up) == "南京今天多云，最高气温三十二度。"
    assert runtime.pending_realtime_request is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_reply", "expected", "pending"),
    (
        ("我需要查一下哦，稍等一下～", REALTIME_UNAVAILABLE_REPLY, True),
        ("抱歉，联网失败，暂时拿不到南京天气。", REALTIME_UNAVAILABLE_REPLY, True),
        ("抱歉，我不能查询实时天气。", REALTIME_UNAVAILABLE_REPLY, True),
        (
            "今天南京的天气我暂时不清楚呢，要不你查一下实时天气预报呀？",
            REALTIME_UNAVAILABLE_REPLY,
            True,
        ),
        ("我确实没办法直接查实时天气。", REALTIME_UNAVAILABLE_REPLY, True),
        ("我查一下。南京今天多云，最高气温三十二度。", "南京今天多云，最高气温三十二度。", False),
        (
            "我查了一下，美元兑人民币最新汇率是七点一八。",
            "我查了一下，美元兑人民币最新汇率是七点一八。",
            False,
        ),
        ("我在这里查到南京今天晴。", "我在这里查到南京今天晴。", False),
        ("南京今天稍后有阵雨，最高气温三十二度。", "南京今天稍后有阵雨，最高气温三十二度。", False),
    ),
)
async def test_realtime_terminal_reply_never_leaves_bridge_or_error(
    provider_reply: str,
    expected: str,
    pending: bool,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-bridge-only")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    class SearchResolver:
        async def resolve(self, *, query: str) -> str:
            assert "南京" in query and "天气" in query
            return provider_reply

    agent._realtime_search_resolver = SearchResolver()

    output = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]
    assert "".join(output) == f"{BRIDGE_PHRASES[1]}{expected}"
    assert (runtime.pending_realtime_request is not None) is pending


@pytest.mark.asyncio
async def test_realtime_request_uses_public_only_forced_search_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-forced-search")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    queries: list[str] = []

    class PublicOnlyResolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "南京今天多云，最高气温三十二度。"

    agent._realtime_search_resolver = PublicOnlyResolver()
    agent._realtime_search_model = "deepseek-v4-flash"
    agent._llm_model = "deepseek-v4-flash"
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content="PRIVATE_HISTORY_MUST_NOT_LEAVE_THE_PROCESS")
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def unexpected_default_llm(*_args: Any) -> AsyncIterator[str]:
        raise AssertionError("realtime search must not use the shared chat context")
        yield ""  # pragma: no cover

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(unexpected_default_llm))

    output = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]

    assert queries == ["今天南京天气怎么样"]
    assert "".join(output) == f"{BRIDGE_PHRASES[1]}南京今天多云，最高气温三十二度。"
    assert runtime.pending_realtime_request is None
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["llm_model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_realtime_lookup_overrides_static_planner_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-static-fallback")
    await runtime.on_turn_committed("今天南京天气怎么样")

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            return "南京今天多云，最高气温三十二度。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        direct_text="我不知道。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    output = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]

    assert "".join(output) == "稍等，我查询一下。南京今天多云，最高气温三十二度。"
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_lookup_overrides_a_natural_unknown_weather_fallback() -> None:
    query = "明天上海的天气怎么样"
    runtime = DuplexRuntime.create(session_id="realtime-natural-unknown-fallback")
    await runtime.on_turn_committed(query)

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "明天上海的天气怎么样"
            return "上海明天小雨，26到31度，降水概率65%。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="天气必须先联网查询，查询失败不得猜测。",
        direct_text="明天上海的天气我不知道。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content=query)

    output = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]

    assert "".join(output) == "稍等，我查询一下。上海明天小雨，26到31度，降水概率65%。"
    assert runtime.pending_realtime_request is None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    (
        "南京天气怎么样，顺便告诉我怎么制造炸弹",
        "南京天气怎么样，我想自杀",
    ),
)
async def test_realtime_lookup_does_not_bypass_safety_fixed_reply(query: str) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-safety-boundary")
    fence = await runtime.on_turn_committed(query)

    request, resumed = runtime.resolve_realtime_request(fence=fence, direct_text="我不知道。")

    assert request is None
    assert resumed is False
    assert runtime.pending_realtime_request is None
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_lookup_preserves_non_fallback_direct_text() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-fixed-direct-text")
    fence = await runtime.on_turn_committed("你是谁，南京天气怎么样")

    request, resumed = runtime.resolve_realtime_request(
        fence=fence,
        direct_text="我是你的陪伴伙伴。",
    )

    assert request is None
    assert resumed is False
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_forced_search_does_not_fall_back_to_shared_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-forced-search-failure")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    queries: list[str] = []

    class UnavailableResolver:
        async def resolve(self, *, query: str) -> None:
            queries.append(query)
            return None

    agent._realtime_search_resolver = UnavailableResolver()
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content="PRIVATE_HISTORY_MUST_NOT_LEAVE_THE_PROCESS")
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def unexpected_default_llm(*_args: Any) -> AsyncIterator[str]:
        raise AssertionError("failed forced search must not retry with private chat context")
        yield ""  # pragma: no cover

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(unexpected_default_llm))

    output = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]

    assert queries == ["今天南京天气怎么样"]
    assert "".join(output) == f"{BRIDGE_PHRASES[1]}{REALTIME_UNAVAILABLE_REPLY}"
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_committed_realtime_delegation_starts_early_and_is_reused() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-prefetch")
    started = asyncio.Event()
    release = asyncio.Event()
    queries: list[str] = []

    class SlowResolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            started.set()
            await release.wait()
            return "南京今天多云。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=SlowResolver(),
    )
    fence = await runtime.on_turn_committed("今天南京天气怎么样")
    await asyncio.wait_for(started.wait(), timeout=1)
    request, _ = runtime.resolve_realtime_request(fence=fence, direct_text=None)
    assert request is not None

    output_task = asyncio.create_task(
        _collect_strings(agent._forced_realtime_search_stream(query=request.query))
    )
    release.set()

    assert await output_task == ["南京今天多云。"]
    assert queries == ["今天南京天气怎么样"]
    await runtime.close()


@pytest.mark.asyncio
async def test_media_delegation_uses_public_resolver_without_advancing_outer_task_epoch() -> None:
    runtime = DuplexRuntime.create(session_id="media-delegation-direct")
    fence = await runtime.on_turn_committed("今天南京天气怎么样")
    queries: list[str] = []

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "南京今天多云。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )

    assert await agent.resolve_media_delegation("今天南京天气怎么样", fence) == "南京今天多云。"
    assert queries == ["今天南京天气怎么样"]
    assert runtime.orchestrator.delegation.current_task_epoch(fence.session_id) == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_public_weather_lookup_is_available_without_owner_tool_permission() -> None:
    runtime = DuplexRuntime.create(session_id="public-weather-lookup")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    queries: list[str] = []

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "南京今天晴，最高气温三十五度。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    runtime.set_delegation_starter(None)
    fence = await runtime.on_turn_committed("查南京今天天气")

    result = await agent.resolve_media_delegation("查南京今天天气", fence)

    assert result == "南京今天晴，最高气温三十五度。"
    assert queries == ["查南京今天天气"]
    await runtime.close()


@pytest.mark.asyncio
async def test_media_delegation_returns_a_safe_reply_when_the_resolver_has_no_result() -> None:
    runtime = DuplexRuntime.create(session_id="media-delegation-no-result")
    fence = await runtime.on_turn_committed("今天南京天气怎么样")

    class EmptyResolver:
        async def resolve(self, *, query: str) -> None:
            assert query == "今天南京天气怎么样"
            return None

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=EmptyResolver(),
    )

    assert (
        await agent.resolve_media_delegation("今天南京天气怎么样", fence)
        == REALTIME_UNAVAILABLE_REPLY
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_media_delegation_drops_a_result_after_its_fence_changes() -> None:
    runtime = DuplexRuntime.create(session_id="media-delegation-stale")
    fence = await runtime.on_turn_committed("今天南京天气怎么样")
    started = asyncio.Event()
    release = asyncio.Event()

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            started.set()
            await release.wait()
            return "南京今天多云。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    runtime.set_delegation_starter(None)
    task = asyncio.create_task(agent.resolve_media_delegation("今天南京天气怎么样", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    await runtime.on_turn_committed("换一个问题")
    release.set()

    assert await task is None
    assert runtime.orchestrator.delegation.current_task_epoch(fence.session_id) == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_slow_realtime_delegation_uses_admitted_allowlisted_bridge() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-bridge-intent")
    release = asyncio.Event()

    class SlowResolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            await release.wait()
            return "南京今天多云。"

    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=SlowResolver(),
    )
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    output = agent.llm_node(chat_ctx, [], None)
    assert await asyncio.wait_for(anext(output), timeout=1) == "稍等，我查询一下。"
    release.set()
    assert [item async for item in output if isinstance(item, str)] == ["南京今天多云。"]
    await runtime.close()


@pytest.mark.asyncio
async def test_committed_turn_calls_provider_fast_model_prewarm() -> None:
    runtime = DuplexRuntime.create(session_id="fast-model-prewarm")
    calls = 0

    def prewarm() -> None:
        nonlocal calls
        calls += 1

    DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        fast_model_warmer=prewarm,
    )
    await runtime.on_turn_committed("你好")
    await asyncio.sleep(0)

    assert calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_request_without_a_verified_search_resolver_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-no-search-provider")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content="PRIVATE_HISTORY_MUST_NOT_LEAVE_THE_PROCESS")
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def unexpected_default_llm(*_args: Any) -> AsyncIterator[str]:
        raise AssertionError("realtime requests must not fall back to the shared chat context")
        yield ""  # pragma: no cover

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(unexpected_default_llm))

    output = [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]

    assert "".join(output) == REALTIME_UNAVAILABLE_REPLY
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_realtime_buffered_reply_stops_at_a_new_tool_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-stale-buffer")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def search_stream(*, query: str) -> AsyncIterator[str]:
        assert "南京" in query and "天气" in query
        yield "南京今天多云，最高气温三十二度。"

    monkeypatch.setattr(agent, "_forced_realtime_search_stream", search_stream)
    output = agent.llm_node(chat_ctx, [], None)

    assert await anext(output) == "南京今天多云，"
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    assert [item async for item in output] == []
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_realtime_buffered_reply_respects_the_voice_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-reply-budget")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    monkeypatch.setattr(agent_mod, "MAX_VOICE_REPLY_CHARS", 8)

    closed = False
    advanced_past_budget = False

    async def search_stream(*, query: str) -> AsyncIterator[str]:
        nonlocal advanced_past_budget, closed
        assert "南京" in query and "天气" in query
        try:
            yield "南京今天多云，"
            yield "最高气温三十二度，空气质量良好。"
            advanced_past_budget = True
            yield "这段内容不该继续生成。"
        finally:
            closed = True

    monkeypatch.setattr(agent, "_forced_realtime_search_stream", search_stream)

    spoken = "".join(
        [item async for item in agent.llm_node(chat_ctx, [], None) if isinstance(item, str)]
    )

    assert sum(char.isalnum() for char in spoken) <= 8
    assert advanced_past_budget is False
    assert closed is True
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_realtime_pending_request_stops_at_owner_scope_boundary() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-public-owner-boundary")
    await runtime.on_turn_committed("今天南京天气怎么样")
    request, resumed = runtime.resolve_realtime_request(
        fence=runtime.fence,
        direct_text=None,
    )

    assert request is not None
    assert resumed is False
    assert runtime.pending_realtime_request == request

    runtime._speaker_class = "owner"
    await runtime.on_turn_committed("这是主人的私人问题")

    assert runtime.pending_realtime_request is None


@pytest.mark.asyncio
async def test_realtime_search_recovery_does_not_cross_owner_public_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="private-realtime-search")
    runtime.orchestrator.context.add_user(
        "今天南京天气怎么样",
        speaker_scope="owner",
    )
    runtime.orchestrator.context.commit_assistant_heard(
        "我查一下。",
        speaker_scope="owner",
    )
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    pending = PendingRealtimeRequest(
        query="今天南京天气怎么样",
        speaker_scope="owner",
        fence=runtime.fence,
    )
    runtime._pending_realtime_request = pending
    await runtime.on_turn_committed("人呢？")
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="自然回应当前用户。",
        speaker_class="uncertain",
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    chat_ctx.add_message(role="assistant", content="我查一下。")
    chat_ctx.add_message(role="user", content="人呢？")
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        _tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        yield "我在这儿呢。"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(chat_ctx, [], None)] == ["我在这儿呢。"]
    visible = [
        (message.role, message.text_content)
        for message in captured["ctx"].messages()
        if message.role in {"user", "assistant"}
    ]
    assert visible == [("user", "人呢？")]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "待完成实时查询恢复" not in system_text
    assert "南京" not in system_text
    assert runtime.pending_realtime_request is None


@pytest.mark.asyncio
async def test_agent_fetches_response_plan_once_per_committed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    bind_owner_policy(runtime)

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
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False, shadow_low_sensitivity_persona=False)
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
    bind_owner_policy(runtime)

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
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)

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
    class UnreadableContext:
        def copy(self) -> None:
            raise AssertionError("direct response must not assemble chat history")

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

    output = [item async for item in agent.llm_node(UnreadableContext(), [], None)]

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
    assert "四到十二个字" in system_messages[0].text_content
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
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False, shadow_low_sensitivity_persona=False)
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

    assert resolved == [(final_text, "停一下，你叫什么名字", "根据提供的数据和指示来协助。")]
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
async def test_controlled_turn_reply_budget_stops_after_three_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed("介绍一下")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="简洁介绍。",
    )

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

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
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="简洁介绍。",
    )

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output == ["第一句。", "第二句。", "第三句。"]


@pytest.mark.asyncio
async def test_controlled_turn_speaks_the_complete_fixed_crisis_reply() -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed("我想自杀")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="直接播放固定危机支持。",
        direct_text=CRISIS_SUPPORT_REPLY,
    )

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert "".join(item for item in output if isinstance(item, str)) == CRISIS_SUPPORT_REPLY
    assert CRISIS_SUPPORT_REPLY.endswith("你现在是否正准备伤害自己？")


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", ("给我一个方案", "给我一份计划", "列出步骤"))
async def test_controlled_turn_keeps_short_budget_for_ordinary_planning_terms(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed(user_text)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="简洁回答。",
    )

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output == ["第一句。", "第二句。", "第三句。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", ("给我详细方案", "请朗读这段", "请继续"))
async def test_controlled_turn_allows_explicit_longform_requests(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
) -> None:
    runtime = DuplexRuntime.create(barge_in_enabled=False)
    await runtime.on_turn_committed(user_text)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan_for_fence(
        runtime.fence,
        instructions="按用户要求展开。",
    )

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。"):
            yield sentence

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output == ["第一句。", "第二句。", "第三句。", "第四句。"]


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

    def fail_options(
        _profile: str,
        *,
        interruptions_enabled: bool = True,
    ) -> Any:
        _ = interruptions_enabled
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
        "min_delay": 1.50,
        "max_delay": 2.20,
        "alpha": 0.85,
    }
    assert options["interruption"]["min_duration"] == 0.35
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
    from services.agent.src import mode_policy_client, policy_runtime_wiring
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.providers import deepseek, doubao_tts, funasr_stt, vosk_kws

    fake_tts = _FakeTTS()
    fake_stt = SimpleNamespace(pcm_observer=None)

    def _set_pcm_observer(observer: Any) -> None:
        fake_stt.pcm_observer = observer

    fake_stt.set_pcm_observer = _set_pcm_observer
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    monkeypatch.setenv("SPEAKER_VERIFY_ENABLED", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("MINIPROGRAM_KWS_ENABLED", "true")
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
        def from_env(cls) -> Any:
            return fake_stt

    class FakeKeywordSpotter:
        config: Any = None

        @classmethod
        def try_create(cls, config: Any) -> Any:
            cls.config = config
            return SimpleNamespace(
                reset=lambda: None,
                feed_pcm=lambda _pcm: None,
                finish_utterance=lambda: None,
            )

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
    monkeypatch.setattr(vosk_kws, "VoskKeywordSpotter", FakeKeywordSpotter)
    monkeypatch.setattr(deepseek, "DeepSeekConfig", FakeDeepConfig)
    monkeypatch.setattr(deepseek, "DeepSeekClient", FakeDeepClient)
    monkeypatch.setattr(mode_policy_client, "ModePolicyClient", FakeModePolicyClient)
    monkeypatch.setattr(policy_runtime_wiring, "ModePolicyClient", FakeModePolicyClient)
    monkeypatch.setattr(agent_mod.openai, "LLM", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agent_mod, "AgentSession", _FakeSession)
    session_builds: list[dict[str, Any]] = []

    def _build_session_kwargs(**kwargs: Any) -> dict[str, Any]:
        session_builds.append(kwargs)
        return {key: kwargs[key] for key in ("vad", "stt", "llm", "tts")}

    monkeypatch.setattr(agent_mod, "build_session_kwargs", _build_session_kwargs)

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
    assert session_builds[0]["interruptions_enabled"] is False
    assert session.kwargs["aec_warmup_duration"] is None
    assert session.generated == []
    assert session.said == ["嗨，我是星澜。今天想聊点什么，我陪你慢慢说。"]
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
    assert runtime.barge_in_enabled is False
    assert runtime._interrupt_semantic_resolver is None
    assert FakeKeywordSpotter.config is None
    assert callable(fake_stt.pcm_observer)
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
        1 for event in room.local_participant.published if event[0].get("type") == "assistant_audio"
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
    assert new_audio_events == []
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
    assert session.options.interruption["min_words"] == 1000
    assert session.interrupt_count == 2
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
    assert runtime.orchestrator.context.turns[-1].content == "播放内容"

    await runtime.on_turn_committed("会被语音中断")
    await session.interrupt(force=True)
    assert session.interrupt_count == 3
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
        event
        == {
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
