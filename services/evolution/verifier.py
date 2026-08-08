"""Deterministic three-layer trajectory verification.

The result and process layers never ask an LLM to guess environment truth or
permissions.  A quality judge may provide rubric verdicts, but it only enters
through bounded, dimension-level evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from services.evolution.domain import (
    EvidenceRef,
    FenceSnapshot,
    LayerVerdict,
    LearningSignal,
    SignalScope,
    SpeakerSnapshot,
    Verdict,
)


@dataclass(frozen=True, slots=True)
class ToolAction:
    tool_name: str
    status: Literal["succeeded", "failed"]
    fence_key: str

    def __post_init__(self) -> None:
        if not self.tool_name.strip() or len(self.tool_name) > 128:
            raise ValueError("tool action name is invalid")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("tool action status is invalid")
        if not self.fence_key.strip() or len(self.fence_key) > 512:
            raise ValueError("tool action fence key is invalid")


@dataclass(frozen=True, slots=True)
class TrajectoryObservation:
    """Canonicalized evidence supplied by the archive/agent boundary."""

    signal_id: str
    task_family: str
    account_id: str | None
    scope: SignalScope
    fence: FenceSnapshot
    speaker: SpeakerSnapshot
    source_event_ids: tuple[str, ...]
    canonicalized: bool
    task_completed: bool | None = None
    expected_state: Mapping[str, object] = field(default_factory=dict)
    actual_state: Mapping[str, object] = field(default_factory=dict)
    actions: tuple[ToolAction, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    privacy_violation: bool = False
    authorization_violation: bool = False
    stale_fence: bool = False
    commitment_action_consistent: bool | None = None
    quality_dimensions: tuple[tuple[str, Verdict], ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    environment_version: str = "unknown"
    artifact_versions: tuple[tuple[str, str], ...] = ()
    diagnosis: str = ""
    failure_code: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.canonicalized:
            raise ValueError("trajectory observation must be server-canonicalized")
        if (
            not self.signal_id.strip()
            or len(self.signal_id) > 128
            or not self.task_family.strip()
            or len(self.task_family) > 128
        ):
            raise ValueError("trajectory observation requires signal and task family")
        if (
            not self.source_event_ids
            or len(set(self.source_event_ids)) != len(self.source_event_ids)
            or any(not value.strip() or len(value) > 128 for value in self.source_event_ids)
        ):
            raise ValueError("trajectory observation requires unique source event ids")
        if len(set(self.quality_dimensions)) != len(self.quality_dimensions) or any(
            not name.strip() or len(name) > 64 for name, _ in self.quality_dimensions
        ):
            raise ValueError("quality dimensions must be unique")
        if any(name not in {"pass", "fail", "uncertain"} for _, name in self.quality_dimensions):
            raise ValueError("quality dimension verdict is invalid")
        if self.scope == "owner_private" and not self.account_id:
            raise ValueError("owner-private observation requires account id")
        if self.scope == "global_redacted" and self.account_id is not None:
            raise ValueError("global-redacted observation cannot carry account id")
        for state in (self.expected_state, self.actual_state):
            if any(not isinstance(key, str) or not key.strip() or len(key) > 64 for key in state):
                raise ValueError("trajectory state keys must be bounded strings")
        if len(set(self.allowed_tools)) != len(self.allowed_tools) or len(
            set(self.forbidden_tools)
        ) != len(self.forbidden_tools):
            raise ValueError("tool policy lists must be unique")
        if len(self.environment_version) > 128 or not self.environment_version.strip():
            raise ValueError("environment version is invalid")
        if len(self.diagnosis) > 2000:
            raise ValueError("trajectory diagnosis is too long")
        if self.failure_code is not None and (
            not self.failure_code.strip() or len(self.failure_code) > 96
        ):
            raise ValueError("trajectory failure code is invalid")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("trajectory token counts must be non-negative")
        if self.latency_ms is not None and (
            not math.isfinite(self.latency_ms) or self.latency_ms < 0
        ):
            raise ValueError("trajectory latency must be finite and non-negative")
        if self.created_at.tzinfo is None:
            raise ValueError("trajectory timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VerificationReport:
    signal_id: str
    task_family: str
    scope: SignalScope
    account_id: str | None
    fence: FenceSnapshot
    speaker: SpeakerSnapshot
    source_event_ids: tuple[str, ...]
    result: LayerVerdict
    process: LayerVerdict
    quality: LayerVerdict
    environment_version: str
    artifact_versions: tuple[tuple[str, str], ...]
    failure_code: str | None
    diagnosis: str
    input_tokens: int
    output_tokens: int
    latency_ms: float | None
    created_at: datetime

    @property
    def failed(self) -> bool:
        return any(
            verdict.verdict == "fail" for verdict in (self.result, self.process, self.quality)
        )

    def learning_signal(self) -> LearningSignal:
        return LearningSignal(
            signal_id=self.signal_id,
            task_family=self.task_family,
            scope=self.scope,
            account_id=self.account_id,
            fence=self.fence,
            speaker=self.speaker,
            source_event_ids=self.source_event_ids,
            result=self.result,
            process=self.process,
            quality=self.quality,
            environment_version=self.environment_version,
            failure_code=self.failure_code,
            diagnosis=self.diagnosis,
            artifact_versions=self.artifact_versions,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=self.latency_ms,
            created_at=self.created_at,
        )


def verify_trajectory(observation: TrajectoryObservation) -> VerificationReport:
    result = _verify_result(observation)
    process = _verify_process(observation)
    quality = _verify_quality(observation)
    # Deterministic privacy, authorization, tool and fence failures cannot be
    # relabelled by an evaluator into a lower-risk prompt/style problem.
    failure_code = (
        _failure_code(process)
        or observation.failure_code
        or _failure_code(result, quality)
    )
    diagnosis = observation.diagnosis or _diagnosis(result, process, quality)
    return VerificationReport(
        signal_id=observation.signal_id,
        task_family=observation.task_family,
        scope=observation.scope,
        account_id=observation.account_id,
        fence=observation.fence,
        speaker=observation.speaker,
        source_event_ids=observation.source_event_ids,
        result=result,
        process=process,
        quality=quality,
        environment_version=observation.environment_version,
        artifact_versions=observation.artifact_versions,
        failure_code=failure_code,
        diagnosis=diagnosis,
        input_tokens=observation.input_tokens,
        output_tokens=observation.output_tokens,
        latency_ms=observation.latency_ms,
        created_at=observation.created_at,
    )


def _verify_result(observation: TrajectoryObservation) -> LayerVerdict:
    refs = observation.evidence_refs
    if observation.task_completed is False:
        return LayerVerdict("fail", ("task_not_completed",), refs, 1.0)
    mismatches = tuple(
        key
        for key, expected in observation.expected_state.items()
        if key not in observation.actual_state or observation.actual_state[key] != expected
    )
    if mismatches:
        return LayerVerdict("fail", tuple(f"state_mismatch:{key}" for key in mismatches), refs, 1.0)
    if observation.task_completed is True or observation.expected_state:
        return LayerVerdict("pass", ("environment_state_verified",), refs, 1.0)
    return LayerVerdict("uncertain", ("environment_result_missing",), refs, 0.0)


def _verify_process(observation: TrajectoryObservation) -> LayerVerdict:
    refs = observation.evidence_refs
    reasons: list[str] = []
    if observation.privacy_violation:
        reasons.append("privacy_violation")
    if observation.authorization_violation:
        reasons.append("authorization_violation")
    if observation.stale_fence:
        reasons.append("stale_fence")
    allowed = set(observation.allowed_tools)
    forbidden = set(observation.forbidden_tools)
    for action in observation.actions:
        if action.tool_name in forbidden:
            reasons.append(f"forbidden_tool:{action.tool_name}")
        if allowed and action.tool_name not in allowed:
            reasons.append(f"tool_not_allowlisted:{action.tool_name}")
        if action.fence_key != observation.fence.key:
            reasons.append("tool_fence_mismatch")
    if observation.commitment_action_consistent is False:
        reasons.append("commitment_action_mismatch")
    if reasons:
        return LayerVerdict("fail", tuple(dict.fromkeys(reasons)), refs, 1.0)
    if observation.commitment_action_consistent is None and not observation.actions:
        return LayerVerdict("uncertain", ("process_evidence_incomplete",), refs, 0.0)
    return LayerVerdict("pass", ("policy_and_action_sequence_verified",), refs, 1.0)


def _verify_quality(observation: TrajectoryObservation) -> LayerVerdict:
    refs = observation.evidence_refs
    if not observation.quality_dimensions:
        return LayerVerdict("uncertain", ("quality_rubric_missing",), refs, 0.0)
    values = [verdict for _, verdict in observation.quality_dimensions]
    if "fail" in values:
        reasons = tuple(
            f"quality_fail:{name}" for name, verdict in observation.quality_dimensions if verdict == "fail"
        )
        return LayerVerdict("fail", reasons, refs, 1.0)
    if "uncertain" in values:
        return LayerVerdict("uncertain", ("quality_rubric_uncertain",), refs, 0.5)
    return LayerVerdict("pass", ("quality_rubric_passed",), refs, 1.0)


def _failure_code(*verdicts: LayerVerdict) -> str | None:
    for verdict in verdicts:
        if verdict.verdict == "fail" and verdict.reason_codes:
            return verdict.reason_codes[0]
    return None


def _diagnosis(*verdicts: LayerVerdict) -> str:
    reasons = [reason for verdict in verdicts for reason in verdict.reason_codes]
    return ";".join(dict.fromkeys(reasons))[:2000]


__all__ = [
    "ToolAction",
    "TrajectoryObservation",
    "VerificationReport",
    "verify_trajectory",
]
