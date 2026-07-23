from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Literal

import pytest
from livekit.agents import llm
from services.agent.src import agent as agent_mod
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _plan(runtime: DuplexRuntime) -> ResponsePlan:
    return ResponsePlan(
        fence=runtime.fence,
        instructions="按当前控制计划自然回答。",
        direct_text=None,
        epistemic_status="not_applicable",
        epistemic_reason_codes=("test",),
        grounded_items=(),
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion",
            profile_id="warm_companion",
            model="seed-tts-2.0",
        ),
        provenance=ResponseProvenance(
            planner_policy_version="digital-self-response-planner-v1",
            interaction_mode="companion",
            mode_policy_version="policy-2",
            digital_self_version_id=None,
            manifest_sha256=None,
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=None,
            relationship_profile_version=None,
            speaker_class="owner",
            speaker_reason_code="owner_match",
            speaker_profile_id="owner",
            speaker_model_version="test",
            speaker_template_version=1,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


def _policy_with_references() -> ModePolicy:
    return replace(
        ModePolicy.companion_for_test(
            policy_version="policy-2",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        ),
        references=(
            ("digital_self_version_id", "digital-self-1"),
            ("relationship_profile_id", "relationship-1"),
        ),
    )


def _self_preview_policy(*, references: tuple[tuple[str, str | None], ...]) -> ModePolicy:
    return ModePolicy(
        mode="self_preview",
        policy_version="policy-self-preview",
        companion_style_id=None,
        style_version=None,
        references=references,
        capabilities=(),
        companion_style=None,
    )


def _approved_personal_plan(runtime: DuplexRuntime) -> ResponsePlan:
    plan = _plan(runtime)
    return replace(
        plan,
        voice_target=ResponseVoiceTarget(
            kind="approved_personal",
            profile_id="voice-profile-1",
            model="seed-icl-2.0",
        ),
        provenance=replace(
            plan.provenance,
            interaction_mode="self_preview",
            mode_policy_version="policy-self-preview",
            digital_self_version_id="digital-self-1",
            manifest_sha256="a" * 64,
            relationship_profile_id="relationship-1",
            relationship_profile_version=4,
        ),
    )


@pytest.mark.asyncio
async def test_exact_response_plan_is_the_only_system_prompt_and_owner_can_use_tools(
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
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = _plan(runtime)
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), ["tool"], None)] == [
        "我在。"
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].messages() if message.role == "system"
    )
    assert "按当前控制计划自然回答" in system_text
    assert "冻结的陪伴方式" not in system_text
    assert len([message for message in captured["ctx"].messages() if message.role == "system"]) == 1
    assert captured["tools"] == ["tool"]
    await runtime.close()


@pytest.mark.asyncio
async def test_unavailable_policy_cannot_load_private_persona_or_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class PersonaStub:
        def cached(self, **_kwargs: object) -> object:
            raise AssertionError("policy failure must not read persona")

    async def fake_llm_node(
        _agent: Any, safe_ctx: Any, tools: list[Any], _settings: Any
    ) -> AsyncIterator[str]:
        nonlocal called
        called = True
        yield "通用回答。"

    runtime = DuplexRuntime.create(session_id="policy-failed")
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("帮我看看")
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        persona_client=PersonaStub(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), ["tool"], None)] == []
    assert called is False
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


