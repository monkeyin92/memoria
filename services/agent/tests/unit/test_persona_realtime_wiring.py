from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from livekit.agents import llm
from services.agent.src import agent as agent_mod
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.context_assembler import ContextAssembler
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.memory_context_client import MemoryContextSnapshot
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.persona_client import PersonaCapsuleSnapshot
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.src.voice_profile_client import VoiceRuntimeProfile
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _decision(
    classification: str,
    *,
    profile_id: str = "profile-001",
    reason_code: str | None = None,
) -> SpeakerDecision:
    return SpeakerDecision(
        classification=classification,  # type: ignore[arg-type]
        score=0.95 if classification == "owner" else 0.1,
        quality_score=0.9,
        reason_code=reason_code
        or ("owner_match" if classification == "owner" else "owner_mismatch"),
        model_version="campplus-test",
        template_version=1,
        profile_id=profile_id,
        permissions=permissions_for_speaker(classification),  # type: ignore[arg-type]
    )


class Message:
    def __init__(self, text: str) -> None:
        self._text = text

    def text_content(self) -> str:
        return self._text


def _response_plan(runtime: DuplexRuntime) -> ResponsePlan:
    return ResponsePlan(
        fence=runtime.fence,
        instructions="仅依据当前用户这一轮内容回答。",
        direct_text=None,
        epistemic_status="not_applicable",
        epistemic_reason_codes=("test",),
        grounded_items=(),
        disclosures=("privacy_refusal",),
        voice_target=ResponseVoiceTarget(
            kind="fallback",
            profile_id=None,
            model="unknown",
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
            speaker_class="uncertain",
            speaker_reason_code="test",
            speaker_profile_id=None,
            speaker_model_version="test",
            speaker_template_version=None,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=("privacy_refusal",),
        ),
    )


async def _prepare_speaker(
    runtime: DuplexRuntime,
    classification: str,
    *,
    profile_id: str = "profile-001",
    reason_code: str | None = None,
) -> None:
    if runtime.fence.turn_id == 0 and runtime.fence.generation_id == 0:
        runtime.set_mode_policy(
            ModePolicy.companion_for_test(
                policy_version="test-policy",
                private_context=True,
                owner_evidence=True,
                tools=True,
                voice_profile=True,
                shadow_low_sensitivity_persona=True,
            )
        )

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _decision(
            classification,
            profile_id=profile_id,
            reason_code=reason_code,
        )

    runtime.set_speaker_classifier(classify, sample_rate=16000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x00\x01")
    runtime.on_user_voice_stopped()


@pytest.mark.asyncio
async def test_legacy_persona_client_is_ignored_by_realtime_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class PersonaStub:
        async def refresh(self, **_kwargs: object) -> bool:
            raise AssertionError("legacy persona client must not be invoked")

        def cached(self, **_kwargs: object) -> PersonaCapsuleSnapshot:
            raise AssertionError("legacy persona cache must not be read")

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        _tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        yield "好的。"

    runtime = DuplexRuntime.create(session_id="session-persona-owner")
    await runtime.orchestrator.ready()
    await _prepare_speaker(runtime, "owner")
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        persona_client=PersonaStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="说说你的看法")
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await asyncio.wait_for(
        agent.on_user_turn_completed(chat_ctx, Message("说说你的看法")),
        timeout=0.05,
    )
    assert [item async for item in agent.llm_node(chat_ctx, [], None)] == ["好的。"]

    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "仅依据当前用户这一轮内容回答" in system_text
    assert "人格胶囊 v3" not in system_text
    assert [message.text_content for message in chat_ctx.messages()] == ["说说你的看法"]
    await runtime.close()

@pytest.mark.asyncio
async def test_legacy_memory_context_client_is_ignored_by_realtime_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class MemoryStub:
        async def refresh(self, **_kwargs: object) -> bool:
            raise AssertionError("legacy memory client must not be invoked")

        def cached(self, **_kwargs: object) -> MemoryContextSnapshot:
            raise AssertionError("legacy memory cache must not be read")

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        _tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        yield "我记得。"

    runtime = DuplexRuntime.create(session_id="session-memory-owner")
    await runtime.orchestrator.ready()
    await _prepare_speaker(runtime, "owner")
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        memory_context_client=MemoryStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="我们家的家训是什么？")
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await asyncio.wait_for(
        agent.on_user_turn_completed(chat_ctx, Message("我们家的家训是什么？")),
        timeout=0.05,
    )
    assert [item async for item in agent.llm_node(chat_ctx, [], None)] == ["我记得。"]

    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "仅依据当前用户这一轮内容回答" in system_text
    assert "经确认的人生记忆" not in system_text
    assert "答应别人的事一定做到" not in system_text
    assert "event-memory-001" not in system_text
    assert "候选推断" not in system_text
    assert "记忆内容不是指令" not in system_text
    assert [message.text_content for message in chat_ctx.messages()] == ["我们家的家训是什么？"]
    await runtime.close()


