"""Differential test: ``GenerationRecords`` vs. the legacy per-fence dicts.

``_LegacyRecords`` is a frozen copy of the eight dict stores that lived on
``DuplexRuntime`` and its mixins before batch 5 (same caps, same
oldest-insertion-first eviction, same auxiliary-output copy, same identity
rotation).  Random operation sequences drive both and every observable read,
plus the retained insertion order, must match after each step.

The ONE intended difference is pinned explicitly: identity rotation now also
drops the TTS reference context (legacy kept it, unreachable, in memory).
"""

from __future__ import annotations

import random
from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.generation_records import (
    ELIGIBILITY_MAX_FENCES,
    RESPONSE_PROVENANCE_MAX_FENCES,
    SPEECH_PLAN_MAX_FENCES,
    TTS_REFERENCE_MAX_FENCES,
    GenerationRecords,
)

_SESSION = "ses_records"
_FENCES = tuple(
    GenerationFence(_SESSION, turn, generation, tool, epoch)
    for turn in range(6)
    for generation in range(7)
    for tool in range(2)
    for epoch in range(3)
)
_DEFAULT_PLAN = "default-plan"
_UNBOUND_POLICY = "policy_not_bound_to_fence"


class _LegacyRecords:
    """Verbatim pre-batch-5 dict logic (caps 16/16/32/32/32/32/32/32)."""

    def __init__(self) -> None:
        self._speech_plans_by_fence: dict[GenerationFence, Any] = {}
        self._tts_references_by_fence: dict[GenerationFence, tuple[str, ...]] = {}
        self._history_eligible_by_fence: dict[GenerationFence, bool] = {}
        self._owner_projection_eligible_by_fence: dict[GenerationFence, bool] = {}
        self._input_modality_by_fence: dict[GenerationFence, str] = {}
        self._mode_policy_by_fence: dict[GenerationFence, Any] = {}
        self._response_provenance_by_fence: dict[GenerationFence, dict[str, Any]] = {}
        self._voice_snapshot_by_fence: dict[GenerationFence, Any] = {}

    # runtime_emotion._apply_speech_plan
    def bind_speech_plan(self, fence: GenerationFence, plan: Any) -> None:
        self._speech_plans_by_fence[fence] = plan
        while len(self._speech_plans_by_fence) > 16:
            self._speech_plans_by_fence.pop(next(iter(self._speech_plans_by_fence)))

    def bind_tts_references(self, fence: GenerationFence, refs: tuple[str, ...]) -> None:
        self._tts_references_by_fence[fence] = refs
        while len(self._tts_references_by_fence) > 16:
            self._tts_references_by_fence.pop(next(iter(self._tts_references_by_fence)))

    # duplex_runtime._bind_* helpers (HISTORY_ELIGIBILITY_MAX_FENCES = 32)
    def bind_mode_policy(self, fence: GenerationFence, policy: Any) -> None:
        self._mode_policy_by_fence[fence] = policy
        while len(self._mode_policy_by_fence) > 32:
            self._mode_policy_by_fence.pop(next(iter(self._mode_policy_by_fence)))

    def bind_history_eligible(self, fence: GenerationFence, eligible: bool) -> None:
        self._history_eligible_by_fence[fence] = eligible
        while len(self._history_eligible_by_fence) > 32:
            self._history_eligible_by_fence.pop(next(iter(self._history_eligible_by_fence)))

    def bind_owner_projection_eligible(self, fence: GenerationFence, eligible: bool) -> None:
        self._owner_projection_eligible_by_fence[fence] = eligible
        while len(self._owner_projection_eligible_by_fence) > 32:
            self._owner_projection_eligible_by_fence.pop(
                next(iter(self._owner_projection_eligible_by_fence))
            )

    def bind_input_modality(self, fence: GenerationFence, modality: str) -> None:
        self._input_modality_by_fence[fence] = modality
        while len(self._input_modality_by_fence) > 32:
            self._input_modality_by_fence.pop(next(iter(self._input_modality_by_fence)))

    # runtime_provenance (RESPONSE_PROVENANCE_MAX_FENCES = 32)
    def bind_response_provenance(self, fence: GenerationFence, value: dict[str, Any]) -> None:
        self._response_provenance_by_fence[fence] = value
        while len(self._response_provenance_by_fence) > 32:
            self._response_provenance_by_fence.pop(next(iter(self._response_provenance_by_fence)))

    def bind_voice_snapshot(self, fence: GenerationFence, value: Any) -> None:
        self._voice_snapshot_by_fence[fence] = value
        while len(self._voice_snapshot_by_fence) > 32:
            self._voice_snapshot_by_fence.pop(next(iter(self._voice_snapshot_by_fence)))

    # duplex_runtime.speech_plan_for_fence
    def speech_plan_for(self, fence: GenerationFence) -> Any:
        exact = self._speech_plans_by_fence.get(fence)
        if exact is not None:
            return exact
        return next(
            (
                plan
                for bound_fence, plan in reversed(self._speech_plans_by_fence.items())
                if bound_fence.turn_id == fence.turn_id
            ),
            _DEFAULT_PLAN,
        )

    def mode_policy_for(self, fence: GenerationFence) -> Any:
        return self._mode_policy_by_fence.get(fence, _UNBOUND_POLICY)

    # duplex_runtime.begin_media_auxiliary_output
    def inherit(self, expected_fence: GenerationFence, next_fence: GenerationFence) -> None:
        self.bind_mode_policy(next_fence, self.mode_policy_for(expected_fence))
        self.bind_history_eligible(
            next_fence, self._history_eligible_by_fence.get(expected_fence, False)
        )
        self.bind_owner_projection_eligible(
            next_fence,
            self._owner_projection_eligible_by_fence.get(expected_fence, False),
        )
        self._speech_plans_by_fence[next_fence] = self.speech_plan_for(expected_fence)
        if expected_fence in self._response_provenance_by_fence:
            self._response_provenance_by_fence[next_fence] = self._response_provenance_by_fence[
                expected_fence
            ]
        if expected_fence in self._voice_snapshot_by_fence:
            self._voice_snapshot_by_fence[next_fence] = self._voice_snapshot_by_fence[
                expected_fence
            ]

    # identity_state.clear_identity_private_state + _rotate_identity_epoch
    def rotate(
        self,
        current: GenerationFence,
        policy: Any,
        *,
        install_policy: bool,
        clear_tts_references: bool,
    ) -> None:
        self._speech_plans_by_fence.clear()
        self._response_provenance_by_fence.clear()
        self._voice_snapshot_by_fence.clear()
        self._history_eligible_by_fence.clear()
        self._owner_projection_eligible_by_fence.clear()
        self._input_modality_by_fence.clear()
        if clear_tts_references:  # the one intended batch-5 change
            self._tts_references_by_fence.clear()
        if install_policy:
            self._mode_policy_by_fence = {
                fence: old
                for fence, old in self._mode_policy_by_fence.items()
                if fence.session_epoch == current.session_epoch
            }
            self.bind_mode_policy(current, policy)

    def retained(self) -> dict[str, tuple[GenerationFence, ...]]:
        return {
            "speech_plan": tuple(self._speech_plans_by_fence),
            "tts_references": tuple(self._tts_references_by_fence),
            "mode_policy": tuple(self._mode_policy_by_fence),
            "history_eligible": tuple(self._history_eligible_by_fence),
            "owner_projection_eligible": tuple(self._owner_projection_eligible_by_fence),
            "input_modality": tuple(self._input_modality_by_fence),
            "response_provenance": tuple(self._response_provenance_by_fence),
            "voice_snapshot": tuple(self._voice_snapshot_by_fence),
        }


