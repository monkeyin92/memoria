"""PostgreSQL authority adapters behind the Session Runtime action ports.

Each adapter calls one ``SECURITY DEFINER`` function on the caller's own
transaction and fails closed on a fence mismatch.  The orchestration service
owns ordering and policy; this module owns only the SQL-shaped authority
reads and the evidence values they produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyActionResourceFence,
    PolicyObligationSpec,
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
    SessionEvent,
)

from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
)
from services.policy.context import PolicyContext, effective_action_resource_fence
from services.policy.evidence import BindingEvidencePort, RelationshipEvidencePort
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.receipts import PolicyReceiptConflictError
from services.session_runtime.device_trust import (
    DeviceTrustSnapshot,
    snapshot_from_authority,
)
from services.session_runtime.errors import (
    PersistentSessionDenied,
    PersistentSessionUnavailable,
)
from services.session_runtime.wire import (
    _canonical_json,
    _decode_json_object,
    _LockedBinding,
    _parse_locked_binding,
)


class _PostgresIdentityAuthority:
    async def lock_binding(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        expected_binding_version: int,
        now: datetime,
    ) -> _LockedBinding:
        try:
            raw = await connection.fetchval(
                "SELECT action_identity_lock_binding($1, $2, $3, $4)",
                actor_id,
                device_id,
                expected_binding_version,
                now,
            )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionDenied(str(exc)) from exc
        if raw is None:
            raise PersistentSessionDenied("active actor/device binding is unavailable")
        binding = _parse_locked_binding(raw)
        if (
            binding.evidence.device_id != device_id
            or binding.evidence.version != expected_binding_version
            or not any(item.person_id == actor_id for item in binding.roles)
        ):
            raise PersistentSessionDenied("binding authority fence mismatch")
        return binding

    async def can_switch_subject(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        binding_version: int,
        subject_id: str,
    ) -> bool:
        try:
            allowed = await connection.fetchval(
                "SELECT action_identity_can_switch_subject($1, $2, $3, $4)",
                actor_id,
                device_id,
                binding_version,
                subject_id,
            )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        return allowed is True

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> BindingEvidencePort:
        binding = await self.lock_binding(
            connection,
            actor_id=request.context.actor_id,
            device_id=request.context.device_id,
            expected_binding_version=request.context.binding_version,
            now=request.now,
        )
        if (
            binding.evidence.binding_id != receipt.binding_id
            or binding.evidence.canonical_hash != receipt.binding_canonical_hash
        ):
            raise ActionAuthorizationError("current binding evidence mismatch")
        return binding.evidence


class _PostgresIdentityRelationshipAuthority:
    def __init__(self, identity: _PostgresIdentityAuthority) -> None:
        self._identity = identity

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> tuple[RelationshipEvidencePort, ...]:
        binding = await self._identity.lock_binding(
            connection,
            actor_id=request.context.actor_id,
            device_id=request.context.device_id,
            expected_binding_version=request.context.binding_version,
            now=request.now,
        )
        expected = set(
            zip(
                receipt.relationship_snapshot_ids,
                receipt.relationship_snapshot_revisions,
                strict=True,
            )
        )
        receipted = {
            (item.snapshot_id, item.revision)
            for item in binding.relationships
            if (item.snapshot_id, item.revision) in expected
        }
        if receipted != expected:
            raise ActionAuthorizationError("current relationship evidence mismatch")
        # The decision context carried every current binding relationship
        # (all of them are hashed), so rebuild exactly that set: a
        # relationship added or withdrawn since issue changes the hash and
        # fails closed, while the receipt still names only what it relied on.
        return tuple(binding.relationships)


class _PostgresDeviceAuthority:
    def __init__(self, *, accept_bound_device: bool = False) -> None:
        self._accept_bound_device = accept_bound_device

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        now: datetime,
    ) -> DeviceTrustSnapshot:
        try:
            raw = await connection.fetchval(
                "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
                actor_id,
                device_id,
                binding_id,
                binding_version,
                now,
            )
        except asyncpg.UndefinedFunctionError:
            return DeviceTrustSnapshot(
                available=False,
                trust="untrusted",
                reason_code="device_authority_unavailable",
            )
        except asyncpg.PostgresError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc
        payload = _decode_json_object(raw, name="Device trust authority")
        try:
            return snapshot_from_authority(
                payload, accept_bound_device=self._accept_bound_device
            )
        except ValueError as exc:
            raise PersistentSessionUnavailable(str(exc)) from exc


class _PostgresPrincipalAuthority:
    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> str:
        del receipt
        actor = await connection.fetchval(
            "SELECT NULLIF(current_setting('app.authenticated_actor', true), '')"
        )
        if not isinstance(actor, str) or actor != request.context.actor_id:
            raise ActionAuthorizationError("authenticated principal is unavailable")
        return actor


class _PostgresSessionActionAuthority:
    def __init__(self, device: _PostgresDeviceAuthority) -> None:
        self._device = device

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: PolicyReceiptV2,
        request: ActionExecutionRequest,
    ) -> PolicyActionResourceFence:
        expected = effective_action_resource_fence(request.context)
        if expected != receipt.action_resource_fence:
            raise ActionAuthorizationError("Session action fence mismatch")
        trust = await self._device.lock_current(
            connection,
            actor_id=request.context.actor_id,
            device_id=request.context.device_id,
            binding_id=request.context.binding_id,
            binding_version=request.context.binding_version,
            now=request.now,
        )
        if trust.trust != request.context.device_trust:
            raise ActionAuthorizationError("current Device trust fence mismatch")
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
        if valid is not True:
            raise ActionAuthorizationError("Session action authority is unavailable")
        return receipt.action_resource_fence


class _ActionExecutorReceiptRepository(ConnectionBoundPolicyReceiptRepository):
    """Append receipts through the Policy SECURITY DEFINER bridge only."""

    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        if not connection.is_in_transaction():
            raise RuntimeError("receipt batch requires caller-owned transaction")
        if len({item.receipt_id for item in receipts}) != len(receipts):
            raise PolicyReceiptConflictError("receipt batch contains duplicate ids")
        for receipt in sorted(receipts, key=lambda item: item.receipt_id):
            raw = await connection.fetchval(
                "SELECT action_policy_insert_receipt($1::jsonb)",
                _canonical_json(receipt.model_dump(mode="json")),
            )
            persisted = PolicyReceiptV2.model_validate(
                _decode_json_object(raw, name="persisted Policy receipt")
            )
            if persisted != receipt:
                raise PolicyReceiptConflictError(
                    "policy receipt id is immutable and content differs"
                )


def _persona_snapshot(assignment_id: str) -> tuple[str, int]:
    persona_id, separator, raw_version = assignment_id.rpartition(":v")
    if not separator or not persona_id or not raw_version.isdigit():
        raise PersistentSessionUnavailable(
            "persona assignment lacks a canonical persona/version snapshot"
        )
    version = int(raw_version)
    if version < 1:
        raise PersistentSessionUnavailable("persona version is invalid")
    return persona_id, version


def _obligation_union(
    receipts: tuple[PolicyReceiptV2, ...],
) -> tuple[PolicyObligationSpec, ...]:
    by_code: dict[str, PolicyObligationSpec] = {}
    for receipt in receipts:
        for obligation in receipt.obligations:
            encoded = _canonical_json(obligation.model_dump(mode="json"))
            previous = by_code.get(obligation.code.value)
            if (
                previous is not None
                and _canonical_json(previous.model_dump(mode="json")) != encoded
            ):
                raise PersistentSessionUnavailable(
                    "Policy returned conflicting obligation parameters"
                )
            by_code[obligation.code.value] = obligation
    return tuple(by_code[key] for key in sorted(by_code))


@dataclass(frozen=True, slots=True)
class _IssuedProfile:
    profile: RuntimeProfileSignedV2
    event: SessionEvent
    allowed: tuple[tuple[PolicyContext, PolicyReceiptV2, object | None], ...]
    denied: tuple[PolicyReceiptV2, ...]

    @property
    def allowed_receipts(self) -> tuple[PolicyReceiptV2, ...]:
        return tuple(item[1] for item in self.allowed)

    def requests(self, *, now: datetime) -> tuple[ActionExecutionRequest, ...]:
        return tuple(
            ActionExecutionRequest(
                receipt_id=receipt.receipt_id,
                context=context,
                now=now,
                consent_authority_proof=consent_authority_proof,
            )
            for context, receipt, consent_authority_proof in self.allowed
        )