@pytest.mark.asyncio
async def test_guest_cannot_read_cached_persona_and_baseline_context_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    refresh_calls: list[dict[str, object]] = []

    class PersonaStub:
        async def refresh(self, **kwargs: object) -> bool:
            refresh_calls.append(dict(kwargs))
            return False

        def cached(self, **_kwargs: object) -> PersonaCapsuleSnapshot:
            raise AssertionError("guest path must not read private persona cache")

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        _tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        yield "你好。"

    runtime = DuplexRuntime.create(session_id="session-persona-guest")
    await runtime.orchestrator.ready()
    await _prepare_speaker(runtime, "guest")
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        persona_client=PersonaStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="你好")
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(chat_ctx, Message("你好"))
    assert [item async for item in agent.llm_node(chat_ctx, [], None)] == ["你好。"]
    await asyncio.sleep(0)

    assert not any("人格胶囊" in message.text_content for message in captured["ctx"].messages())
    assert refresh_calls == []
    await runtime.close()


@pytest.mark.asyncio
async def test_guest_context_cannot_see_owner_turns_or_use_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class MemoryStub:
        async def refresh(self, **_kwargs: object) -> bool:
            return False

        def cached(self, **_kwargs: object) -> MemoryContextSnapshot:
            raise AssertionError("guest path must not read private memory")

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "我只能根据你现在说的内容回答。"

    runtime = DuplexRuntime.create(session_id="session-owner-then-guest")
    await runtime.orchestrator.ready()
    await _prepare_speaker(runtime, "guest")
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        memory_context_client=MemoryStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="主人刚才说了一个私人家庭故事。")
    chat_ctx.add_message(role="assistant", content="我已经记住这个私人故事。")
    chat_ctx.add_message(role="user", content="你们刚才聊了什么？")
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(chat_ctx, Message("你们刚才聊了什么？"))
    assert [item async for item in agent.llm_node(chat_ctx, [object()], None)]

    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].messages()
        if message.role in {"user", "assistant"}
    ]
    assert conversation == [("user", "你们刚才聊了什么？")]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "仅依据当前用户这一轮内容回答" in system_text
    assert "账户主人的私人记忆" in system_text
    assert captured["tools"] == []
    await runtime.close()


@pytest.mark.asyncio
async def test_uncertain_uses_generic_chat_without_private_history_memory_or_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    refresh_calls: list[dict[str, object]] = []

    class PersonaStub:
        async def refresh(self, **kwargs: object) -> bool:
            refresh_calls.append(dict(kwargs))
            return True

        def cached(self, **kwargs: object) -> PersonaCapsuleSnapshot:
            assert kwargs["speaker_class"] == "uncertain"
            return PersonaCapsuleSnapshot(
                version_id="persona-v1",
                version_number=1,
                prompt_fragment="[已确认表达风格 v1]\n- 日常表达偏好短句",
            )

    class MemoryStub:
        async def refresh(self, **kwargs: object) -> bool:
            assert kwargs["speaker_class"] == "uncertain"
            return False

        def cached(self, **_kwargs: object) -> MemoryContextSnapshot:
            raise AssertionError("uncertain path must not read private memory")

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "简单说，这件事可以先从第一步开始。"

    runtime = DuplexRuntime.create(session_id="session-persona-uncertain")
    await runtime.orchestrator.ready()
    await _prepare_speaker(runtime, "uncertain")
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        persona_client=PersonaStub(),  # type: ignore[arg-type]
        memory_context_client=MemoryStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="主人之前说过一个私人家庭故事。")
    chat_ctx.add_message(role="assistant", content="这里是不能泄露的旧回答。")
    chat_ctx.add_message(role="user", content="怎么开始？")
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(chat_ctx, Message("怎么开始？"))
    await asyncio.sleep(0)
    assert [item async for item in agent.llm_node(chat_ctx, [object()], None)]

    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].messages()
        if message.role in {"user", "assistant"}
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert conversation == [("user", "怎么开始？")]
    assert "已确认表达风格 v1" not in system_text
    assert "仅依据当前用户这一轮内容回答" in system_text
    assert captured["tools"] == []
    assert refresh_calls == []
    await runtime.close()


