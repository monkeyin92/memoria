"""Production prompt wiring and RuntimeProfile gate contract tests.

Covers: real consumer invocation, cascade/Omni symmetry, unknown_safe
fail-closed behaviour, subject-switch invalidation (PR-08), P0-1 verified-type
injection, P0-2 session binding and same-epoch identity rules, P0-3 epoch-keyed
permission caches, P0-5 revoked/expired sinks and P0-6 startup/refresh seams.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from services.agent.src import agent as agent_module
from services.agent.src import media_agent_factory as factory_module
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.prompt_composition import compose_production_prompt
from services.agent.src.runtime_profile import (
    VERIFY_KEY_ENV,
    RuntimeProfile,
    parse_runtime_profile,
)
from services.agent.src.transactional_effect_commit import (
    CommitReceipt,
    CommitReconcileResult,
    CommitReconcileState,
    EffectAuthorization,
    PreparedToolEffect,
    fence_fingerprint,
)
from services.agent.src.tutor_session import production_system_prompt
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    UNKNOWN_SAFE_OBLIGATIONS,
    canonical_wire_payload,
    install_receipt_verifier,
)
from services.common.companions import designed_voice_speaker_sha256
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _speaker_decision() -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.98,
        quality_score=0.95,
        reason_code="owner_match",
        model_version="campplus-test",
        template_version=1,
        profile_id="owner-profile",
        permissions=permissions_for_speaker("owner"),
    )


def _profile(
    epoch: int,
    *,
    subject: str = "person_child",
    mode: str = "student_minor",
    session_id: str = "ses_prompt_wiring",
) -> object:
    is_child = subject == "person_child"
    return canonical_wire_payload(
        runtime_profile_id=f"rp_epoch_{epoch}",
        session_id=session_id,
        active_subject_id=subject,
        subject_category="minor" if is_child else "adult",
        age_band="under_14" if is_child else "adult",
        service_mode=mode,
        session_epoch=epoch,
        capabilities=["chat", "tutor", "english_practice", "memory_recall_private"],
    )


def _policy(profile: object) -> ModePolicy:
    base = ModePolicy.companion_for_test(
        policy_version="test-policy",
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=False,
        session_focus="chat",
    )
    if profile is None:
        return replace(base, runtime_profile=None)
    parsed = parse_runtime_profile(profile, verify_key=TEST_VERIFY_KEY)
    assert parsed is not None
    return replace(base, runtime_profile=parsed)


def _runtime(monkeypatch: pytest.MonkeyPatch) -> DuplexRuntime:
    monkeypatch.setenv(VERIFY_KEY_ENV, TEST_VERIFY_KEY)
    runtime = DuplexRuntime.create(session_id="ses_prompt_wiring", device_id="dev_01J_test")
    install_receipt_verifier(runtime)

    async def _static_refresh() -> object:
        return runtime.orchestrator.runtime_profiles.current

    runtime.set_runtime_profile_refresher(_static_refresh)
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        install_playback_stop_seam,
    )

    install_playback_stop_seam(runtime)
    return runtime


def test_production_prompt_is_the_shared_seam_for_cascade_and_omni() -> None:
    """Both production call sites consume the exact same function object."""

    assert agent_module.production_system_prompt is production_system_prompt
    assert factory_module.production_system_prompt is production_system_prompt


def test_real_consumer_invocation_with_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    prompt = production_system_prompt(runtime)
    assert "【不可变安全底线】" in prompt
    assert "当前是儿童/学生学习陪伴模式" in prompt
    assert "儿童/学生" in prompt
    assert "身份已确认" in prompt
    assert "星澜" in prompt  # persona from the signed profile
    assert "不得自称或讨论" not in prompt
    assert "如实说明你是由人工智能驱动的机器人伙伴" in prompt


def test_real_consumer_invocation_without_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(None))
    prompt = production_system_prompt(runtime)
    assert "当前是安全模式" in prompt
    assert "不写入长期记忆" in prompt
    assert "未确认" in prompt
    assert "私人记忆" not in prompt


def test_session_epoch_is_frozen_into_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=7)))
    gate = runtime.orchestrator.runtime_profiles
    assert runtime.fence.session_epoch == 7
    assert gate.current is not None
    assert gate.current.profile.session_epoch == 7
    assert gate.for_fence(runtime.fence, current_fence=runtime.fence) is not None


def test_bare_runtime_profile_injection_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: a plain dataclass is never accepted as a verification proof."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    forged = RuntimeProfile(
        runtime_profile_id="forged",
        actor_id="a",
        binding_id="b",
        binding_version=1,
        subject_revision=1,
        active_subject_id="person_child",
        subject_category="minor",
        age_band="under_14",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        service_mode="student_minor",
        persona_assignment_id="starlight:v4",
        persona_id="starlight",
        persona_version=4,
        relationship_stage="familiar",
        policy_bundle_version="v5",
        policy_receipt_ids=(),
        session_epoch=1,
        issued_at=datetime(2098, 1, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        session_id="ses_prompt_wiring",
        device_id="dev_01J_test",
    )
    assert gate.apply(forged, runtime.fence) is None
    assert gate.current is None
    assert runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "unknown"}) == 1


def test_wrong_session_profile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-2: a validly signed profile for another session cannot be replayed."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    other_session = _profile(epoch=1, session_id="ses_other")
    assert gate.apply(other_session, runtime.fence) is None
    assert gate.current is None
    assert runtime.fence.session_epoch == 0


def test_same_epoch_different_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-2: same-epoch identity/permission change never replaces the subject."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    assert gate.current is not None
    different = _profile(epoch=1, subject="person_parent", mode="adult_companion")
    assert gate.apply(different, runtime.fence) is None
    assert gate.current is None  # fail closed, never swapped
    assert runtime.fence.session_epoch == 1
    assert gate.for_fence(runtime.fence, current_fence=runtime.fence) is None


def test_idempotent_same_epoch_replay_refreshes_without_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=2)))
    replayed = _profile(epoch=2)
    refreshed = gate.apply(replayed, runtime.fence)
    assert refreshed is not None
    assert gate.current is not None
    assert runtime.fence.session_epoch == 2


def test_subject_switch_invalidates_old_prompt_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    old_fence = runtime.fence
    old_prompt = production_system_prompt(runtime)
    assert "儿童/学生" in old_prompt

    assert runtime.orchestrator.fence_gate.accept(old_fence, source="test") is True

    gate.apply(
        _profile(epoch=2, subject="person_parent", mode="adult_companion"),
        runtime.fence,
    )
    new_fence = runtime.fence
    assert new_fence.session_epoch == 2
    assert not new_fence.matches(old_fence)
    assert runtime.orchestrator.fence_gate.accept(old_fence, source="test") is False
    assert gate.for_fence(old_fence, current_fence=runtime.fence) is None
    old_composed = compose_production_prompt(
        profile=gate.for_fence(old_fence, current_fence=runtime.fence),
        memory_block="owner-private",
    )
    assert old_composed.service_mode == "unknown_safe"
    assert "owner-private" not in old_composed.system

    new_prompt = production_system_prompt(runtime)
    assert "当前是成人个人陪伴模式" in new_prompt
    assert "儿童/学生" not in new_prompt


@pytest.mark.asyncio
async def test_epoch_keyed_permission_caches_never_hit_old_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-3: old-subject policy/history/voice lookups miss after the switch."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    runtime.authenticate_text_owner()

    async def _commit() -> None:
        await runtime.on_turn_committed("第一条。", input_modality="text")

    await _commit()
    old_fence = runtime.fence
    assert runtime.mode_policy_for_fence(old_fence).available
    assert runtime._history_eligible(old_fence) is True
    assert runtime._owner_projection_eligible(old_fence) is True
    assert runtime.bind_response_provenance(old_fence, {"planner": "p1"})
    assert runtime.bind_generation_voice(
        old_fence,
        profile_id="warm_companion",
        resource_id="seed-tts-2.0",
        speaker_sha256=designed_voice_speaker_sha256("warm_companion") or "",
        voice_kind="designed",
    )

    gate.apply(
        _profile(epoch=2, subject="person_parent", mode="adult_companion"),
        runtime.fence,
    )
    # The new epoch keeps the same turn/generation numbers: every lookup for
    # that identity context must miss the old subject's values (P0-3).
    new_fence = runtime.fence
    assert new_fence.turn_id == old_fence.turn_id
    assert new_fence.generation_id == old_fence.generation_id
    assert not runtime.mode_policy_for_fence(new_fence).available
    assert runtime._history_eligible(new_fence) is False
    assert runtime._owner_projection_eligible(new_fence) is False
    assert runtime.response_provenance_for(new_fence) is None
    assert runtime.generation_voice_for(new_fence) is None
    assert runtime.input_modality_for_fence(new_fence) == "audio"


@pytest.mark.asyncio
async def test_working_context_is_cleared_on_subject_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit() -> None:
        await runtime.on_turn_committed("这是我的私人安排。", input_modality="text")

    await _commit()
    assert runtime.orchestrator.context.turns
    gate.apply(
        _profile(epoch=2, subject="person_parent", mode="adult_companion"),
        runtime.fence,
    )
    assert runtime.orchestrator.context.turns == []


def test_stale_lower_epoch_profile_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=3)))
    applied = gate.apply(_profile(epoch=1), runtime.fence)
    assert applied is None
    assert runtime.fence.session_epoch == 3
    assert gate.current is not None
    assert gate.current.profile.session_epoch == 3


def test_expired_profile_binding_is_unknown_safe_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    expired = _profile(epoch=1)
    expired["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    from services.agent.tests.unit.runtime_profile_test_helpers import sign_wire_payload

    expired["signature"] = sign_wire_payload(expired, TEST_VERIFY_KEY)
    gate = runtime.orchestrator.runtime_profiles
    assert gate.apply(expired, runtime.fence) is None
    assert gate.current is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_expired_use_attempts_total", {"reason": "expired_at_parse"}
        )
        == 1
    )
    assert runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "unknown"}) == 1


@pytest.mark.asyncio
async def test_profile_expiry_without_epoch_change_stops_real_sinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-5: expired profile (same epoch) stops LLM/TTS/tool/history sinks."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit() -> None:
        await runtime.on_turn_committed("先记录一下。", input_modality="text")

    await _commit()
    fence = runtime.fence
    assert runtime.gate_llm_token(fence, "token") == "token"
    assert runtime.gate_tts_audio(fence, b"pcm") == b"pcm"
    assert runtime.gate_tool_result(fence, {"ok": True}) == {"ok": True}

    gate.clock = lambda: datetime(2100, 1, 1, tzinfo=UTC)
    assert gate.for_fence(fence, current_fence=runtime.fence) is None
    assert runtime.gate_llm_token(fence, "token") is None
    assert runtime.gate_tts_audio(fence, b"pcm") is None
    assert runtime.gate_tool_result(fence, {"ok": True}) is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_expired_use_attempts_total", {"reason": "expired_at_use"}
        )
        == 1
    )


@pytest.mark.asyncio
async def test_late_tool_tts_and_context_results_are_rejected_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-3: session-epoch switch drops in-flight LLM/TTS/tool results."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit() -> None:
        await runtime.on_turn_committed("先记录一下。", input_modality="text")

    await _commit()
    fence = runtime.fence
    assert runtime.gate_llm_token(fence, "token") == "token"
    assert runtime.gate_tts_audio(fence, b"pcm") == b"pcm"
    assert runtime.gate_tool_result(fence, {"ok": True}) == {"ok": True}

    gate.apply(
        _profile(epoch=2, subject="person_parent", mode="adult_companion"),
        runtime.fence,
    )
    assert runtime.gate_llm_token(fence, "token") is None
    assert runtime.gate_tts_audio(fence, b"pcm") is None
    assert runtime.gate_tool_result(fence, {"ok": True}) is None
    assert runtime.orchestrator.fence_gate.accept(fence, source="context") is False


@pytest.mark.asyncio
async def test_mid_session_refresh_is_a_first_class_consumer_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-6: signed-wire apply works at session start AND subject switch."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    # Startup seam: policy carries the verified profile.
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    assert gate.current is not None

    async def _commit() -> None:
        await runtime.on_turn_committed("你好。", input_modality="text")

    await _commit()
    # Refresh seam: same fingerprint replay at the same epoch (re-sign).
    refreshed = gate.apply(_profile(epoch=1), runtime.fence)
    assert refreshed is not None
    # Switch seam: higher epoch mid-session.
    switched = gate.apply(
        _profile(epoch=2, subject="person_parent", mode="adult_companion"),
        runtime.fence,
    )
    assert switched is not None
    assert runtime.fence.session_epoch == 2


def test_unknown_safe_profile_counts_unknown_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: only a confirmed non-safe profile counts as confirmed resolution."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    unknown_safe = canonical_wire_payload(
        session_id="ses_prompt_wiring",
        active_subject_id=None,
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        service_mode="unknown_safe",
        session_epoch=1,
        capabilities=["chat"],
        obligations=UNKNOWN_SAFE_OBLIGATIONS,
    )
    applied = gate.apply(unknown_safe, runtime.fence)
    assert applied is not None
    assert (
        runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "confirmed"}) == 0
    )
    assert runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "unknown"}) == 1


def test_no_profile_never_grants_sensitive_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 1: without a signed profile, private history/tools are False."""

    runtime = _runtime(monkeypatch)
    runtime.authenticate_text_owner()
    runtime.set_mode_policy(_policy(None))
    gate = runtime.orchestrator.runtime_profiles
    assert (
        gate.sensitive_effect_allowed(
            runtime.fence, current_fence=runtime.fence, capability="memory_recall_private"
        )
        is False
    )
    assert gate.sensitive_effect_allowed(runtime.fence, current_fence=runtime.fence) is False
    # Ordinary unknown-safe chat remains possible.
    assert gate.output_allowed(runtime.fence, current_fence=runtime.fence) is True


