"""Immutable contracts shared by the self-evolution control plane.

The module deliberately contains no model or tool execution code.  Evidence is
canonicalized before it enters these contracts; candidate artifacts are data
until an independent validator changes their lifecycle state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from services.common.redaction import redact_pii

Verdict = Literal["pass", "fail", "uncertain"]
SignalScope = Literal["owner_private", "global_redacted"]
ArtifactKind = Literal["knowledge", "prompt", "skill", "harness", "parameter"]
CandidateStatus = Literal["candidate", "validated", "canary", "stable", "rejected", "retired"]
LifecycleEventType = Literal[
    "transition",
    "supersede",
    "curate",
    "rollback_retire",
    "rollback_restore",
]
SpeakerClass = Literal["owner", "guest", "uncertain", "assistant", "system"]
RiskLevel = Literal["low", "medium", "high"]
REQUIRED_VALIDATION_GATES = frozenset(
    {
        "failure_replay",
        "retention",
        "transfer",
        "safety",
    }
)


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("evolution payload must be finite canonical JSON") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: str, *, name: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} must be a non-empty string <= {maximum} characters")
    return value.strip()


@dataclass(frozen=True, slots=True)
class FenceSnapshot:
    """The server-owned generation identity attached to one learning signal."""

    session_id: str
    turn_id: int
    generation_id: int
    tool_epoch: int

    def __post_init__(self) -> None:
        _text(self.session_id, name="session_id", maximum=128)
        if self.turn_id < 0 or self.generation_id < 0 or self.tool_epoch < 0:
            raise ValueError("fence counters must be non-negative")

    @property
    def key(self) -> str:
        return f"{self.session_id}:{self.turn_id}:{self.generation_id}:{self.tool_epoch}"


@dataclass(frozen=True, slots=True)
class SpeakerSnapshot:
    classification: SpeakerClass
    reason_code: str
    history_eligible: bool
    owner_projection_eligible: bool
    profile_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.reason_code, name="speaker reason_code", maximum=96)
        if self.profile_id is not None:
            _text(self.profile_id, name="speaker profile_id", maximum=128)
        if self.classification == "owner" and not (
            self.history_eligible and self.owner_projection_eligible
        ):
            raise ValueError("owner snapshot must be projection eligible")
        if self.classification != "owner" and self.owner_projection_eligible:
            raise ValueError("non-owner snapshot cannot be owner projection eligible")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    item_id: str
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.kind, name="evidence kind", maximum=64)
        _text(self.item_id, name="evidence item_id", maximum=128)
        if not self.source_event_ids or any(
            not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128
            for event_id in self.source_event_ids
        ):
            raise ValueError("evidence refs require bounded source event ids")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("evidence source event ids must be unique")


@dataclass(frozen=True, slots=True)
class LayerVerdict:
    verdict: Verdict
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("verdict confidence must be between zero and one")
        if len(self.reason_codes) > 16 or any(
            not isinstance(reason, str) or not reason.strip() or len(reason) > 96
            for reason in self.reason_codes
        ):
            raise ValueError("verdict reason codes are invalid")
        if len({(ref.kind, ref.item_id) for ref in self.evidence_refs}) != len(self.evidence_refs):
            raise ValueError("verdict evidence refs must be unique")


@dataclass(frozen=True, slots=True)
class LearningSignal:
    """An immutable, structured diagnosis of one canonical trajectory."""

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
    failure_code: str | None = None
    diagnosis: str = ""
    artifact_versions: tuple[tuple[str, str], ...] = ()
    supporting_signal_ids: tuple[str, ...] = ()
    refuting_signal_ids: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _text(self.signal_id, name="signal_id", maximum=128)
        _text(self.task_family, name="task_family", maximum=128)
        _text(self.environment_version, name="environment_version", maximum=128)
        if not self.source_event_ids or any(
            not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128
            for event_id in self.source_event_ids
        ):
            raise ValueError("learning signals require bounded source event ids")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("learning source event ids must be unique")
        if self.scope == "owner_private":
            if not self.account_id or self.speaker.classification != "owner":
                raise ValueError("owner-private signals require an owner account snapshot")
            if not self.speaker.history_eligible or not self.speaker.owner_projection_eligible:
                raise ValueError("owner-private signals require history eligibility")
        elif self.account_id is not None:
            raise ValueError("global-redacted signals cannot carry an account id")
        if self.failure_code is not None:
            _text(self.failure_code, name="failure_code", maximum=96)
        if len(self.diagnosis) > 2000:
            raise ValueError("diagnosis must not exceed 2000 characters")
        if any(value < 0 for value in (self.input_tokens, self.output_tokens)):
            raise ValueError("token counts must be non-negative")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        for ids, name in (
            (self.supporting_signal_ids, "supporting_signal_ids"),
            (self.refuting_signal_ids, "refuting_signal_ids"),
        ):
            if len(set(ids)) != len(ids) or any(not value.strip() for value in ids):
                raise ValueError(f"{name} must contain unique non-empty ids")
        if len(set(key for key, _ in self.artifact_versions)) != len(self.artifact_versions):
            raise ValueError("artifact versions must have unique keys")
        for key, version in self.artifact_versions:
            _text(key, name="artifact version key", maximum=96)
            _text(version, name="artifact version", maximum=128)

    @property
    def failed(self) -> bool:
        return any(
            verdict.verdict == "fail" for verdict in (self.result, self.process, self.quality)
        )

    @property
    def payload_hash(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))

    @property
    def trajectory_evaluation_key(self) -> tuple[str, str, str] | None:
        """Return the stable identity used to deduplicate evaluator evidence.

        A canonical trajectory is always a user/assistant pair.  The evaluator
        version is deliberately part of the key so independent evaluators may
        provide separate reviews while retries or re-keyed submissions from
        one evaluator cannot inflate support for a single pair.
        """

        evaluator_version = dict(self.artifact_versions).get("trajectory_evaluator")
        if evaluator_version is None or len(self.source_event_ids) != 2:
            return None
        return (
            self.source_event_ids[0],
            self.source_event_ids[1],
            evaluator_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "task_family": self.task_family,
            "scope": self.scope,
            "account_id": self.account_id,
            "fence": {
                "session_id": self.fence.session_id,
                "turn_id": self.fence.turn_id,
                "generation_id": self.fence.generation_id,
                "tool_epoch": self.fence.tool_epoch,
            },
            "speaker": {
                "classification": self.speaker.classification,
                "reason_code": self.speaker.reason_code,
                "history_eligible": self.speaker.history_eligible,
                "owner_projection_eligible": self.speaker.owner_projection_eligible,
                "profile_id": self.speaker.profile_id,
            },
            "source_event_ids": list(self.source_event_ids),
            "result": _verdict_dict(self.result),
            "process": _verdict_dict(self.process),
            "quality": _verdict_dict(self.quality),
            "environment_version": self.environment_version,
            "failure_code": self.failure_code,
            "diagnosis": self.diagnosis,
            "artifact_versions": dict(self.artifact_versions),
            "supporting_signal_ids": list(self.supporting_signal_ids),
            "refuting_signal_ids": list(self.refuting_signal_ids),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    candidate_id: str
    task_family: str
    kind: ArtifactKind
    scope: SignalScope
    account_id: str | None
    version: int
    payload: Mapping[str, object]
    source_signal_ids: tuple[str, ...]
    expected_behavior: str
    regression_guards: tuple[str, ...]
    risk: RiskLevel
    trusted_root_sha256: str
    status: CandidateStatus = "candidate"
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _text(self.candidate_id, name="candidate_id", maximum=128)
        _text(self.task_family, name="task_family", maximum=128)
        if self.version < 1:
            raise ValueError("candidate version must be positive")
        if self.scope == "owner_private" and not self.account_id:
            raise ValueError("owner-private candidates require an account")
        if self.scope == "global_redacted" and self.account_id is not None:
            raise ValueError("global-redacted candidates cannot carry an account")
        if not self.source_signal_ids or len(set(self.source_signal_ids)) != len(self.source_signal_ids):
            raise ValueError("candidate requires unique source signals")
        _text(self.expected_behavior, name="expected_behavior", maximum=2000)
        if not self.regression_guards or any(
            not isinstance(guard, str) or not guard.strip() or len(guard) > 500
            for guard in self.regression_guards
        ):
            raise ValueError("candidate requires bounded regression guards")
        if len(self.trusted_root_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.trusted_root_sha256.lower()
        ):
            raise ValueError("candidate trusted root digest must be sha256")
        if len(self.reason) > 500:
            raise ValueError("candidate lifecycle reason must not exceed 500 characters")
        payload_bytes = canonical_json(dict(self.payload)).encode("utf-8")
        if len(payload_bytes) > 64 * 1024:
            raise ValueError("candidate payload must not exceed 64 KiB")
        if self.kind == "prompt":
            _validate_prompt_payload(
                self.payload,
                global_redacted=self.scope == "global_redacted",
            )
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("candidate timestamps must be timezone-aware")

    @property
    def artifact_hash(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "task_family": self.task_family,
                    "kind": self.kind,
                    "scope": self.scope,
                    "account_id": self.account_id,
                    "version": self.version,
                    "payload": dict(self.payload),
                    "source_signal_ids": list(self.source_signal_ids),
                    "expected_behavior": self.expected_behavior,
                    "regression_guards": list(self.regression_guards),
                    "risk": self.risk,
                    "trusted_root_sha256": self.trusted_root_sha256,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One append-only candidate lifecycle decision in durable commit order."""

    sequence: int
    event_id: str
    candidate_id: str
    event_type: LifecycleEventType
    from_status: CandidateStatus
    to_status: CandidateStatus
    reason: str = ""
    related_candidate_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("lifecycle event sequence must be positive")
        _text(self.event_id, name="lifecycle event_id", maximum=128)
        _text(self.candidate_id, name="lifecycle candidate_id", maximum=128)
        if self.related_candidate_id is not None:
            _text(
                self.related_candidate_id,
                name="lifecycle related_candidate_id",
                maximum=128,
            )
        if len(self.reason) > 500:
            raise ValueError("lifecycle reason must not exceed 500 characters")
        if self.created_at.tzinfo is None:
            raise ValueError("lifecycle timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    evidence: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        _text(self.name, name="gate name", maximum=96)
        if len(self.reason) > 500:
            raise ValueError("gate reason must not exceed 500 characters")
        if any(not value.strip() for value in self.evidence):
            raise ValueError("gate evidence must be non-empty")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    validation_id: str
    candidate_id: str
    gates: tuple[GateResult, ...]
    metrics: tuple[tuple[str, float], ...] = ()
    validator_version: str = "evolution-verifier-v1"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _text(self.validation_id, name="validation_id", maximum=128)
        _text(self.candidate_id, name="candidate_id", maximum=128)
        _text(self.validator_version, name="validator_version", maximum=128)
        if not self.gates:
            raise ValueError("validation requires at least one gate")
        if len({gate.name for gate in self.gates}) != len(self.gates):
            raise ValueError("validation gate names must be unique")
        if len({key for key, _ in self.metrics}) != len(self.metrics):
            raise ValueError("validation metric keys must be unique")
        for key, value in self.metrics:
            _text(key, name="metric key", maximum=96)
            if value != value or value in {float("inf"), float("-inf")}:
                raise ValueError("validation metrics must be finite")
        if self.created_at.tzinfo is None:
            raise ValueError("validation timestamp must be timezone-aware")

    @property
    def passed(self) -> bool:
        gates = {gate.name: gate for gate in self.gates}
        return (
            self.required_gates_present
            and not self.missing_evidence_gates
            and all(gate.passed for gate in self.gates)
            and all(gates[name].passed for name in REQUIRED_VALIDATION_GATES)
        )

    @property
    def required_gates_present(self) -> bool:
        return REQUIRED_VALIDATION_GATES.issubset({gate.name for gate in self.gates})

    @property
    def missing_required_gates(self) -> tuple[str, ...]:
        return tuple(sorted(REQUIRED_VALIDATION_GATES - {gate.name for gate in self.gates}))

    @property
    def missing_evidence_gates(self) -> tuple[str, ...]:
        gates = {gate.name: gate for gate in self.gates}
        return tuple(
            sorted(
                name
                for name in REQUIRED_VALIDATION_GATES
                if name in gates and not gates[name].evidence
            )
        )

    def metric(self, name: str) -> float | None:
        return dict(self.metrics).get(name)


def _verdict_dict(verdict: LayerVerdict) -> dict[str, object]:
    return {
        "verdict": verdict.verdict,
        "reason_codes": list(verdict.reason_codes),
        "evidence_refs": [
            {
                "kind": ref.kind,
                "item_id": ref.item_id,
                "source_event_ids": list(ref.source_event_ids),
            }
            for ref in verdict.evidence_refs
        ],
        "confidence": verdict.confidence,
    }


_GLOBAL_REDACTED_PROMPT_KEYS = frozenset(
    {
        # These fields are structured, non-content provenance.  Free-form
        # diagnosis is intentionally excluded: it can accidentally contain a
        # guest transcript even when the trajectory was globally redacted.
        "cluster_key",
        "failure_code",
        "environments",
        "support_count",
        "refuting_signal_ids",
        "proposal",
    }
)
_GLOBAL_REDACTED_PROPOSAL_KEYS = frozenset({"instruction", "match_terms"})
_REDACTED_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _validate_global_redacted_prompt_payload(payload: Mapping[str, object]) -> None:
    """Reject content-bearing fields before a global candidate is persisted.

    Global-redacted candidates may be reused across accounts, so their
    manifest is deliberately narrower than owner-private candidates.  An
    allowlist (rather than a denylist) prevents future fields such as
    ``transcript``, ``account_id`` or ``private_notes`` from silently becoming
    durable global data.  The proposal itself is limited to the two fields
    consumed by the resolver.
    """

    if any(not isinstance(key, str) or key not in _GLOBAL_REDACTED_PROMPT_KEYS for key in payload):
        raise ValueError("global-redacted prompt payload contains a forbidden field")

    for key, maximum in (("cluster_key", 64), ("failure_code", 96)):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, str)
            or len(value) > maximum
            or _REDACTED_LABEL.fullmatch(value) is None
        ):
            raise ValueError("global-redacted prompt payload contains an invalid label")

    environments = payload.get("environments")
    if environments is not None and (
        not isinstance(environments, list)
        or not 1 <= len(environments) <= 32
        or any(
            not isinstance(value, str)
            or _REDACTED_LABEL.fullmatch(value) is None
            for value in environments
        )
    ):
        raise ValueError("global-redacted prompt payload contains invalid environments")

    support_count = payload.get("support_count")
    if support_count is not None and (
        not isinstance(support_count, int)
        or isinstance(support_count, bool)
        or not 2 <= support_count <= 10000
    ):
        raise ValueError("global-redacted prompt payload contains invalid support count")

    refuting_signal_ids = payload.get("refuting_signal_ids")
    if refuting_signal_ids is not None and (
        not isinstance(refuting_signal_ids, list)
        or len(refuting_signal_ids) > 100
        or any(
            not isinstance(value, str) or _REDACTED_LABEL.fullmatch(value) is None
            for value in refuting_signal_ids
        )
        or len(set(refuting_signal_ids)) != len(refuting_signal_ids)
    ):
        raise ValueError("global-redacted prompt payload contains invalid evidence ids")

    proposal = payload.get("proposal")
    if not isinstance(proposal, Mapping) or any(
        not isinstance(key, str) or key not in _GLOBAL_REDACTED_PROPOSAL_KEYS
        for key in proposal
    ):
        raise ValueError("global-redacted prompt proposal contains a forbidden field")


