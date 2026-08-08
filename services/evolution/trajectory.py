"""Canonical, replay-only trajectory contracts for offline evaluation.

Normal archive events prove delivery and preserve the evidence ledger.  They do
not prove that a task was solved.  This module is the narrow boundary where a
separate evaluator may turn a server-owned user/assistant pair into a
three-layer ``TrajectoryObservation``.  The evaluator can supply task and
quality facts, but it cannot choose the account scope, speaker snapshot,
generation fence, source evidence, or signal identity.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, cast

from services.archive.domain import EvidenceEvent
from services.evolution.domain import (
    EvidenceRef,
    FenceSnapshot,
    SignalScope,
    SpeakerSnapshot,
    Verdict,
)
from services.evolution.verifier import ToolAction, TrajectoryObservation

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_EVENT_ID_MAX = 128
_TEXT_MAX = 8_192
_STATE_VALUE_MAX = 256


class CanonicalTrajectoryError(ValueError):
    """The archive pair cannot safely be used as evaluation evidence."""


def _safe_label(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CanonicalTrajectoryError(f"{name} must be a string")
    clean = value.strip()
    if not clean or len(clean) > maximum or _LABEL.fullmatch(clean) is None:
        raise CanonicalTrajectoryError(f"{name} must be a bounded label")
    return clean


def _safe_event_id(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _EVENT_ID_MAX:
        raise CanonicalTrajectoryError(f"{name} must be a bounded event id")
    return value.strip()


def _nonnegative_int(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CanonicalTrajectoryError(f"{name} must be a non-negative integer")
    return value


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CanonicalTrajectoryError("canonical trajectory timestamp is invalid") from exc
    else:
        raise CanonicalTrajectoryError("canonical trajectory timestamp is required")
    if parsed.tzinfo is None:
        raise CanonicalTrajectoryError("canonical trajectory timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _payload(event: Mapping[str, object], *, name: str) -> Mapping[str, object]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise CanonicalTrajectoryError(f"{name} payload must be an object")
    return payload


def _event_mapping(event: EvidenceEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "account_id": event.account_id,
        "event_type": event.event_type,
        "speaker_class": event.speaker_class,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "generation_id": event.generation_id,
        "occurred_at": event.occurred_at,
        "tool_epoch": event.payload.get("tool_epoch"),
        "payload": dict(event.payload),
    }


@dataclass(frozen=True, slots=True)
class TrajectoryReplayInput:
    """The bounded view made available to an offline evaluator.

    Guest and uncertain trajectories are globally redacted: neither account
    identity nor transcript text can be used by the evaluator.  Owner-private
    trajectories may expose the archived, already-redacted text in memory, but
    no text from either scope can be written into a learning signal.
    """

    scope: SignalScope
    account_id: str | None
    fence: FenceSnapshot
    speaker: SpeakerSnapshot
    source_event_ids: tuple[str, str]
    user: Mapping[str, object]
    assistant: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CanonicalTrajectory:
    """A server-owned, matched archive pair suitable for replay."""

    scope: SignalScope
    account_id: str | None
    fence: FenceSnapshot
    speaker: SpeakerSnapshot
    user_event_id: str
    assistant_event_id: str
    occurred_at: datetime
    replay_input: TrajectoryReplayInput

    @property
    def source_event_ids(self) -> tuple[str, str]:
        return self.user_event_id, self.assistant_event_id

    @classmethod
    def from_events(cls, user_event: EvidenceEvent, assistant_event: EvidenceEvent) -> CanonicalTrajectory:
        return cls.from_mappings(_event_mapping(user_event), _event_mapping(assistant_event))

    @classmethod
    def from_mappings(
        cls,
        user_event: Mapping[str, object],
        assistant_event: Mapping[str, object],
    ) -> CanonicalTrajectory:
        user_payload = _payload(user_event, name="user event")
        assistant_payload = _payload(assistant_event, name="assistant event")
        account_id = _safe_event_id(user_event.get("account_id"), name="user account_id")
        if account_id != _safe_event_id(
            assistant_event.get("account_id"), name="assistant account_id"
        ):
            raise CanonicalTrajectoryError("canonical trajectory account mismatch")
        if user_event.get("event_type") != "speech.utterance_finalized":
            raise CanonicalTrajectoryError("canonical trajectory requires a finalized user turn")
        if assistant_event.get("event_type") != "assistant.playout_stopped":
            raise CanonicalTrajectoryError("canonical trajectory requires stopped assistant playout")
        speaker_class = user_event.get("speaker_class")
        if speaker_class not in {"owner", "guest", "uncertain"}:
            raise CanonicalTrajectoryError("canonical trajectory user speaker is invalid")
        if assistant_event.get("speaker_class") != "assistant":
            raise CanonicalTrajectoryError("canonical trajectory assistant speaker is invalid")
        if assistant_payload.get("actual_heard") is not True:
            raise CanonicalTrajectoryError("canonical trajectory requires actual-heard delivery")

        session_id = _safe_event_id(user_event.get("session_id"), name="user session_id")
        if session_id != _safe_event_id(assistant_event.get("session_id"), name="assistant session_id"):
            raise CanonicalTrajectoryError("canonical trajectory session mismatch")
        turn_id = _nonnegative_int(user_event.get("turn_id"), name="user turn_id")
        if turn_id != _nonnegative_int(assistant_event.get("turn_id"), name="assistant turn_id"):
            raise CanonicalTrajectoryError("canonical trajectory turn mismatch")
        generation_id = _nonnegative_int(user_event.get("generation_id"), name="user generation_id")
        if generation_id != _nonnegative_int(
            assistant_event.get("generation_id"), name="assistant generation_id"
        ):
            raise CanonicalTrajectoryError("canonical trajectory generation mismatch")
        tool_epoch = _nonnegative_int(user_event.get("tool_epoch"), name="user tool_epoch")
        if tool_epoch != _nonnegative_int(
            assistant_event.get("tool_epoch"), name="assistant tool_epoch"
        ):
            raise CanonicalTrajectoryError("canonical trajectory tool epoch mismatch")
        user_event_id = _safe_event_id(user_event.get("event_id"), name="user event_id")
        assistant_event_id = _safe_event_id(
            assistant_event.get("event_id"), name="assistant event_id"
        )
        _validate_response_provenance(
            assistant_payload,
            session_id=session_id,
            turn_id=turn_id,
            generation_id=generation_id,
            tool_epoch=tool_epoch,
        )

        try:
            raw_reason_code = user_payload.get("speaker_reason_code")
            reason_code = _canonical_reason_code(
                raw_reason_code,
                classification=cast(Any, speaker_class),
            )
            speaker = SpeakerSnapshot(
                classification=cast(Any, speaker_class),
                reason_code=reason_code,
                history_eligible=user_payload.get("history_eligible") is True,
                owner_projection_eligible=user_payload.get("owner_projection_eligible") is True,
                profile_id=(
                    str(user_payload["speaker_profile_id"])
                    if isinstance(user_payload.get("speaker_profile_id"), str)
                    else None
                ),
            )
        except ValueError as exc:
            raise CanonicalTrajectoryError("canonical trajectory speaker snapshot is invalid") from exc
        scope: SignalScope = (
            "owner_private"
            if speaker.classification == "owner"
            and speaker.history_eligible
            and speaker.owner_projection_eligible
            else "global_redacted"
        )
        scoped_account_id = account_id if scope == "owner_private" else None
        scoped_speaker = _speaker_for_scope(speaker, scope)
        if scope == "global_redacted":
            # Global evaluators need stable deduplication keys but must not
            # receive directly reusable archive/session identifiers.
            session_id = _opaque_ref("session", session_id)
            user_event_id = _opaque_ref("event", user_event_id)
            assistant_event_id = _opaque_ref("event", assistant_event_id)
        fence = FenceSnapshot(session_id, turn_id, generation_id, tool_epoch)
        occurred_at = _timestamp(assistant_event.get("occurred_at"))
        replay_input = TrajectoryReplayInput(
            scope=scope,
            account_id=scoped_account_id,
            fence=fence,
            speaker=scoped_speaker,
            source_event_ids=(user_event_id, assistant_event_id),
            user=_frozen_mapping(
                _evaluation_payload(user_payload, include_text=scope == "owner_private")
            ),
            assistant=_frozen_mapping(
                _evaluation_payload(assistant_payload, include_text=scope == "owner_private")
            ),
        )
        return cls(
            scope=scope,
            account_id=scoped_account_id,
            fence=fence,
            speaker=scoped_speaker,
            user_event_id=user_event_id,
            assistant_event_id=assistant_event_id,
            occurred_at=occurred_at,
            replay_input=replay_input,
        )


@dataclass(frozen=True, slots=True)
class EvaluatedToolAction:
    tool_name: str
    status: Literal["succeeded", "failed"]

    def __post_init__(self) -> None:
        _safe_label(self.tool_name, name="evaluated tool name", maximum=128)
        if self.status not in {"succeeded", "failed"}:
            raise CanonicalTrajectoryError("evaluated tool status is invalid")


@dataclass(frozen=True, slots=True)
class TrajectoryAssessment:
    """Bounded result emitted by an independent offline evaluator."""

    evaluator_version: str
    task_family: str
    environment_version: str
    task_completed: bool | None = None
    expected_state: Mapping[str, object] | None = None
    actual_state: Mapping[str, object] | None = None
    actions: tuple[EvaluatedToolAction, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    privacy_violation: bool = False
    authorization_violation: bool = False
    stale_fence: bool = False
    commitment_action_consistent: bool | None = None
    quality_dimensions: tuple[tuple[str, Verdict], ...] = ()
    failure_code: str | None = None
    diagnosis_code: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        _safe_label(self.evaluator_version, name="evaluator_version", maximum=128)
        _safe_label(self.task_family, name="task_family", maximum=128)
        _safe_label(self.environment_version, name="environment_version", maximum=128)
        if self.task_completed is not None and not isinstance(self.task_completed, bool):
            raise CanonicalTrajectoryError("task_completed must be boolean or null")
        expected_state = _bounded_state(self.expected_state or {}, name="expected_state")
        actual_state = _bounded_state(self.actual_state or {}, name="actual_state")
        object.__setattr__(self, "expected_state", _frozen_mapping(expected_state))
        object.__setattr__(self, "actual_state", _frozen_mapping(actual_state))
        if self.task_completed is None and not expected_state:
            raise CanonicalTrajectoryError("result evidence is required")
        _labels(self.allowed_tools, name="allowed_tools", maximum=128)
        _labels(self.forbidden_tools, name="forbidden_tools", maximum=128)
        if set(self.allowed_tools) & set(self.forbidden_tools):
            raise CanonicalTrajectoryError("tool policy cannot both allow and forbid a tool")
        if (
            self.commitment_action_consistent is None
            and not self.actions
            and not self.privacy_violation
            and not self.authorization_violation
            and not self.stale_fence
        ):
            raise CanonicalTrajectoryError("process evidence is required")
        if not self.quality_dimensions:
            raise CanonicalTrajectoryError("quality evidence is required")
        if len(self.quality_dimensions) > 32 or len(
            {name for name, _ in self.quality_dimensions}
        ) != len(self.quality_dimensions):
            raise CanonicalTrajectoryError("quality dimensions must be unique and bounded")
        for name, verdict in self.quality_dimensions:
            _safe_label(name, name="quality dimension", maximum=64)
            if verdict not in {"pass", "fail", "uncertain"}:
                raise CanonicalTrajectoryError("quality dimension verdict is invalid")
        if self.failure_code is not None:
            _safe_label(self.failure_code, name="failure_code", maximum=96)
        if self.diagnosis_code is not None:
            _safe_label(self.diagnosis_code, name="diagnosis_code", maximum=96)
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise CanonicalTrajectoryError("token counts must be non-negative")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, (int, float))
            or not math.isfinite(float(self.latency_ms))
            or self.latency_ms < 0
        ):
            raise CanonicalTrajectoryError("latency must be finite and non-negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TrajectoryAssessment:
        raw_actions = value.get("actions")
        actions: list[EvaluatedToolAction] = []
        if raw_actions is not None:
            if not isinstance(raw_actions, list) or len(raw_actions) > 32:
                raise CanonicalTrajectoryError("actions must be a bounded list")
            for action in raw_actions:
                if not isinstance(action, Mapping):
                    raise CanonicalTrajectoryError("evaluated action must be an object")
                status = action.get("status")
                if status not in {"succeeded", "failed"}:
                    raise CanonicalTrajectoryError("evaluated action status is invalid")
                actions.append(
                    EvaluatedToolAction(
                        tool_name=_safe_label(
                            action.get("tool_name"), name="evaluated tool name", maximum=128
                        ),
                        status=cast(Any, status),
                    )
                )
        raw_quality = value.get("quality_dimensions")
        quality: list[tuple[str, Verdict]] = []
        if raw_quality is not None:
            if not isinstance(raw_quality, Mapping) or len(raw_quality) > 32:
                raise CanonicalTrajectoryError("quality dimensions must be a bounded object")
            for name, verdict in raw_quality.items():
                quality.append(
                    (
                        _safe_label(name, name="quality dimension", maximum=64),
                        _verdict(verdict),
                    )
                )
        return cls(
            evaluator_version=_safe_label(
                value.get("evaluator_version"), name="evaluator_version", maximum=128
            ),
            task_family=_safe_label(value.get("task_family"), name="task_family", maximum=128),
            environment_version=_safe_label(
                value.get("environment_version"), name="environment_version", maximum=128
            ),
            task_completed=_optional_bool(value.get("task_completed"), name="task_completed"),
            expected_state=_bounded_state(value.get("expected_state") or {}, name="expected_state"),
            actual_state=_bounded_state(value.get("actual_state") or {}, name="actual_state"),
            actions=tuple(actions),
            allowed_tools=_labels(value.get("allowed_tools") or (), name="allowed_tools", maximum=128),
            forbidden_tools=_labels(
                value.get("forbidden_tools") or (), name="forbidden_tools", maximum=128
            ),
            privacy_violation=_required_bool(
                value.get("privacy_violation", False), name="privacy_violation"
            ),
            authorization_violation=_required_bool(
                value.get("authorization_violation", False), name="authorization_violation"
            ),
            stale_fence=_required_bool(value.get("stale_fence", False), name="stale_fence"),
            commitment_action_consistent=_optional_bool(
                value.get("commitment_action_consistent"),
                name="commitment_action_consistent",
            ),
            quality_dimensions=tuple(quality),
            failure_code=_optional_label(value.get("failure_code"), name="failure_code", maximum=96),
            diagnosis_code=_optional_label(
                value.get("diagnosis_code"), name="diagnosis_code", maximum=96
            ),
            input_tokens=_nonnegative_int(value.get("input_tokens", 0), name="input_tokens"),
            output_tokens=_nonnegative_int(value.get("output_tokens", 0), name="output_tokens"),
            latency_ms=_optional_latency(value.get("latency_ms")),
        )

    def observation(self, trajectory: CanonicalTrajectory, *, evaluation_id: str) -> TrajectoryObservation:
        safe_evaluation_id = _safe_label(evaluation_id, name="evaluation_id", maximum=128)
        return TrajectoryObservation(
            signal_id=evaluation_signal_id(safe_evaluation_id),
            task_family=self.task_family,
            account_id=trajectory.account_id,
            scope=trajectory.scope,
            fence=trajectory.fence,
            speaker=trajectory.speaker,
            source_event_ids=trajectory.source_event_ids,
            canonicalized=True,
            task_completed=self.task_completed,
            expected_state=_bounded_state(self.expected_state or {}, name="expected_state"),
            actual_state=_bounded_state(self.actual_state or {}, name="actual_state"),
            actions=tuple(
                ToolAction(action.tool_name, action.status, trajectory.fence.key)
                for action in self.actions
            ),
            allowed_tools=self.allowed_tools,
            forbidden_tools=self.forbidden_tools,
            privacy_violation=self.privacy_violation,
            authorization_violation=self.authorization_violation,
            stale_fence=self.stale_fence,
            commitment_action_consistent=self.commitment_action_consistent,
            quality_dimensions=self.quality_dimensions,
            evidence_refs=(
                EvidenceRef(
                    "canonical_trajectory_replay",
                    safe_evaluation_id,
                    trajectory.source_event_ids,
                ),
            ),
            environment_version=self.environment_version,
            artifact_versions=_artifact_versions(trajectory.replay_input, self.evaluator_version),
            diagnosis=self.diagnosis_code or "",
            failure_code=self.failure_code,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=self.latency_ms,
            # A server-owned event timestamp makes repeated delivery of the
            # same evaluation payload genuinely idempotent.
            created_at=trajectory.occurred_at,
        )


def _validate_response_provenance(
    payload: Mapping[str, object],
    *,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
) -> None:
    provenance = payload.get("response_provenance")
    if provenance is None:
        return
    if not isinstance(provenance, Mapping):
        raise CanonicalTrajectoryError("response provenance must be an object")
    fence = provenance.get("fence")
    if not isinstance(fence, Mapping):
        raise CanonicalTrajectoryError("response provenance fence is required")
    if (
        fence.get("session_id") != session_id
        or fence.get("turn_id") != turn_id
        or fence.get("generation_id") != generation_id
        or fence.get("tool_epoch") != tool_epoch
    ):
        raise CanonicalTrajectoryError("response provenance fence does not match trajectory")


def _speaker_for_scope(speaker: SpeakerSnapshot, scope: SignalScope) -> SpeakerSnapshot:
    if scope == "owner_private":
        return speaker
    return SpeakerSnapshot(
        classification=speaker.classification,
        # A guest/uncertain reason is routing metadata, not evaluator input.
        # Replace the archive-provided value so a forged field cannot smuggle
        # PII or transcript text into a global-redacted replay bundle.
        reason_code=f"redacted_{speaker.classification}",
        history_eligible=False,
        owner_projection_eligible=False,
        profile_id=None,
    )


def _canonical_reason_code(value: object, *, classification: str) -> str:
    if classification != "owner":
        return f"redacted_{classification}"
    if isinstance(value, str):
        clean = value.strip()
        if clean and len(clean) <= 96 and _LABEL.fullmatch(clean) is not None:
            return clean
    return "unavailable"


def _evaluation_payload(payload: Mapping[str, object], *, include_text: bool) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in (
        "input_modality",
        "interaction_mode",
        "mode_policy_version",
        "history_eligible",
        "owner_projection_eligible",
        "tool_epoch",
        "actual_heard",
    ):
        value = payload.get(key)
        if isinstance(value, (str, bool, int)):
            result[key] = value
    text = payload.get("text")
    if include_text and isinstance(text, str):
        result["text"] = text[:_TEXT_MAX]
    provenance = payload.get("response_provenance")
    if isinstance(provenance, Mapping):
        sanitized = _provenance_for_evaluator(provenance)
        if sanitized:
            result["response_provenance"] = sanitized
    return result


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _frozen_value(item) for key, item in value.items()})


def _frozen_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _frozen_mapping(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return tuple(_frozen_value(item) for item in value)
    return value


def _provenance_for_evaluator(provenance: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in ("planner_policy_version", "interaction_mode", "mode_policy_version"):
        value = provenance.get(key)
        if isinstance(value, str) and len(value) <= 128:
            result[key] = value
    fence = provenance.get("fence")
    if isinstance(fence, Mapping):
        result["fence"] = {
            key: fence[key]
            for key in ("session_id", "turn_id", "generation_id", "tool_epoch")
            if key in fence
        }
    artifacts = provenance.get("evolution_artifacts")
    if isinstance(artifacts, list):
        safe_artifacts: list[dict[str, object]] = []
        for artifact in artifacts[:16]:
            if not isinstance(artifact, Mapping):
                continue
            candidate_id = artifact.get("candidate_id")
            artifact_hash = artifact.get("artifact_hash")
            version = artifact.get("version")
            status = artifact.get("status")
            if (
                isinstance(candidate_id, str)
                and len(candidate_id) <= 128
                and isinstance(artifact_hash, str)
                and len(artifact_hash) == 64
                and isinstance(version, int)
                and not isinstance(version, bool)
                and version >= 1
                and status in {"canary", "stable"}
            ):
                safe_artifacts.append(
                    {
                        "candidate_id": candidate_id,
                        "artifact_hash": artifact_hash,
                        "version": version,
                        "status": status,
                    }
                )
        if safe_artifacts:
            result["evolution_artifacts"] = safe_artifacts
    return result


def _artifact_versions(input: TrajectoryReplayInput, evaluator_version: str) -> tuple[tuple[str, str], ...]:
    versions: dict[str, str] = {"trajectory_evaluator": evaluator_version}
    provenance = input.assistant.get("response_provenance")
    if isinstance(provenance, Mapping):
        planner = provenance.get("planner_policy_version")
        if isinstance(planner, str) and _LABEL.fullmatch(planner) is not None and len(planner) <= 128:
            versions["planner_policy"] = planner
        artifacts = provenance.get("evolution_artifacts")
        if isinstance(artifacts, (list, tuple)):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                candidate_id = artifact.get("candidate_id")
                artifact_hash = artifact.get("artifact_hash")
                version = artifact.get("version")
                status = artifact.get("status")
                if (
                    isinstance(candidate_id, str)
                    and _LABEL.fullmatch(candidate_id) is not None
                    and len(candidate_id) <= 86
                    and isinstance(artifact_hash, str)
                    and len(artifact_hash) == 64
                    and isinstance(version, int)
                    and not isinstance(version, bool)
                    and version >= 1
                    and status in {"canary", "stable"}
                ):
                    versions[f"evolution:{candidate_id}"] = f"v{version}:{status}:{artifact_hash}"
    return tuple(versions.items())


def _bounded_state(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CanonicalTrajectoryError(f"{name} must be an object")
    if len(value) > 32:
        raise CanonicalTrajectoryError(f"{name} has too many entries")
    result: dict[str, object] = {}
    for key, item in value.items():
        safe_key = _safe_label(key, name=f"{name} key", maximum=64)
        if isinstance(item, str):
            if len(item) > _STATE_VALUE_MAX or _LABEL.fullmatch(item) is None:
                raise CanonicalTrajectoryError(f"{name} values must be bounded labels")
            result[safe_key] = item
        elif item is None or isinstance(item, bool):
            result[safe_key] = item
        elif isinstance(item, int) and not isinstance(item, bool):
            result[safe_key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[safe_key] = item
        else:
            raise CanonicalTrajectoryError(f"{name} values must be bounded scalar evidence")
    return result


def _labels(value: object, *, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or len(value) > 32:
        raise CanonicalTrajectoryError(f"{name} must be a bounded list")
    labels = tuple(_safe_label(item, name=name, maximum=maximum) for item in value)
    if len(set(labels)) != len(labels):
        raise CanonicalTrajectoryError(f"{name} must be unique")
    return labels


def _verdict(value: object) -> Verdict:
    if value not in {"pass", "fail", "uncertain"}:
        raise CanonicalTrajectoryError("quality dimension verdict is invalid")
    return cast(Verdict, value)


def _optional_label(value: object, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _safe_label(value, name=name, maximum=maximum)


def _required_bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise CanonicalTrajectoryError(f"{name} must be boolean")
    return value


def _optional_bool(value: object, *, name: str) -> bool | None:
    if value is None:
        return None
    return _required_bool(value, name=name)


def _optional_latency(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalTrajectoryError("latency_ms must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise CanonicalTrajectoryError("latency_ms must be finite and non-negative")
    return result


def evaluation_signal_id(evaluation_id: str) -> str:
    digest = hashlib.sha256(f"memoria:evolution:evaluation:{evaluation_id}".encode()).hexdigest()
    return f"evaluation:{digest}"


def _opaque_ref(kind: str, value: str) -> str:
    return hashlib.sha256(f"memoria:evolution:global:{kind}:{value}".encode()).hexdigest()


__all__ = [
    "CanonicalTrajectory",
    "CanonicalTrajectoryError",
    "EvaluatedToolAction",
    "TrajectoryAssessment",
    "TrajectoryReplayInput",
    "evaluation_signal_id",
]
