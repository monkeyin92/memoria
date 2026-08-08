"""Longitudinal evaluation harness for static, append-only and evolving agents."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.evolution.release_policy import EvolutionReleasePolicy
from services.evolution.resolver import EvolutionResolver
from services.evolution.store import EvolutionStore

EvolutionMode = Literal["static", "append_only", "evolving"]
TaskPhase = Literal["learn", "transfer", "change", "retain"]


@dataclass(frozen=True, slots=True)
class EvolutionTask:
    task_id: str
    task_family: str
    phase: TaskPhase
    key: str
    expected_value: str
    feedback_value: str | None = None
    allow_update: bool = True

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.task_id, self.task_family, self.key)):
            raise ValueError("evaluation task identity must not be blank")
        if not self.expected_value.strip():
            raise ValueError("evaluation task expected value must not be blank")
        if self.feedback_value is not None and not self.feedback_value.strip():
            raise ValueError("evaluation feedback value must not be blank")


@dataclass(frozen=True, slots=True)
class EvolutionMetrics:
    mode: EvolutionMode
    task_count: int
    accuracy: float
    transfer_accuracy: float
    rule_replacement_accuracy: float
    retention_accuracy: float
    stale_reference_rate: float
    negative_transfer_rate: float
    update_count: int
    version_count: int

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "mode": self.mode,
            "task_count": self.task_count,
            "accuracy": self.accuracy,
            "transfer_accuracy": self.transfer_accuracy,
            "rule_replacement_accuracy": self.rule_replacement_accuracy,
            "retention_accuracy": self.retention_accuracy,
            "stale_reference_rate": self.stale_reference_rate,
            "negative_transfer_rate": self.negative_transfer_rate,
            "update_count": self.update_count,
            "version_count": self.version_count,
        }


@dataclass(frozen=True, slots=True)
class EvolutionEvaluationReport:
    dataset_version: str
    metrics: EvolutionMetrics
    task_results: tuple[tuple[str, bool, str | None, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_version": self.dataset_version,
            "metrics": self.metrics.as_dict(),
            "task_results": [
                {
                    "task_id": task_id,
                    "correct": correct,
                    "answer": answer,
                    "expected": expected,
                }
                for task_id, correct, answer, expected in self.task_results
            ],
        }


def run_evolution_evaluation(
    tasks: tuple[EvolutionTask, ...],
    *,
    mode: EvolutionMode,
    dataset_version: str = "self-evolution-zh-v1",
) -> EvolutionEvaluationReport:
    if not tasks:
        raise ValueError("evolution evaluation requires tasks")
    memory: dict[str, str] = {}
    versions: dict[str, int] = {}
    results: list[tuple[str, bool, str | None, str]] = []
    updates = 0
    for task in tasks:
        answer = memory.get(task.key)
        correct = answer == task.expected_value
        results.append((task.task_id, correct, answer, task.expected_value))
        if task.allow_update and task.feedback_value is not None and mode != "static":
            if mode == "append_only" and task.key in memory:
                continue
            if memory.get(task.key) != task.feedback_value:
                memory[task.key] = task.feedback_value
                versions[task.key] = versions.get(task.key, 0) + 1
                updates += 1
    by_phase: dict[TaskPhase, list[bool]] = {"learn": [], "transfer": [], "change": [], "retain": []}
    stale = negative = 0
    for task, (_, correct, answer, _) in zip(tasks, results, strict=True):
        by_phase[task.phase].append(correct)
        if task.phase == "change" and answer is not None and answer != task.expected_value:
            stale += 1
        if task.phase in {"transfer", "retain"} and not correct:
            negative += 1
    metrics = EvolutionMetrics(
        mode=mode,
        task_count=len(tasks),
        accuracy=_mean(correct for _, correct, _, _ in results),
        transfer_accuracy=_mean(by_phase["transfer"]),
        rule_replacement_accuracy=_mean(by_phase["change"]),
        retention_accuracy=_mean(by_phase["retain"]),
        stale_reference_rate=_ratio(stale, len(by_phase["change"])),
        negative_transfer_rate=_ratio(negative, len(by_phase["transfer"]) + len(by_phase["retain"])),
        update_count=updates,
        version_count=sum(versions.values()),
    )
    return EvolutionEvaluationReport(dataset_version, metrics, tuple(results))


def evaluate_all_modes(
    tasks: tuple[EvolutionTask, ...],
    *,
    dataset_version: str = "self-evolution-zh-v1",
) -> dict[EvolutionMode, EvolutionEvaluationReport]:
    modes: tuple[EvolutionMode, ...] = ("static", "append_only", "evolving")
    return {
        mode: run_evolution_evaluation(tasks, mode=mode, dataset_version=dataset_version)
        for mode in modes
    }


def default_evolution_tasks() -> tuple[EvolutionTask, ...]:
    """A small fixed set with learning, transfer, replacement and retention phases."""

    return (
        EvolutionTask("learn-weather-v1", "weather", "learn", "weather_date", "target_date", "target_date"),
        EvolutionTask("learn-retry-v1", "reliability", "learn", "retry_non_retryable", "stop", "stop"),
        EvolutionTask("learn-privacy-v1", "privacy", "learn", "guest_private", "deny", "deny"),
        EvolutionTask("learn-echo-v1", "voice", "learn", "echo_final", "drop", "drop"),
        EvolutionTask("learn-tool-v1", "tools", "learn", "tool_confirmation", "confirm", "confirm"),
        EvolutionTask("transfer-weather-paraphrase", "weather", "transfer", "weather_date", "target_date"),
        EvolutionTask("transfer-retry-paraphrase", "reliability", "transfer", "retry_non_retryable", "stop"),
        EvolutionTask("transfer-privacy-paraphrase", "privacy", "transfer", "guest_private", "deny"),
        EvolutionTask("change-weather-rule", "weather", "change", "weather_date", "new_target_date", "new_target_date"),
        EvolutionTask("change-weather-recovery", "weather", "change", "weather_date", "new_target_date"),
        EvolutionTask("change-privacy-rule", "privacy", "change", "guest_private", "deny_after_policy", "deny_after_policy"),
        EvolutionTask("change-privacy-recovery", "privacy", "change", "guest_private", "deny_after_policy"),
        EvolutionTask("change-retry-rule", "reliability", "change", "retry_non_retryable", "open_circuit", "open_circuit"),
        EvolutionTask("change-retry-recovery", "reliability", "change", "retry_non_retryable", "open_circuit"),
        EvolutionTask("retain-echo", "voice", "retain", "echo_final", "drop"),
        EvolutionTask("retain-tool", "tools", "retain", "tool_confirmation", "confirm"),
        EvolutionTask("retain-weather", "weather", "retain", "weather_date", "new_target_date"),
    )


def report_json(reports: dict[EvolutionMode, EvolutionEvaluationReport]) -> str:
    return json.dumps(
        {mode: report.as_dict() for mode, report in reports.items()},
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


@dataclass(frozen=True, slots=True)
class RuntimeControlPlaneMetrics:
    mode: EvolutionMode
    case_count: int
    activation_rate: float
    adherence_rate: float
    outcome_rate: float
    transfer_accuracy: float
    replacement_accuracy: float
    retention_accuracy: float
    privacy_pass_rate: float
    negative_transfer_rate: float
    stable_artifact_count: int

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "mode": self.mode,
            "case_count": self.case_count,
            "activation_rate": self.activation_rate,
            "adherence_rate": self.adherence_rate,
            "outcome_rate": self.outcome_rate,
            "transfer_accuracy": self.transfer_accuracy,
            "replacement_accuracy": self.replacement_accuracy,
            "retention_accuracy": self.retention_accuracy,
            "privacy_pass_rate": self.privacy_pass_rate,
            "negative_transfer_rate": self.negative_transfer_rate,
            "stable_artifact_count": self.stable_artifact_count,
        }


@dataclass(frozen=True, slots=True)
class RuntimeControlPlaneReport:
    dataset_version: str
    metrics: RuntimeControlPlaneMetrics
    cases: tuple[tuple[str, bool, tuple[str, ...]], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_version": self.dataset_version,
            "metrics": self.metrics.as_dict(),
            "cases": [
                {"case_id": case_id, "passed": passed, "artifact_ids": list(artifact_ids)}
                for case_id, passed, artifact_ids in self.cases
            ],
        }


@dataclass(frozen=True, slots=True)
class _RuntimeCase:
    case_id: str
    phase: Literal["transfer", "change", "retain", "privacy", "negative"]
    account_id: str
    speaker_class: Literal["owner", "guest", "uncertain"]
    query: str
    expected_artifact_id: str | None


def run_runtime_control_plane_evaluation(
    *,
    mode: EvolutionMode,
    dataset_version: str = "memoria-runtime-evolution-zh-v1",
    database_path: Path | None = None,
) -> RuntimeControlPlaneReport:
    """Exercise the real store, lifecycle and resolver with Chinese control cases.

    This is deliberately a control-plane harness, not a claim of real-phone
    acoustic acceptance. It validates candidate selection, version replacement,
    owner isolation and activation telemetry against the production modules.
    """

    temporary: TemporaryDirectory[str] | None = None
    if database_path is None:
        temporary = TemporaryDirectory(prefix="memoria-evolution-eval-")
        database_path = Path(temporary.name) / "evolution.sqlite3"
    try:
        store = EvolutionStore(database_path)
        root = "e" * 64
        if mode != "static":
            _promote_runtime_candidate(
                store,
                _runtime_candidate(
                    "weather-v1",
                    task_family="weather",
                    version=1,
                    scope="global_redacted",
                    account_id=None,
                    match_terms=("天气", "预报"),
                    instruction="回答天气时使用用户请求的目标日期。",
                    root=root,
                ),
            )
            _promote_runtime_candidate(
                store,
                _runtime_candidate(
                    "retry-v1",
                    task_family="reliability",
                    version=1,
                    scope="global_redacted",
                    account_id=None,
                    match_terms=("重试", "失败"),
                    instruction="不可重试错误必须停止并给出明确状态。",
                    root=root,
                ),
            )
            _promote_runtime_candidate(
                store,
                _runtime_candidate(
                    "owner-private-v1",
                    task_family="owner_preference",
                    version=1,
                    scope="owner_private",
                    account_id="owner-a",
                    match_terms=("我的私密",),
                    instruction="仅对已确认主人使用该私人规则。",
                    root=root,
                ),
            )
        if mode == "evolving":
            _promote_runtime_candidate(
                store,
                _runtime_candidate(
                    "weather-v2",
                    task_family="weather",
                    version=2,
                    scope="global_redacted",
                    account_id=None,
                    match_terms=("天气", "预报"),
                    instruction="回答天气时使用用户请求的目标日期和当地时区。",
                    root=root,
                ),
            )
        resolver = EvolutionResolver(
            store,
            trusted_root_sha256=root,
            canary_percent=100,
            release_policy=EvolutionReleasePolicy(
                frozenset({"weather", "reliability", "owner_preference"})
            ),
        )
        weather_expected = "weather-v2" if mode == "evolving" else "weather-v1"
        cases = (
            _RuntimeCase(
                "transfer-weather-paraphrase",
                "transfer",
                "guest-a",
                "guest",
                "南京明天天气预报怎么样？",
                weather_expected,
            ),
            _RuntimeCase(
                "change-weather-rule",
                "change",
                "guest-a",
                "guest",
                "杭州天气按当地日期回答。",
                "weather-v2",
            ),
            _RuntimeCase(
                "retain-retry-rule",
                "retain",
                "guest-a",
                "guest",
                "这个失败要不要重试？",
                "retry-v1",
            ),
            _RuntimeCase(
                "owner-private-allowed",
                "privacy",
                "owner-a",
                "owner",
                "我的私密资料怎么处理？",
                "owner-private-v1",
            ),
            _RuntimeCase(
                "owner-private-guest-denied",
                "privacy",
                "owner-a",
                "guest",
                "我的私密资料怎么处理？",
                None,
            ),
            _RuntimeCase(
                "negative-unrelated-chat",
                "negative",
                "guest-a",
                "guest",
                "讲一个睡前故事。",
                None,
            ),
        )
        results: list[tuple[str, bool, tuple[str, ...]]] = []
        for index, case in enumerate(cases):
            artifacts = resolver.resolve(
                account_id=case.account_id,
                session_id=f"runtime-eval-{index}",
                speaker_class=case.speaker_class,
                query=case.query,
            )
            artifact_ids = tuple(item.candidate_id for item in artifacts)
            passed = artifact_ids == ((case.expected_artifact_id,) if case.expected_artifact_id else ())
            results.append((case.case_id, passed, artifact_ids))
            for artifact in artifacts:
                store.record_activation(
                    candidate_id=artifact.candidate_id,
                    task_id=case.case_id,
                    activated=True,
                    adhered=passed,
                    outcome_passed=passed,
                    evidence_event_id=f"runtime-eval-event-{index}-{artifact.candidate_id}",
                )
        by_id = {case.case_id: passed for case, (_, passed, _) in zip(cases, results, strict=True)}
        all_metrics = [
            store.activation_metrics(candidate.candidate_id)
            for candidate in store.list_candidates(status="stable")
        ]
        activation_values = [item["activation_rate"] for item in all_metrics]
        adherence_values = [item["adherence_rate"] for item in all_metrics]
        outcome_values = [item["activation_outcome_rate"] for item in all_metrics]
        metrics = RuntimeControlPlaneMetrics(
            mode=mode,
            case_count=len(cases),
            activation_rate=sum(activation_values) / len(activation_values) if activation_values else 0.0,
            adherence_rate=sum(adherence_values) / len(adherence_values) if adherence_values else 0.0,
            outcome_rate=sum(outcome_values) / len(outcome_values) if outcome_values else 0.0,
            transfer_accuracy=float(by_id["transfer-weather-paraphrase"]),
            replacement_accuracy=float(by_id["change-weather-rule"]),
            retention_accuracy=float(by_id["retain-retry-rule"]),
            privacy_pass_rate=_mean(
                (by_id["owner-private-allowed"], by_id["owner-private-guest-denied"])
            ),
            negative_transfer_rate=float(not by_id["negative-unrelated-chat"]),
            stable_artifact_count=len(store.list_candidates(status="stable")),
        )
        return RuntimeControlPlaneReport(dataset_version, metrics, tuple(results))
    finally:
        if temporary is not None:
            temporary.cleanup()


def evaluate_runtime_control_plane_all_modes(
    *,
    dataset_version: str = "memoria-runtime-evolution-zh-v1",
) -> dict[EvolutionMode, RuntimeControlPlaneReport]:
    modes: tuple[EvolutionMode, ...] = ("static", "append_only", "evolving")
    return {
        mode: run_runtime_control_plane_evaluation(mode=mode, dataset_version=dataset_version)
        for mode in modes
    }


def runtime_control_plane_report_json(
    reports: dict[EvolutionMode, RuntimeControlPlaneReport],
) -> str:
    return json.dumps(
        {mode: report.as_dict() for mode, report in reports.items()},
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _runtime_candidate(
    candidate_id: str,
    *,
    task_family: str,
    version: int,
    scope: Literal["owner_private", "global_redacted"],
    account_id: str | None,
    match_terms: tuple[str, ...],
    instruction: str,
    root: str,
) -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family=task_family,
        kind="prompt",
        scope=scope,
        account_id=account_id,
        version=version,
        payload={"proposal": {"instruction": instruction, "match_terms": list(match_terms)}},
        source_signal_ids=(f"{candidate_id}-signal-a", f"{candidate_id}-signal-b"),
        expected_behavior="apply the reviewed runtime rule only to matching requests",
        regression_guards=("privacy_leakage_zero", "retention"),
        risk="low",
        trusted_root_sha256=root,
        created_at=now,
        updated_at=now,
    )


def _promote_runtime_candidate(store: EvolutionStore, candidate: CandidateArtifact) -> None:
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id=f"runtime-validation-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (f"{name}-runtime-evidence",))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    store.transition_candidate(candidate.candidate_id, "validated")
    store.transition_candidate(candidate.candidate_id, "canary")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"bootstrap-{candidate.candidate_id}-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"bootstrap-evidence-{candidate.candidate_id}-{index}",
        )
    store.transition_candidate(candidate.candidate_id, "stable")


def _mean(values: Iterable[bool]) -> float:
    values_list = list(values)
    return sum(bool(value) for value in values_list) / len(values_list) if values_list else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


__all__ = [
    "EvolutionEvaluationReport",
    "EvolutionMetrics",
    "EvolutionMode",
    "EvolutionTask",
    "default_evolution_tasks",
    "evaluate_all_modes",
    "report_json",
    "run_evolution_evaluation",
]
