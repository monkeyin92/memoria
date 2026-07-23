from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.digital_self.preview import (
    FIDELITY_CATEGORIES,
    FidelityTrialSpec,
    PreviewConflictError,
    SelfPreviewRegistry,
)


def _trial_specs() -> tuple[FidelityTrialSpec, ...]:
    return tuple(
        FidelityTrialSpec(
            category=category,
            prompt=f"{category} prompt",
            generic_answer=f"generic {category}",
            digital_self_answer=f"digital {category}",
            available=True,
            coverage_gap=None,
            epistemic_status=(
                "unknown"
                if category in {"unknown", "privacy"}
                else "inference"
                if category == "decision"
                else "fact"
            ),
            has_source=category not in {"unknown", "privacy"},
            unsupported_fact=False,
            decision_inference_disclosed=True,
            privacy_refused=category != "privacy" or True,
            identity_disclosed=True,
        )
        for category in FIDELITY_CATEGORIES
    )


@pytest.mark.asyncio
async def test_preview_grant_is_account_scoped_idempotent_and_one_time(
    tmp_path: Path,
) -> None:
    registry = SelfPreviewRegistry.sqlite(tmp_path / "preview.sqlite3")
    now = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    grant = await registry.issue_grant(
        account_id="owner-a",
        version_id="version-a",
        manifest_sha256="a" * 64,
        perspective="child",
        expires_at=now + timedelta(minutes=10),
        idempotency_key="grant-once",
        now=now,
    )
    replay = await registry.issue_grant(
        account_id="owner-a",
        version_id="version-a",
        manifest_sha256="a" * 64,
        perspective="child",
        expires_at=now + timedelta(minutes=10),
        idempotency_key="grant-once",
        now=now,
    )

    assert replay == grant
    assert await registry.get_grant(
        account_id="owner-b", grant_id=grant.grant_id, now=now
    ) is None
    consumed = await registry.consume_grant(
        account_id="owner-a", grant_id=grant.grant_id, now=now
    )
    assert consumed.used_at == now
    with pytest.raises(PreviewConflictError):
        await registry.consume_grant(
            account_id="owner-a", grant_id=grant.grant_id, now=now
        )


@pytest.mark.asyncio
async def test_fidelity_evaluation_hides_mapping_and_requires_all_trials(
    tmp_path: Path,
) -> None:
    registry = SelfPreviewRegistry.sqlite(tmp_path / "preview.sqlite3")
    now = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    evaluation = await registry.start_evaluation(
        account_id="owner-a",
        version_id="version-a",
        manifest_sha256="a" * 64,
        trial_specs=_trial_specs(),
        idempotency_key="evaluation-once",
        now=now,
    )

    assert tuple(trial.category for trial in evaluation.trials) == FIDELITY_CATEGORIES
    assert all(trial.slot_a and trial.slot_b for trial in evaluation.trials)
    with pytest.raises(PreviewConflictError):
        await registry.complete_evaluation(
            account_id="owner-a",
            evaluation_id=evaluation.evaluation_id,
            verdict="approve",
            rationale=None,
            now=now,
        )

    for trial in evaluation.trials:
        await registry.submit_trial_choice(
            account_id="owner-a",
            evaluation_id=evaluation.evaluation_id,
            trial_id=trial.trial_id,
            preferred_slot="a",
            rationale=None,
            now=now,
        )
    completed = await registry.complete_evaluation(
        account_id="owner-a",
        evaluation_id=evaluation.evaluation_id,
        verdict="reject",
        rationale="仍需修正",
        now=now,
    )

    assert completed.status == "completed"
    assert completed.verdict == "reject"
    assert completed.summary.completed_trials == 7
    assert completed.summary.owner_approved is False


@pytest.mark.asyncio
async def test_fidelity_approve_requires_owner_to_prefer_the_digital_self(
    tmp_path: Path,
) -> None:
    registry = SelfPreviewRegistry.sqlite(tmp_path / "preview.sqlite3")
    now = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    rejected = await registry.start_evaluation(
        account_id="owner-a",
        version_id="version-a",
        manifest_sha256="a" * 64,
        trial_specs=_trial_specs(),
        idempotency_key="evaluation-low-fidelity",
        now=now,
    )
    for trial in rejected.trials:
        await registry.submit_trial_choice(
            account_id="owner-a",
            evaluation_id=rejected.evaluation_id,
            trial_id=trial.trial_id,
            preferred_slot=(
                "a" if trial.slot_a.startswith("generic") else "b"
            ),
            rationale=None,
            now=now,
        )
    with pytest.raises(PreviewConflictError):
        await registry.complete_evaluation(
            account_id="owner-a",
            evaluation_id=rejected.evaluation_id,
            verdict="approve",
            rationale=None,
            now=now,
        )

    approved = await registry.start_evaluation(
        account_id="owner-a",
        version_id="version-a",
        manifest_sha256="a" * 64,
        trial_specs=_trial_specs(),
        idempotency_key="evaluation-high-fidelity",
        now=now,
    )
    for trial in approved.trials:
        await registry.submit_trial_choice(
            account_id="owner-a",
            evaluation_id=approved.evaluation_id,
            trial_id=trial.trial_id,
            preferred_slot=(
                "a" if trial.slot_a.startswith("digital") else "b"
            ),
            rationale=None,
            now=now,
        )
    completed = await registry.complete_evaluation(
        account_id="owner-a",
        evaluation_id=approved.evaluation_id,
        verdict="approve",
        rationale="盲测达到内部首版门槛",
        now=now,
    )

    assert completed.summary.gates["owner_blind_preference"] is True
    assert completed.summary.digital_self_preference_rate == 1.0