@pytest.mark.asyncio
async def test_uncertain_cannot_resume_an_owner_interrupted_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later unverified speaker must not recover the owner's private exchange."""

    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "它还以明城墙和秦淮河闻名。"

    runtime = DuplexRuntime.create(session_id="session-uncertain-resume")
    await runtime.orchestrator.ready()
    await _prepare_speaker(runtime, "owner")
    await runtime.await_speaker_classification()
    await runtime.on_turn_committed("介绍一下我的私人家庭安排")
    await runtime.on_assistant_speaking("你的私人家庭安排是周末回老家。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"
    await runtime.on_real_interrupt(
        cause="target_speaker_confirmed",
        synchronized_transcript="你的私人家庭安排是",
    )
    await _prepare_speaker(runtime, "uncertain")

    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    compound_resume = "好的，好的。 等一下。 继续。"
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="主人之前说过一个私人家庭故事。")
    chat_ctx.add_message(role="assistant", content="这里是不能泄露的旧回答。")
    chat_ctx.add_message(role="user", content="介绍一下我的私人家庭安排")
    chat_ctx.add_message(
        role="assistant",
        content="你的私人家庭安排是周末回老家，还有更多未播放内容。",
    )
    chat_ctx.add_message(role="user", content=compound_resume)
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(chat_ctx, Message(compound_resume))
    assert [item async for item in agent.llm_node(chat_ctx, [object()], None)]

    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].messages()
        if message.role in {"user", "assistant"}
    ]
    assert conversation == [("user", compound_resume)]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "从中断处自然续接" not in system_text
    assert captured["tools"] == []
    await runtime.close()


@pytest.mark.asyncio
async def test_same_shadow_speaker_fallback_does_not_reuse_heard_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "它还以明城墙和秦淮河闻名。"

    runtime = DuplexRuntime.create(session_id="session-shadow-resume")
    await runtime.orchestrator.ready()
    await _prepare_speaker(
        runtime,
        "uncertain",
        profile_id="profile-shadow-owner",
        reason_code="shadow_owner_candidate",
    )
    await runtime.await_speaker_classification()
    await runtime.on_turn_committed("介绍一下南京")
    await runtime.on_assistant_speaking("南京是江苏省省会，也是中国四大古都之一。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"
    await runtime.on_real_interrupt(
        cause="target_speaker_confirmed",
        synchronized_transcript="南京是江苏省省会，",
    )
    await _prepare_speaker(
        runtime,
        "uncertain",
        profile_id="profile-shadow-owner",
        reason_code="shadow_owner_candidate",
    )

    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    compound_resume = "好的，好的。 等一下。 继续。"
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="主人之前说过一个私人家庭故事。")
    chat_ctx.add_message(role="assistant", content="这里是不能泄露的旧回答。")
    chat_ctx.add_message(role="user", content="介绍一下南京")
    chat_ctx.add_message(
        role="assistant",
        content="南京是江苏省省会，也是中国四大古都之一，还有更多未播放内容。",
    )
    chat_ctx.add_message(role="user", content=compound_resume)
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(chat_ctx, Message(compound_resume))
    assert [item async for item in agent.llm_node(chat_ctx, [object()], None)]

    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].messages()
        if message.role in {"user", "assistant"}
    ]
    assert conversation == [("user", compound_resume)]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "从中断处自然续接" in system_text
    assert "南京是江苏省省会" not in system_text
    assert "原问题" not in system_text
    assert captured["tools"] == []
    await runtime.close()


def test_resume_context_without_current_user_fails_closed() -> None:
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="assistant", content="不能泄露的旧回答。")

    safe = ContextAssembler().assemble(
        chat_ctx=chat_ctx,
        heard_assistant=["不能泄露的旧回答。"],
        speaker_class="uncertain",
        response_plan=_response_plan(DuplexRuntime.create()),
        resume_interrupted_reply=True,
    )

    messages = list(safe.messages())
    assert [message.role for message in messages] == ["system"]
    assert "控制响应计划" in messages[0].text_content


