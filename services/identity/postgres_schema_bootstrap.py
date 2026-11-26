"""Schema bootstrap and role verification for the identity PostgreSQL store.

``PostgresIdentitySchemaMixin`` is composed into ``PostgresIdentityStore``.  The
store owns the pools and the lifecycle; this module owns the one-time schema
apply and the privilege checks that make the pools safe to use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
from asyncpg.exceptions import InsufficientPrivilegeError

_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")

_REGISTRATION_ROLE = "memoria_identity_registration"

_API_NO_DIRECT_WRITE_TABLES = (
    "identity_persons",
    "identity_audit_events",
    "identity_outbox",
    "identity_idempotency_records",
)

# Create-once tables the API role may INSERT/DELETE but must never UPDATE:
# the immutability trigger is the database-layer guard, this is the grant-level
# double lock.
_API_NO_UPDATE_TABLES = ("identity_custom_personas",)

_REQUIRED_TABLES = frozenset(
    {
        "identity_persons",
        "identity_relationships",
        "identity_device_bindings",
        "identity_device_binding_roles",
        "identity_audit_events",
        "identity_outbox",
        "identity_transfer_intents",
        "identity_idempotency_records",
        "identity_persona_assignments",
        "identity_custom_personas",
        "identity_subject_releases",
    }
)

_REQUIRED_ROLES = frozenset(
    {
        "memoria_identity",
        "memoria_identity_registration",
        "memoria_identity_outbox",
        "memoria_identity_migration",
    }
)


class PostgresIdentitySchemaMixin:
    """Apply the identity schema once and verify the roles and ports it needs."""

    if TYPE_CHECKING:
        # Shared state owned by PostgresIdentityStore.
        _dsn: str
        _registration_dsn: str | None
        _pool: asyncpg.Pool | None
        _registration_pool: asyncpg.Pool | None
        _initialize_lock: asyncio.Lock

        # Core member consumed by the bootstrap.
        async def close(self) -> None: ...

    async def initialize(self, *, expected_role: str | None = None) -> None:
        # Once, under a lock (lazy first use); a failure leaves nothing half-open.
        async with self._initialize_lock:
            if self._pool is not None:
                return
            try:
                await self._open_and_verify(expected_role=expected_role)
            except BaseException:
                await self.close()
                raise

    async def _open_and_verify(self, *, expected_role: str | None) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        assert self._pool is not None
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        async with self._pool.acquire() as connection:
            try:
                await connection.execute(schema)
            except InsufficientPrivilegeError:
                # A non-owner role (e.g. the API role) cannot run DDL; the
                # schema was bootstrapped by an admin.  Verification below
                # still guards roles / FORCE RLS / open policies.
                pass
            await connection.execute("SET application_name = 'memoria-identity'")
            present = {
                str(row["tablename"])
                for row in await connection.fetch(
                    """
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'identity\\_%'
                    """
                )
            }
            missing = _REQUIRED_TABLES - present
            if missing:
                raise RuntimeError(f"identity schema tables missing: {sorted(missing)}")
            rls_state = await connection.fetch(
                """
                SELECT c.relname AS tablename,
                       c.relrowsecurity,
                       c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname LIKE 'identity\\_%'
                  AND c.relkind = 'r'
                """
            )
            unenabled = [
                str(row["tablename"])
                for row in rls_state
                if not bool(row["relrowsecurity"])
            ]
            if unenabled:
                raise RuntimeError(
                    f"identity tables must enable row-level security: "
                    f"{sorted(unenabled)}"
                )
            unforced = [
                str(row["tablename"])
                for row in rls_state
                if not bool(row["relforcerowsecurity"])
            ]
            if unforced:
                raise RuntimeError(
                    f"identity tables must force row-level security: "
                    f"{sorted(unforced)}"
                )
            open_policies = [
                f"{row['tablename']}.{row['policyname']}"
                for row in await connection.fetch(
                    """
                    SELECT tablename, policyname, qual, with_check
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'identity\\_%'
                    """
                )
                if str(row["qual"] or "").strip().lower() in {"true", "(true)"}
                or str(row["with_check"] or "").strip().lower() in {"true", "(true)"}
            ]
            if open_policies:
                raise RuntimeError(
                    "identity RLS must not contain open USING(true)/WITH CHECK(true) "
                    f"policies: {sorted(open_policies)}"
                )
            present_roles = {
                str(row["rolname"])
                for row in await connection.fetch(
                    """
                    SELECT rolname FROM pg_roles
                    WHERE rolname = ANY($1::text[])
                    """
                    , list(_REQUIRED_ROLES),
                )
            }
            missing_roles = _REQUIRED_ROLES - present_roles
            if missing_roles:
                raise RuntimeError(
                    "identity production roles missing "
                    f"(LOGIN NOSUPERUSER NOBYPASSRLS): {sorted(missing_roles)}"
                )
            # The API role must never hold direct write privileges on the
            # authoritative tables: registration, audit, outbox, idempotency
            # records AND person updates go through SECURITY DEFINER ports
            # only (profile / age declaration / verification).  A GUC
            # (app.identity_scope / app.identity_actor) is not a credential
            # and must never unlock raw DML.
            for table in _API_NO_DIRECT_WRITE_TABLES:
                for privilege in ("INSERT", "UPDATE"):
                    granted = await connection.fetchval(
                        """
                        SELECT has_table_privilege(
                            current_user, $1::regclass, $2
                        )
                        """,
                        table,
                        privilege,
                    )
                    if granted:
                        raise RuntimeError(
                            f"identity API role must not hold {privilege} on "
                            f"{table} (registration/audit/outbox/idempotency "
                            "writes go through SECURITY DEFINER ports only)"
                        )
            # Create-once tables: the API role must never hold UPDATE; the
            # immutability trigger is the primary guard, this is the grant
            # double lock.
            for table in _API_NO_UPDATE_TABLES:
                granted = await connection.fetchval(
                    """
                    SELECT has_table_privilege(
                        current_user, $1::regclass, 'UPDATE'
                    )
                    """,
                    table,
                )
                if granted:
                    raise RuntimeError(
                        f"identity API role must not hold UPDATE on {table} "
                        "(create-once entity guarded by an immutability trigger)"
                    )
            # Generic audit/outbox/update ports must not exist for the API
            # role: business mutation events are synthesized by the triggers
            # with fixed actions, and person updates are RLS/action-level.
            for signature in (
                "identity_write_audit(text,text,text,text,text,text,text,"
                "text,jsonb,timestamptz)",
                "identity_enqueue_outbox(text,text,text,jsonb,timestamptz)",
                "identity_update_person(text,text,text,text,text,text,text,"
                "text,timestamptz,text)",
                "identity_update_age_evidence(text,text,text,text,"
                "timestamptz,text)",
            ):
                exists_or_granted = await connection.fetchval(
                    """
                    SELECT to_regprocedure($1) IS NOT NULL
                           OR has_function_privilege(
                               current_user, to_regprocedure($1), 'EXECUTE'
                           )
                    """,
                    signature,
                )
                if exists_or_granted:
                    raise RuntimeError(
                        f"generic port {signature.split('(')[0]} must not "
                        "exist for the identity API role"
                    )
            for port, signature in (
                (
                    "identity_self_update_profile",
                    "identity_self_update_profile(text,text,text,text,"
                    "timestamptz,text)",
                ),
                (
                    "identity_declare_age_evidence",
                    "identity_declare_age_evidence(text,text,timestamptz,text)",
                ),
            ):
                can_exec = await connection.fetchval(
                    """
                    SELECT has_function_privilege(
                        current_user, to_regprocedure($1), 'EXECUTE'
                    )
                    """,
                    signature,
                )
                if not can_exec:
                    raise RuntimeError(
                        f"identity API role must be able to EXECUTE {port}"
                    )
            verify_denied = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure(
                        'identity_verify_age_evidence(text,text,text,text,'
                        'text,timestamptz)'
                    ),
                    'EXECUTE'
                )
                """
            )
            if verify_denied:
                raise RuntimeError(
                    "identity API role must NOT EXECUTE "
                    "identity_verify_age_evidence"
                )
            reconciliation_denied = await connection.fetchval(
                """
                SELECT has_function_privilege(current_user, to_regprocedure(
                    'identity_reconcile_account_registration(text,text,integer,'
                    'timestamptz,timestamptz)'
                ), 'EXECUTE')
                """
            )
            if reconciliation_denied:
                raise RuntimeError(
                    "identity API role must NOT EXECUTE identity_reconcile_account_registration"
                )
            audit_triggers = {
                str(row["tgname"])
                for row in await connection.fetch(
                    """
                    SELECT tgname FROM pg_trigger
                    WHERE tgname IN (
                        'identity_relationships_audit_trigger',
                        'identity_bindings_audit_trigger',
                        'identity_transfers_audit_trigger',
                        'identity_persons_update_audit_trigger'
                    )
                    """
                )
            }
            missing_triggers = {
                "identity_relationships_audit_trigger",
                "identity_bindings_audit_trigger",
                "identity_transfers_audit_trigger",
                "identity_persons_update_audit_trigger",
            } - audit_triggers
            if missing_triggers:
                raise RuntimeError(
                    "identity mutation audit triggers missing: "
                    f"{sorted(missing_triggers)}"
                )
            role_row = await connection.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            if role_row is None:
                raise RuntimeError("cannot verify the connecting PostgreSQL role")
            if bool(role_row["rolsuper"]):
                raise RuntimeError(
                    "PostgresIdentityStore must connect as a non-superuser role"
                )
            if bool(role_row["rolbypassrls"]):
                raise RuntimeError(
                    "PostgresIdentityStore role must not bypass RLS"
                )
            current_role = str(await connection.fetchval("SELECT current_user"))
            if expected_role is not None and current_role != expected_role:
                raise RuntimeError(
                    f"PostgresIdentityStore expected role {expected_role!r}, "
                    f"connected as {current_role!r}"
                )
            role_attrs = await connection.fetchval(
                """
                SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
                FROM pg_roles WHERE rolname = current_user
                """
            )
            if not role_attrs:
                raise RuntimeError(
                    "identity store role must be LOGIN NOSUPERUSER NOBYPASSRLS"
                )
            version_port = await connection.fetchval(
                """
                SELECT pg_get_userbyid(p.proowner) = 'memoria_identity_owner'
                       AND p.prosecdef
                       AND 'row_security=on' = ANY(p.proconfig)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.oid = to_regprocedure(
                      'identity_next_binding_version(text)'
                  )
                """
            )
            if not version_port:
                raise RuntimeError(
                    "identity_next_binding_version must be SECURITY DEFINER "
                    "owned by memoria_identity_owner with row_security=on"
                )
            version_exec = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure('identity_next_binding_version(text)'),
                    'EXECUTE'
                )
                """
            )
            if not version_exec:
                raise RuntimeError(
                    "identity API role must be able to EXECUTE "
                    "identity_next_binding_version"
                )
        if self._registration_dsn is None:
            raise RuntimeError(
                "PostgresIdentityStore requires an explicit registration "
                "authority (registration_dsn / "
                "MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL): person "
                "registration must run as the dedicated "
                f"{_REGISTRATION_ROLE!r} role; refusing to run without it"
            )
        self._registration_pool = await asyncpg.create_pool(
            self._registration_dsn, min_size=1, max_size=2
        )
        assert self._registration_pool is not None
        async with self._registration_pool.acquire() as connection:
            await connection.execute("SET application_name = 'memoria-identity-reg'")
            current_role = str(await connection.fetchval("SELECT current_user"))
            if current_role != _REGISTRATION_ROLE:
                raise RuntimeError(
                    "identity registration pool must connect as "
                    f"{_REGISTRATION_ROLE!r}, connected as {current_role!r}"
                )
            attrs = await connection.fetchval(
                """
                SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
                FROM pg_roles WHERE rolname = current_user
                """
            )
            if not attrs:
                raise RuntimeError(
                    "identity registration role must be LOGIN NOSUPERUSER "
                    "NOBYPASSRLS"
                )
            table_privs = await connection.fetch(
                """
                SELECT c.relname,
                       has_table_privilege(current_user, c.oid, 'SELECT')
                           AS can_select,
                       has_table_privilege(current_user, c.oid, 'INSERT')
                           AS can_insert,
                       has_table_privilege(current_user, c.oid, 'UPDATE')
                           AS can_update
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname LIKE 'identity\\_%'
                  AND c.relkind = 'r'
                """
            )
            for row in table_privs:
                if any(bool(row[key]) for key in ("can_select", "can_insert", "can_update")):
                    raise RuntimeError(
                        "identity registration role must hold NO table "
                        f"privileges on identity_* (got {row['relname']})"
                    )
            can_register = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure(
                        'identity_register_person(text,text,text,text,text,'
                        'text,text,text,timestamptz,timestamptz,text,jsonb,'
                        'text)'
                    ),
                    'EXECUTE'
                )
                """
            )
            if not can_register:
                raise RuntimeError(
                    "identity registration role must be able to EXECUTE "
                    "identity_register_person"
                )
            can_verify = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure(
                        'identity_verify_age_evidence(text,text,text,text,'
                        'text,timestamptz)'
                    ),
                    'EXECUTE'
                )
                """
            )
            if not can_verify:
                raise RuntimeError(
                    "identity registration role must be able to EXECUTE "
                    "identity_verify_age_evidence"
                )
            reconciliation_port = await connection.fetchval(
                """
                SELECT pg_get_userbyid(p.proowner) = 'memoria_identity_owner'
                       AND p.prosecdef
                       AND 'row_security=on' = ANY(p.proconfig)
                       AND 'search_path=pg_catalog, public' = ANY(p.proconfig)
                       AND has_function_privilege(current_user, p.oid, 'EXECUTE')
                FROM pg_proc p WHERE p.oid = to_regprocedure(
                    'identity_reconcile_account_registration(text,text,integer,'
                    'timestamptz,timestamptz)'
                )
                """
            )
            if not reconciliation_port:
                raise RuntimeError(
                    "identity registration reconciliation port missing or insecure"
                )
            denied_ports = {
                "identity_write_idempotency": (
                    "identity_write_idempotency(text,text,text,text,jsonb,"
                    "timestamptz)"
                ),
                "identity_patch_idempotency_result": (
                    "identity_patch_idempotency_result(text,text,jsonb)"
                ),
                "identity_self_update_profile": (
                    "identity_self_update_profile(text,text,text,text,"
                    "timestamptz,text)"
                ),
                "identity_declare_age_evidence": (
                    "identity_declare_age_evidence(text,text,timestamptz,text)"
                ),
            }
            for port, signature in denied_ports.items():
                can_exec = await connection.fetchval(
                    """
                    SELECT has_function_privilege(
                        current_user, to_regprocedure($1), 'EXECUTE'
                    )
                    """,
                    signature,
                )
                if can_exec:
                    raise RuntimeError(
                        "identity registration role must NOT be able to "
                        f"EXECUTE {port}"
                    )
            port_owner = await connection.fetchval(
                """
                SELECT pg_get_userbyid(p.proowner) = 'memoria_identity_owner'
                       AND p.prosecdef
                       AND 'row_security=on' = ANY(p.proconfig)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.proname = 'identity_register_person'
                """
            )
            if not port_owner:
                raise RuntimeError(
                    "identity_register_person must be SECURITY DEFINER owned "
                    "by memoria_identity_owner with row_security=on"
                )
