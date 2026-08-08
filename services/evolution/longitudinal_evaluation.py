"""Independent, versioned longitudinal evaluation for learned Agent behavior.

This module intentionally does not call an LLM or infer a verdict from a
candidate prompt.  An adapter supplies independently judged observations from
canonical replay or a device run; this module checks coverage and calculates
the static / append-only / evolving comparison used for a release decision.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

EvolutionArm = Literal["static", "append_only", "evolving"]
EvaluationPhase = Literal["transfer", "change", "retention", "negative", "safety"]
EvidenceMode = Literal["text", "archive_replay", "device"]

_ARMS: tuple[EvolutionArm, ...] = ("static", "append_only", "evolving")
_PHASES: tuple[EvaluationPhase, ...] = (
    "transfer",
    "change",
    "retention",
    "negative",
    "safety",
)
_EVIDENCE_MODES: tuple[EvidenceMode, ...] = ("text", "archive_replay", "device")
_DATASET_KEYS = frozenset({"version", "cases"})
_DATASET_CASE_KEYS = frozenset(
    {"case_id", "scenario", "phase", "task_family", "evidence_mode", "safety_critical"}
)
_RECORDED_BUNDLE_KEYS = frozenset(
    {"dataset_version", "dataset_sha256", "adapter", "observations"}
)
_RECORDED_OBSERVATION_KEYS = frozenset(
    {
        "case_id",
        "arm",
        "result_passed",
        "process_passed",
        "quality_passed",
        "activated",
        "adhered",
        "outcome_passed",
        "evidence_id",
        "evidence_mode",
        "latency_ms",
        "input_tokens",
        "output_tokens",
    }
)


@dataclass(frozen=True, slots=True)
class EvolutionHoldoutCase:
    """A public, versioned test contract with no user transcript content."""

    case_id: str
    scenario: str
    phase: EvaluationPhase
    task_family: str
    evidence_mode: EvidenceMode
    safety_critical: bool = False

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("case_id", self.case_id, 128),
            ("scenario", self.scenario, 128),
            ("task_family", self.task_family, 128),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} must be a bounded non-empty string")
        if self.phase not in _PHASES:
            raise ValueError("evaluation phase is invalid")
        if self.evidence_mode not in _EVIDENCE_MODES:
            raise ValueError("evaluation evidence mode is invalid")


@dataclass(frozen=True, slots=True)
class EvolutionHoldoutDataset:
    version: str
    manifest_sha256: str
    cases: tuple[EvolutionHoldoutCase, ...]

    def __post_init__(self) -> None:
        if not self.version.strip() or len(self.version) > 128:
            raise ValueError("evaluation dataset version is invalid")
        if len(self.manifest_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.manifest_sha256
        ):
            raise ValueError("evaluation dataset digest is invalid")
        if not self.cases:
            raise ValueError("evaluation dataset must contain cases")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("evaluation dataset case ids must be unique")
        if not any(case.phase == "transfer" for case in self.cases):
            raise ValueError("evaluation dataset requires transfer coverage")
        if not any(case.phase == "change" for case in self.cases):
            raise ValueError("evaluation dataset requires change coverage")
        if not any(case.phase == "retention" for case in self.cases):
            raise ValueError("evaluation dataset requires retention coverage")
        if not any(case.safety_critical for case in self.cases):
            raise ValueError("evaluation dataset requires safety coverage")


@dataclass(frozen=True, slots=True)
class LongitudinalObservation:
    """A verdict from an evaluator that is independent from the update agent."""

    case_id: str
    arm: EvolutionArm
    result_passed: bool
    process_passed: bool
    quality_passed: bool
    activated: bool
    adhered: bool
    outcome_passed: bool
    evidence_id: str
    evidence_mode: EvidenceMode
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.case_id.strip() or len(self.case_id) > 128:
            raise ValueError("observation case_id is invalid")
        if self.arm not in _ARMS:
            raise ValueError("observation arm is invalid")
        if self.evidence_mode not in _EVIDENCE_MODES:
            raise ValueError("observation evidence mode is invalid")
        if not self.evidence_id.strip() or len(self.evidence_id) > 256:
            raise ValueError("observation evidence_id is invalid")
        if not self.activated and (self.adhered or self.outcome_passed):
            raise ValueError("inactive artifacts cannot adhere or produce an activation outcome")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("observation token counts must be non-negative")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("observation latency must be finite and non-negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "arm": self.arm,
            "result_passed": self.result_passed,
            "process_passed": self.process_passed,
            "quality_passed": self.quality_passed,
            "activated": self.activated,
            "adhered": self.adhered,
            "outcome_passed": self.outcome_passed,
            "evidence_id": self.evidence_id,
            "evidence_mode": self.evidence_mode,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class LongitudinalEvaluationAdapter(Protocol):
    """Runs the actual arm and returns independently evaluated evidence."""

    name: str

    async def observe(
        self,
        case: EvolutionHoldoutCase,
        arm: EvolutionArm,
    ) -> LongitudinalObservation: ...


class RecordedObservationAdapter:
    """Replay an evaluator-owned JSON bundle without changing its verdicts."""

    def __init__(self, *, name: str, observations: Sequence[LongitudinalObservation]) -> None:
        if not name.strip() or len(name) > 128:
            raise ValueError("recorded evaluator name is invalid")
        self.name = name
        self._observations = {
            (observation.arm, observation.case_id): observation for observation in observations
        }
        if len(self._observations) != len(observations):
            raise ValueError("recorded evaluator observations must be unique")

    async def observe(
        self,
        case: EvolutionHoldoutCase,
        arm: EvolutionArm,
    ) -> LongitudinalObservation:
        try:
            return self._observations[(arm, case.case_id)]
        except KeyError as exc:
            raise ValueError(f"recorded evaluator is missing {arm}/{case.case_id}") from exc


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    arm: EvolutionArm
    case_count: int
    task_success_rate: float
    process_pass_rate: float
    quality_pass_rate: float
    activation_rate: float
    adherence_rate: float
    activation_outcome_rate: float
    transfer_success_rate: float
    rule_change_recovery_rate: float
    retention_rate: float
    negative_transfer_rate: float
    safety_pass_rate: float
    device_case_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    input_tokens: int
    output_tokens: int

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "arm": self.arm,
            "case_count": self.case_count,
            "task_success_rate": self.task_success_rate,
            "process_pass_rate": self.process_pass_rate,
            "quality_pass_rate": self.quality_pass_rate,
            "activation_rate": self.activation_rate,
            "adherence_rate": self.adherence_rate,
            "activation_outcome_rate": self.activation_outcome_rate,
            "transfer_success_rate": self.transfer_success_rate,
            "rule_change_recovery_rate": self.rule_change_recovery_rate,
            "retention_rate": self.retention_rate,
            "negative_transfer_rate": self.negative_transfer_rate,
            "safety_pass_rate": self.safety_pass_rate,
            "device_case_count": self.device_case_count,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True, slots=True)
class LongitudinalEvaluationReport:
    dataset_version: str
    dataset_sha256: str
    adapter: str
    metrics: Mapping[EvolutionArm, ArmMetrics]
    observations: tuple[LongitudinalObservation, ...]

    def as_dict(self) -> dict[str, object]:
        observation_manifest = json.dumps(
            [observation.as_dict() for observation in self.observations],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return {
            "dataset_version": self.dataset_version,
            "dataset_sha256": self.dataset_sha256,
            "adapter": self.adapter,
            "metrics": {arm: metric.as_dict() for arm, metric in self.metrics.items()},
            "observation_count": len(self.observations),
            "observation_manifest_sha256": hashlib.sha256(observation_manifest).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class EvolutionReleaseGate:
    """Conservative admission gate for a learned runtime candidate.

    Safety and retention never trade off against a public-task uplift.  The
    minimum device count makes a text-only contract run insufficient to claim
    a voice release.
    """

    minimum_device_cases: int = 2
    minimum_transfer_gain: float = 0.0

    def __post_init__(self) -> None:
        if self.minimum_device_cases < 0:
            raise ValueError("minimum_device_cases must be non-negative")
        if not math.isfinite(self.minimum_transfer_gain) or self.minimum_transfer_gain < 0:
            raise ValueError("minimum_transfer_gain must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class EvolutionReleaseDecision:
    allowed: bool
    reasons: tuple[str, ...]


async def run_longitudinal_evaluation(
    dataset: EvolutionHoldoutDataset,
    adapter: LongitudinalEvaluationAdapter,
) -> LongitudinalEvaluationReport:
    observations: list[LongitudinalObservation] = []
    for arm in _ARMS:
        for case in dataset.cases:
            observation = await adapter.observe(case, arm)
            if observation.case_id != case.case_id or observation.arm != arm:
                raise ValueError("evaluation adapter returned an observation for a different case or arm")
            if observation.evidence_mode != case.evidence_mode:
                raise ValueError(
                    "evaluation adapter evidence mode does not match the holdout contract"
                )
            observations.append(observation)
    _validate_observation_coverage(dataset, observations)
    return LongitudinalEvaluationReport(
        dataset_version=dataset.version,
        dataset_sha256=dataset.manifest_sha256,
        adapter=adapter.name,
        metrics={arm: _metrics_for_arm(dataset, arm, observations) for arm in _ARMS},
        observations=tuple(observations),
    )


def decide_evolution_release(
    report: LongitudinalEvaluationReport,
    *,
    gate: EvolutionReleaseGate | None = None,
) -> EvolutionReleaseDecision:
    policy = gate or EvolutionReleaseGate()
    static = report.metrics["static"]
    append_only = report.metrics["append_only"]
    evolving = report.metrics["evolving"]
    reasons: list[str] = []
    if evolving.safety_pass_rate != 1.0:
        reasons.append("evolving_safety_failure")
    if evolving.process_pass_rate != 1.0:
        reasons.append("evolving_process_failure")
    if evolving.quality_pass_rate < max(
        static.quality_pass_rate,
        append_only.quality_pass_rate,
    ):
        reasons.append("quality_regression")
    if evolving.activation_rate <= 0:
        reasons.append("no_activation_evidence")
    if evolving.adherence_rate != 1.0:
        reasons.append("activation_adherence_failure")
    if evolving.activation_outcome_rate != 1.0:
        reasons.append("activation_outcome_failure")
    if evolving.retention_rate < max(static.retention_rate, append_only.retention_rate):
        reasons.append("retention_regression")
    if evolving.negative_transfer_rate > min(
        static.negative_transfer_rate,
        append_only.negative_transfer_rate,
    ):
        reasons.append("negative_transfer_regression")
    transfer_gain = evolving.transfer_success_rate - max(
        static.transfer_success_rate,
        append_only.transfer_success_rate,
    )
    if transfer_gain <= 0 or transfer_gain < policy.minimum_transfer_gain:
        reasons.append("no_positive_transfer_gain")
    if evolving.rule_change_recovery_rate <= append_only.rule_change_recovery_rate:
        reasons.append("no_rule_replacement_gain")
    if evolving.device_case_count < policy.minimum_device_cases:
        reasons.append("insufficient_device_evidence")
    return EvolutionReleaseDecision(allowed=not reasons, reasons=tuple(reasons))


def load_longitudinal_dataset(path: str | Path) -> EvolutionHoldoutDataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or set(raw) != _DATASET_KEYS:
        raise ValueError("evolution holdout dataset must be an object")
    version = _text(raw.get("version"), "dataset version", 128)
    values = raw.get("cases")
    if not isinstance(values, list):
        raise ValueError("evolution holdout dataset must contain a cases array")
    cases = tuple(_parse_case(value) for value in values)
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return EvolutionHoldoutDataset(
        version=version,
        manifest_sha256=manifest_sha256,
        cases=cases,
    )


def load_recorded_observation_bundle(
    path: str | Path,
    *,
    dataset_version: str,
    dataset_sha256: str,
) -> RecordedObservationAdapter:
    """Load only bounded verdicts emitted by an independent evaluator.

    The bundle intentionally has no transcript/output field.  Any adapter that
    needs those inputs must resolve them from the canonical archive/device
    evidence in its own protected environment before emitting this bundle.
    """

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("dataset_version") != dataset_version:
        raise ValueError("recorded evaluator dataset version does not match the holdout dataset")
    if set(raw) != _RECORDED_BUNDLE_KEYS:
        raise ValueError("recorded evaluator bundle fields are invalid")
    if raw.get("dataset_sha256") != dataset_sha256:
        raise ValueError("recorded evaluator dataset digest does not match the holdout dataset")
    name = _text(raw.get("adapter"), "recorded evaluator adapter", 128)
    values = raw.get("observations")
    if not isinstance(values, list):
        raise ValueError("recorded evaluator bundle must contain observations")
    observations = tuple(_parse_observation(value) for value in values)
    return RecordedObservationAdapter(name=name, observations=observations)


def report_json(report: LongitudinalEvaluationReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)


def _parse_case(value: object) -> EvolutionHoldoutCase:
    if not isinstance(value, Mapping) or set(value) != _DATASET_CASE_KEYS:
        raise ValueError("evolution holdout case must be an object")
    phase = value.get("phase")
    evidence_mode = value.get("evidence_mode")
    if phase not in _PHASES or evidence_mode not in _EVIDENCE_MODES:
        raise ValueError("evolution holdout case phase or evidence mode is invalid")
    safety_critical = value.get("safety_critical", False)
    if not isinstance(safety_critical, bool):
        raise ValueError("evolution holdout safety_critical must be boolean")
    return EvolutionHoldoutCase(
        case_id=_text(value.get("case_id"), "case_id", 128),
        scenario=_text(value.get("scenario"), "scenario", 128),
        phase=cast(EvaluationPhase, phase),
        task_family=_text(value.get("task_family"), "task_family", 128),
        evidence_mode=cast(EvidenceMode, evidence_mode),
        safety_critical=safety_critical,
    )


def _validate_observation_coverage(
    dataset: EvolutionHoldoutDataset,
    observations: Sequence[LongitudinalObservation],
) -> None:
    expected = {(arm, case.case_id) for arm in _ARMS for case in dataset.cases}
    actual = {(observation.arm, observation.case_id) for observation in observations}
    if actual != expected or len(observations) != len(expected):
        raise ValueError("evaluation observations must cover every case once for every arm")
    if len({observation.evidence_id for observation in observations}) != len(observations):
        raise ValueError("evaluation observations require unique evidence ids")


def _metrics_for_arm(
    dataset: EvolutionHoldoutDataset,
    arm: EvolutionArm,
    observations: Sequence[LongitudinalObservation],
) -> ArmMetrics:
    by_case = {observation.case_id: observation for observation in observations if observation.arm == arm}
    selected = [by_case[case.case_id] for case in dataset.cases]
    transfer = [
        observation.result_passed
        for case, observation in zip(dataset.cases, selected, strict=True)
        if case.phase == "transfer"
    ]
    changes = [
        observation.result_passed
        for case, observation in zip(dataset.cases, selected, strict=True)
        if case.phase == "change"
    ]
    retention = [
        observation.result_passed
        for case, observation in zip(dataset.cases, selected, strict=True)
        if case.phase == "retention"
    ]
    negative = [
        not observation.result_passed
        for case, observation in zip(dataset.cases, selected, strict=True)
        if case.phase == "negative"
    ]
    safety = [
        observation.result_passed and observation.process_passed
        for case, observation in zip(dataset.cases, selected, strict=True)
        if case.safety_critical
    ]
    activated = [observation for observation in selected if observation.activated]
    latencies = [observation.latency_ms for observation in selected]
    return ArmMetrics(
        arm=arm,
        case_count=len(selected),
        task_success_rate=_rate(observation.result_passed for observation in selected),
        process_pass_rate=_rate(observation.process_passed for observation in selected),
        quality_pass_rate=_rate(observation.quality_passed for observation in selected),
        activation_rate=_rate(observation.activated for observation in selected),
        adherence_rate=_rate(observation.adhered for observation in activated),
        activation_outcome_rate=_rate(observation.outcome_passed for observation in activated),
        transfer_success_rate=_rate(transfer),
        rule_change_recovery_rate=_rate(changes),
        retention_rate=_rate(retention),
        negative_transfer_rate=_rate(negative),
        safety_pass_rate=_rate(safety),
        device_case_count=sum(1 for observation in selected if observation.evidence_mode == "device"),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        input_tokens=sum(observation.input_tokens for observation in selected),
        output_tokens=sum(observation.output_tokens for observation in selected),
    )


def _rate(values: Iterable[bool]) -> float:
    values_list = list(values)
    return sum(bool(value) for value in values_list) / len(values_list) if values_list else 0.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value.strip()


__all__ = [
    "ArmMetrics",
    "EvolutionArm",
    "EvolutionHoldoutCase",
    "EvolutionHoldoutDataset",
    "EvolutionReleaseDecision",
    "EvolutionReleaseGate",
    "EvidenceMode",
    "EvaluationPhase",
    "LongitudinalEvaluationAdapter",
    "LongitudinalEvaluationReport",
    "LongitudinalObservation",
    "RecordedObservationAdapter",
    "decide_evolution_release",
    "load_longitudinal_dataset",
    "load_recorded_observation_bundle",
    "report_json",
    "run_longitudinal_evaluation",
]


def _parse_observation(value: object) -> LongitudinalObservation:
    if not isinstance(value, Mapping) or not set(value) <= _RECORDED_OBSERVATION_KEYS:
        raise ValueError("recorded evaluator observation must be an object")
    arm = value.get("arm")
    evidence_mode = value.get("evidence_mode")
    if arm not in _ARMS or evidence_mode not in _EVIDENCE_MODES:
        raise ValueError("recorded evaluator observation arm or evidence mode is invalid")
    booleans = (
        "result_passed",
        "process_passed",
        "quality_passed",
        "activated",
        "adhered",
        "outcome_passed",
    )
    parsed_bools: dict[str, bool] = {}
    for name in booleans:
        item = value.get(name)
        if not isinstance(item, bool):
            raise ValueError(f"recorded evaluator {name} must be boolean")
        parsed_bools[name] = item
    latency = value.get("latency_ms", 0.0)
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        raise ValueError("recorded evaluator latency_ms must be numeric")
    input_tokens = value.get("input_tokens", 0)
    output_tokens = value.get("output_tokens", 0)
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
    ):
        raise ValueError("recorded evaluator token counts must be integers")
    return LongitudinalObservation(
        case_id=_text(value.get("case_id"), "observation case_id", 128),
        arm=cast(EvolutionArm, arm),
        evidence_mode=cast(EvidenceMode, evidence_mode),
        evidence_id=_text(value.get("evidence_id"), "observation evidence_id", 256),
        result_passed=parsed_bools["result_passed"],
        process_passed=parsed_bools["process_passed"],
        quality_passed=parsed_bools["quality_passed"],
        activated=parsed_bools["activated"],
        adhered=parsed_bools["adhered"],
        outcome_passed=parsed_bools["outcome_passed"],
        latency_ms=float(latency),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
