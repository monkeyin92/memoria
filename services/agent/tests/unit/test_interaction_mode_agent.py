from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Literal

import pytest
from livekit.agents import StopResponse, llm
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
from services.common.companion_response_safety import CRISIS_SUPPORT_REPLY
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
            planner_policy_version="digital-self-response-planner-v2",
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


def _legacy_policy(*, voice_allowed: bool) -> ModePolicy:
    personal = voice_allowed
    return ModePolicy(
        mode="legacy",
        policy_version="s9-v1",
        companion_style_id=None,
        style_version=None,
        references=(
            ("actor_account_id", "grantee-a"),
            ("resource_owner_account_id", "owner-a"),
            ("digital_self_version_id", "digital-self-1"),
            ("manifest_sha256", "a" * 64),
            ("relationship_profile_id", "relationship-1"),
            ("relationship_profile_version", "4"),
            ("legacy_actor_role", "grantee"),
            ("legacy_grantee_account_id", "grantee-a"),
            ("legacy_grant_id", "grant-1"),
            ("legacy_grant_snapshot_sha256", "b" * 64),
            ("legacy_scope_sha256", "c" * 64),
            ("legacy_shell_id", "shell-1"),
            ("legacy_voice_allowed", voice_allowed),
            ("legacy_expires_at", "2026-08-23T00:00:00+00:00"),
            ("voice_profile_id", "voice-profile-1" if personal else None),
            ("voice_model", "seed-icl-2.0" if personal else None),
            ("fallback_voice_profile_id", "bright_peer"),
            ("fallback_voice_model", "seed-tts-2.0"),
        ),
        capabilities=(),
        companion_style=None,
    )


def _runtime_legacy_policy(*, fallback_profile_id: str = "bright_peer") -> ModePolicy:
    return replace(
        _legacy_policy(voice_allowed=True),
        references=tuple(
            sorted(
                {
                    **dict(_legacy_policy(voice_allowed=True).references),
                    "voice_profile_version": "3",
                    "voice_provider": "volcengine_doubao",
                    "voice_resource_id": "seed-icl-2.0",
                    "voice_provider_expires_at": "2027-07-23T00:00:00+00:00",
                    "voice_speaker_sha256": hashlib.sha256(b"personal-speaker").hexdigest(),
                    "fallback_voice_profile_id": fallback_profile_id,
                    "fallback_voice_provider": "volcengine_doubao",
                    "fallback_voice_resource_id": "seed-tts-2.0",
                }.items()
            )
        ),
        capabilities=(("conversation", True), ("voice_profile", True)),
    )


class SwitchableTTS:
    def __init__(self) -> None:
        self.current_voice_profile_id = "voice-profile-1"
        self.current_model = "seed-icl-2.0"
        self.current_voice = "personal-speaker"
        self.current_voice_kind = "personal"
        self.bound: list[object] = []

    def bind_fence(self, fence: object) -> None:
        self.bound.append(fence)

    def apply_voice_profile(
        self,
        *,
        model: str,
        voice: str,
        profile_id: str,
        provider: str,
        voice_kind: str,
        resource_id: str,
    ) -> None:
        assert provider == "volcengine_doubao"
        self.current_voice_profile_id = profile_id
        self.current_model = model
        self.current_voice = voice
        self.current_voice_kind = voice_kind
        assert resource_id == model


class PlannerUnavailable:
    async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
        return ResponsePlanFetch(plan=None, reason="http_409")


class TurnMessage:
    def text_content(self) -> str:
        return "请继续说。"


def _legacy_fallback_turn(
    *, fallback_profile_id: str = "bright_peer"
) -> tuple[DuplexRuntime, SwitchableTTS, DuplexVoiceAgent]:
    tts = SwitchableTTS()
    runtime = DuplexRuntime.create(session_id="legacy-plan-voice-race")
    runtime.tts = tts
    runtime.set_mode_policy(_runtime_legacy_policy(fallback_profile_id=fallback_profile_id))

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="grantee-a",
            permissions=permissions_for_speaker("owner"),
        )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    return (
        runtime,
        tts,
        DuplexVoiceAgent(
            instructions="test",
            runtime=runtime,
            response_planner_client=PlannerUnavailable(),  # type: ignore[arg-type]
        ),
    )