def test_resolution_metric_counts_three_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 2: confirmed / unknown_safe / invalid count correctly."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    # Confirmed adult profile -> confirmed.
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    assert (
        runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "confirmed"}) == 1
    )
    # Unknown-safe profile -> unknown, never confirmed.
    unknown_safe = canonical_wire_payload(
        session_id="ses_prompt_wiring",
        active_subject_id=None,
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        service_mode="unknown_safe",
        session_epoch=2,
        capabilities=["chat"],
        obligations=UNKNOWN_SAFE_OBLIGATIONS,
    )
    applied = gate.apply(unknown_safe, runtime.fence)
    assert applied is not None
    assert (
        runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "confirmed"}) == 1
    )
    assert runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "unknown"}) == 1
    # Invalid payload -> unknown.
    assert gate.apply(b"not-a-profile", runtime.fence) is None
    assert runtime.orchestrator.metrics.get("subject_resolution_total", {"status": "unknown"}) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"actor_id": "actor_other"},
        {"subject_category": "adult", "age_band": "adult", "active_subject_id": "person_parent"},
        {"age_band": "unknown"},
        {"speaker_state": "unconfirmed"},
        {"speaker_confidence": 0.5},
        {"persona": {"persona_id": "starlight", "version": 4, "relationship_stage": "new"}},
        {"policy_receipt_ids": ["receipt_other"]},
        {"binding_id": "bind_other"},
        {"binding_version": 2},
        {"subject_revision": 2},
        {"active_subject_id": "person_other"},
        {"persona_assignment_id": "axu:v1"},
        {"persona": {"persona_id": "axu", "version": 1, "relationship_stage": "new"}},
        {"policy_bundle_version": "cn-minor-v6"},
        {"service_mode": "family_shared"},
        {"capabilities": ["chat"]},
        {"obligations": []},
        {"runtime_profile_id": "rp_other"},
    ],
)
def test_same_epoch_identity_mutation_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
) -> None:
    """Audit 4: every identity/permission field is frozen at the epoch."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    assert gate.current is not None
    mutated = _profile(epoch=1)
    if "persona" in mutation:
        mutated["persona"] = mutation["persona"]
        rest = {k: v for k, v in mutation.items() if k != "persona"}
    else:
        rest = mutation
    mutated.update(rest)
    from services.agent.tests.unit.runtime_profile_test_helpers import sign_wire_payload

    mutated["signature"] = sign_wire_payload(mutated, TEST_VERIFY_KEY)
    assert gate.apply(mutated, runtime.fence) is None
    assert gate.current is None  # fail closed, never replaced


def test_time_only_refresh_fields_are_mutable_at_same_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 4: issued_at/expires_at refresh keeps the identity valid."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    refreshed = _profile(epoch=1)
    refreshed["issued_at"] = "2098-06-01T00:00:00+00:00"
    refreshed["expires_at"] = "2099-06-01T00:00:00+00:00"
    from services.agent.tests.unit.runtime_profile_test_helpers import sign_wire_payload

    refreshed["signature"] = sign_wire_payload(refreshed, TEST_VERIFY_KEY)
    assert gate.apply(refreshed, runtime.fence) is not None
    assert runtime.fence.session_epoch == 1


@pytest.mark.asyncio
async def test_refresh_failure_distinguishes_timeout_from_programming_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: authority refresh failure revokes fail-closed, but a timeout is
    counted separately from an unexpected error so programming mistakes stay
    visible in bounded metrics/logs (no sensitive data)."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _timeout() -> object:
        raise TimeoutError("authority slow")

    runtime.set_runtime_profile_refresher(_timeout)  # type: ignore[arg-type]
    assert await runtime.refresh_runtime_profile() is None
    assert runtime.orchestrator.runtime_profiles.current is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "timeout"}
        )
        == 1
    )
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "error"}
        )
        == 0
    )

    async def _boom() -> object:
        raise RuntimeError("programming bug")

    runtime.set_runtime_profile_refresher(_boom)  # type: ignore[arg-type]
    assert await runtime.refresh_runtime_profile() is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "error"}
        )
        == 1
    )
    assert runtime.orchestrator.runtime_profiles.current is None


@pytest.mark.asyncio
async def test_missing_refresher_revokes_profile_before_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: without an authority refresher the identity degrades at the next
    turn boundary — the old epoch is void, sensitive permissions fail closed,
    and the fresh degraded epoch continues public unknown-safe chat."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    gate = runtime.orchestrator.runtime_profiles
    assert gate.current is not None
    assert gate.permits(runtime.fence, current_fence=runtime.fence) is True
    old_epoch = runtime.fence.session_epoch

    # Production wiring gap: the refresher is removed/uninstalled.
    runtime.set_runtime_profile_refresher(None)

    async def _commit() -> None:
        await runtime.on_turn_committed("下一轮。", input_modality="text")

    await _commit()
    assert gate.current is None
    # The identity epoch advanced: the old fence is void, late results die.
    assert runtime.fence.session_epoch == old_epoch + 1
    old_fence = runtime.fence.with_session_epoch(old_epoch)
    assert runtime.gate_llm_token(old_fence, "late") is None
    assert runtime.gate_tts_audio(old_fence, b"late") is None
    assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
    assert (
        gate.permits(runtime.fence, current_fence=runtime.fence, capability="memory_recall_private")
        is False
    )
    assert (
        gate.permits(runtime.fence, current_fence=runtime.fence, capability="memory_capture")
        is False
    )
    # The fresh degraded epoch never held a profile: unknown-safe chat stays
    # possible (public output flows) while every sensitive side effect stays
    # closed through the capability gate.
    assert gate.output_allowed(runtime.fence, current_fence=runtime.fence) is True
    assert runtime.gate_llm_token(runtime.fence, "公开回复") == "公开回复"
    assert runtime.gate_tts_audio(runtime.fence, b"\x01\x02") == b"\x01\x02"


@pytest.mark.asyncio
async def test_authority_loss_clears_old_subject_context_and_recovers_on_new_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: authority loss (refresh timeout) atomically advances the epoch
    and clears every old-subject cache/snapshot/policy; the old fence's late
    results are refused, the degraded prompt is unknown-safe, and recovery
    requires a signed profile at not less than the current epoch."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    gate = runtime.orchestrator.runtime_profiles

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("我的私人安排是周三去体检。")
    old_fence = runtime.fence
    old_epoch = old_fence.session_epoch
    assert old_fence.session_epoch == 1
    assert [turn.content for turn in runtime.orchestrator.context.turns] == [
        "我的私人安排是周三去体检。"
    ]
    assert runtime.gate_llm_token(old_fence, "token") == "token"

    async def _timeout() -> object:
        raise TimeoutError("authority slow")

    runtime.set_runtime_profile_refresher(_timeout)  # type: ignore[arg-type]
    await _commit("下一轮。")

    # Identity degraded: fresh epoch, no profile, old fence fully void.
    assert runtime.fence.session_epoch == 2
    assert gate.current is None
    assert gate.for_fence(old_fence, current_fence=runtime.fence) is None
    assert runtime.gate_llm_token(old_fence, "late") is None
    assert runtime.gate_tts_audio(old_fence, b"late") is None
    assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
    assert gate.output_allowed(runtime.fence, current_fence=runtime.fence) is True
    assert gate.permits(runtime.fence, current_fence=runtime.fence) is False

    # Old-subject context/snapshot is gone, prompt is unknown-safe.
    degraded_snapshot = runtime.orchestrator.context_snapshots.current(runtime.session_id)
    assert not degraded_snapshot.recent_committed_turns
    assert not degraded_snapshot.summary
    # Only the new degraded public turn survives; the old private turn is gone.
    assert [turn.content for turn in runtime.orchestrator.context.turns] == ["下一轮。"]
    prompt = production_system_prompt(runtime)
    assert "当前是安全模式" in prompt
    assert "周三" not in prompt
    assert "星澜" not in prompt
    # Explicit local degraded surface: conversation-only, everything
    # sensitive closed through the policy and the profile gate.
    assert runtime.mode_policy.mode == "unknown_safe"
    assert runtime.mode_policy.policy_version == "degraded-unknown-safe-v1"
    assert runtime.mode_policy.allows_conversation() is True
    assert runtime.mode_policy.allows_tools("owner") is False
    assert runtime.mode_policy.allows_learning("owner") is False
    assert runtime.mode_policy.allows_private_persona("owner") is False
    assert runtime.mode_policy.allows_voice_profile() is False
    assert runtime.mode_policy.history_eligible("owner") is False
    assert runtime.mode_policy.owner_projection_eligible("owner") is False
    # Physical cache clearing: no old-epoch fence maps, no old speaker
    # evidence (the degraded turn may bind fresh epoch-2 entries).
    assert not any(fence.session_epoch == old_epoch for fence in runtime._speech_plans_by_fence)
    assert not any(
        fence.session_epoch == old_epoch for fence in runtime._response_provenance_by_fence
    )
    assert not any(fence.session_epoch == old_epoch for fence in runtime._voice_snapshot_by_fence)
    assert not any(fence.session_epoch == old_epoch for fence in runtime._history_eligible_by_fence)
    assert not any(
        fence.session_epoch == old_epoch for fence in runtime._owner_projection_eligible_by_fence
    )
    assert not any(fence.session_epoch == old_epoch for fence in runtime._input_modality_by_fence)
    assert not runtime._speaker_pcm
    assert not runtime._trusted_playback_pcm
    assert runtime._speaker_decision is None
    assert runtime._speaker_class == "uncertain"
    assert runtime._pending_assistant_text == ""
    assert runtime._played_assistant_text == ""

    # Recovery needs a profile at not less than the degraded epoch.
    async def _recover() -> object:
        return parse_runtime_profile(_profile(epoch=2), verify_key=TEST_VERIFY_KEY)

    runtime.set_runtime_profile_refresher(_recover)  # type: ignore[arg-type]
    await _commit("恢复后的一轮。")
    assert gate.current is not None
    assert runtime.fence.session_epoch == 2
    assert gate.permits(runtime.fence, current_fence=runtime.fence) is True
    # A stale lower-epoch profile must never restore the old epoch.
    assert gate.apply(_profile(epoch=1), runtime.fence) is None


@pytest.mark.asyncio
async def test_subject_switch_via_seam_clears_every_old_subject_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: a legitimate subject switch through the public seam rotates the
    whole identity state — every old-subject cache/task is physically cleared,
    old fences reject late output and the new subject stays usable."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    gate = runtime.orchestrator.runtime_profiles

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 的私人安排。")
    old_fence = runtime.fence
    assert old_fence.session_epoch == 1
    # Seed distinctive old-subject state across every by-fence map.
    runtime._speech_plans_by_fence[old_fence] = runtime.speech_plan
    runtime._response_provenance_by_fence[old_fence] = {"digital_self_version_id": "a"}
    runtime._voice_snapshot_by_fence[old_fence] = object()  # type: ignore[assignment]
    runtime._history_eligible_by_fence[old_fence] = True
    runtime._owner_projection_eligible_by_fence[old_fence] = True
    runtime._input_modality_by_fence[old_fence] = "audio"
    runtime._speaker_pcm.extend(b"\x01\x00" * 8)
    runtime._speaker_decision = _speaker_decision()
    runtime._speaker_class = "owner"
    runtime._pending_assistant_text = "A 的半截回复"
    runtime._played_assistant_text = "A 已播放"
    runtime._emotion_segments_by_turn[1] = [("sad", "A 的情绪")]

    switched = _profile(epoch=2, subject="person_parent", mode="adult_companion")
    applied = runtime.apply_runtime_profile(switched)
    assert applied is not None
    assert runtime.fence.session_epoch == 2

    # Every old-subject physical cache is gone; no old-epoch fence keys.
    for container in (
        runtime._speech_plans_by_fence,
        runtime._response_provenance_by_fence,
        runtime._voice_snapshot_by_fence,
        runtime._history_eligible_by_fence,
        runtime._owner_projection_eligible_by_fence,
        runtime._input_modality_by_fence,
    ):
        assert not any(fence.session_epoch == 1 for fence in container), (
            f"old-epoch entry survived in {type(container).__name__}"
        )
    assert not runtime._emotion_segments_by_turn
    assert not runtime._speaker_pcm
    assert runtime._speaker_decision is None
    assert runtime._speaker_class == "uncertain"
    assert runtime._pending_assistant_text == ""
    assert runtime._played_assistant_text == ""
    # The old fence rejects every sink; the new subject is usable.
    assert runtime.gate_llm_token(old_fence, "late") is None
    assert runtime.gate_tts_audio(old_fence, b"late") is None
    assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
    assert gate.for_fence(old_fence, current_fence=runtime.fence) is None
    assert gate.for_fence(runtime.fence, current_fence=runtime.fence) is not None
    assert gate.permits(runtime.fence, current_fence=runtime.fence) is True
    # ONE consistent authority after the switch: the ModePolicy is derived
    # from B's signed profile (never A's policy, never an unrelated surface).
    assert runtime.mode_policy.mode == "companion"
    assert runtime.mode_policy.policy_version == "cn-minor-v5"
    assert runtime.mode_policy.runtime_profile is applied
    assert runtime.mode_policy.allows_conversation() is True
    assert runtime.mode_policy.history_eligible("owner") is True
    # Tools stay per-ToolSpec gated; the derived policy never grants the
    # whole LiveKit list from an aggregate capability.
    assert runtime.mode_policy.allows_tools("owner") is False
    # B carries tutor without the no-learning-progress obligation.
    assert runtime.mode_policy.allows_learning("owner") is True
    assert runtime.mode_policy.allows_private_persona("owner") is False
    assert runtime.mode_policy.allows_voice_profile() is False
    prompt = production_system_prompt(runtime)
    assert "身份已确认" in prompt
    assert "A 的私人安排" not in prompt
    # History needs B's own fresh speaker decision: the old subject's
    # decision was physically cleared, so no stale eligibility survives.
    assert runtime._speaker_decision is None
    assert runtime._current_history_eligible() is False


@pytest.mark.asyncio
async def test_refresh_invalid_payloads_degrade_and_recovery_needs_new_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: every non-applicable authority refresh (None/tampered/wrong
    session/expired) rotates to unknown-safe; only a valid higher-epoch
    profile recovers the session."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    gate = runtime.orchestrator.runtime_profiles

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("第一轮。")
    old_fence = runtime.fence
    assert runtime.gate_llm_token(old_fence, "ok") == "ok"

    invalid_payloads = [
        None,
        _profile(epoch=1, subject="person_parent"),
        _profile(epoch=2, subject="person_parent", session_id="ses_other"),
    ]
    tampered = _profile(epoch=2, subject="person_parent")
    tampered["capabilities"] = ["chat", "tutor", "voice_clone_use"]
    invalid_payloads.append(tampered)
    for payload in invalid_payloads:

        async def _serve(payload: object = payload) -> object:
            return payload

        runtime.set_runtime_profile_refresher(_serve)  # type: ignore[arg-type]
        await _commit("又一轮。")
        assert gate.current is None
        assert runtime.fence.session_epoch >= 2
        assert runtime.gate_llm_token(old_fence, "late") is None
        assert runtime.gate_tts_audio(old_fence, b"late") is None
        assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
        assert gate.output_allowed(runtime.fence, current_fence=runtime.fence) is True
        assert gate.permits(runtime.fence, current_fence=runtime.fence) is False

    assert runtime.orchestrator.metrics.get(
        "runtime_profile_refresh_failures_total", {"reason": "invalid"}
    ) == len(invalid_payloads)

    # Valid higher-epoch recovery works through the same seam.
    async def _recover() -> object:
        return parse_runtime_profile(
            _profile(epoch=3, subject="person_parent", mode="adult_companion"),
            verify_key=TEST_VERIFY_KEY,
        )

    runtime.set_runtime_profile_refresher(_recover)  # type: ignore[arg-type]
    await _commit("恢复。")
    assert gate.current is not None
    assert runtime.fence.session_epoch == 3
    assert gate.permits(runtime.fence, current_fence=runtime.fence) is True


@pytest.mark.asyncio
async def test_epoch_rotation_cancels_old_fence_async_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: a subject switch synchronously signals/cancels every old-fence
    async owner (TTS event, LLM/TTS tasks, cancellable tool/delegation
    records) so old-subject work cannot keep running or commit, while the new
    subject's public work starts normally."""

    from services.agent.src.orchestration.task_manager import ToolTask

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    old_fence = runtime.fence
    before_discarded = list(orch.tts_pool.discarded)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking() -> None:
        started.set()
        await release.wait()

    llm_task = asyncio.create_task(_blocking())
    tts_task = asyncio.create_task(_blocking())
    tool_task = asyncio.create_task(_blocking())
    orch.set_active_llm_task(llm_task)
    orch.set_active_tts_task(tts_task)
    record = ToolTask(
        tool_task_id="old-tool-1",
        tool_name="external_write",
        fence=old_fence,
        cancellable=True,
        task=tool_task,
        cancel_event=asyncio.Event(),
        task_epoch=0,
        context_version=1,
        expires_at_ms=0,
        side_effect_policy="write",
        committed=False,
    )
    orch.task_manager.tasks["old-tool-1"] = record
    await asyncio.sleep(0)
    # Prove the tasks were really running before the switch.
    assert started.is_set()
    assert not llm_task.done() and not tts_task.done() and not tool_task.done()

    async def _switch_and_commit(text: str) -> object:
        applied = runtime.apply_runtime_profile(
            _profile(epoch=2, subject="person_parent", mode="adult_companion")
        )
        assert applied is not None
        await asyncio.sleep(0)
        # Synchronous bump already signalled/cancelled the old-fence owners.
        assert orch.tts_cancel_event().is_set()
        assert llm_task.cancelled()
        assert tts_task.cancelled()
        assert tool_task.cancelled()
        assert record.cancelled is True
        assert record.cancel_event.is_set()
        # Fail-closed while the drain barrier is pending.
        assert runtime.gate_llm_token(runtime.fence, "blocked") is None
        assert runtime.gate_tts_audio(runtime.fence, b"blocked") is None
        assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
        await _commit(text)
        return applied

    applied = await _switch_and_commit("B 第一轮。")
    assert applied is not None
    assert runtime.fence.session_epoch == 2
    release.set()
    await asyncio.sleep(0)

    # Cancellation signals fired; old work cannot commit.
    # Drain barrier observable: old TTS provider connection was discarded.
    assert old_fence in orch.tts_pool.discarded
    assert len(orch.tts_pool.discarded) == len(before_discarded) + 1
    assert runtime._pending_epoch_drain is None
    assert runtime.gate_llm_token(old_fence, "late") is None
    assert runtime.gate_tts_audio(old_fence, b"late") is None
    assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
    # New-subject public work starts normally.
    assert runtime.gate_llm_token(runtime.fence, "B 输出") == "B 输出"
    assert runtime.gate_tts_audio(runtime.fence, b"B pcm") == b"B pcm"
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) == {"ok": True}
    await runtime.close()


