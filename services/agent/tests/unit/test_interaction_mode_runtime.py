from __future__ import annotations

import asyncio

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _companion_policy() -> ModePolicy:
    return ModePolicy.companion_for_test(
        policy_version="mode-policy-3",
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=True,
    )


def _decision(classification: str, *, reason_code: str | None = None) -> SpeakerDecision:
    return SpeakerDecision(
        classification=classification,  # type: ignore[arg-type]
        score=0.95 if classification == "owner" else 0.1,
        quality_score=0.9,
        reason_code=reason_code or ("owner_match" if classification == "owner" else "uncertain"),
        model_version="campplus-test",
        template_version=1,
        profile_id="profile-001",
        permissions=permissions_for_speaker(classification),  # type: ignore[arg-type]
    )


async def _set_speaker(runtime: DuplexRuntime, classification: str, *, reason_code: str | None = None) -> None:
    async def classify(_: bytes, __: int) -> SpeakerDecision:
        return _decision(classification, reason_code=reason_code)

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00")
    runtime.on_user_voice_stopped()
    await runtime.await_speaker_classification()


@pytest.mark.asyncio
async def test_policy_provenance_is_frozen_per_fence_and_owner_evidence_is_server_gated() -> None:
    runtime = DuplexRuntime.create(session_id="session-policy")
    runtime.set_mode_policy(_companion_policy())
    await runtime.orchestrator.ready()
    await _set_speaker(runtime, "owner")
    accepted, _ = runtime.accept_user_turn("这是一个足够长的主人真实表达。")
    assert accepted
    fence = await runtime.on_turn_committed("这是一个足够长的主人真实表达。")
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(
        speaker="user", text="这是一个足够长的主人真实表达。", final=True, fence=fence
    )
    await asyncio.sleep(0)

    assert published[0]["payload"] == {
        "text": "这是一个足够长的主人真实表达。",
        "persona_eligible": True,
        "interaction_mode": "companion",
        "mode_policy_version": "mode-policy-3",
        "simulated_output": False,
        "history_eligible": True,
        "owner_projection_eligible": True,
        "speaker_reason_code": "owner_match",
        "speaker_profile_id": "profile-001",
        "speaker_quality_score": 0.9,
        "speaker_model_version": "campplus-test",
        "speaker_template_version": 1,
    }
    assert runtime._history_eligible(fence) is True
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classification", "reason_code"),
    [
        ("guest", "owner_mismatch"),
        ("uncertain", "uncertain"),
        ("uncertain", "shadow_ambiguous_candidate"),
    ],
)
async def test_guest_uncertain_and_shadow_cannot_become_owner_projection(
    classification: str, reason_code: str
) -> None:
    runtime = DuplexRuntime.create(session_id=f"session-{classification}-{reason_code}")
    runtime.set_mode_policy(_companion_policy())
    await runtime.orchestrator.ready()
    await _set_speaker(runtime, classification, reason_code=reason_code)
    accepted, _ = runtime.accept_user_turn("这是当前说话人的一句完整表达。")
    assert accepted
    fence = await runtime.on_turn_committed("这是当前说话人的一句完整表达。")

    assert runtime._history_eligible(fence) is False
    policy = runtime.mode_policy_for_fence(fence)
    assert policy.allows_private_context(classification) is False
    assert policy.allows_tools(classification) is False
    assert policy.owner_projection_eligible(classification) is False
    assert policy.allows_low_sensitivity_persona(is_shadow=reason_code == "shadow_owner_candidate") is (
        reason_code == "shadow_owner_candidate"
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_shadow_owner_candidate_keeps_history_and_low_sensitivity_learning_without_owner_authority() -> None:
    runtime = DuplexRuntime.create(session_id="session-shadow-owner")
    runtime.set_mode_policy(_companion_policy())
    await runtime.orchestrator.ready()
    await _set_speaker(runtime, "uncertain", reason_code="shadow_owner_candidate")
    accepted, _ = runtime.accept_user_turn("这是主人在本轮留下的完整表达。")
    assert accepted
    fence = await runtime.on_turn_committed("这是主人在本轮留下的完整表达。")

    assert runtime._history_eligible(fence) is True
    policy = runtime.mode_policy_for_fence(fence)
    assert policy.history_eligible(
        "uncertain", reason_code="shadow_owner_candidate"
    ) is True
    assert policy.owner_projection_eligible("uncertain") is False
    assert policy.allows_learning(
        "uncertain", reason_code="shadow_owner_candidate"
    ) is True
    assert policy.allows_private_context("uncertain") is False
    assert policy.allows_tools("uncertain") is False
    await runtime.close()


def test_shadow_owner_reason_cannot_upgrade_a_guest_classification() -> None:
    policy = _companion_policy()

    assert policy.history_eligible(
        "guest", reason_code="shadow_owner_candidate"
    ) is False
    assert policy.owner_projection_eligible("guest") is False
    assert policy.allows_learning(
        "guest", reason_code="shadow_owner_candidate"
    ) is False


@pytest.mark.asyncio
async def test_unavailable_or_simulated_mode_never_writes_owner_evidence_or_history() -> None:
    runtime = DuplexRuntime.create(session_id="session-closed")
    runtime.set_mode_policy(ModePolicy.unavailable("policy_missing"))
    await runtime.orchestrator.ready()
    await _set_speaker(runtime, "owner")
    accepted, _ = runtime.accept_user_turn("这是不会成为主人证据的一段表达。")
    assert accepted is False
    assert runtime.orchestrator.metrics.get(
        "guarded_user_input_total",
        {"reason": "interaction_mode_blocked"},
    ) == 1

    assert runtime.fence.turn_id == 0
    await runtime.close()
