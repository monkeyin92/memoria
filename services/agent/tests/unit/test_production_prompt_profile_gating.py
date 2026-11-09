"""RuntimeProfile gate and production prompt seam contract tests.

Covers the shared prompt seam, verified profile binding, same-epoch identity
rules, subject-switch invalidation (PR-08), and revoked or expired sinks."""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)

import pytest
from services.agent.src import media_agent_factory as factory_module
from services.agent.src.prompt_composition import (
    PROMPT_SECTIONS,
    SERVICE_MODE_BLOCKS,
    compose_production_prompt,
)
from services.agent.src.prompts import EMOTION_TAG_RULE
from services.agent.src.runtime_profile import (
    RuntimeProfile,
    parse_runtime_profile,
)
from services.agent.src.tutor_session import production_system_prompt
from services.agent.tests.unit.production_wiring_support import (
    _policy,
    _profile,
    _runtime,
    _speaker_decision,
)
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    UNKNOWN_SAFE_OBLIGATIONS,
    canonical_wire_payload,
)
from services.agent.tests.unit.runtime_state_helpers import (
    bind_owner_speaker,
    set_floor,
)
from services.common.companions import designed_voice_speaker_sha256


def test_production_prompt_is_the_shared_seam_for_the_media_session() -> None:
    """The media session factory consumes the shared prompt function object."""

    assert factory_module.production_system_prompt is production_system_prompt


def test_real_consumer_invocation_with_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    prompt = production_system_prompt(runtime)
    assert dict(PROMPT_SECTIONS)["safety"] in prompt
    assert SERVICE_MODE_BLOCKS["student_minor"] in prompt
    assert "儿童/学生" in prompt
    assert "身份已确认" in prompt
    assert "没有个人名字" in prompt  # persona style without a spoken self-name
    assert EMOTION_TAG_RULE in prompt
    assert "星澜" not in prompt
    assert "不得自称或讨论" not in prompt
    assert "如实说明你是由人工智能驱动的聊天助手" in prompt


def test_real_consumer_invocation_without_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(None))
    prompt = production_system_prompt(runtime)
    assert SERVICE_MODE_BLOCKS["unknown_safe"] in prompt
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


@pytest.mark.asyncio
async def test_bootstrap_identity_epoch_settles_before_first_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    assert runtime._pending_epoch_drain is not None
    assert runtime.gate_tts_audio(runtime.fence, b"pcm") is None

    await runtime.settle_bootstrap_identity_epoch()

    assert runtime._pending_epoch_drain is None
    assert runtime.gate_tts_audio(runtime.fence, b"pcm") == b"pcm"
    await runtime.close()


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
    assert SERVICE_MODE_BLOCKS["adult_companion"] in new_prompt
    assert "儿童/学生" not in new_prompt


@pytest.mark.asyncio
async def test_epoch_keyed_permission_caches_never_hit_old_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-3: old-subject policy/history/voice lookups miss after the switch."""

    runtime = _runtime(monkeypatch)
    gate = runtime.orchestrator.runtime_profiles
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    bind_owner_speaker(runtime)

    async def _commit() -> None:
        await runtime.on_turn_committed("第一条。", input_modality="text")

    await _commit()
    old_fence = runtime.fence
    assert runtime.mode_policy_for_fence(old_fence).available
    assert runtime._generation_records.history_eligible(old_fence) is True
    assert runtime._generation_records.owner_projection_eligible(old_fence) is True
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
    assert runtime._generation_records.history_eligible(new_fence) is False
    assert runtime._generation_records.owner_projection_eligible(new_fence) is False
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
    bind_owner_speaker(runtime)
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
    assert SERVICE_MODE_BLOCKS["unknown_safe"] in prompt
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
    for kind, fences in runtime._generation_records.retained_fences().items():
        assert not any(fence.session_epoch == old_epoch for fence in fences), kind
    assert not runtime._speaker_pcm
    assert runtime._speaker_decision is None
    assert runtime._speaker_class == "uncertain"
    assert runtime._voice_floor.pending_assistant_text == ""
    assert runtime._voice_floor.played_assistant_text == ""

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
    records = runtime._generation_records
    records.bind_speech_plan(old_fence, runtime.speech_plan)
    records.bind_tts_references(old_fence, ("A 的参考上下文",))
    records.bind_response_provenance(old_fence, {"digital_self_version_id": "a"})
    records.bind_voice_snapshot(old_fence, object())  # type: ignore[arg-type]
    records.bind_history_eligible(old_fence, True)
    records.bind_owner_projection_eligible(old_fence, True)
    records.bind_input_modality(old_fence, "audio")
    runtime._speaker_pcm.extend(b"\x01\x00" * 8)
    runtime._speaker_decision = _speaker_decision()
    runtime._speaker_class = "owner"
    set_floor(runtime, pending_assistant_text="A 的半截回复", played_assistant_text="A 已播放")
    runtime._emotion_segments_by_turn[1] = [("sad", "A 的情绪")]

    switched = _profile(epoch=2, subject="person_parent", mode="adult_companion")
    applied = runtime.apply_runtime_profile(switched)
    assert applied is not None
    assert runtime.fence.session_epoch == 2

    # Every old-subject physical cache is gone; no old-epoch fence keys
    # (interaction policies included: this rotation installs a policy).
    for kind, fences in records.retained_fences().items():
        assert not any(fence.session_epoch == 1 for fence in fences), (
            f"old-epoch entry survived in {kind}"
        )
    assert records.tts_references_for(old_fence) == ()
    assert not runtime._emotion_segments_by_turn
    assert not runtime._speaker_pcm
    assert runtime._speaker_decision is None
    assert runtime._speaker_class == "uncertain"
    assert runtime._voice_floor.pending_assistant_text == ""
    assert runtime._voice_floor.played_assistant_text == ""
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
    assert runtime.mode_policy.policy_version == "s2-v1"
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