@pytest.mark.asyncio
async def test_stubborn_side_effect_handler_commit_is_denied_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the external write is only reachable through the transactional
    effect commit port.  A handler that ignores cancellation and returns a
    payload can only prepare; the port's atomic re-verification denies the
    commit after a real identity switch (0 commits)."""

    import time
    from types import SimpleNamespace

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.identity_state import exact_fence_commit_authority
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    proceed = asyncio.Event()

    async def side_effect_writer(
        _args: dict[str, object],
        _cancel: asyncio.Event,
    ) -> PreparedToolEffect:
        await proceed.wait()
        # Malicious/faulty preparer: ignores the cancel event and returns an
        # intent; it has no external-write capability of its own.
        return PreparedToolEffect(intent="memory_write", payload={"done": True})

    commits = 0
    evidence = SimpleNamespace(expires_at=datetime(2099, 1, 1, tzinfo=UTC))

    class Port:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            if not exact_fence_commit_authority(
                runtime,
                authorization.fence,
                authorization.capability,
                evidence,
            ):
                return None
            commits += 1
            return CommitReceipt(
                intent_id=f"intent-{idempotency_key}",
                idempotency_key=idempotency_key,
                committed_at=datetime.now(UTC),
                fence_fingerprint=fence_fingerprint(authorization.fence),
                authority_revision=1,
                capability=authorization.capability,
                purpose=authorization.purpose,
                resource_id=authorization.resource_id,
                intent_sha256=prepared.payload_sha256,
                evidence_refs=authorization.evidence_refs,
            )

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(Port())
    tm.register_side_effect(
        ToolSpec(
            name="external_write",
            description="committed side-effect tool",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        side_effect_writer,
    )
    old_fence = GenerationFence(
        session_id="ses-stubborn",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    rec = await tm.start(
        "external_write",
        {},
        old_fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=EffectAuthorization(
            fence=old_fence,
            capability="memory_capture",
            purpose="memory_capture",
            resource_id="memory:subject-scoped",
            evidence_refs=("receipt_memory_1",),
            action_id="memory-write-123",
        ),
    )
    await asyncio.sleep(0)
    assert not rec.task.done()
    # Real identity switch while the intent is pending.
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    proceed.set()
    await rec.task
    assert rec.finished and rec.error is None
    assert commits == 0
    assert rec.cancelled is True
    # The commit-time exact-fence CAS still denies the result.
    rec.cancel_event.set()
    assert (
        tm.accept_result(
            rec.tool_task_id,
            old_fence.with_session_epoch(2),
            current_task_epoch=0,
            current_context_version=1,
            now_ms=int(time.time() * 1_000),
            relevant=True,
            current_side_effect_policy="idempotent",
        )
        is None
    )
    assert tm.accepted_broadcast_count == 0
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_cancel_swallowing_commit_port_is_bounded_and_idempotent() -> None:
    """P0: a commit port that swallows CancelledError cannot hang the
    TaskManager — the first attempt returns bounded with no adopted receipt,
    and a real retry through ``TaskManager.start`` with the same action
    identity reconciles to the SAME durable receipt (exactly one commit)."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    commits = 0
    seen: dict[str, CommitReceipt] = {}

    class SwallowPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            if idempotency_key in seen:
                return seen[idempotency_key]  # idempotent reconcile
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Durable commit happened, then the coroutine keeps running.
                pass
            commits += 1
            receipt = CommitReceipt(
                intent_id=f"intent-{idempotency_key}",
                idempotency_key=idempotency_key,
                committed_at=datetime.now(UTC),
                fence_fingerprint=fence_fingerprint(authorization.fence),
                authority_revision=1,
                capability=authorization.capability,
                purpose=authorization.purpose,
                resource_id=authorization.resource_id,
                intent_sha256=prepared.payload_sha256,
                evidence_refs=authorization.evidence_refs,
            )
            seen[idempotency_key] = receipt
            return receipt

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            if idempotency_key in seen:
                return CommitReconcileResult(CommitReconcileState.COMMITTED, seen[idempotency_key])
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(SwallowPort())
    tm.register_side_effect(
        ToolSpec(
            name="stubborn_commit",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-timeout",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="memory-write-retry",
    )
    rec = await tm.start(
        "stubborn_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    await asyncio.wait_for(rec.task, timeout=2)
    assert rec.finished
    # Bounded return: the TaskManager never adopted the late commit.
    assert rec.commit_receipt is None
    assert rec.cancelled is True
    assert tm.accepted_broadcast_count == 0
    # A real service retry through the TaskManager with the SAME action
    # identity (same args/fence/effect) derives the same key, reconciles to
    # the port's durable record and adopts the SAME receipt — the durable
    # intent is never duplicated.
    retry = await tm.start(
        "stubborn_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    assert retry.commit_receipt is not None
    await asyncio.wait_for(retry.task, timeout=2)
    assert retry.finished and retry.error is None
    assert commits == 1  # exactly one durable intent across both attempts
    assert retry.commit_receipt.idempotency_key == retry.tool_task_id
    assert retry.commit_receipt.fence_fingerprint == fence_fingerprint(fence)
    assert retry.commit_receipt.capability == "memory_capture"
    assert retry.commit_receipt.purpose == "memory_capture"
    assert retry.commit_receipt.evidence_refs == ("receipt_memory_1",)
    assert (
        retry.commit_receipt.intent_sha256
        == (await _prepared_intent({}, asyncio.Event())).payload_sha256
    )


@pytest.mark.asyncio
async def test_prepared_tool_effect_is_deeply_immutable_and_canonical() -> None:
    """P0: a prepared intent's payload is deep-frozen canonical JSON; nested
    mutation attempts after construction cannot change what the port sees."""

    import hashlib

    from services.agent.src.transactional_effect_commit import (
        PreparedToolEffect,
        canonical_json_bytes,
    )

    original = {"outer": {"inner": [1, 2, {"deep": "v"}]}, "flag": True}
    prepared = PreparedToolEffect(intent="memory_write", payload=original)
    assert prepared.payload_sha256 == hashlib.sha256(canonical_json_bytes(original)).hexdigest()
    snapshot = prepared.payload_json
    with pytest.raises(TypeError):
        prepared.payload["outer"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.payload["outer"]["inner"][0] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.payload["outer"]["inner"][2]["deep"] = "mutated"  # type: ignore[index]
    assert prepared.payload_json == snapshot
    assert prepared.payload_sha256 == hashlib.sha256(snapshot).hexdigest()
    # The port sees the same fixed content.
    assert prepared.payload["outer"]["inner"][2]["deep"] == "v"


@pytest.mark.asyncio
async def test_prepared_tool_effect_rejects_non_json_nan_and_oversized() -> None:
    """P0: non-JSON values, NaN/Infinity, bytes and oversized payloads are
    rejected at construction; the intent is bounded and serializable."""

    from services.agent.src.transactional_effect_commit import PreparedToolEffect

    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"fn": lambda: None})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"nan": float("nan")})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"inf": float("inf")})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"blob": b"bytes"})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"data": "x" * (300 * 1024)})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="   ", payload={})


