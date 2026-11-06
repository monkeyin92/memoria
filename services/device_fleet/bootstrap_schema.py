"""Deployment schema bootstrap mixin for the device-onboarding store.

``BootstrapSchemaMixin`` is composed into ``PostgresBootstrapStore``; the DSN,
pinned role, schema path, pool and initialized flag stay owned by the store and
are only declared here for type checking.  The module owns schema installation,
role-password rotation and the FORCE-RLS readiness check run on first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import asyncpg

from services.device_fleet.bootstrap_sync import PostgresBootstrapStoreContextError

BootstrapPostgresRole = Literal["api", "maintenance"]
_ROLE_NAMES: dict[BootstrapPostgresRole, str] = {
    "api": "memoria_device_onboarding_api",
    "maintenance": "memoria_device_onboarding_maintenance",
}
_SCHEMA_PATH = Path(__file__).with_name("bootstrap_postgres_schema.sql")


class BootstrapSchemaMixin:
    """Install the schema and pin the runtime role on first use."""

    if TYPE_CHECKING:
        # Shared state owned by PostgresBootstrapStore.__init__.
        _dsn: str
        role: BootstrapPostgresRole
        _schema_path: Path
        _pool: asyncpg.Pool | None
        _initialized: bool

    async def _initialize_async(
        self,
        *,
        bootstrap_dsn: str | None,
        role_password: str | None,
    ) -> None:
        if self._initialized:
            return
        if bootstrap_dsn is not None:
            await self._bootstrap(bootstrap_dsn, role_password=role_password)
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=8,
            command_timeout=30,
        )
        assert self._pool is not None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT current_user AS name, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            expected = _ROLE_NAMES[self.role]
            if (
                row is None
                or str(row["name"]) != expected
                or bool(row["rolsuper"])
                or bool(row["rolbypassrls"])
            ):
                await self._pool.close()
                self._pool = None
                raise PostgresBootstrapStoreContextError(
                    f"Device Onboarding DSN must authenticate as {expected} "
                    "with NOSUPERUSER NOBYPASSRLS"
                )
            required_tables = (
                "device_onboarding_devices",
                "device_onboarding_sessions",
                "device_onboarding_events",
                "device_onboarding_challenges",
                "device_media_challenges",
                "device_onboarding_claims",
                "device_onboarding_bindings",
                "device_onboarding_activations",
            )
            security_rows = await connection.fetch(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])",
                list(required_tables),
            )
            security = {str(item["relname"]): item for item in security_rows}
            if set(security) != set(required_tables) or any(
                not bool(security[name]["relrowsecurity"])
                or not bool(security[name]["relforcerowsecurity"])
                for name in required_tables
            ):
                await self._pool.close()
                self._pool = None
                raise PostgresBootstrapStoreContextError(
                    "Device Onboarding schema is not fully FORCE RLS protected"
                )
        self._initialized = True

    async def _bootstrap(self, dsn: str, *, role_password: str | None) -> None:
        connection = await asyncpg.connect(dsn)
        try:
            row = await connection.fetchrow(
                "SELECT current_user AS name, rolsuper, rolcreaterole "
                "FROM pg_roles WHERE rolname = current_user"
            )
            if row is None or (not bool(row["rolsuper"]) and not bool(row["rolcreaterole"])):
                raise PostgresBootstrapStoreContextError(
                    "Device Onboarding schema requires an administrator/CREATEROLE DSN"
                )
            await connection.execute(self._schema_path.read_text(encoding="utf-8"))
            if role_password is not None:
                for role_name in _ROLE_NAMES.values():
                    statement = await connection.fetchval(
                        "SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', "
                        "$1::text, $2::text)",
                        role_name,
                        role_password,
                    )
                    assert isinstance(statement, str)
                    await connection.execute(statement)
        finally:
            await connection.close()
