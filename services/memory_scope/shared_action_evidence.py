"""Authority evidence decoding for family-shared memory actions.

The executor and its Postgres adapters read one locked authority snapshot and
decode its JSON payloads here, at the boundary where untyped rows become typed
evidence.  The action failure vocabulary lives with the decoders that raise
it, so this module imports no database handle and the executor imports both
the evidence values and the errors it re-raises.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceDeclaredModeValue,
)

from services.policy.evidence import BindingStatusValue


class FamilySharedActionUnavailableError(RuntimeError):
    """The PostgreSQL action authority is not ready or cannot be proven."""


class FamilySharedActionDeniedError(PermissionError):
    """The current authenticated subject is not authorized for the action."""


class FamilySharedActionConflictError(RuntimeError):
    """The current proposal/resource fence lost a CAS race or is stale."""


def _decode_json(value: object, *, field: str) -> object:
    decoded = json.loads(value) if isinstance(value, str) else value
    if decoded is None:
        raise FamilySharedActionUnavailableError(f"{field} is unavailable")
    return decoded


def _json_object(value: object, *, field: str) -> dict[str, object]:
    decoded = _decode_json(value, field=field)
    if not isinstance(decoded, dict):
        raise FamilySharedActionUnavailableError(f"{field} is not an object")
    return {str(key): item for key, item in decoded.items()}


def _json_array(value: object, *, field: str) -> list[object]:
    decoded = _decode_json(value, field=field)
    if not isinstance(decoded, list):
        raise FamilySharedActionUnavailableError(f"{field} is not an array")
    return decoded


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FamilySharedActionUnavailableError(f"{field} is missing")
    return value


def _int(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FamilySharedActionUnavailableError(f"{field} is invalid")
    return value


def _time(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FamilySharedActionUnavailableError(
                f"{field} is invalid"
            ) from exc
    else:
        raise FamilySharedActionUnavailableError(f"{field} is invalid")
    if result.tzinfo is None or result.utcoffset() is None:
        raise FamilySharedActionUnavailableError(f"{field} must be timezone-aware")
    return result


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tuple_strings(value: object, *, field: str) -> tuple[str, ...]:
    values = _json_array(value, field=field)
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise FamilySharedActionUnavailableError(f"{field} contains an invalid id")
    return tuple(cast(str, item) for item in values)


@dataclass(frozen=True, slots=True)
class _BindingEvidence:
    binding_id: str
    version: int
    device_id: str
    status: BindingStatusValue
    declared_mode: DeviceDeclaredModeValue
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str

    def is_active_at(self, now: datetime) -> bool:
        return (
            self.status == "active"
            and self.valid_from <= now < self.valid_until
        )


@dataclass(frozen=True, slots=True)
class _MembershipEvidence:
    snapshot_id: str
    revision: int
    canonical_hash: str
    status: str
    family_space_id: str
    family_owner_subject_id: str
    subject_ids: tuple[str, ...]
    binding_id: str
    binding_version: int
    valid_from: datetime
    valid_until: datetime

    def is_active_at(self, now: datetime) -> bool:
        return (
            self.status == "active"
            and self.valid_from <= now < self.valid_until
        )


@dataclass(frozen=True, slots=True)
class _CaptureEvidence:
    evidence_id: str
    revision: int
    canonical_hash: str
    status: str
    subject_id: str
    binding_id: str
    binding_version: int
    valid_from: datetime
    valid_until: datetime

    def is_active_at(self, now: datetime) -> bool:
        return (
            self.status == "active"
            and self.valid_from <= now < self.valid_until
        )


@dataclass(frozen=True, slots=True)
class _ProposalEvidence:
    proposal_id: str
    revision: int
    canonical_hash: str
    status: str
    family_space_id: str
    family_owner_subject_id: str
    required_approval_subject_ids: tuple[str, ...]
    valid_until: datetime

    def is_active_at(self, now: datetime) -> bool:
        return (
            self.status == "active"
            and now < self.valid_until
        )


@dataclass(frozen=True, slots=True)
class _ApprovalEvidence:
    snapshot_id: str
    revision: int
    canonical_hash: str
    status: str
    subject_id: str
    proposal_id: str
    proposal_revision: int
    decision: str
    voted_at: datetime

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.voted_at <= now


def _binding_from_json(payload: object) -> _BindingEvidence:
    row = _json_object(payload, field="binding evidence")
    binding_id = _text(row.get("binding_id"), field="binding_id")
    version = _int(row.get("binding_version"), field="binding_version", minimum=1)
    device_id = _text(row.get("device_id"), field="device_id")
    valid_from = _time(row.get("valid_from"), field="binding.valid_from")
    valid_until = (
        datetime.max.replace(tzinfo=UTC)
        if row.get("valid_until") is None
        else _time(row.get("valid_until"), field="binding.valid_until")
    )
    canonical_hash = _text(
        row.get("canonical_hash")
        or _sha256(
            {
                "binding_id": binding_id,
                "version": version,
                "device_id": device_id,
                "declared_mode": row.get("declared_mode"),
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
            }
        ),
        field="binding.canonical_hash",
    )
    status = _text(row.get("status", "active"), field="binding.status")
    if status not in {"active", "superseded", "revoked", "expired"}:
        raise FamilySharedActionUnavailableError("binding.status is invalid")
    declared_mode = _text(row.get("declared_mode"), field="declared_mode")
    if declared_mode not in {
        "parent_for_child",
        "self_use",
        "child_for_parent",
        "family_shared",
    }:
        raise FamilySharedActionUnavailableError("declared_mode is invalid")
    return _BindingEvidence(
        binding_id=binding_id,
        version=version,
        device_id=device_id,
        status=cast(BindingStatusValue, status),
        declared_mode=cast(DeviceDeclaredModeValue, declared_mode),
        valid_from=valid_from,
        valid_until=valid_until,
        canonical_hash=canonical_hash,
    )


def _membership_from_json(payload: object) -> _MembershipEvidence:
    row = _json_object(payload, field="membership evidence")
    return _MembershipEvidence(
        snapshot_id=_text(row.get("snapshot_id"), field="membership.snapshot_id"),
        revision=_int(row.get("revision"), field="membership.revision", minimum=1),
        canonical_hash=_text(
            row.get("canonical_hash"), field="membership.canonical_hash"
        ),
        status=_text(row.get("status"), field="membership.status"),
        family_space_id=_text(
            row.get("family_space_id"), field="membership.family_space_id"
        ),
        family_owner_subject_id=_text(
            row.get("family_owner_subject_id"),
            field="membership.family_owner_subject_id",
        ),
        subject_ids=_tuple_strings(row.get("subject_ids"), field="membership.subject_ids"),
        binding_id=_text(row.get("binding_id"), field="membership.binding_id"),
        binding_version=_int(
            row.get("binding_version"), field="membership.binding_version", minimum=1
        ),
        valid_from=_time(row.get("valid_from"), field="membership.valid_from"),
        valid_until=_time(row.get("valid_until"), field="membership.valid_until"),
    )


def _captures_from_json(payload: object) -> tuple[_CaptureEvidence, ...]:
    result: list[_CaptureEvidence] = []
    for item in _json_array(payload, field="capture evidence"):
        row = _json_object(item, field="capture evidence item")
        result.append(
            _CaptureEvidence(
                evidence_id=_text(row.get("evidence_id"), field="evidence_id"),
                revision=_int(row.get("revision"), field="capture.revision", minimum=1),
                canonical_hash=_text(
                    row.get("canonical_hash"), field="capture.canonical_hash"
                ),
                status=_text(row.get("status"), field="capture.status"),
                subject_id=_text(row.get("subject_id"), field="capture.subject_id"),
                binding_id=_text(row.get("binding_id"), field="capture.binding_id"),
                binding_version=_int(
                    row.get("binding_version"),
                    field="capture.binding_version",
                    minimum=1,
                ),
                valid_from=_time(row.get("valid_from"), field="capture.valid_from"),
                valid_until=_time(row.get("valid_until"), field="capture.valid_until"),
            )
        )
    return tuple(result)


def _proposal_from_json(payload: object, *, now: datetime) -> tuple[dict[str, object], _ProposalEvidence]:
    row = _json_object(payload, field="proposal authority")
    proposal_id = _text(row.get("proposal_id"), field="proposal_id")
    revision = _int(row.get("proposal_revision"), field="proposal_revision", minimum=1)
    valid_until = _time(row.get("valid_until"), field="proposal.valid_until")
    raw_status = _text(row.get("status"), field="proposal.status")
    # A privacy command may freeze or withdraw an already-promoted record.
    # The SQL port still owns the command-specific state transition; keeping
    # every non-withdrawn proposal as current evidence lets the receipt/fence
    # revalidation cover that revocation path as well.
    evidence_status = (
        "active"
        if raw_status in {"pending", "approvals_complete", "promoted", "frozen"}
        else "inactive"
    )
    required = tuple(
        dict.fromkeys(
            (
                _text(row.get("proposer_subject_id"), field="proposer_subject_id"),
                *_tuple_strings(row.get("co_subject_ids"), field="co_subject_ids"),
            )
        )
    )
    evidence = _ProposalEvidence(
        proposal_id=proposal_id,
        revision=revision,
        canonical_hash=_sha256(
            {
                "proposal_id": proposal_id,
                "revision": revision,
                "family_space_id": row.get("family_space_id"),
                "proposer_subject_id": row.get("proposer_subject_id"),
                "co_subject_ids": sorted(required),
                "status": raw_status,
            }
        ),
        status=evidence_status,
        family_space_id=_text(row.get("family_space_id"), field="family_space_id"),
        family_owner_subject_id=_text(
            row.get("proposer_subject_id"), field="family_owner_subject_id"
        ),
        required_approval_subject_ids=required,
        valid_until=valid_until,
    )
    if valid_until <= now and evidence_status == "active":
        raise FamilySharedActionConflictError("shared proposal fence has expired")
    return row, evidence


def _approvals_from_json(payload: object) -> tuple[_ApprovalEvidence, ...]:
    latest: dict[str, _ApprovalEvidence] = {}
    for item in _json_array(payload, field="approval evidence"):
        row = _json_object(item, field="approval evidence item")
        evidence = _ApprovalEvidence(
            snapshot_id=_text(row.get("snapshot_id"), field="approval.snapshot_id"),
            revision=_int(row.get("revision"), field="approval.revision", minimum=1),
            canonical_hash=_text(
                row.get("canonical_hash"), field="approval.canonical_hash"
            ),
            status=_text(row.get("status"), field="approval.status"),
            subject_id=_text(row.get("subject_id"), field="approval.subject_id"),
            proposal_id=_text(row.get("proposal_id"), field="approval.proposal_id"),
            proposal_revision=_int(
                row.get("proposal_revision"),
                field="approval.proposal_revision",
                minimum=1,
            ),
            decision=_text(row.get("decision"), field="approval.decision"),
            voted_at=_time(row.get("voted_at"), field="approval.voted_at"),
        )
        previous = latest.get(evidence.subject_id)
        if previous is None or previous.voted_at <= evidence.voted_at:
            latest[evidence.subject_id] = evidence
    return tuple(sorted(latest.values(), key=lambda item: item.subject_id))
