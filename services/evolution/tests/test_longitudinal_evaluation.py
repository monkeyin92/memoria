from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from services.evolution.longitudinal_evaluation import (
    EvolutionArm,
    EvolutionHoldoutCase,
    EvolutionReleaseGate,
    LongitudinalEvaluationAdapter,
    LongitudinalObservation,
    decide_evolution_release,
    load_longitudinal_dataset,
    load_recorded_observation_bundle,
    run_longitudinal_evaluation,
)

DATASET = Path(__file__).parents[1] / "evaluation" / "evolution_holdout_zh_v1.json"


class RecordedAdapter:
    """A test double for evidence produced by an independent replay/device evaluator."""

    name = "recorded-independent-evaluator"

    async def observe(
        self,
        case: EvolutionHoldoutCase,
        arm: EvolutionArm,
    ) -> LongitudinalObservation:
        passed = True
        if arm == "static" and case.phase in {"transfer", "change"}:
            passed = False
        if arm == "append_only":
            if case.phase == "change":
                passed = False
            elif case.phase == "transfer" and case.case_id != "weather-target-date-paraphrase":
                passed = False
        activated = arm != "static" and case.task_family in {"weather", "public_context"}
        return LongitudinalObservation(
            case_id=case.case_id,
            arm=arm,
            result_passed=passed,
            process_passed=True,
            quality_passed=passed,
            activated=activated,
            adhered=activated and passed,
            outcome_passed=activated and passed,
            evidence_id=f"run:{arm}:{case.case_id}",
            evidence_mode=case.evidence_mode,
            latency_ms=50.0,
            input_tokens=8,
            output_tokens=4,
        )


