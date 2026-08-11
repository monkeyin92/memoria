"""Append-only PostgreSQL receipt repository (CONTRACT §8.6, table
``policy_receipts_v2``).

The repository exposes only ``insert`` and ``get``. ``insert`` is append-only:
an exact immutable replay under the same receipt_id succeeds as a no-op, while
different content raises ``PolicyReceiptConflictError``. There is intentionally
no update/delete surface. Obligations are stored as JSONB.

Retry idempotency is coordinated by ``DecisionService`` with a read-before-write
protocol: same scope/key outcomes replay stored rows, invalidated evidence gets
a deterministic outcome-version row, and valid-evidence outcome drift conflicts.
The repository remains the immutable comparison boundary and never treats a
timestamp-only reconstructed row as an exact replay.

Each insert/replay comparison runs in one explicit asyncpg transaction against
the single ``policy_receipts_v2`` table. There is no audit/outbox write: the
immutable receipt row itself is the audit record.

``asyncpg`` is imported lazily so the module (and all non-PG tests) import
cleanly without it; live tests must be run with ``MEMORIA_TEST_POSTGRES_DSN``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams as GeneratedObligationParams,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyActionResourceFence,
    PolicyObligationSpec,
    PolicyReceiptV2,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyObligation as PolicyObligationEnum,
)

from services.policy.action_fence import verify_action_resource_fence
from services.policy.obligations import obligation_params_from_json
from services.policy.receipts import PolicyReceiptConflictError

_SCHEMA_PATH = Path(__file__).with_name("postgres_receipt_schema.sql")
POLICY_RECEIPTS_V2_SCHEMA_SQL: str = _SCHEMA_PATH.read_text(encoding="utf-8")
_OBLIGATION_PARAM_KEYS = {
    "max_session_seconds",
    "retention_ttl_seconds",
    "quiet_hours",
    "extras",
}

_RECEIPT_SELECT = """
SELECT receipt_id, actor_id, subject_id, resource_owner_id,
       device_id, capability, purpose, effect, reason_code,
       obligations, policy_version, context_hash,
       action_resource_fence, action_fence_hash,
       consent_snapshot_ids, consent_snapshot_revisions,
       relationship_snapshot_ids, relationship_snapshot_revisions,
       binding_id, binding_version,
       binding_canonical_hash, session_id, session_epoch, runtime_profile_id,
       subject_revision, device_trust, data_classification,
       safety_state, jurisdiction, created_at, expires_at,
       exact_fence
FROM policy_receipts_v2 WHERE receipt_id = $1
"""

_RECEIPT_INSERT = """
INSERT INTO policy_receipts_v2 (
    receipt_id, actor_id, subject_id, resource_owner_id,
    device_id, capability, purpose, effect, reason_code,
    obligations, policy_version, context_hash,
    action_resource_fence, action_fence_hash,
    consent_snapshot_ids, consent_snapshot_revisions,
    relationship_snapshot_ids, relationship_snapshot_revisions,
    binding_id, binding_version,
    binding_canonical_hash, session_id, session_epoch, runtime_profile_id,
    subject_revision, device_trust, data_classification,
    safety_state, jurisdiction, created_at, expires_at,
    exact_fence
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
          $11, $12, $13::jsonb, $14, $15::jsonb, $16::jsonb,
          $17::jsonb, $18::jsonb, $19, $20, $21, $22, $23,
          $24, $25, $26, $27, $28, $29, $30, $31, $32)
