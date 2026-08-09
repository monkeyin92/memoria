"""Audited control endpoints for offline Agent self-evolution curation."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.archive.domain import EvidenceEvent, LifeArchivePort
from services.control_api.app.account_gate import (
    AccountDeletingError,
    AccountOperationGate,
    SubjectProfileStore,
    require_capability_for_account_id,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.evolution.account_fence import AccountSubjectBlockedError, AccountWriteBlockedError
from services.evolution.curation import EvolutionControlPlane, SleepCycleReport
from services.evolution.domain import (
    CandidateArtifact,
    GateResult,
    LifecycleEvent,
    ValidationReport,
    canonical_json,
    sha256_text,
)
from services.evolution.runtime import EvolutionRuntimeCapture
from services.evolution.store import (
    EvolutionConflictError,
    EvolutionNotFoundError,
    EvolutionStore,
    EvolutionTransitionError,
)
from services.evolution.trajectory import CanonicalTrajectory, CanonicalTrajectoryError

router = APIRouter(prefix="/v1/evolution", tags=["evolution"])


class CandidateCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_id: str = Field(min_length=1, max_length=128)
    task_family: str = Field(min_length=1, max_length=128)
    kind: Literal["knowledge", "prompt", "skill", "harness", "parameter"]
    scope: Literal["owner_private", "global_redacted"]
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    version: int = Field(ge=1)
    payload: dict[str, Any]
    source_signal_ids: list[str] = Field(min_length=2, max_length=100)
    expected_behavior: str = Field(min_length=1, max_length=2000)
    regression_guards: list[str] = Field(min_length=1, max_length=32)
    risk: Literal["low", "medium", "high"] = "medium"

    @field_validator("source_signal_ids")
    @classmethod
    def validate_source_signal_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item.strip() or len(item) > 128 for item in value
        ):
            raise ValueError("source signal ids must be unique bounded strings")
        return value

    @field_validator("regression_guards")
    @classmethod
    def validate_regression_guards(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("regression guards must be bounded non-empty strings")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> CandidateCreateBody:
        if self.scope == "owner_private" and self.account_id is None:
            raise ValueError("owner-private candidates require an account")
        if self.scope == "global_redacted" and self.account_id is not None:
            raise ValueError("global-redacted candidates cannot carry an account")
        return self


class ValidationGateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=96)
    passed: bool
    evidence: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="", max_length=500)

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("validation evidence must contain bounded non-empty strings")
        return value


class ValidationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    validation_id: str = Field(min_length=1, max_length=128)
    gates: list[ValidationGateBody] = Field(min_length=1, max_length=32)
    metrics: dict[str, float] = Field(default_factory=dict)
    validator_version: str = Field(default="evolution-verifier-v1", min_length=1, max_length=128)


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: Literal["validated", "canary", "stable", "rejected", "retired"]
    reason: str = Field(default="", max_length=500)


class RollbackBody(BaseModel):
    """An operator-supplied reason is part of the rollback audit trail."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=500)


class SleepCycleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    candidate_kind: Literal["knowledge", "prompt", "skill", "harness", "parameter"] = "prompt"


class ActivationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    activated: bool
    adhered: bool
    outcome_passed: bool
    evidence_event_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_outcome(self) -> ActivationBody:
        if not self.activated and (self.adhered or self.outcome_passed):
            raise ValueError("inactive candidates cannot be adhered or outcome-passing")
        return self


class EvaluatedToolActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tool_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")
    status: Literal["succeeded", "failed"]


class TrajectoryEvaluationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evaluation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    account_id: str = Field(min_length=1, max_length=128)
    user_event_id: str = Field(min_length=1, max_length=128)
    assistant_event_id: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    task_family: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    task_completed: bool | None = None
    expected_state: dict[str, Any] = Field(default_factory=dict)
    actual_state: dict[str, Any] = Field(default_factory=dict)
    actions: list[EvaluatedToolActionBody] = Field(default_factory=list, max_length=32)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=32)
    privacy_violation: bool = False
    authorization_violation: bool = False
    stale_fence: bool = False
    commitment_action_consistent: bool | None = None
    quality_dimensions: dict[str, Literal["pass", "fail", "uncertain"]] = Field(
        default_factory=dict,
        max_length=32,
    )
    environment_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    diagnosis_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)

    @field_validator("allowed_tools", "forbidden_tools")
    @classmethod
    def validate_tool_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item.strip() or len(item) > 128 for item in value
        ):
            raise ValueError("tool policies must contain unique bounded names")
        return value

    @model_validator(mode="after")
    def validate_payload_bound(self) -> TrajectoryEvaluationBody:
        if len(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ) > 64 * 1024:
            raise ValueError("trajectory evaluation must not exceed 64 KiB")
        return self


