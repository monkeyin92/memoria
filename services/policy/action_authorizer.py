"""Transaction-bound PostgreSQL action authorization.

``PostgresActionAuthorizer.execute_authorized`` is the production entry point.
The caller must already own an asyncpg transaction.  This module never
acquires a pool and never starts, commits, or rolls back a transaction.  It
locks the immutable receipt, asks one same-connection authority adapter to
lock/rebuild all current evidence in canonical order, validates the exact
fence, and invokes the business callback while those locks are still held.
No reusable boolean or bearer token escapes the transaction.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, TypeVar, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyActionResourceFence,
)

from services.policy.context import PolicyContext
from services.policy.engine import SENSITIVE_CAPABILITIES
from services.policy.evidence import (
    ApprovalEvidencePort,
    BindingEvidencePort,
    CaptureEvidencePort,
    ConsentEvidencePort,
    MembershipEvidencePort,
    ProposalEvidencePort,
    RelationshipEvidencePort,
)
from services.policy.postgres_receipt_repository import lock_policy_receipt
from services.policy.receipts import (
    PolicyReceiptV2,
    exact_evidence_fence_valid,
    receipt_fence_valid,
)

T = TypeVar("T")


class ActionAuthorizationError(PermissionError):
    """Current locked authority does not authorize the requested write."""


@dataclass(frozen=True, slots=True)
class ActionExecutionRequest:
    receipt_id: str
    context: PolicyContext
    now: datetime
    consent_authority_proof: object | None = None


class _ActionAuthorityPort(Protocol):
    """Lock current authorities on the supplied connection and rebuild context.

    Implementations must lock in the canonical order consent, relationship,
    membership, binding, proposal, approval, capture.  A revoker/updater must
    contend on the same authority head lock before this method may return.
    """

    async def _lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyContext: ...


class PrincipalAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> str: ...


class ConsentAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[ConsentEvidencePort, ...]: ...


class RelationshipAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[RelationshipEvidencePort, ...]: ...


class MembershipAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> MembershipEvidencePort: ...


class BindingAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> BindingEvidencePort: ...


class ProposalAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> ProposalEvidencePort: ...


class ActionResourceAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyActionResourceFence: ...


class ApprovalAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[ApprovalEvidencePort, ...]: ...


class CaptureAuthorityAdapter(Protocol):
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[CaptureEvidencePort, ...]: ...


class CompositeActionAuthority:
    """Concrete fixed-order composition of current authority head adapters.

    Cross-service SQL adapters are injected because their schemas are owned by
    Consent, Identity, Memory and Session.  This composition itself is usable
    in production: it selects exactly the adapters required by the generated
    receipt/action fence, locks them in one deterministic order, rebuilds one
    strict ``PolicyContext``, and fails closed when any required adapter is
    absent.  The rebuilt context remains internal to ``execute_authorized``.
    """

    def __init__(
        self,
        *,
        principal: PrincipalAuthorityAdapter | None = None,
        consent: ConsentAuthorityAdapter | None = None,
        relationship: RelationshipAuthorityAdapter | None = None,
        membership: MembershipAuthorityAdapter | None = None,
        binding: BindingAuthorityAdapter | None = None,
        proposal: ProposalAuthorityAdapter | None = None,
        action: ActionResourceAuthorityAdapter | None = None,
        approvals: ApprovalAuthorityAdapter | None = None,
        capture: CaptureAuthorityAdapter | None = None,
    ) -> None:
        self._principal = principal
        self._consent = consent
        self._relationship = relationship
        self._membership = membership
        self._binding = binding
        self._proposal = proposal
        self._action = action
        self._approvals = approvals
        self._capture = capture

    async def _lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyContext:
        principal = _required(self._principal, "principal")
        actor_id = await principal.lock_current(connection, receipt, request)
        if actor_id != receipt.actor_id or actor_id != request.context.actor_id:
            raise ActionAuthorizationError("authenticated principal mismatch")

        fence = receipt.action_resource_fence
        consents: tuple[ConsentEvidencePort, ...] = ()
        if receipt.consent_snapshot_ids or fence.consent_snapshot_id is not None:
            consent = _required(self._consent, "consent")
            consents = await consent.lock_current(connection, receipt, request)

        relationships: tuple[RelationshipEvidencePort, ...] = ()
        if receipt.relationship_snapshot_ids:
            relationship = _required(self._relationship, "relationship")
            relationships = await relationship.lock_current(
                connection, receipt, request
            )

        membership_evidence: MembershipEvidencePort | None = None
        if fence.membership_snapshot_id is not None:
            membership = _required(self._membership, "membership")
            membership_evidence = await membership.lock_current(
                connection, receipt, request
            )

        binding_evidence: BindingEvidencePort | None = None
        if receipt.exact_fence:
            binding = _required(self._binding, "binding")
            binding_evidence = await binding.lock_current(connection, receipt, request)

        proposal_evidence: ProposalEvidencePort | None = None
        # Proposal creation names the *future* resource in its exact action
        # fence so the action-resource adapter can perform a non-existence
        # CAS.  It cannot also lock an existing ProposalEvidence row before
        # the write.  Every later approval/privacy/promotion action still
        # requires the current proposal authority here.
        if (
            fence.proposal_id is not None
            and receipt.capability.value != "family_shared_memory_proposal"
        ):
            proposal = _required(self._proposal, "proposal")
            proposal_evidence = await proposal.lock_current(
                connection, receipt, request
            )

        action = _required(self._action, "action")
        action_fence = await action.lock_current(connection, receipt, request)

        approval_evidence: tuple[ApprovalEvidencePort, ...] = ()
        if fence.approval_snapshots:
            approvals = _required(self._approvals, "approvals")
            approval_evidence = await approvals.lock_current(
                connection, receipt, request
            )

        capture_evidence: tuple[CaptureEvidencePort, ...] = ()
        if fence.capture_evidence_ids:
            capture = _required(self._capture, "capture")
            capture_evidence = await capture.lock_current(
                connection, receipt, request
            )

        return replace(
            request.context,
            consent_evidence=consents,
            relationship_evidence=relationships,
            binding_evidence=binding_evidence,
            action_resource_fence=action_fence,
            membership_evidence=membership_evidence,
            proposal_evidence=proposal_evidence,
            approval_evidence=approval_evidence,
            capture_evidence=capture_evidence,
        )


def _required[T](adapter: T | None, name: str) -> T:
    if adapter is None:
        raise ActionAuthorizationError(f"required {name} adapter is not configured")
    return adapter


class RejectingActionAuthority:
    """Default production posture when any authority adapter is missing."""

    async def _lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyContext:
        raise ActionAuthorizationError("current authority adapter is not configured")


type ReceiptLocker = Callable[
    [asyncpg.Connection, str],
    PolicyReceiptV2 | None | Awaitable[PolicyReceiptV2 | None],
]
type WriteCallback[T] = Callable[
    [asyncpg.Connection, PolicyReceiptV2], T | Awaitable[T]
]


class PostgresActionAuthorizer:
    """Authorize and execute one side effect inside a caller-owned transaction."""

    def __init__(
        self,
        *,
        authority: _ActionAuthorityPort | None = None,
        receipt_locker: ReceiptLocker = lock_policy_receipt,
    ) -> None:
        self._authority = authority or RejectingActionAuthority()
        self._receipt_locker = receipt_locker

    async def execute_authorized(
        self,
        connection: asyncpg.Connection,
        request: ActionExecutionRequest,
        write_callback: WriteCallback[T],
    ) -> T:
        """Lock, verify, write, then return; never manage the transaction."""
        locked = await self._lock_and_validate(connection, request)
        result = write_callback(connection, locked)
        if inspect.isawaitable(result):
            return await cast(Awaitable[T], result)
        return result

    async def _lock_and_validate(
        self,
        connection: asyncpg.Connection,
        request: ActionExecutionRequest,
    ) -> PolicyReceiptV2:
        """Internal batch seam; validation facts never escape a public call."""
        in_transaction = getattr(connection, "is_in_transaction", None)
        if not callable(in_transaction) or not in_transaction():
            raise ActionAuthorizationError(
                "execute_authorized requires an already-open transaction"
            )
        locked_value = self._receipt_locker(connection, request.receipt_id)
        locked = (
            await locked_value
            if inspect.isawaitable(locked_value)
            else locked_value
        )
        if locked is None:
            raise ActionAuthorizationError("policy receipt does not exist")
        if locked.receipt_id != request.receipt_id:
            raise ActionAuthorizationError("locked receipt identity mismatch")
        if locked.effect not in {"allow", "allow_with_obligations"}:
            raise ActionAuthorizationError("deny receipt cannot authorize a write")

        current = await self._authority._lock_current(connection, locked, request)
        if locked.capability in SENSITIVE_CAPABILITIES:
            valid = exact_evidence_fence_valid(
                locked, context=current, now=request.now
            )
        else:
            valid = receipt_fence_valid(locked, context=current, now=request.now)
        if not valid:
            raise ActionAuthorizationError("current exact policy fence is invalid")
        return locked
