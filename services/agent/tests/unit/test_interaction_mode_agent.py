from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from livekit.agents import llm
from services.agent.src import agent as agent_mod
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.persona_client import PersonaCapsuleSnapshot


@pytest.mark.asyncio
async def test_companion_style_is_a_low_priority_prompt_and_owner_can_use_authorized_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any, safe_ctx: Any, tools: list[Any], _settings: Any
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        captured["tools"] = tools
        yield "我在。"

    runtime = DuplexRuntime.create(session_id="companion-style")
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="policy-2",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        )
    )
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("今天有点累")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    agent._current_speaker_class = "owner"
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), ["tool"], None)] == [
        "我在。"
    ]
    system_text = "\n".join(
        message.text_content
        for message in captured["ctx"].messages()
        if message.role == "system"
    )
    assert "冻结的陪伴方式" in system_text
    assert "低于事实、安全和用户当前指令" in system_text
    assert captured["tools"] == ["tool"]
    await runtime.close()


@pytest.mark.asyncio
async def test_unavailable_policy_cannot_load_private_persona_or_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class PersonaStub:
        def cached(self, **_kwargs: object) -> PersonaCapsuleSnapshot:
            raise AssertionError("policy failure must not read persona")

    async def fake_llm_node(
        _agent: Any, safe_ctx: Any, tools: list[Any], _settings: Any
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        captured["tools"] = tools
        yield "通用回答。"

    runtime = DuplexRuntime.create(session_id="policy-failed")
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("帮我看看")
    agent = DuplexVoiceAgent(
        instructions="test", runtime=runtime, persona_client=PersonaStub()  # type: ignore[arg-type]
    )
    agent._current_speaker_class = "owner"
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), ["tool"], None)] == [
        "通用回答。"
    ]
    assert captured["tools"] == []
    assert not any(
        "人格胶囊" in message.text_content for message in captured["ctx"].messages()
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_explicitly_failed_policy_blocks_llm_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_llm_node(
        _agent: Any, _safe_ctx: Any, _tools: list[Any], _settings: Any
    ) -> AsyncIterator[str]:
        nonlocal called
        called = True
        yield "不应生成"

    runtime = DuplexRuntime.create(session_id="policy-explicitly-failed")
    runtime.set_mode_policy(ModePolicy.unavailable("fetch_failed"))
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)] == []
    assert called is False
    await runtime.close()