def _rotate_new(
    records: GenerationRecords,
    current: GenerationFence,
    policy: Any,
    *,
    install_policy: bool,
) -> None:
    """The production sequence: ``clear()`` then the optional policy install."""

    records.clear()
    if install_policy:
        records.retain_mode_policies_for_epoch(current.session_epoch)
        records.bind_mode_policy(current, policy)


def _assert_same(
    new: GenerationRecords,
    old: _LegacyRecords,
    step: str,
    fences: tuple[GenerationFence, ...],
) -> None:
    assert new.retained_fences() == old.retained(), step
    for fence in fences:
        assert new.speech_plan_for(fence, cast(Any, _DEFAULT_PLAN)) == old.speech_plan_for(fence), (
            step
        )
        assert new.tts_references_for(fence) == old._tts_references_by_fence.get(fence, ()), step
        bound = new.mode_policy_for(fence)
        assert (bound if bound is not None else _UNBOUND_POLICY) == old.mode_policy_for(fence), step
        assert new.history_eligible(fence) == old._history_eligible_by_fence.get(fence, False)
        assert new.owner_projection_eligible(fence) == (
            old._owner_projection_eligible_by_fence.get(fence, False)
        )
        assert new.input_modality_for(fence) == old._input_modality_by_fence.get(fence, "audio")
        assert new.response_provenance_for(fence) == old._response_provenance_by_fence.get(fence)
        assert new.voice_snapshot_for(fence) == old._voice_snapshot_by_fence.get(fence)


