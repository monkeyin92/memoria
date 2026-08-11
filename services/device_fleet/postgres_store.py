"""Role-pinned PostgreSQL adapter for the Device Fleet authority seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncpg

from services.device_fleet.domain import DeviceFleetContext

DeviceFleetRole = Literal[
    "api", "projector", "worker", "maintenance", "action_executor"
]
_ROLE_NAMES: dict[DeviceFleetRole, str] = {
    "api": "memoria_device_fleet_api",
    "projector": "memoria_device_fleet_projector",
    "worker": "memoria_device_fleet_worker",
    "maintenance": "memoria_device_fleet_maintenance",
    "action_executor": "memoria_action_executor",
}
_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")


class PostgresDeviceFleetStore:
    """Small store interface: role validation, transactions and RLS context."""

    def __init__(
        self,
        dsn: str,
        *,
        role: DeviceFleetRole,
        schema_path: str | Path | None = None,
    ) -> None:
        self._dsn = dsn
        self.role = role
        self._schema_path = Path(schema_path) if schema_path is not None else _SCHEMA_PATH
        self._pool: asyncpg.Pool | None = None

    async def initialize(
        self,
        *,
        bootstrap_dsn: str | None = None,
        role_password: str | None = None,
    ) -> None:
        if bootstrap_dsn is not None:
            await self._bootstrap(bootstrap_dsn, role_password=role_password)
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=8)
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT current_user AS name, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            expected = _ROLE_NAMES[self.role]
            if (
                row is None
                or row["name"] != expected
                or bool(row["rolsuper"])
                or bool(row["rolbypassrls"])
            ):
                raise RuntimeError(
                    f"Device Fleet {self.role} DSN must authenticate as {expected} "
                    "with NOSUPERUSER NOBYPASSRLS"
                )

    async def _bootstrap(self, dsn: str, *, role_password: str | None) -> None:
        connection = await asyncpg.connect(dsn)
        try:
            row = await connection.fetchrow(
                "SELECT current_user AS name, rolsuper FROM pg_roles "
                "WHERE rolname = current_user"
            )
            if row is None or (
                not bool(row["rolsuper"])
                and row["name"] != _ROLE_NAMES["maintenance"]
            ):
                raise RuntimeError(
                    "Device Fleet migrations require an administrator or maintenance role"
                )
            await connection.execute(self._schema_path.read_text(encoding="utf-8"))
            for role_name in _ROLE_NAMES.values():
                if role_password is None:
                    statement = await connection.fetchval(
                        "SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS', $1::text)",
                        role_name,
                    )
                else:
                    statement = await connection.fetchval(
                        "SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', "
                        "$1::text, $2::text)",
                        role_name,
                        role_password,
                    )
                await connection.execute(statement)
        finally:
            await connection.close()

    def require_role(self, *roles: DeviceFleetRole) -> None:
        if self.role not in roles:
            names = ", ".join(roles)
            raise RuntimeError(f"Device Fleet operation requires database role: {names}")

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Device Fleet store is not initialized")
        return self._pool

    async def set_context_on_connection(
        self,
        connection: asyncpg.Connection,
        context: DeviceFleetContext | None,
    ) -> None:
        """Install transaction-local scope for same-transaction integrations."""

        if not connection.is_in_transaction():
            raise RuntimeError("Device Fleet context requires an active transaction")
        values = {
            "role": self.role,
            "device_id": context.device_id if context is not None else "",
            "binding_id": context.binding_id if context is not None else "",
            "binding_version": str(context.binding_version) if context is not None else "",
            "family_space_id": context.family_space_id if context is not None else "",
        }
        for key, value in values.items():
            await connection.execute(
                "SELECT set_config('app.device_fleet.' || $1, $2, true)", key, value
            )

    @asynccontextmanager
    async def transaction(
        self,
        context: DeviceFleetContext | None = None,
    ) -> AsyncIterator[asyncpg.Connection]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self.set_context_on_connection(connection, context)
                yield connection

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()


__all__ = ["DeviceFleetRole", "PostgresDeviceFleetStore"]
