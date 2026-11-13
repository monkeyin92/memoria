"""Physical connection adapters and PostgreSQL authority database checks."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from datetime import datetime
from typing import Protocol

import asyncpg


class SessionRuntimeStoreError(RuntimeError):
    """Base persistence failure."""


def _json_object(value: object, *, field: str) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise SessionRuntimeStoreError(f"{field} is not a JSON object")
    return {str(key): item for key, item in decoded.items()}


class _Connect(Protocol):
    def __call__(self, dsn: str, *, application_name: str) -> Awaitable[asyncpg.Connection]: ...


async def same_authority_database(
    projector: asyncpg.Connection, connect: _Connect, *, read_dsn: str, action_dsn: str,
) -> bool:
    identity = await _database_identity(projector, "memoria_session_projector")
    if identity is None:
        return False
    for dsn, role in ((read_dsn, "memoria_session_api"), (action_dsn, "memoria_action_executor")):
        authority = await connect(dsn, application_name="memoria-session-authority-check")
        try:
            async with authority.transaction(readonly=True):
                if await _database_identity(authority, role) != identity:
                    return False
        finally:
            await authority.close()
    return True


async def _database_identity(connection: asyncpg.Connection, role: str) -> str | None:
    if await connection.fetchval("SELECT current_user") != role:
        return None
    identity = await connection.fetchval(
        """
        SELECT pg_catalog.json_build_array(
            control.system_identifier::text, database.oid::text, database.datname,
            CASE WHEN pg_catalog.inet_server_addr() IN ('127.0.0.1'::inet, '::1'::inet)
                 THEN 'loopback' ELSE pg_catalog.inet_server_addr()::text END,
            pg_catalog.inet_server_port()
        )::text
        FROM pg_catalog.pg_control_system() AS control, pg_catalog.pg_database AS database
        WHERE database.datname = pg_catalog.current_database()
          AND NOT pg_catalog.pg_is_in_recovery()
          AND pg_catalog.inet_server_addr() IS NOT NULL
        """
    )
    return identity if isinstance(identity, str) and identity else None


class _ActionExecutorConnection:
    """Capability-reducing facade over one physical asyncpg connection.

    Policy's public Session batch currently reads its locked receipt through a
    table-shaped repository query after invoking ``policy_lock_receipt_v2``.
    The unified action role deliberately has no table SELECT.  This facade maps
    only that explicit lock/read sequence to Session's validated
    ``action_policy_lock_receipt`` SECURITY DEFINER port; every other operation
    stays on the same underlying connection and transaction.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    def is_in_transaction(self) -> bool:
        return bool(self._connection.is_in_transaction())

    async def execute(self, query: str, *args: object, **kwargs: object) -> str:
        return str(await self._connection.execute(query, *args, **kwargs))

    async def fetchval(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if "policy_lock_receipt_v2" in query:
            raw = await self._connection.fetchval(
                "SELECT action_policy_lock_receipt($1)",
                *args,
            )
            return raw is not None
        return await self._connection.fetchval(query, *args, **kwargs)

    async def fetchrow(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if "FROM policy_receipts_v2 WHERE receipt_id = $1" in query:
            raw = await self._connection.fetchval(
                "SELECT action_policy_lock_receipt($1)",
                *args,
            )
            if raw is None:
                return None
            receipt = _json_object(raw, field="policy receipt")
            for field in ("created_at", "expires_at"):
                value = receipt.get(field)
                if isinstance(value, str):
                    receipt[field] = datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    )
            return receipt
        return await self._connection.fetchrow(query, *args, **kwargs)

    async def fetch(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> list[asyncpg.Record]:
        return list(await self._connection.fetch(query, *args, **kwargs))