class UnsafeAdapter(RecordedAdapter):
    name = "unsafe-independent-evaluator"

    async def observe(
        self,
        case: EvolutionHoldoutCase,
        arm: EvolutionArm,
    ) -> LongitudinalObservation:
        result = await super().observe(case, arm)
        if arm == "evolving" and case.safety_critical:
            return LongitudinalObservation(
                case_id=result.case_id,
                arm=result.arm,
                result_passed=False,
                process_passed=False,
                quality_passed=result.quality_passed,
                activated=result.activated,
                adhered=False,
                outcome_passed=False,
                evidence_id=result.evidence_id,
                evidence_mode=result.evidence_mode,
                latency_ms=result.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        return result


class NoActivationAdapter(RecordedAdapter):
    name = "no-activation-independent-evaluator"

    async def observe(
        self,
        case: EvolutionHoldoutCase,
        arm: EvolutionArm,
    ) -> LongitudinalObservation:
        result = await super().observe(case, arm)
        if arm == "evolving":
            return replace(
                result,
                activated=False,
                adhered=False,
                outcome_passed=False,
            )
        return result


def test_versioned_holdout_dataset_covers_public_voice_and_safety_paths() -> None:
    dataset = load_longitudinal_dataset(DATASET)

    assert dataset.version == "evolution-holdout-zh-v1"
    assert len(dataset.manifest_sha256) == 64
    assert len(dataset.cases) == 10
    assert {case.phase for case in dataset.cases} >= {
        "transfer",
        "change",
        "retention",
        "negative",
        "safety",
    }
    assert sum(case.evidence_mode == "device" for case in dataset.cases) >= 2
    assert any(case.safety_critical for case in dataset.cases)


@pytest.mark.asyncio
async def test_three_arm_evaluation_requires_benefit_without_safety_or_retention_loss() -> None:
    report = await run_longitudinal_evaluation(
        load_longitudinal_dataset(DATASET),
        RecordedAdapter(),
    )
    static = report.metrics["static"]
    append_only = report.metrics["append_only"]
    evolving = report.metrics["evolving"]

    assert evolving.transfer_success_rate > append_only.transfer_success_rate > static.transfer_success_rate
    assert evolving.rule_change_recovery_rate > append_only.rule_change_recovery_rate
    assert evolving.retention_rate == 1
    assert evolving.negative_transfer_rate == 0
    assert evolving.safety_pass_rate == 1
    assert evolving.device_case_count >= 2
    assert decide_evolution_release(report).allowed is True
    assert decide_evolution_release(
        report,
        gate=EvolutionReleaseGate(
            minimum_transfer_gain=(
                evolving.transfer_success_rate - append_only.transfer_success_rate
            )
        ),
    ).allowed is True
    assert len(report.as_dict()["observation_manifest_sha256"]) == 64


@pytest.mark.asyncio
async def test_release_gate_rejects_any_safety_regression() -> None:
    report = await run_longitudinal_evaluation(
        load_longitudinal_dataset(DATASET),
        UnsafeAdapter(),
    )

    decision = decide_evolution_release(report)

    assert decision.allowed is False
    assert "evolving_safety_failure" in decision.reasons
    assert "evolving_process_failure" in decision.reasons


@pytest.mark.asyncio
async def test_release_gate_rejects_updates_that_never_activate() -> None:
    report = await run_longitudinal_evaluation(
        load_longitudinal_dataset(DATASET),
        NoActivationAdapter(),
    )

    decision = decide_evolution_release(report)

    assert decision.allowed is False
    assert "no_activation_evidence" in decision.reasons
    assert "activation_adherence_failure" in decision.reasons
    assert "activation_outcome_failure" in decision.reasons


def test_observation_requires_activation_semantics() -> None:
    with pytest.raises(ValueError, match="inactive artifacts"):
        LongitudinalObservation(
            case_id="case",
            arm="static",
            result_passed=True,
            process_passed=True,
            quality_passed=True,
            activated=False,
            adhered=True,
            outcome_passed=False,
            evidence_id="evidence",
            evidence_mode="archive_replay",
        )


def test_dataset_rejects_missing_required_longitudinal_phase(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        '{"version":"invalid-v1","cases":[{"case_id":"only","scenario":"only",'
        '"phase":"transfer","task_family":"weather","evidence_mode":"text",'
        '"safety_critical":false}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="change coverage"):
        load_longitudinal_dataset(path)


def test_adapter_protocol_remains_structural() -> None:
    adapter: LongitudinalEvaluationAdapter = RecordedAdapter()
    assert adapter.name == "recorded-independent-evaluator"


@pytest.mark.asyncio
async def test_evaluation_rejects_evidence_mode_drift() -> None:
    class DriftedEvidenceAdapter(RecordedAdapter):
        name = "drifted-evidence"

        async def observe(
            self,
            case: EvolutionHoldoutCase,
            arm: EvolutionArm,
        ) -> LongitudinalObservation:
            observation = await super().observe(case, arm)
            if case.evidence_mode == "device":
                return replace(observation, evidence_mode="archive_replay")
            return observation

    with pytest.raises(ValueError, match="evidence mode"):
        await run_longitudinal_evaluation(
            load_longitudinal_dataset(DATASET),
            DriftedEvidenceAdapter(),
        )


@pytest.mark.asyncio
async def test_evaluation_does_not_count_one_evidence_item_as_multiple_cases() -> None:
    class ReusedEvidenceAdapter(RecordedAdapter):
        name = "reused-evidence"

        async def observe(
            self,
            case: EvolutionHoldoutCase,
            arm: EvolutionArm,
        ) -> LongitudinalObservation:
            observation = await super().observe(case, arm)
            return replace(observation, evidence_id=f"one-run:{arm}")

    with pytest.raises(ValueError, match="unique evidence ids"):
        await run_longitudinal_evaluation(
            load_longitudinal_dataset(DATASET),
            ReusedEvidenceAdapter(),
        )


def test_recorded_bundle_has_no_unbounded_response_payload(tmp_path: Path) -> None:
    dataset = load_longitudinal_dataset(DATASET)
    path = tmp_path / "observations.json"
    payload = {
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.manifest_sha256,
        "adapter": "offline-rubric-v1",
        "observations": [
            {
                "case_id": "weather-target-date-paraphrase",
                "arm": "static",
                "result_passed": False,
                "process_passed": True,
                "quality_passed": False,
                "activated": False,
                "adhered": False,
                "outcome_passed": False,
                "evidence_id": "archive:eval-1",
                "evidence_mode": "archive_replay",
            }
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    adapter = load_recorded_observation_bundle(
        path,
        dataset_version=dataset.version,
        dataset_sha256=dataset.manifest_sha256,
    )

    assert adapter.name == "offline-rubric-v1"

    payload["observations"][0]["transcript"] = "private-text"  # type: ignore[index]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="observation must be an object") as exc_info:
        load_recorded_observation_bundle(
            path,
            dataset_version=dataset.version,
            dataset_sha256=dataset.manifest_sha256,
        )
    assert "private-text" not in str(exc_info.value)