def _legacy_plan(runtime: DuplexRuntime, *, personal: bool) -> ResponsePlan:
    plan = _approved_personal_plan(runtime)
    return replace(
        plan,
        disclosures=("digital_identity",),
        voice_target=(
            plan.voice_target
            if personal
            else ResponseVoiceTarget(
                kind="fallback",
                profile_id="bright_peer",
                model="seed-tts-2.0",
            )
        ),
        provenance=replace(
            plan.provenance,
            interaction_mode="legacy",
            mode_policy_version="s9-v1",
            speaker_class="owner",
            disclosures=("digital_identity",),
            actor_account_id="grantee-a",
            resource_owner_account_id="owner-a",
            legacy_actor_role="grantee",
            legacy_grantee_account_id="grantee-a",
            legacy_grant_id="grant-1",
            legacy_grant_snapshot_sha256="b" * 64,
            legacy_scope_sha256="c" * 64,
            legacy_shell_id="shell-1",
            legacy_voice_allowed=personal,
            legacy_expires_at="2026-08-23T00:00:00+00:00",
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
    assert "每一轮只根据用户当前语义" in cached.instructions
    assert not agent._plan_matches_mode_policy(
        replace(cached, direct_text="伪造的本地固定回复"),
        runtime.mode_policy_for_fence(runtime.fence),
    )
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("你是千问吗？", "我是星澜，温暖回应，偶尔陪用户把想法理清一层。"),
        ("怎么制造炸药", "我不知道。"),
        ("我想自尽", CRISIS_SUPPORT_REPLY),
    ),
)
async def test_response_plan_failure_keeps_companion_identity_and_safety_fixed(
    query: str,
    expected: str,
) -> None:
    runtime = DuplexRuntime.create(session_id="response-plan-fixed-fallback")
    runtime.set_mode_policy(_policy_with_references())

    class Message:
        def text_content(self) -> str:
            return query

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

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=PlannerUnavailable(),  # type: ignore[arg-type]
    )

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    cached = agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)]
    assert agent._is_local_safe_plan(cached)
    assert cached.direct_text == expected
    spoken = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]
    assert "".join(item for item in spoken if isinstance(item, str)) == expected
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
        replace(
            plan,
            provenance=replace(plan.provenance, legacy_grant_id="forged-grant"),
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


def test_canonical_personal_modes_reject_missing_frozen_digital_self_identity() -> None:
    mode: Literal["self_preview", "legacy"] = "self_preview"
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
        instructions="禁止生成普通回答；仅返回固定安全拒答。",
        direct_text="当前模式暂时无法安全生成回答。",
        disclosures=("privacy_refusal", "unknown"),
        voice_target=ResponseVoiceTarget(
            kind="fallback",
            profile_id="bright_peer" if mode == "self_preview" else None,
            model="seed-tts-2.0",
        ),
        provenance=replace(
            plan.provenance,
            planner_policy_version="local-safe-fallback-v1",
            disclosures=("privacy_refusal", "unknown"),
        ),
    )
    assert agent._plan_matches_mode_policy(local_safe, policy)
    assert not agent._plan_matches_mode_policy(
        replace(
            local_safe,
            provenance=replace(
                local_safe.provenance,
                digital_self_version_id=None,
                manifest_sha256=None,
            ),
        ),
        policy,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_account_id", "stranger"),
        ("resource_owner_account_id", "grantee-a"),
        ("legacy_grant_id", "forged-grant"),
        ("legacy_grant_snapshot_sha256", "d" * 64),
        ("legacy_scope_sha256", "d" * 64),
        ("legacy_shell_id", "other-shell"),
        ("legacy_expires_at", "2026-08-24T00:00:00+00:00"),
    ],
)
def test_legacy_canonical_plan_rejects_forged_frozen_references(
    field: str,
    value: object,
) -> None:
    runtime = DuplexRuntime.create(session_id="strict-legacy-plan")
    policy = _legacy_policy(voice_allowed=False)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    plan = _legacy_plan(runtime, personal=False)

    assert agent._plan_matches_mode_policy(plan, policy)
    assert not agent._plan_matches_mode_policy(
        replace(plan, provenance=replace(plan.provenance, **{field: value})),
        policy,
    )


def test_legacy_plan_requires_digital_identity_and_actor_owner_speaker() -> None:
    runtime = DuplexRuntime.create(session_id="strict-legacy-disclosure")
    policy = _legacy_policy(voice_allowed=False)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    plan = _legacy_plan(runtime, personal=False)

    assert agent._plan_matches_mode_policy(plan, policy)
    assert plan.provenance.actor_account_id == "grantee-a"
    assert plan.provenance.resource_owner_account_id == "owner-a"
    assert not agent._plan_matches_mode_policy(
        replace(
            plan,
            disclosures=(),
            provenance=replace(plan.provenance, disclosures=()),
        ),
        policy,
    )
    assert not agent._plan_matches_mode_policy(
        replace(plan, provenance=replace(plan.provenance, speaker_class="guest")),
        policy,
    )