def _validate_prompt_payload(
    payload: Mapping[str, object],
    *,
    global_redacted: bool = False,
) -> None:
    """Keep the only hot-resolved artifact a small, typed rule object."""

    if global_redacted:
        _validate_global_redacted_prompt_payload(payload)

    proposal = payload.get("proposal")
    if not isinstance(proposal, Mapping):
        raise ValueError("prompt candidate requires a proposal object")
    instruction = proposal.get("instruction")
    terms = proposal.get("match_terms")
    if (
        not isinstance(instruction, str)
        or not instruction.strip()
        or len(instruction) > 1000
        or any(ord(character) < 32 and character not in "\n\t" for character in instruction)
        or not isinstance(terms, list)
        or not 1 <= len(terms) <= 16
        or any(
            not isinstance(term, str)
            or not term.strip()
            or len(term) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in term)
            for term in terms
        )
        or len({term.casefold() for term in terms}) != len(terms)
    ):
        raise ValueError(
            "prompt candidate proposal requires one bounded instruction and unique match terms"
        )
    if global_redacted and isinstance(instruction, str) and redact_pii(instruction) != instruction:
        raise ValueError("global-redacted prompt instruction contains PII")


__all__ = [
    "ArtifactKind",
    "CandidateArtifact",
    "CandidateStatus",
    "EvidenceRef",
    "FenceSnapshot",
    "GateResult",
    "LayerVerdict",
    "LearningSignal",
    "LifecycleEvent",
    "LifecycleEventType",
    "REQUIRED_VALIDATION_GATES",
    "SignalScope",
    "SpeakerSnapshot",
    "ValidationReport",
    "Verdict",
    "canonical_json",
    "sha256_text",
]