ON CONFLICT (receipt_id) DO NOTHING
"""


def _obligations_to_json(
    obligations: tuple[PolicyObligationSpec, ...],
) -> list[dict[str, object]]:
    return [obligation.model_dump(mode="json") for obligation in obligations]


def _obligations_from_json(data: object) -> tuple[PolicyObligationSpec, ...]:
    if not isinstance(data, list):
        raise ValueError("obligations must be a JSON array")
    result: list[PolicyObligationSpec] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("obligation entries must be objects")
        if frozenset(entry) != {"code", "params"}:
            raise ValueError("obligation entries must contain exactly code and params")
        code = entry.get("code")
        params_data = entry.get("params")
        if (
            not isinstance(code, str)
            or PolicyObligationEnum.from_value(code) is None
            or not isinstance(params_data, dict)
        ):
            raise ValueError("obligation entry must carry code + params")
        if set(params_data) != _OBLIGATION_PARAM_KEYS:
            raise ValueError("obligation params require exact canonical keys")
        params = obligation_params_from_json(params_data)
        result.append(
            PolicyObligationSpec(
                code=code,  # type: ignore[arg-type]
                params=GeneratedObligationParams(
                    max_session_seconds=params.max_session_seconds,
                    retention_ttl_seconds=params.retention_ttl_seconds,
                    quiet_hours=params.quiet_hours,
                    extras=params.extras,
                ),
            )
        )
    return tuple(result)


def _str_list(value: object, name: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str)
        or not item.strip()
        or len(item) > maximum
        for item in value
    ):
        raise ValueError(
            f"{name} must be a JSON array of non-empty strings up to {maximum} chars"
        )
    return tuple(value)


def _int_list(value: object, name: str, *, minimum: int) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < minimum
        for item in value
    ):
        raise ValueError(f"{name} must be a JSON array of integers >= {minimum}")
    return tuple(value)


class PostgresPolicyReceiptRepository:
    """Append-only receipts on PostgreSQL (only INSERT + SELECT)."""

    def __init__(
        self,
        *,
        dsn: str,
        connect_timeout_seconds: float = 5.0,
        command_timeout_seconds: float = 10.0,
        application_name: str = "memoria-policy-api",
    ) -> None:
        self._dsn = dsn
        self._connect_timeout_seconds = connect_timeout_seconds
        self._command_timeout_seconds = command_timeout_seconds
        self._application_name = application_name

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(
            self._dsn,
            timeout=self._connect_timeout_seconds,
            command_timeout=self._command_timeout_seconds,
            server_settings={"application_name": self._application_name},
        )

    async def insert(
        self,
        receipt: PolicyReceiptV2,
        *,
        actor_id: str | None = None,
        subject_id: str | None = None,
    ) -> None:
        """Insert only with an explicit authenticated actor/subject scope.

        The optional spelling preserves the repository port's historical
        method shape, but an omitted principal is deliberately rejected.  A
        receipt's own actor/subject fields are data, never authorization
        context; callers must provide the independently authenticated values.
        """
        if actor_id is None or subject_id is None:
            raise ValueError(
                "Postgres policy receipt writes require authenticated actor and subject"
            )
        await self.insert_scoped(
            receipt,
            actor_id=actor_id,
            subject_id=subject_id,
        )

    async def insert_scoped(
        self,
        receipt: PolicyReceiptV2,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> None:
        _validate_principal(actor_id, subject_id)
        connection = await self._connect()
        try:
            async with connection.transaction():
                await _set_transaction_principal(
                    connection,
                    actor_id=actor_id,
                    subject_id=subject_id,
                )
                await _insert_with_connection(connection, receipt)
        finally:
            await connection.close()

    async def get(
        self,
        receipt_id: str,
        *,
        actor_id: str | None = None,
        subject_id: str | None = None,
    ) -> PolicyReceiptV2 | None:
        if actor_id is None or subject_id is None:
            raise ValueError(
                "Postgres policy receipt reads require authenticated actor and subject"
            )
        return await self.get_scoped(
            receipt_id,
            actor_id=actor_id,
            subject_id=subject_id,
        )

    async def get_scoped(
        self,
        receipt_id: str,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> PolicyReceiptV2 | None:
        _validate_principal(actor_id, subject_id)
        connection = await self._connect()
        try:
            async with connection.transaction():
                await _set_transaction_principal(
                    connection,
                    actor_id=actor_id,
                    subject_id=subject_id,
                )
                return await self._get_with_connection(connection, receipt_id)
        finally:
            await connection.close()

    async def _get_with_connection(
        self, connection: asyncpg.Connection, receipt_id: str
    ) -> PolicyReceiptV2 | None:
        row = await connection.fetchrow(
            _RECEIPT_SELECT,
            receipt_id,
        )
        if row is None:
            return None
        return policy_receipt_from_row(row)


class ConnectionBoundPolicyReceiptRepository:
    """Append/replay receipts on one caller-owned asyncpg transaction.

    This adapter never acquires a connection and never opens, commits, or rolls
    back a transaction.  ``insert_many`` is deliberately sequential in sorted
    receipt-id order, so concurrent batches contend deterministically.  A
    conflict raises inside the caller's transaction; callers must let that
    transaction roll back to preserve all-or-nothing batch semantics.
    """

    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        _require_caller_transaction(connection)
        if len({item.receipt_id for item in receipts}) != len(receipts):
            raise PolicyReceiptConflictError("receipt batch contains duplicate ids")
        await connection.execute("SAVEPOINT policy_receipt_batch_insert")
        try:
            for receipt in sorted(receipts, key=lambda item: item.receipt_id):
                await _insert_with_connection(connection, receipt)
        except BaseException:
            await connection.execute("ROLLBACK TO SAVEPOINT policy_receipt_batch_insert")
            await connection.execute("RELEASE SAVEPOINT policy_receipt_batch_insert")
            raise
        else:
            await connection.execute("RELEASE SAVEPOINT policy_receipt_batch_insert")

    async def get_for_update(
        self, connection: asyncpg.Connection, receipt_id: str
    ) -> PolicyReceiptV2 | None:
        _require_caller_transaction(connection)
        return await lock_policy_receipt(connection, receipt_id)


def _require_caller_transaction(connection: asyncpg.Connection) -> None:
    in_transaction = getattr(connection, "is_in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        raise RuntimeError("connection-bound repository requires caller-owned transaction")


def _validate_principal(actor_id: str, subject_id: str | None) -> None:
    if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 128:
        raise ValueError("authenticated actor must be a bounded non-empty string")
    if subject_id is not None and (
        not isinstance(subject_id, str)
        or not subject_id.strip()
        or len(subject_id) > 128
    ):
        raise ValueError("authenticated subject must be null or a bounded non-empty string")


async def _set_transaction_principal(
    connection: asyncpg.Connection,
    *,
    actor_id: str,
    subject_id: str | None,
) -> None:
    """Install caller-authenticated identity for this transaction only."""
    await connection.execute(
        "SELECT set_config('app.authenticated_actor', $1, true)",
        actor_id,
    )
    await connection.execute(
        "SELECT set_config('app.authenticated_subject', $1, true)",
        subject_id or "",
    )


async def _insert_with_connection(
    connection: asyncpg.Connection, receipt: PolicyReceiptV2
) -> None:
    if not isinstance(receipt, PolicyReceiptV2):
        raise TypeError("receipt must be generated PolicyReceiptV2")
    if not verify_action_resource_fence(receipt.action_resource_fence):
        raise ValueError("action_resource_fence hashes are invalid")
    status = await connection.execute(
        _RECEIPT_INSERT,
        *_receipt_insert_args(receipt),
    )
    if status == "INSERT 0 0":
        row = await connection.fetchrow(
            _RECEIPT_SELECT,
            receipt.receipt_id,
        )
        current = None if row is None else policy_receipt_from_row(row)
        if current != receipt:
            raise PolicyReceiptConflictError(
                "policy receipt id is immutable and content differs"
            )


def _receipt_insert_args(receipt: PolicyReceiptV2) -> tuple[object, ...]:
    return (
        receipt.receipt_id,
        receipt.actor_id,
        receipt.subject_id,
        receipt.resource_owner_id,
        receipt.device_id,
        receipt.capability,
        receipt.purpose,
        receipt.effect,
        receipt.reason_code,
        _json_dump(_obligations_to_json(receipt.obligations)),
        receipt.policy_version,
        receipt.context_hash,
        _json_dump(receipt.action_resource_fence.model_dump(mode="json")),
        receipt.action_fence_hash,
        _json_dump(list(receipt.consent_snapshot_ids)),
        _json_dump(list(receipt.consent_snapshot_revisions)),
        _json_dump(list(receipt.relationship_snapshot_ids)),
        _json_dump(list(receipt.relationship_snapshot_revisions)),
        receipt.binding_id,
        receipt.binding_version,
        receipt.binding_canonical_hash,
        receipt.session_id,
        receipt.session_epoch,
        receipt.runtime_profile_id,
        receipt.subject_revision,
        receipt.device_trust,
        receipt.data_classification,
        receipt.safety_state,
        receipt.jurisdiction,
        receipt.created_at,
        receipt.expires_at,
        receipt.exact_fence,
    )


async def lock_policy_receipt(
    connection: asyncpg.Connection, receipt_id: str
) -> PolicyReceiptV2 | None:
    """Lock one immutable receipt row on the caller-owned transaction.

    Runtime roles intentionally lack UPDATE, so a narrow SECURITY DEFINER
    function takes the row lock.  The row itself is then decoded through the
    caller's ordinary SELECT/RLS boundary on the same connection.
    """
    found = await connection.fetchval(
        "SELECT policy_lock_receipt_v2($1)",
        receipt_id,
    )
    if found is not True:
        return None
    row = await connection.fetchrow(
        _RECEIPT_SELECT,
        receipt_id,
    )
    return None if row is None else policy_receipt_from_row(row)


def policy_receipt_from_row(row: object) -> PolicyReceiptV2:
    """Strictly decode a PostgreSQL record-like mapping."""
    record = cast(Any, row)
    action_fence = _action_fence_from_json(_json_load(record["action_resource_fence"]))
    obligations = _obligations_from_json(_json_load(record["obligations"]))
    payload = {
            "receipt_id": record["receipt_id"],
            "actor_id": record["actor_id"],
            "subject_id": record["subject_id"],
            "resource_owner_id": record["resource_owner_id"],
            "device_id": record["device_id"],
            "capability": record["capability"],
            "purpose": record["purpose"],
            "effect": record["effect"],
            "reason_code": record["reason_code"],
            "obligations": [item.model_dump(mode="json") for item in obligations],
            "policy_version": record["policy_version"],
            "context_hash": record["context_hash"],
            "action_resource_fence": action_fence.model_dump(mode="json"),
            "action_fence_hash": record["action_fence_hash"],
            "consent_snapshot_ids": list(_str_list(
                _json_load(record["consent_snapshot_ids"]),
                "consent_snapshot_ids",
                maximum=128,
            )),
            "consent_snapshot_revisions": list(_int_list(
                _json_load(record["consent_snapshot_revisions"]),
                "consent_snapshot_revisions",
                minimum=1,
            )),
            "relationship_snapshot_ids": list(_str_list(
                _json_load(record["relationship_snapshot_ids"]),
                "relationship_snapshot_ids",
                maximum=128,
            )),
            "relationship_snapshot_revisions": list(_int_list(
                _json_load(record["relationship_snapshot_revisions"]),
                "relationship_snapshot_revisions",
                minimum=1,
            )),
            "binding_id": record["binding_id"],
            "binding_version": record["binding_version"],
            "binding_canonical_hash": record["binding_canonical_hash"],
            "session_id": record["session_id"],
            "session_epoch": record["session_epoch"],
            "runtime_profile_id": record["runtime_profile_id"],
            "subject_revision": record["subject_revision"],
            "device_trust": record["device_trust"],
            "data_classification": record["data_classification"],
            "safety_state": record["safety_state"],
            "jurisdiction": record["jurisdiction"],
            "created_at": _db_timestamp(record["created_at"]),
            "expires_at": _db_timestamp(record["expires_at"]),
            "exact_fence": record["exact_fence"],
    }
    return PolicyReceiptV2.model_validate(payload)


def _db_timestamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("PostgreSQL receipt timestamps must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_load(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _action_fence_from_json(value: object) -> PolicyActionResourceFence:
    if not isinstance(value, dict):
        raise ValueError("action_resource_fence must be a JSON object")
    fence = PolicyActionResourceFence.model_validate(value)
    if not verify_action_resource_fence(fence):
        raise ValueError("action_resource_fence hashes are invalid")
    return fence
