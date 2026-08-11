"""The deep boundary for family-shared memory actions.

HTTP and future callers provide business intent only.  The executor owns the
translation from one immutable authority snapshot to the resource-specific
authorization and durable lifecycle operation.  In particular, this module
does not expose receipt, consent, family or fence fields as action input.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, Protocol, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceDeclaredModeValue,
    PolicyActionResourceFence,
    PolicyApprovalSnapshotFence,
)

from services.memory_scope.repository import MemoryAuthoritySnapshot
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    PostgresActionAuthorizer,
)
from services.policy.action_fence import (
    DEFAULT_ACTION_FENCE_TTL,
    build_action_resource_fence,
)
from services.policy.context import PolicyContext
from services.policy.evidence import (
    ApprovalEvidencePort,
    BindingEvidencePort,
    BindingStatusValue,
    CaptureEvidencePort,
    MembershipEvidencePort,
    ProposalEvidencePort,
)
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.production_wiring import (
    PostgresCurrentConsentAuthorityAdapter,
    ProductionAuthorityAdapters,
    SensitiveWriteService,
    build_composite_action_authority,
)
from services.policy.receipts import (
    PolicyReceiptConflictError,
    PolicyReceiptV2,
)

LOGGER = logging.getLogger(__name__)

FamilySharedActionName = Literal["propose", "confirm", "object", "withdraw"]


@dataclass(frozen=True, slots=True)
class FamilySharedActionInput:
    """Validated business intent for exactly one shared-memory action."""

    name: FamilySharedActionName
    proposal_id: str | None = None
    title: str | None = None
    content: str | None = None
    source_evidence_ids: tuple[str, ...] = ()
    co_subject_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.name == "propose":
            if self.proposal_id is not None:
                raise ValueError("propose must not include proposal_id")
            if not self.title or not self.content:
                raise ValueError("propose requires title and content")
            if not self.source_evidence_ids:
                raise ValueError("propose requires source evidence")
            return
        if not self.proposal_id:
            raise ValueError(f"{self.name} requires proposal_id")
        if any(
            value is not None
            for value in (self.title, self.content)
        ) or self.source_evidence_ids or self.co_subject_ids:
            raise ValueError(f"{self.name} accepts proposal_id only")

    @classmethod
    def propose(
        cls,
        *,
        title: str,
        content: str,
        source_evidence_ids: tuple[str, ...],
        co_subject_ids: tuple[str, ...],
    ) -> FamilySharedActionInput:
        return cls(
            name="propose",
            title=title,
            content=content,
            source_evidence_ids=source_evidence_ids,
            co_subject_ids=co_subject_ids,
        )

    @classmethod
    def command(
        cls,
        name: Literal["confirm", "object", "withdraw"],
        *,
        proposal_id: str,
    ) -> FamilySharedActionInput:
        return cls(name=name, proposal_id=proposal_id)


@dataclass(frozen=True, slots=True)
class FamilySharedActionResult:
    """Stable result returned by the shared-action boundary."""

    proposal_id: str
    status: str


class FamilySharedActionExecutorPort(Protocol):
    """Execute one family-shared action from authoritative context.

    Implementations must obtain/verify the exact resource-scoped policy and
    consent evidence inside their own transaction.  A profile receipt in the
    snapshot is deliberately not an implementation shortcut for that work.
    """

    async def execute(
        self,
        *,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
    ) -> FamilySharedActionResult: ...


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


async def _fetch_json_value(
    connection: asyncpg.Connection,
    query: str,
    *args: object,
    field: str,
) -> object:
    try:
        return await connection.fetchval(query, *args)
    except asyncpg.PostgresError as exc:
        _raise_database_error(exc, field=field)
        raise AssertionError("unreachable") from exc


async def _fetch_json_object(
    connection: asyncpg.Connection,
    query: str,
    *args: object,
    field: str,
) -> dict[str, object]:
    return _json_object(
        await _fetch_json_value(connection, query, *args, field=field),
        field=field,
    )


def _raise_database_error(
    exc: asyncpg.PostgresError, *, field: str
) -> NoReturn:
    state = getattr(exc, "sqlstate", None)
    if state in {"SR403", "42501"}:
        raise FamilySharedActionDeniedError(f"{field} denied") from exc
    if state in {"SR409", "SR412", "23505"}:
        raise FamilySharedActionConflictError(f"{field} fence conflict") from exc
    if state in {"SR404"}:
        raise FamilySharedActionDeniedError(f"{field} not found") from exc
    raise FamilySharedActionUnavailableError(f"{field} authority unavailable") from exc


def _raise_policy_error(exc: ActionAuthorizationError) -> NoReturn:
    message = str(exc)
    if any(
        marker in message
        for marker in (
            "required ",
            "authority adapter",
            "transaction",
            "receipt does not exist",
        )
    ):
        raise FamilySharedActionUnavailableError(message) from exc
    raise FamilySharedActionDeniedError(message) from exc


class _ActionExecutorReceiptRepository(ConnectionBoundPolicyReceiptRepository):
    """Policy receipt bridge for the no-table-privilege action role."""

    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        if not connection.is_in_transaction():
            raise RuntimeError(
                "action executor receipt insertion requires a caller transaction"
            )
        if len({receipt.receipt_id for receipt in receipts}) != len(receipts):
            raise PolicyReceiptConflictError("receipt batch contains duplicate ids")
        for receipt in sorted(receipts, key=lambda item: item.receipt_id):
            try:
                raw = await connection.fetchval(
                    "SELECT action_policy_insert_receipt($1::jsonb)",
                    json.dumps(receipt.model_dump(mode="json")),
                )
            except asyncpg.PostgresError as exc:
                _raise_database_error(exc, field="policy receipt insert")
            current = PolicyReceiptV2.model_validate(
                _json_object(raw, field="policy receipt insert result")
            )
            if current != receipt:
                raise PolicyReceiptConflictError(
                    "policy receipt id is immutable and content differs"
                )


async def _lock_action_receipt(
    connection: asyncpg.Connection,
    receipt_id: str,
) -> PolicyReceiptV2 | None:
    try:
        raw = await connection.fetchval(
            "SELECT action_policy_lock_receipt($1)", receipt_id
        )
    except asyncpg.PostgresError as exc:
        _raise_database_error(exc, field="policy receipt lock")
    if raw is None:
        return None
    return PolicyReceiptV2.model_validate(
        _json_object(raw, field="policy receipt lock result")
    )


class _PrincipalAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> str:
        try:
            valid = await connection.fetchval(
                """
                SELECT session_runtime_assert_action_context(
                    $1, $2, $3, $4, $5, $6, $7
                )
                """,
                receipt.session_id,
                receipt.runtime_profile_id,
                receipt.actor_id,
                receipt.device_id,
                receipt.binding_id,
                receipt.binding_version,
                receipt.session_epoch,
            )
            trust_raw = await connection.fetchval(
                "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
                receipt.actor_id,
                receipt.device_id,
                receipt.binding_id,
                receipt.binding_version,
                request.now,
            )
            actor = await connection.fetchval(
                "SELECT current_setting('app.authenticated_actor', true)"
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Session/Device authority")
        if valid is not True or actor != receipt.actor_id:
            raise ActionAuthorizationError("authenticated principal mismatch")
        trust = _json_object(trust_raw, field="device authority")
        if trust.get("device_trust") != request.context.device_trust:
            raise ActionAuthorizationError("device trust changed during action")
        return cast(str, actor)


class _BindingAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> BindingEvidencePort:
        try:
            raw = await connection.fetchval(
                "SELECT action_identity_lock_binding($1, $2, $3, $4)",
                receipt.actor_id,
                receipt.device_id,
                receipt.binding_version,
                request.now,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Identity binding authority")
        if raw is None:
            raise ActionAuthorizationError("binding authority is unavailable")
        binding = _binding_from_json(raw)
        if (
            binding.binding_id != receipt.binding_id
            or binding.version != receipt.binding_version
            or binding.device_id != receipt.device_id
        ):
            raise ActionAuthorizationError("binding evidence mismatch")
        return binding


class _MembershipAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> MembershipEvidencePort:
        fence = receipt.action_resource_fence
        family_space_id = fence.family_space_id
        if family_space_id is None or fence.membership_snapshot_id is None:
            raise ActionAuthorizationError("membership fence is missing")
        required = tuple(fence.required_approval_subject_ids)
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_membership($1, $2::text[], $3, $4, $5)",
            family_space_id,
            list(required),
            receipt.binding_id,
            receipt.binding_version,
            request.now,
            field="membership authority",
        )
        membership = _membership_from_json(raw)
        if (
            membership.snapshot_id != fence.membership_snapshot_id
            or membership.revision != fence.membership_snapshot_revision
            or membership.canonical_hash != fence.membership_snapshot_hash
            or membership.family_owner_subject_id != fence.family_owner_subject_id
        ):
            raise ActionAuthorizationError("membership evidence mismatch")
        return membership


class _CaptureAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[CaptureEvidencePort, ...]:
        fence = receipt.action_resource_fence
        if not fence.capture_evidence_ids:
            return ()
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_capture_evidence($1::text[], $2, $3, $4, $5)",
            list(fence.capture_evidence_ids),
            receipt.subject_id,
            receipt.binding_id,
            receipt.binding_version,
            request.now,
            field="capture authority",
        )
        captures = _captures_from_json(raw)
        if tuple(sorted(item.evidence_id for item in captures)) != tuple(
            sorted(fence.capture_evidence_ids)
        ):
            raise ActionAuthorizationError("capture evidence set mismatch")
        return captures


class _ProposalAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> ProposalEvidencePort:
        proposal_id = receipt.action_resource_fence.proposal_id
        if proposal_id is None:
            raise ActionAuthorizationError("proposal fence is missing")
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_proposal($1, $2, $3)",
            proposal_id,
            receipt.actor_id,
            request.now,
            field="proposal authority",
        )
        row, proposal = _proposal_from_json(raw, now=request.now)
        source_ids = _tuple_strings(
            row.get("source_evidence_ids"), field="proposal.source_evidence_ids"
        )
        if source_ids:
            # The action fence for approval/promotion intentionally omits
            # capture ids, but the proposal authority still locks the
            # original capture evidence before Policy revalidation returns.
            await _fetch_json_value(
                connection,
                "SELECT memory_shared_lock_capture_evidence($1::text[], $2, $3, $4, $5)",
                list(source_ids),
                proposal.family_owner_subject_id,
                receipt.binding_id,
                receipt.binding_version,
                request.now,
                field="proposal capture authority",
            )
        if proposal.revision != receipt.action_resource_fence.proposal_revision:
            raise ActionAuthorizationError("proposal revision mismatch")
        return proposal


class _ApprovalAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[ApprovalEvidencePort, ...]:
        proposal_id = receipt.action_resource_fence.proposal_id
        if proposal_id is None:
            raise ActionAuthorizationError("approval proposal is missing")
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_approvals($1, $2)",
            proposal_id,
            request.now,
            field="approval authority",
        )
        return _approvals_from_json(raw)


class _ActionResourceAuthorityAdapter:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyActionResourceFence:
        fence = receipt.action_resource_fence
        try:
            valid = await connection.fetchval(
                """
                SELECT memory_shared_lock_action_resource(
                    $1, $2, $3, $4
                )
                """,
                receipt.capability,
                fence.action_resource_id,
                fence.proposal_id,
                fence.proposal_revision or 0,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="shared action resource authority")
        if valid is not True:
            raise ActionAuthorizationError("shared action resource is stale")
        return fence


class PostgresFamilySharedActionExecutor:
    """Real action-executor implementation of ``FamilySharedActionExecutorPort``.

    The public interface stays deliberately small.  Internally one physical
    ``memoria_action_executor`` connection owns the transaction; the Policy
    service mints/inserts/locks a fresh resource receipt on that connection,
    then the narrow Memory SECURITY DEFINER port persists the lifecycle row,
    vote, status/audit and outbox.  No profile receipt or in-memory authority
    is consulted as a write proof.
    """

    _REQUIRED_FUNCTIONS = (
        "action_identity_lock_binding(text,text,integer,timestamptz)",
        "action_device_lock_trust(text,text,text,integer,timestamptz)",
        "session_runtime_assert_action_context(text,text,text,text,text,integer,integer)",
        "action_policy_insert_receipt(jsonb)",
        "action_policy_lock_receipt(text)",
        "memory_capture_lock_action_resource(text,text,text,text[],text,integer)",
        "memory_capture_commit(jsonb)",
        "consent_discover_action_fence(text,text,text,text,text,integer,text,text,timestamptz)",
        "memory_shared_lock_membership(text,text[],text,integer,timestamptz)",
        "memory_shared_lock_capture_evidence(text[],text,text,integer,timestamptz)",
        "memory_shared_lock_proposal(text,text,timestamptz)",
        "memory_shared_lock_approvals(text,timestamptz)",
        "memory_shared_lock_action_resource(text,text,text,integer)",
        "memory_shared_action_propose(jsonb)",
        "memory_shared_action_confirm(jsonb)",
        "memory_shared_action_object(jsonb)",
        "memory_shared_action_withdraw(jsonb)",
        "memory_shared_action_promote(jsonb)",
    )

    def __init__(self, action_executor_dsn: str) -> None:
        if not action_executor_dsn.strip():
            raise ValueError("action_executor_dsn must not be empty")
        self._dsn = action_executor_dsn
        self._pool: asyncpg.Pool | None = None
        self._ready = False
        self._consent = PostgresCurrentConsentAuthorityAdapter()
        authority = build_composite_action_authority(
            ProductionAuthorityAdapters(
                principal=_PrincipalAuthorityAdapter(),
                consent=self._consent,
                membership=_MembershipAuthorityAdapter(),
                binding=_BindingAuthorityAdapter(),
                proposal=_ProposalAuthorityAdapter(),
                action=_ActionResourceAuthorityAdapter(),
                approvals=_ApprovalAuthorityAdapter(),
                capture=_CaptureAuthorityAdapter(),
            )
        )
        self._sensitive_write = SensitiveWriteService(
            authorizer=PostgresActionAuthorizer(
                authority=authority,
                receipt_locker=_lock_action_receipt,
            ),
            repository=_ActionExecutorReceiptRepository(),
            consent_discovery=self._consent,
        )

    @property
    def available(self) -> bool:
        return self._ready and self._pool is not None

    async def initialize(self) -> None:
        """Probe every narrow port; an incomplete install stays unavailable."""
        if self.available:
            return
        try:
            pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with pool.acquire() as connection:
                role = await connection.fetchval("SELECT current_user")
                if role != "memoria_action_executor":
                    raise RuntimeError(
                        "shared action adapter must connect as memoria_action_executor"
                    )
                for signature in self._REQUIRED_FUNCTIONS:
                    row = await connection.fetchrow(
                        """
                        SELECT to_regprocedure($1) IS NOT NULL AS present,
                               COALESCE(
                                   has_function_privilege(
                                       current_user,
                                       to_regprocedure($1),
                                       'EXECUTE'
                                   ), false
                               ) AS executable
                        """,
                        signature,
                    )
                    if row is None or not row["present"] or not row["executable"]:
                        raise RuntimeError(
                            f"shared action port is unavailable: {signature}"
                        )
            self._pool = pool
            self._ready = True
        except Exception:
            LOGGER.exception("family shared action PostgreSQL adapter is unavailable")
            self._ready = False
            if "pool" in locals():
                await pool.close()
            self._pool = None

    async def close(self) -> None:
        self._ready = False
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    async def execute(
        self,
        *,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
    ) -> FamilySharedActionResult:
        if not self.available:
            raise FamilySharedActionUnavailableError(
                "family shared action PostgreSQL adapter is not ready (503)"
            )
        now = datetime.now(UTC)
        self._validate_actor_snapshot(snapshot, actor_subject_id, now)
        pool = self._pool
        if pool is None:
            raise FamilySharedActionUnavailableError(
                "family shared action PostgreSQL pool is unavailable (503)"
            )
        async with pool.acquire() as connection:
            try:
                async with connection.transaction():
                    await self._set_action_context(
                        connection, snapshot=snapshot, actor_subject_id=actor_subject_id
                    )
                    await self._assert_current_session(connection, snapshot)
                    if action.name == "propose":
                        return await self._execute_propose(
                            connection, snapshot, actor_subject_id, action, now
                        )
                    if action.name == "confirm":
                        return await self._execute_confirm(
                            connection, snapshot, actor_subject_id, action, now
                        )
                    return await self._execute_privacy_command(
                        connection, snapshot, actor_subject_id, action, now
                    )
            except FamilySharedActionUnavailableError:
                raise
            except FamilySharedActionDeniedError:
                raise
            except FamilySharedActionConflictError:
                raise
            except ActionAuthorizationError as exc:
                _raise_policy_error(exc)
            except asyncpg.PostgresError as exc:
                _raise_database_error(exc, field="family shared action")

    @staticmethod
    def _validate_actor_snapshot(
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        now: datetime,
    ) -> None:
        fence = snapshot.fence
        if (
            actor_subject_id != snapshot.active_subject_id
            or actor_subject_id != fence.actor_subject_id
            or actor_subject_id != fence.active_subject_id
        ):
            raise FamilySharedActionDeniedError(
                "family shared action requires the authenticated current subject"
            )
        if not snapshot.family_space_id or fence.family_space_id != snapshot.family_space_id:
            raise FamilySharedActionUnavailableError(
                "family shared action requires a current family authority (503)"
            )
        if fence.is_expired(now):
            raise FamilySharedActionConflictError("session action fence has expired")

    @staticmethod
    async def _set_action_context(
        connection: asyncpg.Connection,
        *,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
    ) -> None:
        fence = snapshot.fence
        values = (
            ("app.authenticated_actor", actor_subject_id),
            ("app.authenticated_device", fence.device_id),
            ("app.authenticated_subject", snapshot.active_subject_id),
            ("app.authenticated_binding", fence.binding_id),
            ("app.session_actor", actor_subject_id),
            ("app.memory.actor_subject_id", actor_subject_id),
            ("app.memory.subject_id", snapshot.active_subject_id),
            ("app.memory.family_space_id", snapshot.family_space_id),
        )
        for name, value in values:
            if value is None or not str(value).strip():
                raise FamilySharedActionUnavailableError(
                    f"{name} is unavailable (503)"
                )
            await connection.execute("SELECT set_config($1, $2, true)", name, str(value))

    @staticmethod
    async def _assert_current_session(
        connection: asyncpg.Connection,
        snapshot: MemoryAuthoritySnapshot,
    ) -> None:
        fence = snapshot.fence
        try:
            valid = await connection.fetchval(
                """
                SELECT session_runtime_assert_action_context(
                    $1, $2, $3, $4, $5, $6, $7
                )
                """,
                fence.session_id,
                fence.runtime_profile_id,
                fence.actor_subject_id,
                fence.device_id,
                fence.binding_id,
                fence.binding_version,
                fence.epoch,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Session action fence")
        if valid is not True:
            raise FamilySharedActionConflictError("Session action fence is stale")

    @staticmethod
    async def _lock_binding(
        connection: asyncpg.Connection,
        snapshot: MemoryAuthoritySnapshot,
        now: datetime,
    ) -> _BindingEvidence:
        fence = snapshot.fence
        try:
            raw = await connection.fetchval(
                "SELECT action_identity_lock_binding($1, $2, $3, $4)",
                fence.actor_subject_id,
                fence.device_id,
                fence.binding_version,
                now,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Identity binding")
        if raw is None:
            raise FamilySharedActionDeniedError("current binding is unavailable")
        binding = _binding_from_json(raw)
        if (
            binding.binding_id != fence.binding_id
            or binding.version != fence.binding_version
            or binding.device_id != fence.device_id
        ):
            raise FamilySharedActionConflictError("binding fence is stale")
        return binding

    @staticmethod
    async def _lock_device_trust(
        connection: asyncpg.Connection,
        snapshot: MemoryAuthoritySnapshot,
        now: datetime,
    ) -> str:
        fence = snapshot.fence
        try:
            raw = await connection.fetchval(
                "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
                fence.actor_subject_id,
                fence.device_id,
                fence.binding_id,
                fence.binding_version,
                now,
            )
        except asyncpg.PostgresError as exc:
            _raise_database_error(exc, field="Device authority")
        trust = _json_object(raw, field="device authority")
        value = _text(trust.get("device_trust"), field="device_trust")
        return value

    @staticmethod
    async def _lock_membership(
        connection: asyncpg.Connection,
        *,
        family_space_id: str,
        subject_ids: tuple[str, ...],
        binding_id: str,
        binding_version: int,
        now: datetime,
    ) -> _MembershipEvidence:
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_membership($1, $2::text[], $3, $4, $5)",
            family_space_id,
            list(subject_ids),
            binding_id,
            binding_version,
            now,
            field="family membership",
        )
        return _membership_from_json(raw)

    @staticmethod
    async def _lock_captures(
        connection: asyncpg.Connection,
        *,
        source_evidence_ids: tuple[str, ...],
        subject_id: str,
        binding_id: str,
        binding_version: int,
        now: datetime,
    ) -> tuple[_CaptureEvidence, ...]:
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_capture_evidence($1::text[], $2, $3, $4, $5)",
            list(source_evidence_ids),
            subject_id,
            binding_id,
            binding_version,
            now,
            field="capture evidence",
        )
        return _captures_from_json(raw)

    async def _discover_consent(
        self,
        connection: asyncpg.Connection,
        *,
        snapshot: MemoryAuthoritySnapshot,
        resource_owner_id: str,
        capability: str,
        now: datetime,
    ) -> tuple[tuple[object, ...], tuple[object, ...]]:
        discovery = await self._consent.discover_current(
            connection,
            actor_id=snapshot.fence.actor_subject_id,
            subject_id=snapshot.active_subject_id,
            resource_owner_id=resource_owner_id,
            device_id=snapshot.fence.device_id,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            capability=cast(Any, capability),
            purpose=cast(Any, capability),
            now=now,
        )
        if not discovery.consent_evidence or not discovery.consent_snapshot_evidence:
            raise FamilySharedActionDeniedError(
                "current Consent does not authorize the family shared action"
            )
        return discovery.consent_evidence, discovery.consent_snapshot_evidence

    @staticmethod
    def _check_consent_snapshot(
        snapshot_evidence: tuple[object, ...],
        *,
        expected_id: str,
        expected_revision: int,
        expected_hash: str,
    ) -> None:
        if len(snapshot_evidence) != 1:
            raise FamilySharedActionConflictError("Consent snapshot cardinality changed")
        current = snapshot_evidence[0]
        if (
            getattr(current, "snapshot_id", None) != expected_id
            or getattr(current, "revision", None) != expected_revision
            or getattr(current, "canonical_hash", None) != expected_hash
        ):
            raise FamilySharedActionConflictError("Consent snapshot CAS failed")

    @staticmethod
    def _check_membership_snapshot(
        membership: _MembershipEvidence,
        *,
        expected_id: str,
        expected_revision: int,
        expected_hash: str,
    ) -> None:
        if (
            membership.snapshot_id != expected_id
            or membership.revision != expected_revision
            or membership.canonical_hash != expected_hash
        ):
            raise FamilySharedActionConflictError("membership snapshot CAS failed")

    @staticmethod
    def _snapshot_context_kwargs(
        snapshot: MemoryAuthoritySnapshot,
        *,
        binding: _BindingEvidence,
        device_trust: str,
        now: datetime,
        fence: PolicyActionResourceFence,
        capability: str,
        consent_evidence: tuple[object, ...],
        consent_snapshots: tuple[object, ...],
        membership: _MembershipEvidence,
        captures: tuple[_CaptureEvidence, ...],
        proposal: _ProposalEvidence | None,
        approvals: tuple[_ApprovalEvidence, ...],
        idempotency_key: str,
    ) -> PolicyContext:
        return PolicyContext(
            actor_id=snapshot.fence.actor_subject_id,
            subject_id=snapshot.active_subject_id,
            resource_owner_id=fence.family_owner_subject_id,
            device_id=snapshot.fence.device_id,
            capability=cast(Any, capability),
            purpose=cast(Any, capability),
            declared_device_mode=cast(Any, binding.declared_mode),
            current_session_mode=cast(Any, snapshot.service_mode),
            subject_category=snapshot.subject_category,
            age_band=snapshot.age_band,
            speaker_state=snapshot.speaker_state,
            speaker_confidence=snapshot.speaker_confidence,
            device_trust=cast(Any, device_trust),
            safety_state="normal",
            jurisdiction="CN",
            data_classification="private",
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            session_id=snapshot.fence.session_id,
            session_epoch=snapshot.fence.epoch,
            runtime_profile_id=snapshot.fence.runtime_profile_id,
            subject_revision=snapshot.fence.subject_revision,
            evaluated_at=now,
            idempotency_key=idempotency_key,
            consent_evidence=cast(Any, consent_evidence),
            consent_snapshot_evidence=cast(Any, consent_snapshots),
            binding_evidence=binding,
            generation_id=snapshot.generation_id,
            turn_id=snapshot.turn_id,
            tool_epoch=snapshot.tool_epoch,
            action_resource_fence=fence,
            membership_evidence=membership,
            approval_evidence=cast(Any, approvals),
            capture_evidence=cast(Any, captures),
            proposal_evidence=proposal,
        )

    @staticmethod
    def _proposal_required_subjects(row: dict[str, object]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    _text(row.get("proposer_subject_id"), field="proposer_subject_id"),
                    *_tuple_strings(row.get("co_subject_ids"), field="co_subject_ids"),
                )
            )
        )

    @staticmethod
    def _check_proposal_matches_snapshot(
        row: dict[str, object],
        snapshot: MemoryAuthoritySnapshot,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> None:
        fence = snapshot.fence
        # Proposal creation provenance is immutable but belongs to the
        # proposer.  A co-subject confirms from a separate actor-bound
        # Session/profile, so those voter-local fields must not equal the
        # proposal's session/generation fence.  Shared resource authority is
        # the family + binding/device snapshot; Policy binds each voter to a
        # fresh current Session and receipt below.
        expected = {
            "binding_id": fence.binding_id,
            "binding_version": fence.binding_version,
            "device_id": fence.device_id,
            "family_space_id": snapshot.family_space_id,
        }
        for name, value in expected.items():
            actual = row.get(name)
            if name == "binding_version":
                if (
                    actual is not None
                    and (
                        isinstance(actual, bool)
                        or not isinstance(actual, (int, str))
                    )
                ):
                    raise FamilySharedActionConflictError(
                        f"proposal {name} is invalid"
                    )
                try:
                    actual = int(actual) if actual is not None else None
                except ValueError as exc:
                    raise FamilySharedActionConflictError(
                        f"proposal {name} is invalid"
                    ) from exc
            if actual != value:
                raise FamilySharedActionConflictError(
                    f"proposal {name} does not match current session fence"
                )
        # ``fence_context_hash`` preserves proposal-time provenance.  It is
        # intentionally not compared with the current voter's fingerprint.
        if not _text(row.get("fence_context_hash"), field="fence_context_hash"):
            raise FamilySharedActionConflictError("proposal provenance fence is missing")
        valid_until = _time(row.get("valid_until"), field="proposal.valid_until")
        if now >= valid_until:
            raise FamilySharedActionConflictError("proposal fence has expired")
        required = PostgresFamilySharedActionExecutor._proposal_required_subjects(row)
        if actor_subject_id not in required:
            raise FamilySharedActionDeniedError("actor is not a proposal subject")

    @staticmethod
    async def _lock_proposal_row(
        connection: asyncpg.Connection,
        *,
        proposal_id: str,
        actor_subject_id: str,
        now: datetime,
    ) -> tuple[dict[str, object], _ProposalEvidence]:
        raw = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_proposal($1, $2, $3)",
            proposal_id,
            actor_subject_id,
            now,
            field="proposal authority",
        )
        return _proposal_from_json(raw, now=now)

    @staticmethod
    def _fence_snapshot_values(
        fence: PolicyActionResourceFence,
    ) -> tuple[str, int, str]:
        if (
            fence.consent_snapshot_id is None
            or fence.consent_snapshot_revision is None
            or fence.consent_snapshot_hash is None
        ):
            raise FamilySharedActionUnavailableError(
                "action fence is missing Consent snapshot evidence (503)"
            )
        return (
            fence.consent_snapshot_id,
            fence.consent_snapshot_revision,
            fence.consent_snapshot_hash,
        )

    @staticmethod
    def _membership_fence_values(
        fence: PolicyActionResourceFence,
    ) -> tuple[str, int, str]:
        if (
            fence.membership_snapshot_id is None
            or fence.membership_snapshot_revision is None
            or fence.membership_snapshot_hash is None
        ):
            raise FamilySharedActionUnavailableError(
                "action fence is missing membership evidence (503)"
            )
        return (
            fence.membership_snapshot_id,
            fence.membership_snapshot_revision,
            fence.membership_snapshot_hash,
        )

    @staticmethod
    def _make_fence(
        *,
        capability: str,
        proposal_id: str,
        proposal_revision: int,
        family_space_id: str,
        family_owner_subject_id: str,
        required_subject_ids: tuple[str, ...],
        snapshot: MemoryAuthoritySnapshot,
        now: datetime,
        valid_until: datetime,
        membership: _MembershipEvidence,
        consent_snapshots: tuple[object, ...],
        capture_records: tuple[tuple[str, int, str], ...] = (),
        voter_subject_id: str | None = None,
        approval_snapshots: tuple[PolicyApprovalSnapshotFence, ...] = (),
    ) -> PolicyActionResourceFence:
        if len(consent_snapshots) != 1:
            raise FamilySharedActionUnavailableError(
                "exactly one current Consent snapshot is required (503)"
            )
        consent = consent_snapshots[0]
        return build_action_resource_fence(
            capability=cast(Any, capability),
            purpose=cast(Any, capability),
            action_resource_id=proposal_id,
            action_revision=proposal_revision,
            generation_id=snapshot.generation_id,
            turn_id=snapshot.turn_id,
            tool_epoch=snapshot.tool_epoch,
            issued_at=now,
            valid_until=valid_until,
            family_space_id=family_space_id,
            family_owner_subject_id=family_owner_subject_id,
            proposal_id=proposal_id,
            proposal_revision=proposal_revision,
            voter_subject_id=cast(Any, voter_subject_id),
            approval_decision=("confirm" if voter_subject_id is not None else None),
            required_approval_subject_ids=required_subject_ids,
            approval_snapshots=approval_snapshots,
            capture_evidence_records=capture_records,
            consent_snapshot_id=getattr(consent, "snapshot_id", None),
            consent_snapshot_revision=getattr(consent, "revision", None),
            consent_snapshot_hash=getattr(consent, "canonical_hash", None),
            membership_snapshot_id=membership.snapshot_id,
            membership_snapshot_revision=membership.revision,
            membership_snapshot_hash=membership.canonical_hash,
        )

    @staticmethod
    def _result(value: object, *, default_proposal_id: str) -> FamilySharedActionResult:
        row = _json_object(value, field="shared action result")
        return FamilySharedActionResult(
            proposal_id=_text(
                row.get("proposal_id") or default_proposal_id,
                field="result.proposal_id",
            ),
            status=_text(row.get("status"), field="result.status"),
        )

    async def _execute_propose(
        self,
        connection: asyncpg.Connection,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
        now: datetime,
    ) -> FamilySharedActionResult:
        if action.name != "propose" or action.title is None or action.content is None:
            raise ValueError("propose action is incomplete")
        family_space_id = snapshot.family_space_id
        if family_space_id is None:
            raise FamilySharedActionUnavailableError("family space is unavailable (503)")
        binding = await self._lock_binding(connection, snapshot, now)
        device_trust = await self._lock_device_trust(connection, snapshot, now)
        required_subject_ids = tuple(
            dict.fromkeys((actor_subject_id, *action.co_subject_ids))
        )
        membership = await self._lock_membership(
            connection,
            family_space_id=family_space_id,
            subject_ids=required_subject_ids,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            now=now,
        )
        captures = await self._lock_captures(
            connection,
            source_evidence_ids=action.source_evidence_ids,
            subject_id=actor_subject_id,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            now=now,
        )
        consent, consent_snapshots = await self._discover_consent(
            connection,
            snapshot=snapshot,
            resource_owner_id=actor_subject_id,
            capability="family_shared_memory_proposal",
            now=now,
        )
        proposal_id = str(uuid.uuid4())
        valid_until = now + DEFAULT_ACTION_FENCE_TTL
        capture_records = tuple(
            (item.evidence_id, item.revision, item.canonical_hash) for item in captures
        )
        fence = self._make_fence(
            capability="family_shared_memory_proposal",
            proposal_id=proposal_id,
            proposal_revision=1,
            family_space_id=family_space_id,
            family_owner_subject_id=actor_subject_id,
            required_subject_ids=required_subject_ids,
            snapshot=snapshot,
            now=now,
            valid_until=valid_until,
            membership=membership,
            consent_snapshots=consent_snapshots,
            capture_records=capture_records,
        )
        context = self._snapshot_context_kwargs(
            snapshot,
            binding=binding,
            device_trust=device_trust,
            now=now,
            fence=fence,
            capability="family_shared_memory_proposal",
            consent_evidence=consent,
            consent_snapshots=consent_snapshots,
            membership=membership,
            captures=captures,
            proposal=None,
            approvals=(),
            idempotency_key=f"family-shared:proposal:{proposal_id}",
        )

        async def write(
            conn: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> object:
            payload = {
                "actor_subject_id": actor_subject_id,
                "proposal_id": proposal_id,
                "family_space_id": family_space_id,
                "co_subject_ids": list(action.co_subject_ids),
                "source_evidence_ids": list(action.source_evidence_ids),
                "title": action.title,
                "content": action.content,
                "binding_version": snapshot.fence.binding_version,
                "session_id": snapshot.fence.session_id,
                "epoch": snapshot.fence.epoch,
                "binding_id": snapshot.fence.binding_id,
                "binding_role": snapshot.fence.binding_role,
                "runtime_profile_id": snapshot.fence.runtime_profile_id,
                "device_id": snapshot.fence.device_id,
                "subject_revision": snapshot.fence.subject_revision,
                "generation_id": snapshot.fence.generation_id,
                "turn_id": snapshot.fence.turn_id,
                "valid_until": valid_until,
                "fence_context_hash": snapshot.fence.fingerprint(),
                "proposal_policy_receipt_id": receipt.receipt_id,
                "consent_snapshot_id": fence.consent_snapshot_id,
                "consent_snapshot_revision": fence.consent_snapshot_revision,
                "consent_snapshot_hash": fence.consent_snapshot_hash,
                "membership_snapshot_id": fence.membership_snapshot_id,
                "membership_snapshot_revision": fence.membership_snapshot_revision,
                "membership_snapshot_hash": fence.membership_snapshot_hash,
                "capture_evidence_hash": fence.capture_evidence_hash,
                "generation": snapshot.generation_id,
                "tool_epoch": snapshot.tool_epoch,
                "now": now,
                "action_resource_fence": fence.model_dump(mode="json"),
            }
            return await _fetch_json_value(
                conn,
                "SELECT memory_shared_action_propose($1::jsonb)",
                json.dumps(payload, default=str),
                field="shared propose write",
            )

        try:
            result: object = await self._sensitive_write.execute(
                connection, context, write
            )
        except ActionAuthorizationError as exc:
            _raise_policy_error(exc)
        return self._result(result, default_proposal_id=proposal_id)

    async def _execute_confirm(
        self,
        connection: asyncpg.Connection,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
        now: datetime,
    ) -> FamilySharedActionResult:
        proposal_id = action.proposal_id
        if action.name != "confirm" or proposal_id is None:
            raise ValueError("confirm action is incomplete")
        row, proposal = await self._lock_proposal_row(
            connection,
            proposal_id=proposal_id,
            actor_subject_id=actor_subject_id,
            now=now,
        )
        self._check_proposal_matches_snapshot(
            row, snapshot, actor_subject_id=actor_subject_id, now=now
        )
        family_space_id = _text(row.get("family_space_id"), field="family_space_id")
        binding = await self._lock_binding(connection, snapshot, now)
        device_trust = await self._lock_device_trust(connection, snapshot, now)
        required_subject_ids = proposal.required_approval_subject_ids
        membership = await self._lock_membership(
            connection,
            family_space_id=family_space_id,
            subject_ids=required_subject_ids,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            now=now,
        )
        self._check_membership_snapshot(
            membership,
            expected_id=_text(row.get("membership_snapshot_id"), field="membership_snapshot_id"),
            expected_revision=_int(
                row.get("membership_snapshot_revision"),
                field="membership_snapshot_revision",
                minimum=1,
            ),
            expected_hash=_text(
                row.get("membership_snapshot_hash"), field="membership_snapshot_hash"
            ),
        )
        source_ids = _tuple_strings(
            row.get("source_evidence_ids"), field="proposal.source_evidence_ids"
        )
        await self._lock_captures(
            connection,
            source_evidence_ids=source_ids,
            subject_id=proposal.family_owner_subject_id,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            now=now,
        )
        consent, consent_snapshots = await self._discover_consent(
            connection,
            snapshot=snapshot,
            resource_owner_id=proposal.family_owner_subject_id,
            capability="family_shared_memory_approval",
            now=now,
        )
        valid_until = _time(row.get("valid_until"), field="proposal.valid_until")
        fence = self._make_fence(
            capability="family_shared_memory_approval",
            proposal_id=proposal_id,
            proposal_revision=proposal.revision,
            family_space_id=family_space_id,
            family_owner_subject_id=proposal.family_owner_subject_id,
            required_subject_ids=required_subject_ids,
            snapshot=snapshot,
            now=now,
            valid_until=valid_until,
            membership=membership,
            consent_snapshots=consent_snapshots,
            voter_subject_id=actor_subject_id,
        )
        context = self._snapshot_context_kwargs(
            snapshot,
            binding=binding,
            device_trust=device_trust,
            now=now,
            fence=fence,
            capability="family_shared_memory_approval",
            consent_evidence=consent,
            consent_snapshots=consent_snapshots,
            membership=membership,
            captures=(),
            proposal=proposal,
            approvals=(),
            idempotency_key=f"family-shared:approval:{proposal_id}:{actor_subject_id}",
        )

        async def write(
            conn: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> object:
            approval_snapshot_id = f"approval:{receipt.receipt_id}"
            approval_snapshot_hash = _sha256(
                {
                    "proposal_id": proposal_id,
                    "proposal_revision": proposal.revision,
                    "subject_id": actor_subject_id,
                    "receipt_id": receipt.receipt_id,
                    "fence": receipt.action_resource_fence.model_dump(mode="json"),
                }
            )
            payload = {
                "actor_subject_id": actor_subject_id,
                "proposal_id": proposal_id,
                "proposal_revision": proposal.revision,
                "family_space_id": family_space_id,
                "binding_id": snapshot.fence.binding_id,
                "binding_version": snapshot.fence.binding_version,
                "approval_receipt_id": receipt.receipt_id,
                "approval_evidence_id": f"approval-evidence:{receipt.receipt_id}",
                "approval_snapshot_id": approval_snapshot_id,
                "approval_snapshot_revision": 1,
                "approval_snapshot_hash": approval_snapshot_hash,
                "now": now,
                "action_resource_fence": receipt.action_resource_fence.model_dump(
                    mode="json"
                ),
            }
            return await _fetch_json_value(
                conn,
                "SELECT memory_shared_action_confirm($1::jsonb)",
                json.dumps(payload, default=str),
                field="shared confirm write",
            )

        try:
            result: object = await self._sensitive_write.execute(
                connection, context, write
            )
        except ActionAuthorizationError as exc:
            _raise_policy_error(exc)
        confirmed = self._result(result, default_proposal_id=proposal_id)
        if confirmed.status != "approvals_complete":
            return confirmed

        # The last confirm is still inside the SAME caller-owned transaction.
        # A fresh promotion PolicyReceiptV2 and the approval/membership/consent
        # CAS are therefore committed or rolled back with this vote.  There is
        # no separate finalizer and no in-memory promotion verifier.
        return await self._promote_after_last_confirm(
            connection,
            snapshot=snapshot,
            actor_subject_id=actor_subject_id,
            proposal_id=proposal_id,
            now=now,
        )

    async def _promote_after_last_confirm(
        self,
        connection: asyncpg.Connection,
        *,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        proposal_id: str,
        now: datetime,
    ) -> FamilySharedActionResult:
        row, proposal = await self._lock_proposal_row(
            connection,
            proposal_id=proposal_id,
            actor_subject_id=actor_subject_id,
            now=now,
        )
        self._check_proposal_matches_snapshot(
            row, snapshot, actor_subject_id=actor_subject_id, now=now
        )
        family_space_id = _text(row.get("family_space_id"), field="family_space_id")
        binding = await self._lock_binding(connection, snapshot, now)
        device_trust = await self._lock_device_trust(connection, snapshot, now)
        membership = await self._lock_membership(
            connection,
            family_space_id=family_space_id,
            subject_ids=proposal.required_approval_subject_ids,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            now=now,
        )
        self._check_membership_snapshot(
            membership,
            expected_id=_text(row.get("membership_snapshot_id"), field="membership_snapshot_id"),
            expected_revision=_int(
                row.get("membership_snapshot_revision"),
                field="membership_snapshot_revision",
                minimum=1,
            ),
            expected_hash=_text(
                row.get("membership_snapshot_hash"), field="membership_snapshot_hash"
            ),
        )
        raw_approvals = await _fetch_json_value(
            connection,
            "SELECT memory_shared_lock_approvals($1, $2)",
            proposal_id,
            now,
            field="promotion approval authority",
        )
        approvals = _approvals_from_json(raw_approvals)
        required = set(proposal.required_approval_subject_ids)
        confirms = tuple(
            item
            for item in approvals
            if item.decision == "confirm"
            and item.status == "active"
            and item.subject_id in required
        )
        if {item.subject_id for item in confirms} != required:
            raise FamilySharedActionUnavailableError(
                "last confirm has no complete current approval set (503)"
            )
        consent, consent_snapshots = await self._discover_consent(
            connection,
            snapshot=snapshot,
            resource_owner_id=proposal.family_owner_subject_id,
            capability="family_shared_memory_promotion",
            now=now,
        )
        valid_until = _time(row.get("valid_until"), field="proposal.valid_until")
        approval_snapshots = tuple(
            PolicyApprovalSnapshotFence(
                subject_id=item.subject_id,
                snapshot_id=item.snapshot_id,
                revision=item.revision,
                canonical_hash=item.canonical_hash,
            )
            for item in confirms
        )
        fence = self._make_fence(
            capability="family_shared_memory_promotion",
            proposal_id=proposal_id,
            proposal_revision=proposal.revision,
            family_space_id=family_space_id,
            family_owner_subject_id=proposal.family_owner_subject_id,
            required_subject_ids=proposal.required_approval_subject_ids,
            snapshot=snapshot,
            now=now,
            valid_until=valid_until,
            membership=membership,
            consent_snapshots=consent_snapshots,
            approval_snapshots=approval_snapshots,
        )
        context = self._snapshot_context_kwargs(
            snapshot,
            binding=binding,
            device_trust=device_trust,
            now=now,
            fence=fence,
            capability="family_shared_memory_promotion",
            consent_evidence=consent,
            consent_snapshots=consent_snapshots,
            membership=membership,
            captures=(),
            proposal=proposal,
            approvals=confirms,
            idempotency_key=f"family-shared:promotion:{proposal_id}",
        )
        record_id = str(uuid.uuid4())

        async def write(
            conn: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> object:
            payload = {
                "actor_subject_id": actor_subject_id,
                "proposal_id": proposal_id,
                "promotion_receipt_id": receipt.receipt_id,
                "record_id": record_id,
                "now": now,
                "action_resource_fence": receipt.action_resource_fence.model_dump(
                    mode="json"
                ),
            }
            return await _fetch_json_value(
                conn,
                "SELECT memory_shared_action_promote($1::jsonb)",
                json.dumps(payload, default=str),
                field="shared promotion write",
            )

        try:
            result: object = await self._sensitive_write.execute(
                connection, context, write
            )
        except ActionAuthorizationError as exc:
            _raise_policy_error(exc)
        promoted = self._result(result, default_proposal_id=proposal_id)
        if promoted.status != "promoted":
            raise FamilySharedActionUnavailableError(
                "promotion did not commit atomically (503)"
            )
        return promoted

    async def _execute_privacy_command(
        self,
        connection: asyncpg.Connection,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
        now: datetime,
    ) -> FamilySharedActionResult:
        proposal_id = action.proposal_id
        if action.name not in {"object", "withdraw"} or proposal_id is None:
            raise ValueError("privacy action is incomplete")
        row, _proposal = await self._lock_proposal_row(
            connection,
            proposal_id=proposal_id,
            actor_subject_id=actor_subject_id,
            now=now,
        )
        self._check_proposal_matches_snapshot(
            row, snapshot, actor_subject_id=actor_subject_id, now=now
        )
        binding = await self._lock_binding(connection, snapshot, now)
        device_trust = await self._lock_device_trust(connection, snapshot, now)
        family_space_id = _text(row.get("family_space_id"), field="family_space_id")
        required_subject_ids = self._proposal_required_subjects(row)
        membership = await self._lock_membership(
            connection,
            family_space_id=family_space_id,
            subject_ids=required_subject_ids,
            binding_id=snapshot.fence.binding_id,
            binding_version=snapshot.fence.binding_version,
            now=now,
        )
        self._check_membership_snapshot(
            membership,
            expected_id=_text(
                row.get("membership_snapshot_id"),
                field="membership_snapshot_id",
            ),
            expected_revision=_int(
                row.get("membership_snapshot_revision"),
                field="membership_snapshot_revision",
                minimum=1,
            ),
            expected_hash=_text(
                row.get("membership_snapshot_hash"),
                field="membership_snapshot_hash",
            ),
        )
        consent, consent_snapshots = await self._discover_consent(
            connection,
            snapshot=snapshot,
            resource_owner_id=_text(
                row.get("proposer_subject_id"), field="proposer_subject_id"
            ),
            capability="family_shared_memory_approval",
            now=now,
        )
        valid_until = _time(row.get("valid_until"), field="proposal.valid_until")
        fence = self._make_fence(
            capability="family_shared_memory_approval",
            proposal_id=proposal_id,
            proposal_revision=_int(
                row.get("proposal_revision"), field="proposal_revision", minimum=1
            ),
            family_space_id=family_space_id,
            family_owner_subject_id=_text(
                row.get("proposer_subject_id"), field="proposer_subject_id"
            ),
            required_subject_ids=required_subject_ids,
            snapshot=snapshot,
            now=now,
            valid_until=valid_until,
            membership=membership,
            consent_snapshots=consent_snapshots,
            voter_subject_id=actor_subject_id,
        )
        context = self._snapshot_context_kwargs(
            snapshot,
            binding=binding,
            device_trust=device_trust,
            now=now,
            fence=fence,
            capability="family_shared_memory_approval",
            consent_evidence=consent,
            consent_snapshots=consent_snapshots,
            membership=membership,
            captures=(),
            proposal=_proposal,
            approvals=(),
            idempotency_key=(
                f"family-shared:privacy:{action.name}:{proposal_id}:"
                f"{actor_subject_id}"
            ),
        )
        function_name = {
            "object": "memory_shared_action_object",
            "withdraw": "memory_shared_action_withdraw",
        }[action.name]

        async def write(
            conn: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> object:
            payload = {
                "actor_subject_id": actor_subject_id,
                "subject_id": snapshot.active_subject_id,
                "proposal_id": proposal_id,
                "proposal_revision": _int(
                    row.get("proposal_revision"),
                    field="proposal_revision",
                    minimum=1,
                ),
                "family_space_id": family_space_id,
                "binding_id": snapshot.fence.binding_id,
                "binding_version": snapshot.fence.binding_version,
                "session_id": snapshot.fence.session_id,
                "epoch": snapshot.fence.epoch,
                "runtime_profile_id": snapshot.fence.runtime_profile_id,
                "device_id": snapshot.fence.device_id,
                "subject_revision": snapshot.fence.subject_revision,
                "generation_id": snapshot.generation_id,
                "turn_id": snapshot.turn_id,
                "tool_epoch": snapshot.tool_epoch,
                "fence_context_hash": _text(
                    row.get("fence_context_hash"), field="fence_context_hash"
                ),
                "privacy_action": action.name,
                "evidence_id": f"privacy:{action.name}:{proposal_id}:{actor_subject_id}",
                "policy_receipt_id": receipt.receipt_id,
                "action_resource_fence": receipt.action_resource_fence.model_dump(
                    mode="json"
                ),
                "now": now,
            }
            return await _fetch_json_value(
                conn,
                f"SELECT {function_name}($1::jsonb)",
                json.dumps(payload, default=str),
                field=f"shared {action.name} write",
            )

        try:
            result: object = await self._sensitive_write.execute(
                connection,
                context,
                write,
            )
        except ActionAuthorizationError as exc:
            _raise_policy_error(exc)
        return self._result(result, default_proposal_id=proposal_id)
