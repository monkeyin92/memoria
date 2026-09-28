"""Release the fleet side of a committed binding once Identity has revoked it.

Unbinding in the Mini Program revokes the Identity binding; without this the
fleet kept the device ``bound``: it still served the device its activation
manifest and answered every scan of its QR with ``DEVICE_ALREADY_BOUND``, so
the owner could never bind it again.

Releasing returns the device to ``provisioned`` with no binding or actor (the
schema's bound <=> binding-fields check), marks the fleet binding and its
committed claim ``released``, and keeps ``activation_version`` and the
monotonic counters, so the next binding's manifest version still moves
forward. It is idempotent: a device that is not bound to ``binding_id`` is
left untouched and reported as not released.

The store modules sit at their line budgets, so both store implementations
live here and use the stores' own transaction and scope helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from services.device_fleet.bootstrap_domain import ClaimStatus, DeviceLifecycle, now_utc
from services.device_fleet.bootstrap_port import BootstrapStorePort
from services.device_fleet.bootstrap_store import SQLiteBootstrapStore

if TYPE_CHECKING:
    import asyncpg

    from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore


def release_committed_binding(
    store: BootstrapStorePort,
    *,
    device_id: str,
    binding_id: str,
    reason_code: str,
    now: datetime,
) -> bool:
    if isinstance(store, SQLiteBootstrapStore):
        return _release_sqlite(
            store, device_id=device_id, binding_id=binding_id, reason_code=reason_code, now=now
        )
    from services.device_fleet.bootstrap_postgres_store import (
        PostgresBootstrapStore as _Postgres,
    )

    if isinstance(store, _Postgres):
        return _release_postgres(
            store, device_id=device_id, binding_id=binding_id, reason_code=reason_code, now=now
        )
    raise TypeError(f"unsupported bootstrap store {type(store).__name__}")


def _release_sqlite(
    store: SQLiteBootstrapStore,
    *,
    device_id: str,
    binding_id: str,
    reason_code: str,
    now: datetime,
) -> bool:
    timestamp = now_utc(now).isoformat().replace("+00:00", "Z")
    with store._write() as connection:  # noqa: SLF001 - see module docstring
        row = connection.execute(
            "SELECT lifecycle_status, binding_id FROM device_onboarding_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if (
            row is None
            or row["lifecycle_status"] != DeviceLifecycle.BOUND.value
            or row["binding_id"] != binding_id
        ):
            return False
        connection.execute(
            """
            UPDATE device_onboarding_devices
            SET lifecycle_status = ?, binding_id = NULL, binding_version = NULL,
                actor_id = NULL, updated_at = ?
            WHERE device_id = ?
            """,
            (DeviceLifecycle.PROVISIONED.value, timestamp, device_id),
        )
        connection.execute(
            "UPDATE device_onboarding_bindings SET status = 'released' WHERE binding_id = ?",
            (binding_id,),
        )
        connection.execute(
            """
            UPDATE device_onboarding_claims
            SET status = ?, released_at = ?, failure_code = ?
            WHERE binding_id = ? AND status = ?
            """,
            (
                ClaimStatus.RELEASED.value,
                timestamp,
                reason_code,
                binding_id,
                ClaimStatus.COMMITTED.value,
            ),
        )
    return True


def _release_postgres(
    store: PostgresBootstrapStore,
    *,
    device_id: str,
    binding_id: str,
    reason_code: str,
    now: datetime,
) -> bool:
    timestamp = now_utc(now)
    # The device scope is what lets the row's actor be cleared under RLS.
    scope = store._scope(device_id=device_id)  # noqa: SLF001 - see module docstring

    async def operation(connection: asyncpg.Connection) -> bool:
        row = await connection.fetchrow(
            """
            SELECT lifecycle_status, binding_id FROM device_onboarding_devices
            WHERE device_id = $1 FOR UPDATE
            """,
            device_id,
        )
        if (
            row is None
            or row["lifecycle_status"] != DeviceLifecycle.BOUND.value
            or row["binding_id"] != binding_id
        ):
            return False
        await connection.execute(
            """
            UPDATE device_onboarding_devices
            SET lifecycle_status = $1, binding_id = NULL, binding_version = NULL,
                actor_id = NULL, updated_at = $2
            WHERE device_id = $3
            """,
            DeviceLifecycle.PROVISIONED.value,
            timestamp,
            device_id,
        )
        await connection.execute(
            "UPDATE device_onboarding_bindings SET status = 'released' WHERE binding_id = $1",
            binding_id,
        )
        await connection.execute(
            """
            UPDATE device_onboarding_claims
            SET status = $1, released_at = $2, failure_code = $3
            WHERE binding_id = $4 AND status = $5
            """,
            ClaimStatus.RELEASED.value,
            timestamp,
            reason_code,
            binding_id,
            ClaimStatus.COMMITTED.value,
        )
        return True

    return store._call(store._transaction(scope, operation))  # noqa: SLF001