@pytest.mark.asyncio
async def test_idempotency_key_binds_full_action_identity() -> None:
    """P0: the derived key covers the full fence + capability/purpose/resource/
    evidence + action id, so two different logical actions never merge into
    one durable intent; explicit keys must be bounded non-blank."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    seen_keys: list[str] = []

    class RecordingPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, now
            seen_keys.append(idempotency_key)
            return None  # deny; we only observe the derived key

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(RecordingPort())
    tm.register_side_effect(
        ToolSpec(
            name="keyed_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )

    def _effect(fence: GenerationFence, *, resource: str) -> EffectAuthorization:
        return EffectAuthorization(
            fence=fence,
            capability="memory_capture",
            purpose="memory_capture",
            resource_id=resource,
            evidence_refs=("receipt_memory_1",),
            action_id="same-logical-action",
        )

    base = GenerationFence(
        session_id="ses-key",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    # Same tool + args, but a different identity epoch: different action.
    rec1 = await tm.start(
        "keyed_write",
        {},
        base,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=_effect(base, resource="memory:a"),
    )
    await asyncio.wait_for(rec1.task, timeout=2)
    rec2 = await tm.start(
        "keyed_write",
        {},
        base.with_session_epoch(2),
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=_effect(base.with_session_epoch(2), resource="memory:a"),
    )
    await asyncio.wait_for(rec2.task, timeout=2)
    # Same tool/args but different resource: different action.
    rec3 = await tm.start(
        "keyed_write",
        {},
        base,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=_effect(base, resource="memory:b"),
    )
    await asyncio.wait_for(rec3.task, timeout=2)
    assert len(seen_keys) == 3
    assert len(set(seen_keys)) == 3  # no accidental merge
    # An explicit key must be bounded/non-blank.
    with pytest.raises(ValueError):
        await tm.start(
            "keyed_write",
            {},
            base,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=_effect(base, resource="memory:a"),
            idempotency_key="   ",
        )


@pytest.mark.asyncio
async def test_reconciled_receipt_mismatch_is_rejected() -> None:
    """P0: a COMMITTED reconcile outcome whose receipt does not match the
    exact action identity (fence/capability/evidence) is never adopted."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    fence = GenerationFence(
        session_id="ses-mismatch",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="memory-write-mismatch",
    )

    class WrongReceiptPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            # A receipt for a DIFFERENT fence fingerprint / capability.
            return CommitReconcileResult(
                CommitReconcileState.COMMITTED,
                CommitReceipt(
                    intent_id="intent-other",
                    idempotency_key=idempotency_key,
                    committed_at=datetime.now(UTC),
                    fence_fingerprint="1" * 64,
                    authority_revision=1,
                    capability="payment",
                    purpose="payment",
                    resource_id="payment:other",
                    intent_sha256="2" * 64,
                    evidence_refs=("receipt_other",),
                ),
            )

    tm = TaskManager()
    tm.set_effect_commit_port(WrongReceiptPort())
    tm.register_side_effect(
        ToolSpec(
            name="mismatch_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "mismatch_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )
    assert tm.accepted_broadcast_count == 0


@pytest.mark.asyncio
async def test_reconcile_swallowing_port_is_bounded_and_fails_closed() -> None:
    """P0: a reconcile that swallows CancelledError cannot hang start(); the
    attempt fails closed within the bounded timeout."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    class HangingReconcilePort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # swallow; never return
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(HangingReconcilePort())
    tm.register_side_effect(
        ToolSpec(
            name="hang_reconcile",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-reconcile-hang",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="hang-reconcile-1",
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "hang_reconcile",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )


@pytest.mark.asyncio
async def test_reconcile_caller_cancellation_propagates() -> None:
    """P0: cancelling the caller while reconcile hangs must keep propagating
    (the start task ends cancelled), with the port call bounded-reaped."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    class HangingReconcilePort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # swallow forever
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(HangingReconcilePort())
    tm.register_side_effect(
        ToolSpec(
            name="hang_reconcile_cancel",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-reconcile-cancel",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="hang-reconcile-cancel-1",
    )
    start_task = asyncio.create_task(
        tm.start(
            "hang_reconcile_cancel",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )
    )
    await asyncio.sleep(0.02)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start_task, timeout=2)
    # The cancelled reconcile port call was bounded-reaped (detached if it
    # swallowed forever); no task record remains pending.
    assert not any(not rec.finished for rec in tm.tasks.values())


@pytest.mark.asyncio
async def test_side_effect_start_requires_committed_turn_and_authorization() -> None:
    """P0: every non-read-only start needs an authoritative committed turn AND
    a per-invocation EffectAuthorization; otherwise the port is never called."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    commits = 0

    class CountingPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            del spec, authorization, prepared, idempotency_key, now
            commits += 1
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(CountingPort())
    tm.register_side_effect(
        ToolSpec(
            name="guarded_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-committed",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="guarded-write-1",
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "guarded_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=False,
            effect=effect,
        )
    with pytest.raises(PermissionError):
        await tm.start(
            "guarded_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
        )
    assert commits == 0
    assert tm.accepted_broadcast_count == 0


@pytest.mark.asyncio
async def test_user_cancel_of_commit_port_is_bounded_and_never_adopts() -> None:
    """P0: cancelling the running tool task (user interrupt / epoch drain)
    bounded-reaps the in-flight port call, never adopts a late receipt, and a
    later retry under the same action reconciles to one durable intent."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    commits = 0
    seen: dict[str, CommitReceipt] = {}

    class SwallowPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            if idempotency_key in seen:
                return seen[idempotency_key]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            commits += 1
            receipt = CommitReceipt(
                intent_id=f"intent-{idempotency_key}",
                idempotency_key=idempotency_key,
                committed_at=datetime.now(UTC),
                fence_fingerprint=fence_fingerprint(authorization.fence),
                authority_revision=1,
                capability=authorization.capability,
                purpose=authorization.purpose,
                resource_id=authorization.resource_id,
                intent_sha256=prepared.payload_sha256,
                evidence_refs=authorization.evidence_refs,
            )
            seen[idempotency_key] = receipt
            return receipt

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            if idempotency_key in seen:
                return CommitReconcileResult(CommitReconcileState.COMMITTED, seen[idempotency_key])
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(SwallowPort())
    tm.register_side_effect(
        ToolSpec(
            name="cancel_commit",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-user-cancel",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="user-cancel-write-1",
    )
    rec = await tm.start(
        "cancel_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    await asyncio.sleep(0)
    assert not rec.task.done()
    rec.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(rec.task, timeout=2)
    assert rec.cancelled is True
    assert rec.commit_receipt is None
    assert tm.accepted_broadcast_count == 0
    # Retry under the same action: reconciles to the SAME durable receipt.
    retry = await tm.start(
        "cancel_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    await asyncio.wait_for(retry.task, timeout=2)
    assert retry.commit_receipt is not None
    assert commits == 1
    assert retry.commit_receipt.fence_fingerprint == fence_fingerprint(fence)


@pytest.mark.asyncio
async def test_set_effect_commit_port_requires_reconcile() -> None:
    """P0: a port lacking reconcile() is rejected at install time."""

    from services.agent.src.orchestration.task_manager import TaskManager

    class CommitOnlyPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

    tm = TaskManager()
    with pytest.raises(TypeError):
        tm.set_effect_commit_port(CommitOnlyPort())


@pytest.mark.asyncio
async def test_reconcile_unknown_state_fails_closed() -> None:
    """P0: an UNKNOWN reconcile outcome must never be re-done under a new key."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    class UnknownPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)

    tm = TaskManager()
    tm.set_effect_commit_port(UnknownPort())
    tm.register_side_effect(
        ToolSpec(
            name="unknown_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-unknown",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="unknown-write-1",
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "unknown_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )


async def test_side_effect_tool_registration_fails_closed_without_port() -> None:
    """P0: without a transactional effect commit port (the production
    default), every side-effect registration fails closed; read-only tools
    stay available."""

    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    async def _handler(
        _args: dict[str, object],
        _cancel: asyncio.Event,
    ) -> dict[str, object]:
        return {}

    tm = TaskManager()
    with pytest.raises(PermissionError):
        tm.register(
            ToolSpec(
                name="w1",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_purpose="memory_capture",
            ),
            _handler,
        )
    # A writer handler cannot masquerade as a preparer for side effects.
    with pytest.raises(PermissionError):
        tm.register_side_effect(
            ToolSpec(
                name="w0",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_purpose="memory_capture",
            ),
            _handler,  # type: ignore[arg-type]  # wrong preparer shape
        )
    # Missing purpose is rejected even with a port installed.
    with pytest.raises(TypeError):
        tm.set_effect_commit_port(object())  # not a commit port -> rejected
    tm.set_effect_commit_port(_FakePort())
    with pytest.raises(PermissionError):
        tm.register_side_effect(
            ToolSpec(
                name="w2",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_purpose=None,
            ),
            _handler,  # type: ignore[arg-type]
        )
    assert "w1" not in tm.specs and "w2" not in tm.specs and "w0" not in tm.specs
    # Read-only tools register fine without any port.
    tm.register(
        ToolSpec(
            name="probe",
            description="t",
            input_schema={},
            cancellable=False,
            idempotent=False,
            timeout_s=5,
            contains_sensitive_data=False,
            side_effect_policy="read_only",
        ),
        _handler,
    )
    assert "probe" in tm.specs


async def test_slow_playback_seam_keeps_barrier_visible_until_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: the drain barrier stays visible while the physical owners are
    still draining — concurrent output callbacks keep failing closed."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    seam_release = asyncio.Event()
    seam_started = asyncio.Event()

    async def _slow_seam() -> None:
        seam_started.set()
        await seam_release.wait()

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    runtime.set_playback_stop_seam(_slow_seam)
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    commit_task = asyncio.create_task(_commit("B 第一轮。"))
    await asyncio.wait_for(seam_started.wait(), timeout=2)
    await asyncio.sleep(0)
    # Barrier still visible during the drain: outputs fail closed.
    assert runtime._pending_epoch_drain is not None
    assert runtime.gate_llm_token(runtime.fence, "blocked") is None
    assert runtime.gate_tts_audio(runtime.fence, b"blocked") is None
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
    seam_release.set()
    await asyncio.wait_for(commit_task, timeout=2)
    assert runtime._pending_epoch_drain is None
    assert runtime.gate_llm_token(runtime.fence, "B 输出") == "B 输出"
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("awaitable_kind", ["future", "task"])
async def test_playback_seam_accepts_non_coroutine_awaitables(
    awaitable_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production LiveKit interrupt seam may return Future/Task, not a coroutine."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.on_turn_committed("A 第一轮。", input_modality="text")

    def _future_seam() -> asyncio.Future[None]:
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def _finished() -> None:
        return None

    def _task_seam() -> asyncio.Task[None]:
        return asyncio.create_task(_finished())

    runtime.set_playback_stop_seam(
        _future_seam if awaitable_kind == "future" else _task_seam
    )
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None

    await runtime.on_turn_committed("B 第一轮。", input_modality="text")

    assert runtime._pending_epoch_drain is None
    assert [turn.content for turn in runtime.orchestrator.context.turns] == [
        "B 第一轮。"
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_hanging_playback_seam_times_out_and_aborts_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-2/P0-3: a permanently hanging playback seam hits the bounded
    deadline; the barrier stays, metrics/log fire and the current turn is
    aborted — no generation, playback, side effect or committed state."""

    runtime = _runtime(monkeypatch)
    runtime.orchestrator.epoch_drain_timeout_s = 0.05
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator

    async def _hanging_seam() -> None:
        await asyncio.Event().wait()

    await runtime.on_turn_committed("A 第一轮。", input_modality="text")
    runtime.set_playback_stop_seam(_hanging_seam)
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    fence_after_apply = runtime.fence
    with pytest.raises(RuntimeError):
        await runtime.on_turn_committed("B 第一轮。", input_modality="text")
    # The turn was aborted: no committed state, no fence advance, no output.
    assert runtime._pending_epoch_drain is not None
    assert runtime.fence.matches(fence_after_apply)
    # Old-subject context was cleared by the rotation; B never committed.
    assert [turn.content for turn in runtime.orchestrator.context.turns] == []
    assert runtime.gate_llm_token(runtime.fence, "x") is None
    assert runtime.gate_tts_audio(runtime.fence, b"x") is None
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
    assert (
        orch.metrics.get("runtime_profile_refresh_failures_total", {"reason": "drain_timeout"}) >= 1
    )
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        "session_id",
        "turn_id",
        "generation_id",
        "tool_epoch",
        "session_epoch",
    ],
)
async def test_commit_authority_requires_full_fence_match(
    mutate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the exact-fence CAS compares EVERY fence field — a same-epoch old
    turn/generation or a forged session can never pass the side-effect gate."""

    from services.agent.src.identity_state import exact_fence_commit_authority

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    current = runtime.fence
    assert (
        exact_fence_commit_authority(runtime, current, "memory_recall_private", {"receipt": "r"})
        is True
    )

    from dataclasses import replace

    altered = replace(
        current,
        **(
            {"session_id": "ses_other"}
            if mutate == "session_id"
            else {mutate: getattr(current, mutate) + 1}
        ),
    )
    assert (
        exact_fence_commit_authority(runtime, altered, "memory_recall_private", {"receipt": "r"})
        is False
    )


@pytest.mark.asyncio
async def test_commit_port_blocks_then_real_bump_denies_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-TOCTOU: the port blocks after prepare; the runtime REALLY advances
    via the real APIs (generation bump / turn commit / higher-epoch profile);
    each round awaits the task and resets independent events."""

    import time
    from types import SimpleNamespace

    from services.agent.src.identity_state import exact_fence_commit_authority
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("第一轮。", input_modality="text")
    evidence = SimpleNamespace(expires_at=datetime(2099, 1, 1, tzinfo=UTC))

    for scenario in ("generation", "turn", "epoch"):
        commits = 0
        blocked = asyncio.Event()
        release = asyncio.Event()

        class Port:
            async def commit_prepared(
                self,
                *,
                spec: object,
                authorization: EffectAuthorization,
                prepared: PreparedToolEffect,
                idempotency_key: str,
                now: object,
                _blocked: asyncio.Event = blocked,
                _release: asyncio.Event = release,
            ) -> CommitReceipt | None:
                nonlocal commits
                _blocked.set()
                await _release.wait()
                if not exact_fence_commit_authority(
                    runtime,
                    authorization.fence,
                    authorization.capability,
                    evidence,
                ):
                    return None
                commits += 1
                return CommitReceipt(
                    intent_id=f"intent-{idempotency_key}",
                    idempotency_key=idempotency_key,
                    committed_at=datetime.now(UTC),
                    fence_fingerprint=fence_fingerprint(authorization.fence),
                    authority_revision=1,
                    capability=authorization.capability,
                    purpose=authorization.purpose,
                    resource_id=authorization.resource_id,
                    intent_sha256=prepared.payload_sha256,
                    evidence_refs=authorization.evidence_refs,
                )

            async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
                del idempotency_key
                return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

        tm = TaskManager()
        tm.set_effect_commit_port(Port())
        tm.register_side_effect(
            ToolSpec(
                name="fenced_write",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_capability="memory_capture",
                required_purpose="memory_capture",
            ),
            _prepared_intent,
        )
        effect = EffectAuthorization(
            fence=runtime.fence,
            capability="memory_capture",
            purpose="memory_capture",
            resource_id="memory:subject-scoped",
            evidence_refs=("receipt_memory_1",),
            action_id=f"fenced-write-{scenario}",
        )
        rec = await tm.start(
            "fenced_write",
            {},
            runtime.fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )
        await asyncio.wait_for(blocked.wait(), timeout=2)
        assert not rec.task.done()
        before = runtime.fence
        if scenario == "generation":
            await runtime.preempt_media_output(
                cause="test-generation-bump",
                synchronized_transcript=None,
            )
            after = runtime.fence
            assert after.generation_id == before.generation_id + 1
            assert after.turn_id == before.turn_id
            assert after.session_epoch == before.session_epoch
            assert after.tool_epoch == before.tool_epoch
        elif scenario == "turn":
            await runtime.orchestrator.commit_turn(
                "下一轮。",
                speaker_scope="public",
            )
            after = runtime.fence
            assert after.turn_id == before.turn_id + 1
            assert after.generation_id == before.generation_id + 1
            assert after.session_epoch == before.session_epoch
            assert after.tool_epoch == before.tool_epoch
        else:
            applied = runtime.apply_runtime_profile(_profile(epoch=3))
            assert applied is not None
            after = runtime.fence
            assert after.session_epoch == 3
            assert after.turn_id == before.turn_id
            assert after.generation_id == before.generation_id
            assert after.tool_epoch == before.tool_epoch
        release.set()
        await asyncio.wait_for(rec.task, timeout=2)
        assert rec.finished
        assert commits == 0
        assert rec.cancelled is True
    await runtime.close()


async def _prepared_intent(
    _args: dict[str, object],
    _cancel: asyncio.Event,
) -> PreparedToolEffect:
    return PreparedToolEffect(intent="memory_write", payload={"prepared": True})


class _FakePort:
    """Test-only commit port implementing the async protocol shape."""

    async def commit_prepared(
        self,
        *,
        spec: object,
        authorization: EffectAuthorization,
        prepared: PreparedToolEffect,
        idempotency_key: str,
        now: object,
    ) -> CommitReceipt | None:
        del spec, now
        return CommitReceipt(
            intent_id=f"intent-{idempotency_key}",
            idempotency_key=idempotency_key,
            committed_at=datetime.now(UTC),
            fence_fingerprint=fence_fingerprint(authorization.fence),
            authority_revision=1,
            capability=authorization.capability,
            purpose=authorization.purpose,
            resource_id=authorization.resource_id,
            intent_sha256=prepared.payload_sha256,
            evidence_refs=authorization.evidence_refs,
        )

    async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
        del idempotency_key
        return CommitReconcileResult(CommitReconcileState.NOT_FOUND)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,epoch_field",
    [
        ("_context_snapshot_prepare_task", "_context_snapshot_prepare_epoch"),
        ("_speaker_classification_task", "_speaker_epoch"),
        ("_voice_profile_refresh_task", "_speaker_epoch"),
        ("_listener_cue_candidate_task", "_speaker_epoch"),
    ],
)
async def test_identity_tasks_are_captured_cancelled_and_void_after_switch(
    field: str,
    epoch_field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: identity-owned background tasks are captured before the reset and
    bounded-cancelled by the drain; after the switch their late bodies cannot
    run (cancelled) and the bumped epochs void any late completion."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _late_writer() -> None:
        started.set()
        await release.wait()
        # Would write an identity-private field on the new subject.
        setattr(runtime, f"{field}_late_write", True)

    task = asyncio.create_task(_late_writer())
    setattr(runtime, field, task)
    await asyncio.sleep(0)
    assert started.is_set() and not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    # The fake task was captured (other real background tasks may be too);
    # the drain (inside the next commit) bounded-cancels them.
    assert runtime._rotation_captured_tasks is not None
    assert task in runtime._rotation_captured_tasks
    await _commit("B 第一轮。")
    assert runtime._rotation_captured_tasks is None
    assert task.cancelled()
    release.set()
    await asyncio.sleep(0)
    # The late body never ran: no private field was written back.  (The
    # runtime may already have spawned a fresh task for the new subject.)
    assert not hasattr(runtime, f"{field}_late_write")
    await runtime.close()


@pytest.mark.asyncio
async def test_cancel_swallowing_identity_task_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: a cancel-swallowing identity task makes the drain time out; the
    barrier stays fail-closed, and even after the task late-completes the
    bumped identity epochs void the write (no pending snapshot or speaker
    decision lands on the new subject)."""

    runtime = _runtime(monkeypatch)
    runtime.orchestrator.epoch_drain_timeout_s = 0.05
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    old_prepare_epoch = runtime._context_snapshot_prepare_epoch
    old_speaker_epoch = runtime._speaker_epoch
    started = asyncio.Event()
    release = asyncio.Event()

    async def _swallow_and_late_complete() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # swallow the cancellation, then late-complete
        await release.wait()
        # Late writes replicate the production epoch checks.
        if runtime._context_snapshot_prepare_epoch == old_prepare_epoch:
            runtime._pending_context_snapshot = object()
        if runtime._speaker_epoch == old_speaker_epoch:
            runtime._speaker_decision = object()  # type: ignore[assignment]

    stubborn = asyncio.create_task(_swallow_and_late_complete())
    runtime._context_snapshot_prepare_task = stubborn
    await asyncio.sleep(0)
    assert started.is_set()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert stubborn in (runtime._rotation_captured_tasks or [])
    with pytest.raises(RuntimeError):
        await _commit("B 第一轮。")  # drain times out -> turn aborted
    # Barrier stays fail-closed; the late complete cannot write back.
    assert runtime._pending_epoch_drain is not None
    release.set()
    await asyncio.wait_for(stubborn, timeout=2)
    assert runtime._pending_context_snapshot is None
    assert runtime._speaker_decision is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "drain_timeout"}
        )
        >= 1
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_real_context_snapshot_prepare_late_draft_is_void_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL ``_schedule_context_snapshot_prepare`` producer with a
    controllable ``prepare_next`` adapter; after the switch the bumped
    prepare epoch voids the late draft (it can never land on the new
    subject's pending snapshot)."""

    from services.agent.src.orchestration.context_snapshot_manager import (
        PendingSnapshot,
    )

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("A 第一轮。", input_modality="text")

    started = asyncio.Event()
    release = asyncio.Event()
    manager = runtime.orchestrator.context_snapshots

    async def _blocking_prepare(*_args: object, **_kwargs: object) -> object:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # late complete after the switch
        base = manager.current(runtime.session_id)
        return PendingSnapshot(runtime.session_id, base.version, base, 0.0)

    # The manager is a frozen dataclass; patch the class method instead.
    monkeypatch.setattr(type(manager), "prepare_next", _blocking_prepare)
    runtime._schedule_context_snapshot_prepare()
    await asyncio.wait_for(started.wait(), timeout=2)
    task = runtime._context_snapshot_prepare_task
    assert task is not None and not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert task in (runtime._rotation_captured_tasks or [])
    release.set()
    await asyncio.wait_for(task, timeout=2)
    # The prepare epoch was bumped during the rotation: the late draft cannot
    # be written into the new subject's pending snapshot.
    assert runtime._pending_context_snapshot is None
    assert runtime._pending_context_snapshot_epoch is None
    await runtime.close()


@pytest.mark.asyncio
async def test_real_speaker_classification_late_decision_is_void_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL ``_classify_speaker`` producer with a controllable
    classifier; after the switch the bumped speaker epoch voids the late
    owner decision (private speaker state never lands on the new subject)."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("A 第一轮。", input_modality="text")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _classifier(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # late complete after the switch
        return _speaker_decision()  # owner decision

    runtime._speaker_classifier = _classifier
    runtime._speaker_pcm = bytearray(b"\x00\x01")
    runtime._start_speaker_classification()
    await asyncio.wait_for(started.wait(), timeout=2)
    task = runtime._speaker_classification_task
    assert task is not None and not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    release.set()
    await asyncio.wait_for(task, timeout=2)
    # The speaker epoch was bumped: the late owner decision cannot land.
    assert runtime._speaker_decision is None
    assert runtime._speaker_class == "uncertain"
    await runtime.close()


@pytest.mark.asyncio
async def test_real_voice_profile_refresh_is_captured_and_dropped_on_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL ``refresh_voice_profile`` task is captured by the
    rotation and its reference is dropped, so a late personal-voice result
    can never be applied for the new subject."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()

    started = asyncio.Event()
    release = asyncio.Event()

    async def _refresher() -> dict[str, object]:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # late complete after the switch
        return {
            "profile_id": "vo_personal_new",
            "voice_profile_id": "vo_personal_new",
            "profile_version": 1,
        }

    runtime.set_voice_profile_refresher(_refresher)
    task = runtime.refresh_voice_profile()
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=2)
    assert not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert task in (runtime._rotation_captured_tasks or [])
    release.set()
    await asyncio.wait_for(task, timeout=2)
    # The refresher reference was dropped during the rotation: nothing can
    # consume the late personal-voice result for the new subject.
    assert runtime._voice_profile_refresh_task is None
    await runtime.close()


@pytest.mark.asyncio
async def test_listener_cue_async_stop_is_awaited_before_switch_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL listener-cue path with an async stop/flush handle.  The
    playing cue is cancelled by the identity rotation, its owner task awaits
    the async stop, and the old cue's finally cannot mutate the new epoch's
    phase or keep old audio playing."""

    from services.agent.src.orchestration.state_machine import InteractionPhase
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        bind_owner_policy,
        canonical_wire_payload,
    )

    class _AsyncCueHandle:
        def __init__(self) -> None:
            self.stop_calls: list[str] = []
            self.playout_started = asyncio.Event()
            self.release = asyncio.Event()

        def stop(self) -> object:
            async def _stop() -> None:
                await asyncio.sleep(0)
                self.stop_calls.append("stopped")

            return _stop()

        async def wait_for_playout(self) -> None:
            self.playout_started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                pass  # swallow; the finally must still stop the handle

    monkeypatch.setenv(VERIFY_KEY_ENV, TEST_VERIFY_KEY)
    runtime = DuplexRuntime.create(
        session_id="cue-async-stop",
        device_id="dev_01J_test",
        listener_cues_enabled=True,
    )
    bind_owner_policy(runtime)
    runtime.cue_scheduler.min_speech_ms = 0
    runtime.cue_scheduler.pause_ms = 0.02
    runtime.cue_scheduler.cooldown_ms = 0
    runtime.set_listener_cue_aec_healthy(True)
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    handle_created = asyncio.Event()
    holder: dict[str, _AsyncCueHandle] = {}

    async def player(text: str) -> _AsyncCueHandle:
        del text
        handle = _AsyncCueHandle()
        holder["h"] = handle
        handle_created.set()
        return handle

    runtime.set_listener_cue_player(player)
    await runtime.orchestrator.ready()
    runtime.on_user_voice_started(now_ns=1_000_000_000)
    runtime.observe_user_transcript(
        "我还在继续讲这件事",
        final=False,
        now_ns=2_100_000_000,
    )
    await asyncio.wait_for(handle_created.wait(), timeout=2)
    handle = holder["h"]
    await asyncio.wait_for(handle.playout_started.wait(), timeout=2)
    # Let the spawned UI publisher task run before asserting on events.
    await asyncio.sleep(0.02)
    assert runtime.fence.session_epoch == 1
    assert runtime.interaction_phase is InteractionPhase.BACKCHANNEL
    assert any(e.get("type") == "listener_cue" and e.get("state") == "started" for e in published)
    cue_task = runtime._listener_cue_candidate_task
    assert cue_task is not None and not cue_task.done()

    # Identity switch while the cue is playing.
    switched = canonical_wire_payload(
        session_id="cue-async-stop",
        device_id="dev_01J_test",
        runtime_profile_id="rp_cue_switched",
        active_subject_id="person_parent",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        service_mode="adult_companion",
        session_epoch=2,
        capabilities=["chat", "tutor", "english_practice"],
    )
    applied = runtime.apply_runtime_profile(switched)
    assert applied is not None
    phase_after_switch = runtime.interaction_phase
    handle.release.set()
    await asyncio.wait_for(cue_task, timeout=2)
    # The async stop was awaited by the captured owner task (not leaked).
    assert handle.stop_calls == ["stopped"]
    assert runtime._active_listener_cue is None
    assert runtime._active_listener_cue_handle is None
    # The old cue's finally did not mutate the new epoch's phase and no old
    # cue audio/event is tagged with the new subject.
    assert runtime.interaction_phase is phase_after_switch
    assert not any(
        e.get("type") == "listener_cue" and e.get("state") == "finished" for e in published
    )
    assert not any(
        e.get("type") == "listener_cue" and e.get("state") == "cancelled" for e in published
    )
    await runtime.close()


async def test_production_wiring_has_no_commit_port_and_side_effects_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the real agent wiring installs NO transactional effect commit port
    yet, so every side-effect registration fails closed while the real
    read-only planner tool registers and runs normally."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    class _PlannerClient:
        async def fetch(self, **_kwargs: object) -> object:
            from services.agent.src.response_planner_client import ResponsePlanFetch

            return ResponsePlanFetch(None, "no_profile")

    agent = agent_module.DuplexVoiceAgent(
        instructions="test",
        runtime=runtime,
        response_planner_client=_PlannerClient(),  # type: ignore[arg-type]
    )
    tm = runtime.orchestrator.task_manager
    # Production default: no persistent commit port -> side effects denied.
    assert tm._effect_commit_port is None
    with pytest.raises(PermissionError):
        tm.register(
            agent_module.ToolSpec(
                name="w",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_capability="memory_capture",
                required_purpose="memory_capture",
            ),
            lambda *_: None,
        )
    assert "w" not in tm.specs
    # The real agent registration path installs the read-only planner tool.
    assert agent._register_response_planner_tool() is True
    assert tm.specs["response_planner"]
    await runtime.close()


async def test_cancel_swallowing_task_drain_timeout_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: a task that swallows CancelledError must not hang the drain:
    the bounded timeout is observable, metrics/log fire, and the new epoch
    stays fail-closed (no sensitive output until a successful drain)."""

    runtime = _runtime(monkeypatch)
    runtime.orchestrator.epoch_drain_timeout_s = 0.05
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _swallow_cancel() -> None:
        started.set()
        while True:
            try:
                await release.wait()
                break
            except asyncio.CancelledError:
                # Malicious/faulty handler: swallows EVERY cancellation.
                continue

    stubborn_llm = asyncio.create_task(_swallow_cancel())
    orch.set_active_llm_task(stubborn_llm)
    await asyncio.sleep(0)
    assert started.is_set() and not stubborn_llm.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert stubborn_llm.cancelling() or stubborn_llm.cancelled()
    with pytest.raises(RuntimeError):
        await _commit("B 第一轮。")  # drain fails -> the turn is aborted

    # Drain failed observably: barrier remains, fail-closed, metric recorded
    # and no committed state advanced.
    assert runtime._pending_epoch_drain is not None
    assert runtime.fence.session_epoch == 2
    assert runtime.gate_llm_token(runtime.fence, "敏感输出") is None
    assert runtime.gate_tts_audio(runtime.fence, b"pcm") is None
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "drain_timeout"}
        )
        == 1
    )
    release.set()
    await asyncio.sleep(0)
    await runtime.close()