class TrajectoryReplayBundleBody(BaseModel):
    """Server-owned event references used by an independent evaluator."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=128)
    user_event_id: str = Field(min_length=1, max_length=128)
    assistant_event_id: str = Field(min_length=1, max_length=128)


def _store(request: Request) -> EvolutionStore:
    return cast(EvolutionStore, request.app.state.evolution_store)


def _plane(request: Request) -> EvolutionControlPlane:
    return cast(EvolutionControlPlane, request.app.state.evolution_control_plane)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _runtime_capture(request: Request) -> EvolutionRuntimeCapture:
    return cast(EvolutionRuntimeCapture, request.app.state.evolution_runtime_capture)


def _require_account_evolution(request: Request, account_id: str) -> None:
    require_capability_for_account_id(
        account_id,
        "account_evolution",
        store=cast(SubjectProfileStore, request.app.state.memory_store),
    )


@asynccontextmanager
async def _evolution_read_lease(request: Request, account_id: str) -> AsyncIterator[None]:
    """Hold the same short read lease used by runtime resolution.

    Tombstone checks remain the cross-process backstop; the in-process lease
    closes the window in which account deletion could otherwise remove the
    archive rows while a validator is still assembling owner-private input.
    """

    if await asyncio.to_thread(_store(request).is_account_deleting, account_id):
        raise HTTPException(status_code=409, detail="account deletion is in progress")
    _require_account_evolution(request, account_id)
    gate = cast(AccountOperationGate | None, getattr(request.app.state, "account_operations", None))
    if gate is None:
        yield
        return
    try:
        async with gate.read(account_id):
            if await asyncio.to_thread(_store(request).is_account_deleting, account_id):
                raise HTTPException(status_code=409, detail="account deletion is in progress")
            yield
    except AccountDeletingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@asynccontextmanager
async def _candidate_read_lease(
    request: Request,
    candidate: CandidateArtifact,
) -> AsyncIterator[None]:
    """Protect owner-private candidate projections during account erasure."""

    if candidate.scope == "owner_private" and candidate.account_id:
        async with _evolution_read_lease(request, candidate.account_id):
            yield
    else:
        yield


def _require_evolution_control_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).evolution_internal_token()
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid evolution control token required")


def _require_evolution_validator_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).evolution_validator_token()
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid evolution validator token required")


def _candidate_payload(candidate: CandidateArtifact) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "task_family": candidate.task_family,
        "kind": candidate.kind,
        "scope": candidate.scope,
        "account_id": candidate.account_id,
        "version": candidate.version,
        "payload": dict(candidate.payload),
        "source_signal_ids": list(candidate.source_signal_ids),
        "expected_behavior": candidate.expected_behavior,
        "regression_guards": list(candidate.regression_guards),
        "risk": candidate.risk,
        "trusted_root_sha256": candidate.trusted_root_sha256,
        "artifact_hash": candidate.artifact_hash,
        "status": candidate.status,
        "reason": candidate.reason,
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


def _lifecycle_event_payload(event: LifecycleEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "candidate_id": event.candidate_id,
        "event_type": event.event_type,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "reason": event.reason,
        "related_candidate_id": event.related_candidate_id,
        "created_at": event.created_at.isoformat(),
    }


def _validation_payload(report: ValidationReport) -> dict[str, object]:
    return {
        "validation_id": report.validation_id,
        "candidate_id": report.candidate_id,
        "gates": [
            {
                "name": gate.name,
                "passed": gate.passed,
                "evidence": list(gate.evidence),
                "reason": gate.reason,
            }
            for gate in report.gates
        ],
        "metrics": dict(report.metrics),
        "validator_version": report.validator_version,
        "required_gates_present": report.required_gates_present,
        "missing_required_gates": list(report.missing_required_gates),
        "missing_evidence_gates": list(report.missing_evidence_gates),
        "passed": report.passed,
        "created_at": report.created_at.isoformat(),
    }


def _visible_to(candidate: CandidateArtifact, account_id: str) -> bool:
    if candidate.scope == "global_redacted":
        return candidate.account_id is None
    return candidate.scope == "owner_private" and candidate.account_id == account_id


def _candidate_or_404(request: Request, candidate_id: str) -> CandidateArtifact:
    try:
        return _store(request).get_candidate(candidate_id)
    except EvolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="evolution candidate not found") from exc


def _validate_activation_evidence(
    candidate: CandidateArtifact,
    event: EvidenceEvent,
    *,
    activated: bool,
) -> None:
    if candidate.scope == "owner_private" and event.account_id != candidate.account_id:
        raise HTTPException(status_code=422, detail="activation evidence scope mismatch")
    if candidate.kind != "prompt":
        raise HTTPException(
            status_code=409,
            detail=f"{candidate.kind} candidates require a separately released runtime adapter",
        )
    if event.event_type != "assistant.playout_stopped":
        raise HTTPException(status_code=422, detail="prompt activation requires assistant evidence")
    provenance = event.payload.get("response_provenance")
    artifacts = provenance.get("evolution_artifacts") if isinstance(provenance, Mapping) else None
    references = artifacts if isinstance(artifacts, list) else []
    exact = any(
        isinstance(reference, Mapping)
        and reference.get("candidate_id") == candidate.candidate_id
        and reference.get("version") == candidate.version
        and reference.get("kind") == "prompt"
        and reference.get("artifact_hash") == candidate.artifact_hash
        and reference.get("status") in {"canary", "stable"}
        for reference in references
    )
    if exact != activated:
        raise HTTPException(status_code=422, detail="activation evidence does not match provenance")


def _canonical_event_payload(event: EvidenceEvent) -> dict[str, object]:
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


def _evaluation_payload(body: TrajectoryEvaluationBody) -> dict[str, object]:
    return {
        "evaluator_version": body.evaluator_version,
        "task_family": body.task_family,
        "task_completed": body.task_completed,
        "expected_state": body.expected_state,
        "actual_state": body.actual_state,
        "actions": [action.model_dump() for action in body.actions],
        "allowed_tools": body.allowed_tools,
        "forbidden_tools": body.forbidden_tools,
        "privacy_violation": body.privacy_violation,
        "authorization_violation": body.authorization_violation,
        "stale_fence": body.stale_fence,
        "commitment_action_consistent": body.commitment_action_consistent,
        "quality_dimensions": body.quality_dimensions,
        "environment_version": body.environment_version,
        "failure_code": body.failure_code,
        "diagnosis_code": body.diagnosis_code,
        "input_tokens": body.input_tokens,
        "output_tokens": body.output_tokens,
        "latency_ms": body.latency_ms,
    }


async def _canonical_trajectory(
    request: Request,
    *,
    account_id: str,
    user_event_id: str,
    assistant_event_id: str,
) -> CanonicalTrajectory:
    store = _store(request)
    if await asyncio.to_thread(store.is_account_deleting, account_id):
        raise HTTPException(status_code=409, detail="account deletion is in progress")
    user_event, assistant_event = await asyncio.gather(
        _archive(request).event(account_id=account_id, event_id=user_event_id),
        _archive(request).event(account_id=account_id, event_id=assistant_event_id),
    )
    if user_event is None or assistant_event is None:
        raise HTTPException(status_code=422, detail="canonical trajectory pair is invalid")
    try:
        trajectory = CanonicalTrajectory.from_events(user_event, assistant_event)
    except CanonicalTrajectoryError as exc:
        raise HTTPException(status_code=422, detail="canonical trajectory pair is invalid") from exc
    if await asyncio.to_thread(store.is_account_deleting, account_id):
        raise HTTPException(status_code=409, detail="account deletion is in progress")
    return trajectory


def _portable_replay_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _portable_replay_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_portable_replay_value(item) for item in value]
    return value


@router.post("/trajectory-replay-bundles")
async def trajectory_replay_bundle(
    body: TrajectoryReplayBundleBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_validator_token)],
) -> dict[str, object]:
    """Return one bounded, scope-aware input to an independent evaluator.

    Owner text is the already-redacted archive value. Guest and uncertain
    trajectories omit both account identity and transcript text. The bundle
    contains no evaluator verdict and cannot create a learning signal by
    itself.
    """

    async with _evolution_read_lease(request, body.account_id):
        trajectory = await _canonical_trajectory(
            request,
            account_id=body.account_id,
            user_event_id=body.user_event_id,
            assistant_event_id=body.assistant_event_id,
        )
        replay = trajectory.replay_input
        payload: dict[str, object] = {
            "scope": replay.scope,
            "account_id": replay.account_id,
            "fence": {
                "session_id": replay.fence.session_id,
                "turn_id": replay.fence.turn_id,
                "generation_id": replay.fence.generation_id,
                "tool_epoch": replay.fence.tool_epoch,
            },
            "speaker": {
                "classification": replay.speaker.classification,
                "reason_code": replay.speaker.reason_code,
                "history_eligible": replay.speaker.history_eligible,
                "owner_projection_eligible": replay.speaker.owner_projection_eligible,
                "profile_id": replay.speaker.profile_id,
            },
            "source_event_ids": list(replay.source_event_ids),
            "user": _portable_replay_value(replay.user),
            "assistant": _portable_replay_value(replay.assistant),
        }
        return {
            "bundle": payload,
            "bundle_sha256": sha256_text(canonical_json(payload)),
        }


@router.post("/trajectory-evaluations", status_code=201)
async def record_trajectory_evaluation(
    body: TrajectoryEvaluationBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_validator_token)],
) -> dict[str, object]:
    """Append an assessment emitted by a separate offline replay evaluator.

    The request contains only bounded evaluation evidence.  The account scope,
    speaker identity and generation fence are reloaded from the append-only
    archive pair; a normal ``actual_heard`` event never reaches this endpoint
    by itself and never becomes a task-success signal.
    """
    async with _evolution_read_lease(request, body.account_id):
        user_event, assistant_event = await asyncio.gather(
            _archive(request).event(account_id=body.account_id, event_id=body.user_event_id),
            _archive(request).event(account_id=body.account_id, event_id=body.assistant_event_id),
        )
        if (
            user_event is None
            or assistant_event is None
            or user_event.event_type != "speech.utterance_finalized"
            or user_event.speaker_class not in {"owner", "guest", "uncertain"}
            or assistant_event.event_type != "assistant.playout_stopped"
            or assistant_event.speaker_class != "assistant"
            or assistant_event.payload.get("actual_heard") is not True
            or user_event.session_id is None
            or user_event.session_id != assistant_event.session_id
            or user_event.turn_id is None
            or user_event.turn_id != assistant_event.turn_id
            or user_event.generation_id is None
            or user_event.generation_id != assistant_event.generation_id
            or not isinstance(user_event.payload.get("tool_epoch"), int)
            or user_event.payload.get("tool_epoch") != assistant_event.payload.get("tool_epoch")
        ):
            raise HTTPException(status_code=422, detail="canonical trajectory pair is invalid")
        try:
            signal_id = await asyncio.to_thread(
                _runtime_capture(request).capture_evaluation_pair,
                _canonical_event_payload(user_event),
                _canonical_event_payload(assistant_event),
                evaluation_id=body.evaluation_id,
                evaluation=_evaluation_payload(body),
            )
        except AccountSubjectBlockedError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "minor_forbidden", "capability": "account_evolution"},
            ) from exc
        except (AccountWriteBlockedError, EvolutionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if signal_id is None:
            raise HTTPException(status_code=422, detail="canonical trajectory pair is invalid")
        signal = await asyncio.to_thread(_store(request).get_signal, signal_id)
        return {
            "signal_id": signal.signal_id,
            "task_family": signal.task_family,
            "scope": signal.scope,
            "failed": signal.failed,
            "failure_code": signal.failure_code,
            "result": signal.result.verdict,
            "process": signal.process.verdict,
            "quality": signal.quality.verdict,
            "source_event_ids": list(signal.source_event_ids),
        }


@router.post("/candidates", status_code=201)
async def create_candidate(
    body: CandidateCreateBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_control_token)],
) -> dict[str, object]:
    settings = cast(ControlSettings, request.app.state.settings)
    now = datetime.now(UTC)
    try:
        if body.scope == "owner_private" and body.account_id is not None:
            _require_account_evolution(request, body.account_id)
        candidate = CandidateArtifact(
            candidate_id=body.candidate_id,
            task_family=body.task_family,
            kind=body.kind,
            scope=body.scope,
            account_id=body.account_id,
            version=body.version,
            payload=body.payload,
            source_signal_ids=tuple(body.source_signal_ids),
            expected_behavior=body.expected_behavior,
            regression_guards=tuple(body.regression_guards),
            risk=body.risk,
            trusted_root_sha256=settings.evolution_trusted_root(),
            created_at=now,
            updated_at=now,
        )
        stored = await asyncio.to_thread(_plane(request).create_candidate, candidate)
    except EvolutionNotFoundError as exc:
        raise HTTPException(status_code=422, detail="candidate source signal not found") from exc
    except AccountSubjectBlockedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "minor_forbidden", "capability": "account_evolution"},
        ) from exc
    except AccountWriteBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EvolutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _candidate_payload(stored)


@router.get("/candidates")
async def list_candidates(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    status: Literal["candidate", "validated", "canary", "stable", "rejected", "retired"] | None = None,
    task_family: str | None = Query(default=None, max_length=128),
) -> dict[str, object]:
    async with _evolution_read_lease(request, user.user_id):
        statuses = (status,) if status is not None else None
        candidates = await asyncio.to_thread(
            _store(request).list_candidates_for_account,
            user.user_id,
            statuses=statuses,
            task_family=task_family,
        )
        return {
            "items": [
                _candidate_payload(item)
                for item in candidates
                if _visible_to(item, user.user_id)
            ]
        }


@router.get("/candidates/{candidate_id}")
async def get_candidate(
    candidate_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    candidate = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
    async with _candidate_read_lease(request, candidate):
        if not _visible_to(candidate, user.user_id):
            raise HTTPException(status_code=404, detail="evolution candidate not found")
        return _candidate_payload(candidate)


@router.get("/candidates/{candidate_id}/metrics")
async def candidate_metrics(
    candidate_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    candidate = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
    async with _candidate_read_lease(request, candidate):
        if not _visible_to(candidate, user.user_id):
            raise HTTPException(status_code=404, detail="evolution candidate not found")
        metrics = await asyncio.to_thread(_store(request).activation_metrics, candidate_id)
        return {"candidate_id": candidate_id, "metrics": metrics}


@router.get("/candidates/{candidate_id}/lifecycle-events")
async def candidate_lifecycle_events(
    candidate_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    candidate = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
    async with _candidate_read_lease(request, candidate):
        if not _visible_to(candidate, user.user_id):
            raise HTTPException(status_code=404, detail="evolution candidate not found")
        events = await asyncio.to_thread(
            _store(request).list_lifecycle_events,
            candidate_id=candidate_id,
        )
        return {
            "candidate_id": candidate_id,
            "items": [_lifecycle_event_payload(event) for event in events],
        }


@router.post("/candidates/{candidate_id}/validations", status_code=201)
async def record_validation(
    candidate_id: str,
    body: ValidationBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_validator_token)],
) -> dict[str, object]:
    try:
        candidate = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
        if candidate.scope == "owner_private" and candidate.account_id is not None:
            _require_account_evolution(request, candidate.account_id)
        report = ValidationReport(
            validation_id=body.validation_id,
            candidate_id=candidate_id,
            gates=tuple(
                GateResult(
                    name=gate.name,
                    passed=gate.passed,
                    evidence=tuple(gate.evidence),
                    reason=gate.reason,
                )
                for gate in body.gates
            ),
            metrics=tuple(body.metrics.items()),
            validator_version=body.validator_version,
            created_at=datetime.now(UTC),
        )
        stored = await asyncio.to_thread(_plane(request).record_validation, report)
    except EvolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="evolution candidate not found") from exc
    except AccountSubjectBlockedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "minor_forbidden", "capability": "account_evolution"},
        ) from exc
    except AccountWriteBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EvolutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _validation_payload(stored)


@router.post("/candidates/{candidate_id}/activations", status_code=201)
async def record_activation(
    candidate_id: str,
    body: ActivationBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_validator_token)],
) -> dict[str, object]:
    async with _evolution_read_lease(request, body.account_id):
        candidate = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
        event = await _archive(request).event(
            account_id=body.account_id,
            event_id=body.evidence_event_id,
        )
        if event is None:
            raise HTTPException(status_code=422, detail="canonical activation evidence not found")
        _validate_activation_evidence(candidate, event, activated=body.activated)
        try:
            activation_id = await asyncio.to_thread(
                _plane(request).record_activation,
                candidate_id=candidate_id,
                task_id=body.task_id,
                activated=body.activated,
                adhered=body.adhered,
                outcome_passed=body.outcome_passed,
                evidence_event_id=body.evidence_event_id,
            )
        except EvolutionTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AccountSubjectBlockedError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "minor_forbidden", "capability": "account_evolution"},
            ) from exc
        except AccountWriteBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EvolutionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"activation_id": activation_id, "candidate_id": candidate_id}


@router.post("/candidates/{candidate_id}/transition")
async def transition_candidate(
    candidate_id: str,
    body: TransitionBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_control_token)],
) -> dict[str, object]:
    try:
        current = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
        if current.scope == "owner_private" and current.account_id is not None:
            _require_account_evolution(request, current.account_id)
        candidate = await asyncio.to_thread(
            _plane(request).transition,
            candidate_id,
            body.target,
            reason=body.reason,
        )
    except EvolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="evolution candidate not found") from exc
    except EvolutionTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AccountSubjectBlockedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "minor_forbidden", "capability": "account_evolution"},
        ) from exc
    except AccountWriteBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _candidate_payload(candidate)


@router.post("/candidates/{candidate_id}/rollback")
async def rollback_candidate(
    candidate_id: str,
    body: RollbackBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_control_token)],
) -> dict[str, object]:
    try:
        current = await asyncio.to_thread(_candidate_or_404, request, candidate_id)
        if current.scope == "owner_private" and current.account_id is not None:
            _require_account_evolution(request, current.account_id)
        candidate = await asyncio.to_thread(
            _plane(request).rollback,
            candidate_id,
            reason=body.reason,
        )
    except EvolutionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="evolution candidate not found") from exc
    except EvolutionTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AccountSubjectBlockedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "minor_forbidden", "capability": "account_evolution"},
        ) from exc
    except AccountWriteBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _candidate_payload(candidate)


@router.post("/sleep-cycle")
async def run_sleep_cycle(
    body: SleepCycleBody,
    request: Request,
    _: Annotated[None, Depends(_require_evolution_control_token)],
) -> dict[str, object]:
    try:
        report = await asyncio.to_thread(
            _plane(request).sleep_cycle,
            force=body.force,
            candidate_kind=body.candidate_kind,
        )
    except AccountWriteBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _sleep_cycle_payload(report)


def _sleep_cycle_payload(report: SleepCycleReport) -> dict[str, object]:
    return {
        "ran": report.ran,
        "signal_count": report.signal_count,
        "new_signal_count": report.new_signal_count,
        "checkpoint": report.checkpoint,
        "clusters": [
            {
                "cluster_key": cluster.cluster_key,
                "task_family": cluster.task_family,
                "failure_code": cluster.failure_code,
                "scope": cluster.scope,
                "account_id": cluster.account_id,
                "signal_ids": list(cluster.signal_ids),
                "refuting_signal_ids": list(cluster.refuting_signal_ids),
                "environments": list(cluster.environments),
            }
            for cluster in report.clusters
        ],
        "candidates": [_candidate_payload(candidate) for candidate in report.candidates],
        "curation": report.curation,
    }


__all__ = ["router"]