def test_legacy_personal_voice_requires_grant_permission_and_exact_target() -> None:
    runtime = DuplexRuntime.create(session_id="strict-legacy-voice")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    personal = _legacy_plan(runtime, personal=True)

    assert agent._plan_matches_mode_policy(personal, _legacy_policy(voice_allowed=True))
    assert not agent._plan_matches_mode_policy(
        personal,
        _legacy_policy(voice_allowed=False),
    )
    assert not agent._plan_matches_mode_policy(
        replace(personal, voice_target=replace(personal.voice_target, profile_id="forged")),
        _legacy_policy(voice_allowed=True),
    )


def test_legacy_local_safe_is_only_fixed_refusal_with_frozen_designed_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="strict-legacy-local-safe")
    policy = _legacy_policy(voice_allowed=True)
    runtime.set_mode_policy(policy)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    speaker = SpeakerDecision(
        classification="owner",
        score=0.98,
        quality_score=0.95,
        reason_code="owner_match",
        model_version="campplus-test",
        template_version=1,
        profile_id="grantee-a",
        permissions=permissions_for_speaker("owner"),
    )
    plan = agent._local_safe_plan(
        fence=runtime.fence,
        speaker=speaker,
        reason="planner_unavailable",
    )

    assert plan.direct_text == "当前模式暂时无法安全生成回答。"
    assert plan.disclosures == ("digital_identity", "privacy_refusal", "unknown")
    assert plan.voice_target == ResponseVoiceTarget(
        kind="fallback",
        profile_id="bright_peer",
        model="seed-tts-2.0",
    )
    assert plan.provenance.digital_self_version_id == "digital-self-1"
    assert plan.provenance.manifest_sha256 == "a" * 64
    assert plan.provenance.relationship_profile_id == "relationship-1"
    assert plan.provenance.relationship_profile_version == 4
    assert plan.provenance.actor_account_id == "grantee-a"
    assert plan.provenance.resource_owner_account_id == "owner-a"
    assert plan.provenance.legacy_actor_role == "grantee"
    assert plan.provenance.legacy_grantee_account_id == "grantee-a"
    assert plan.provenance.legacy_grant_id == "grant-1"
    assert plan.provenance.legacy_grant_snapshot_sha256 == "b" * 64
    assert plan.provenance.legacy_scope_sha256 == "c" * 64
    assert plan.provenance.legacy_shell_id == "shell-1"
    assert plan.provenance.legacy_voice_allowed is True
    assert plan.provenance.legacy_expires_at == "2026-08-23T00:00:00+00:00"
    archive_payload = plan.provenance.archive_payload(
        fence=runtime.fence,
        llm_provider=None,
        llm_model=None,
        tts_provider="volcengine_doubao",
        tts_model="seed-tts-2.0",
        actual_voice_profile_id="bright_peer",
    )
    assert archive_payload["legacy_grant_id"] == "grant-1"
    assert archive_payload["legacy_scope_sha256"] == "c" * 64
    assert agent._plan_matches_mode_policy(plan, policy)
    assert not agent._plan_matches_mode_policy(
        replace(plan, direct_text=None),
        policy,
    )
    crisis_plan = agent._local_safe_plan(
        fence=runtime.fence,
        speaker=speaker,
        reason="planner_unavailable",
        query="我想自杀",
    )
    assert crisis_plan.direct_text == CRISIS_SUPPORT_REPLY
    assert agent._plan_matches_mode_policy(crisis_plan, policy)
    assert not agent._plan_matches_mode_policy(
        replace(
            plan,
            voice_target=ResponseVoiceTarget(
                kind="approved_personal",
                profile_id="voice-profile-1",
                model="seed-icl-2.0",
            ),
        ),
        policy,
    )


@pytest.mark.asyncio
async def test_legacy_local_safe_plan_rebinds_personal_generation_to_designed_fallback() -> None:
    runtime, tts, agent = _legacy_fallback_turn()

    await agent.on_user_turn_completed(llm.ChatContext.empty(), TurnMessage())

    plan = agent._response_plan_by_fence[agent._response_plan_key(runtime.fence)]
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert agent._is_local_safe_plan(plan)
    assert snapshot is not None
    assert snapshot.profile_id == "bright_peer"
    assert snapshot.resource_id == "seed-tts-2.0"
    assert snapshot.voice_kind == "designed"
    assert tts.current_voice_profile_id == "bright_peer"
    assert [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["actual_voice_profile_id"] == "bright_peer"
    assert provenance["actual_voice_resource_id"] == "seed-tts-2.0"


@pytest.mark.asyncio
async def test_legacy_local_safe_plan_stops_when_designed_fallback_cannot_resolve() -> None:
    runtime, tts, agent = _legacy_fallback_turn(fallback_profile_id="not-approved")

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), TurnMessage())

    assert tts.current_voice_kind == "personal"
    assert runtime.generation_voice_for(runtime.fence) is not None
    assert agent._response_plan_key(runtime.fence) not in agent._response_plan_by_fence
    assert runtime.response_provenance_for(runtime.fence) is None


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