@pytest.mark.asyncio
async def test_drain_stops_production_playback_owner_and_clears_queued_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: drain calls the production playback owner (LiveKit stop seam)
    and clears queued PCM, not just the in-process controller flag."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator
    stopped_owners: list[str] = []

    async def _production_stop() -> None:
        stopped_owners.append("livekit_handle")
        orch.playback.pcm_played.clear()
        orch.playback.playing = False

    runtime.set_playback_stop_seam(_production_stop)
    orch.playback.start()
    orch.playback.push_pcm(b"\x01\x00" * 64)
    assert orch.playback.playing is True
    assert bytes(orch.playback.pcm_played) == b"\x01\x00" * 64

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    assert stopped_owners == ["livekit_handle"]
    assert orch.playback.playing is False
    assert not orch.playback.pcm_played
    assert runtime._pending_epoch_drain is None
    await runtime.close()


def test_non_cancellable_side_effect_tool_registration_is_rejected() -> None:
    """P0-2: non-cancellable tools must be NONE/READ_ONLY and non-sensitive,
    otherwise they are rejected at registration (never exposed to runtime)."""

    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    tm = TaskManager()
    for side_effect, sensitive in (
        ("idempotent", False),
        ("high_risk", False),
        ("read_only", True),
        ("none", True),
    ):
        with pytest.raises(PermissionError):
            tm.register(
                ToolSpec(
                    name=f"bad-{side_effect}-{sensitive}",
                    description="must be rejected",
                    input_schema={},
                    cancellable=False,
                    idempotent=False,
                    timeout_s=5,
                    contains_sensitive_data=sensitive,
                    side_effect_policy=side_effect,
                ),
                lambda *_: None,
            )
    # Without a commit authority, even a cancellable side-effect tool is
    # rejected: the runtime never carries unguarded external writes.
    with pytest.raises(PermissionError):
        tm.register(
            ToolSpec(
                name="bad-cancellable-write",
                description="must be rejected without authority",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
            ),
            lambda *_: None,
        )
    # A compliant non-cancellable read-only tool registers fine.
    tm.register(
        ToolSpec(
            name="safe-probe",
            description="read-only probe",
            input_schema={},
            cancellable=False,
            idempotent=False,
            timeout_s=5,
            contains_sensitive_data=False,
            side_effect_policy="read_only",
        ),
        lambda *_: None,
    )
    assert "safe-probe" in tm.specs