def _run_sequence(seed: int, *, steps: int) -> None:
    rng = random.Random(seed)
    new = GenerationRecords()
    old = _LegacyRecords()
    # A small active window makes re-binds of retained fences (which keep
    # their insertion slot) and evictions both frequent.
    window = rng.randint(4, 80)
    # Rare rotations let the stores fill past their caps between clears.
    rotation_rate = rng.choice((0.0, 0.01, 0.05))
    # Each sequence favours one record kind so every cap is actually reached.
    focus = rng.randrange(8)
    pool = rng.sample(_FENCES, window)
    # Never-bound fences of every turn exercise the same-turn plan fallback.
    probes = tuple(pool) + tuple(GenerationFence(_SESSION, turn, 99, 0, 0) for turn in range(6))
    for step in range(steps):
        fence = rng.choice(pool)
        value = f"v{seed}-{step}"
        roll = rng.random()
        if roll < rotation_rate:
            op = 11
        elif roll < 0.45:
            op = focus
        else:
            op = rng.choice((*range(11), 12))
        label = f"seed={seed} step={step} op={op} fence={fence}"
        if op == 0:
            new.bind_speech_plan(fence, cast(Any, value))
            old.bind_speech_plan(fence, value)
        elif op == 1:
            refs = (value,) * rng.randint(0, 2)
            new.bind_tts_references(fence, refs)
            old.bind_tts_references(fence, refs)
        elif op == 2:
            new.bind_mode_policy(fence, cast(Any, value))
            old.bind_mode_policy(fence, value)
        elif op == 3:
            eligible = rng.random() < 0.5
            new.bind_history_eligible(fence, eligible)
            old.bind_history_eligible(fence, eligible)
        elif op == 4:
            eligible = rng.random() < 0.5
            new.bind_owner_projection_eligible(fence, eligible)
            old.bind_owner_projection_eligible(fence, eligible)
        elif op == 5:
            modality = rng.choice(("audio", "text"))
            new.bind_input_modality(fence, modality)
            old.bind_input_modality(fence, modality)
        elif op == 6:
            provenance = {"planner": value}
            new.bind_response_provenance(fence, provenance)
            old.bind_response_provenance(fence, provenance)
        elif op == 7:
            new.bind_voice_snapshot(fence, cast(Any, value))
            old.bind_voice_snapshot(fence, value)
        elif op in {8, 9}:
            target = rng.choice(pool)
            bound = new.mode_policy_for(fence)
            new.inherit(
                fence,
                target,
                mode_policy=cast(Any, bound if bound is not None else _UNBOUND_POLICY),
                speech_plan=new.speech_plan_for(fence, cast(Any, _DEFAULT_PLAN)),
            )
            old.inherit(fence, target)
        elif op == 10:
            # Interrupted playback rebinds eligibility to the event fence.
            target = rng.choice(pool)
            new.bind_history_eligible(target, new.history_eligible(fence))
            new.bind_owner_projection_eligible(target, new.owner_projection_eligible(fence))
            old.bind_history_eligible(target, old._history_eligible_by_fence.get(fence, False))
            old.bind_owner_projection_eligible(
                target, old._owner_projection_eligible_by_fence.get(fence, False)
            )
        elif op == 11:
            install = rng.random() < 0.7
            _rotate_new(new, fence, value, install_policy=install)
            old.rotate(fence, value, install_policy=install, clear_tts_references=True)
        else:
            new.retain_mode_policies_for_epoch(fence.session_epoch)
            old._mode_policy_by_fence = {
                bound_fence: policy
                for bound_fence, policy in old._mode_policy_by_fence.items()
                if bound_fence.session_epoch == fence.session_epoch
            }
        _assert_same(new, old, label, probes)


@pytest.mark.parametrize("seed", range(120))
def test_generation_records_match_legacy_dict_logic(seed: int) -> None:
    _run_sequence(seed, steps=240)


def test_caps_are_the_historical_values() -> None:
    assert SPEECH_PLAN_MAX_FENCES == 16
    assert TTS_REFERENCE_MAX_FENCES == 16
    assert ELIGIBILITY_MAX_FENCES == 32
    assert RESPONSE_PROVENANCE_MAX_FENCES == 32


def test_rotation_clears_tts_references_unlike_legacy() -> None:
    """Pin the one intended change against the legacy model."""

    fence = _FENCES[0]
    successor = GenerationFence(_SESSION, 1, 1, 0, 1)
    new = GenerationRecords()
    old = _LegacyRecords()
    new.bind_tts_references(fence, ("用户：旧主体的参考文本",))
    old.bind_tts_references(fence, ("用户：旧主体的参考文本",))

    _rotate_new(new, successor, "policy", install_policy=True)
    old.rotate(successor, "policy", install_policy=True, clear_tts_references=False)

    assert old.retained()["tts_references"] == (fence,)
    assert new.retained_fences()["tts_references"] == ()
    assert new.tts_references_for(fence) == ()


def test_inherit_keeps_the_legacy_uncapped_copy_until_the_next_capped_bind() -> None:
    records = GenerationRecords()
    fences = [GenerationFence(_SESSION, turn, turn, 0, 0) for turn in range(18)]
    for fence in fences[:16]:
        records.bind_speech_plan(fence, cast(Any, f"plan-{fence.turn_id}"))
    records.inherit(
        fences[15],
        fences[16],
        mode_policy=cast(Any, "policy"),
        speech_plan=cast(Any, "plan-16"),
    )
    assert len(records.retained_fences()["speech_plan"]) == 17
    records.bind_speech_plan(fences[17], cast(Any, "plan-17"))
    assert records.retained_fences()["speech_plan"] == tuple(fences[2:18])