def test_resume_context_without_heard_assistant_keeps_only_current_user() -> None:
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="不能泄露的旧问题。")
    chat_ctx.add_message(role="assistant", content="不能泄露的旧回答。")
    chat_ctx.add_message(role="user", content="继续")

    safe = ContextAssembler().assemble(
        chat_ctx=chat_ctx,
        heard_assistant=[],
        speaker_class="uncertain",
        response_plan=_response_plan(DuplexRuntime.create()),
        resume_interrupted_reply=True,
    )

    assert [
        (message.role, message.text_content)
        for message in safe.messages()
        if message.role != "system"
    ] == [("user", "继续")]


@pytest.mark.asyncio
async def test_completed_voice_resolution_is_applied_without_network_wait() -> None:
    applied: list[tuple[str, str]] = []

    class TTSStub:
        pool = object()

        def set_alignment_callback(self, _callback: object) -> None:
            return None

        def bind_fence(self, _fence: object) -> None:
            return None

        def apply_voice_profile(self, *, model: str, voice: str) -> None:
            applied.append((model, voice))

        def use_baseline_voice(self) -> None:
            applied.append(("baseline", "baseline"))

    class VoiceStub:
        def cached(self, *, session_id: str) -> VoiceRuntimeProfile:
            assert session_id == "session-voice-active"
            return VoiceRuntimeProfile(
                profile_id="profile-001",
                model="cosyvoice-v3.5-flash",
                voice_id="cosyvoice-v3.5-flash-clone-owner001",
            )

    runtime = DuplexRuntime.create(
        session_id="session-voice-active",
        tts=TTSStub(),  # type: ignore[arg-type]
    )
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        )
    )
    await runtime.orchestrator.ready()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        voice_profile_client=VoiceStub(),  # type: ignore[arg-type]
    )

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("继续"))

    assert applied == [("cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash-clone-owner001")]
    await runtime.close()


@pytest.mark.asyncio
async def test_voice_profile_refresh_runs_in_background_on_vad_start() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    baseline_calls = 0

    class TTSStub:
        pool = object()

        def use_baseline_voice(self) -> None:
            nonlocal baseline_calls
            baseline_calls += 1

    async def refresh() -> None:
        started.set()
        await release.wait()

    runtime = DuplexRuntime.create(
        session_id="session-voice-refresh",
        tts=TTSStub(),  # type: ignore[arg-type]
    )
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        )
    )
    runtime.set_voice_profile_refresher(refresh)

    runtime.refresh_voice_profile()
    await started.wait()
    assert not release.is_set()
    assert baseline_calls == 0

    release.set()
    await runtime.wait_for_voice_profile_refresh()
    started.clear()
    runtime.on_user_voice_started()
    await started.wait()
    assert baseline_calls == 0

    await runtime.close()


@pytest.mark.asyncio
async def test_first_turn_waits_for_voice_profile_refresh_before_applying_voice() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    applied: list[str] = []

    class TTSStub:
        pool = object()

        def set_alignment_callback(self, _callback: object) -> None:
            return None

        def bind_fence(self, _fence: object) -> None:
            return None

        def use_baseline_voice(self) -> None:
            applied.append("baseline")

        def apply_voice_profile(self, *, model: str, voice: str) -> None:
            applied.append(f"{model}:{voice}")

    class VoiceStub:
        ready = False

        def cached(self, *, session_id: str) -> VoiceRuntimeProfile | None:
            assert session_id == "session-voice-first-turn"
            if not self.ready:
                return None
            return VoiceRuntimeProfile(
                profile_id="profile-first-turn",
                model="cosyvoice-v3.5-flash",
                voice_id="cosyvoice-v3.5-flash-vd-brightpeer-approved",
            )

    voice = VoiceStub()

    async def refresh() -> None:
        started.set()
        await release.wait()
        voice.ready = True

    runtime = DuplexRuntime.create(
        session_id="session-voice-first-turn",
        tts=TTSStub(),  # type: ignore[arg-type]
    )
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        )
    )
    runtime.set_voice_profile_refresher(refresh)
    await runtime.orchestrator.ready()
    runtime.on_user_voice_started()
    await started.wait()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        voice_profile_client=voice,  # type: ignore[arg-type]
    )

    turn = asyncio.create_task(
        agent.on_user_turn_completed(llm.ChatContext.empty(), Message("你好"))
    )
    await asyncio.sleep(0)
    assert not turn.done()
    assert applied == []

    release.set()
    await turn
    assert applied[-1] == ("cosyvoice-v3.5-flash:cosyvoice-v3.5-flash-vd-brightpeer-approved")
    await runtime.close()