@pytest.mark.asyncio
async def test_policy_mismatched_fetched_plan_downgrades_to_local_safe_plan() -> None:
    runtime = DuplexRuntime.create(session_id="response-plan-policy-fallback")
    runtime.set_mode_policy(_policy_with_references())

    class Message:
        def text_content(self) -> str:
            return "当前问题"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="owner",
            permissions=permissions_for_speaker("owner"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            plan = _plan(runtime)
            return ResponsePlanFetch(
                plan=replace(
                    plan,
                    provenance=replace(plan.provenance, interaction_mode="legacy"),
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

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    cached = agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)]
    assert agent._is_local_safe_plan(cached)
    assert cached.epistemic_reason_codes == ("local_safe_fallback", "mode_policy_mismatch")
    await runtime.close()


def test_plan_policy_validation_rejects_noncanonical_companion_or_voice_target() -> None:
    runtime = DuplexRuntime.create(session_id="strict-companion-plan")
    policy = ModePolicy.companion_for_test(
        policy_version="policy-2",
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=True,
    )
    runtime.set_mode_policy(policy)
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        tts_model="seed-tts-2.0",
    )
    plan = _plan(runtime)

    assert agent._plan_matches_mode_policy(plan, policy)
    assert not agent._plan_matches_mode_policy(
        replace(
            plan,
            provenance=replace(plan.provenance, planner_policy_version="test"),
        ),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, voice_target=replace(plan.voice_target, profile_id="other-voice")),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, voice_target=replace(plan.voice_target, model="other-model")),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, provenance=replace(plan.provenance, persona_style_only=True)),
        policy,
    )
    assert agent._plan_matches_mode_policy(
        replace(
            plan,
            provenance=replace(
                plan.provenance,
                persona_version_id="persona-version-owner",
                persona_version_number=3,
            ),
        ),
        policy,
    )
    assert agent._plan_matches_mode_policy(
        replace(
            plan,
            provenance=replace(
                plan.provenance,
                persona_version_id="persona-version-shadow",
                persona_version_number=4,
                persona_style_only=True,
                speaker_class="uncertain",
                speaker_reason_code="shadow_owner_candidate",
                speaker_profile_id=None,
                source_refs=(),
            ),
        ),
        policy,
    )
    assert agent._plan_matches_mode_policy(
        replace(
            plan,
            provenance=replace(
                plan.provenance,
                speaker_class="guest",
                speaker_reason_code="owner_mismatch",
                speaker_profile_id=None,
            ),
        ),
        policy,
    )


def test_plan_policy_validation_requires_version_and_voice_references_for_personal_mode() -> None:
    runtime = DuplexRuntime.create(session_id="strict-personal-plan")
    references = (
        ("digital_self_version_id", "digital-self-1"),
        ("manifest_sha256", "a" * 64),
        ("relationship_profile_id", "relationship-1"),
        ("relationship_profile_version", "4"),
        ("voice_profile_id", "voice-profile-1"),
        ("voice_model", "seed-icl-2.0"),
        ("voice_speaker_sha256", "a" * 64),
        ("fallback_voice_profile_id", "bright_peer"),
        ("fallback_voice_provider", "volcengine_doubao"),
        ("fallback_voice_model", "seed-tts-2.0"),
        ("fallback_voice_resource_id", "seed-tts-2.0"),
    )
    policy = _self_preview_policy(references=references)
    runtime.set_mode_policy(policy)
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        tts_model="seed-tts-2.0",
    )
    plan = _approved_personal_plan(runtime)

    assert agent._plan_matches_mode_policy(plan, policy)
    assert not agent._plan_matches_mode_policy(
        replace(plan, provenance=replace(plan.provenance, manifest_sha256="b" * 64)),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, provenance=replace(plan.provenance, relationship_profile_version=5)),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        plan,
        _self_preview_policy(
            references=tuple(
                reference
                for reference in references
                if reference[0] not in {"manifest_sha256", "relationship_profile_version"}
            )
        ),
    )
    assert not agent._plan_matches_mode_policy(
        plan,
        _self_preview_policy(
            references=tuple(
                (
                    key,
                    "other-voice-profile" if key == "voice_profile_id" else value,
                )
                for key, value in references
            )
        ),
    )
    assert not agent._plan_matches_mode_policy(
        plan,
        _self_preview_policy(
            references=tuple(
                (
                    key,
                    "other-voice-model" if key == "voice_model" else value,
                )
                for key, value in references
            )
        ),
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, voice_target=replace(plan.voice_target, profile_id=None)),
        policy,
    )
    fallback = replace(
        plan,
        voice_target=ResponseVoiceTarget(
            kind="fallback",
            profile_id="bright_peer",
            model="seed-tts-2.0",
        ),
    )
    assert agent._plan_matches_mode_policy(fallback, policy)
    assert not agent._plan_matches_mode_policy(
        replace(
            fallback,
            provenance=replace(fallback.provenance, manifest_sha256="b" * 64),
        ),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        plan,
        _self_preview_policy(
            references=tuple(
                reference
                for reference in references
                if reference[0] not in {"voice_profile_id", "voice_model"}
            )
        ),
    )


