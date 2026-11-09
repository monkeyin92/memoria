"""Authority adapters for the family shared-action executor.

Each adapter locks exactly one authority family on the executor transaction
and returns the evidence the policy decision was made against.  Shared state
stays owned by the executor; this module only holds the per-family locks and
the small JSON/database helpers they share with the executor.
"""

from __future__ import annotations

from typing import NoReturn, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyActionResourceFence,
)

from services.memory_scope.shared_action_evidence import (
    FamilySharedActionConflictError,
    FamilySharedActionDeniedError,
    FamilySharedActionUnavailableError,
    _approvals_from_json,
    _binding_from_json,
    _captures_from_json,
    _json_object,
    _membership_from_json,
    _proposal_from_json,
    _tuple_strings,
)
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
)
from services.policy.evidence import (
    ApprovalEvidencePort,
    BindingEvidencePort,
    CaptureEvidencePort,
    MembershipEvidencePort,
    ProposalEvidencePort,
)
from services.policy.receipts import PolicyReceiptV2


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