@pytest.mark.parametrize("mode", ["self_preview", "legacy"])
def test_canonical_personal_modes_reject_missing_frozen_digital_self_identity(
    mode: Literal["self_preview", "legacy"],
) -> None:
    runtime = DuplexRuntime.create(session_id=f"strict-{mode}-identity")
    references = (
        ("digital_self_version_id", "digital-self-1"),
        ("manifest_sha256", "a" * 64),
        ("relationship_profile_id", "relationship-1"),
        ("relationship_profile_version", "4"),
        ("voice_profile_id", "voice-profile-1"),
        ("voice_model", "seed-icl-2.0"),
        ("fallback_voice_profile_id", "bright_peer"),
        ("fallback_voice_model", "seed-tts-2.0"),
    )
    policy = replace(
        _self_preview_policy(references=references),
        mode=mode,
        policy_version=f"policy-{mode}",
    )
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        tts_model="seed-tts-2.0",
    )
    plan = replace(
        _approved_personal_plan(runtime),
        provenance=replace(
            _approved_personal_plan(runtime).provenance,
            interaction_mode=mode,
            mode_policy_version=f"policy-{mode}",
        ),
    )

    assert agent._plan_matches_mode_policy(plan, policy)
    assert not agent._plan_matches_mode_policy(
        replace(
            plan,
            provenance=replace(
                plan.provenance,
                digital_self_version_id=None,
                manifest_sha256=None,
            ),
        ),
        policy,
    )
    local_safe = replace(
        plan,
        voice_target=ResponseVoiceTarget(
            kind="fallback",
            profile_id="bright_peer" if mode == "self_preview" else None,
            model="seed-tts-2.0",
        ),
        provenance=replace(
            plan.provenance,
            planner_policy_version="local-safe-fallback-v1",
            digital_self_version_id=None,
            manifest_sha256=None,
            relationship_profile_id=None,
            relationship_profile_version=None,
        ),
    )
    assert agent._plan_matches_mode_policy(local_safe, policy)


def test_local_safe_fallback_requires_the_actual_tts_voice_and_binds_provenance() -> None:
    runtime = DuplexRuntime.create(session_id="strict-fallback-plan")
    policy = ModePolicy.companion_for_test(
        policy_version="policy-2",
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=True,
    )
    runtime.set_mode_policy(policy)
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        tts_model="seed-tts-2.0",
    )
    plan = agent._local_safe_plan(
        fence=runtime.fence,
        speaker=SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="owner",
            permissions=permissions_for_speaker("owner"),
        ),
        reason="planner_unavailable",
    )

    assert agent._plan_matches_mode_policy(plan, policy)
    assert agent._bind_response_plan_provenance(runtime.fence, plan)
    assert runtime.response_provenance_for(runtime.fence) is not None
    assert not agent._plan_matches_mode_policy(
        replace(plan, voice_target=replace(plan.voice_target, profile_id="other-voice")),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, voice_target=replace(plan.voice_target, model="other-model")),
        policy,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provenance_changes", "voice_kind"),
    [
        ({"interaction_mode": "legacy"}, "companion"),
        ({"mode_policy_version": "other-policy"}, "companion"),
        ({"digital_self_version_id": "other-self"}, "companion"),
        ({"relationship_profile_id": "other-relationship"}, "companion"),
        ({}, "approved_personal"),
    ],
    ids=(
        "policy-mismatch",
        "version-mismatch",
        "digital-self-reference-mismatch",
        "relationship-reference-mismatch",
        "voice-mismatch",
    ),
)
async def test_llm_node_rejects_plan_inconsistent_with_frozen_mode_policy(
    monkeypatch: pytest.MonkeyPatch,
    provenance_changes: dict[str, str],
    voice_kind: str,
) -> None:
    called = False

    async def fake_llm_node(*_args: Any) -> AsyncIterator[str]:
        nonlocal called
        called = True
        yield "不应生成"

    runtime = DuplexRuntime.create(session_id="response-plan-policy-check")
    runtime.set_mode_policy(_policy_with_references())
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("当前问题")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    plan = _plan(runtime)
    provenance_values = {
        "digital_self_version_id": "digital-self-1",
        "relationship_profile_id": "relationship-1",
    }
    provenance_values.update(provenance_changes)
    plan = replace(
        plan,
        voice_target=replace(plan.voice_target, kind=voice_kind),  # type: ignore[arg-type]
        provenance=replace(plan.provenance, **provenance_values),
    )
    agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)] = plan
    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), ["tool"], None)] == []
    assert called is False
    await runtime.close()
