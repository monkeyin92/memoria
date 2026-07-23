from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.legacy.domain import LegacyManifestItemRef, RegisteredGranteeSnapshot
from services.legacy.postgres_registry import PostgresLegacyRegistry
from services.legacy.tests.test_legacy_registry import NOW, _snapshots


def test_postgres_schema_forces_rls_uses_nobypassrls_and_guards_immutable_rows() -> None:
    schema = Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(encoding="utf-8")
    for table in (
        "legacy_grants",
        "legacy_relationship_shells",
        "legacy_shell_turns",
        "legacy_audit_events",
        "legacy_command_receipts",
    ):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in schema
    assert "legacy_grant_immutable_guard" in schema
    assert "legacy_shell_turn_immutable_guard" in schema
    assert "legacy_audit_event_immutable_guard" in schema
    assert "current_setting('app.actor_account_id', true)" in schema
    assert "legacy_receipts_related_delete" in schema
    assert "NOBYPASSRLS" in schema
    assert "target_kind TEXT" in schema
    assert "'read_source'," in schema
    assert "'privacy_refusal', 'unknown_refusal'" in schema


def test_postgres_registry_rejects_non_postgres_dsn() -> None:
    try:
        PostgresLegacyRegistry("sqlite:///tmp/legacy.sqlite3")
    except ValueError as exc:
        assert "PostgreSQL" in str(exc)
    else:
        raise AssertionError("non-PostgreSQL DSN was accepted")


def _postgres_dsn(dsn: str, *, user: str, password: str, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{quote(user)}:{quote(password)}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL Legacy contract",
)
async def test_postgres_nobypassrls_migration_idempotency_and_triggers() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    database = f"memoria_legacy_{suffix}"
    app_role = f"memoria_legacy_{suffix}"
    password = f"legacy-{suffix}-password"
    admin = await asyncpg.connect(admin_dsn)
    registry: PostgresLegacyRegistry | None = None
    repeated: PostgresLegacyRegistry | None = None
    try:
        await admin.execute(
            f'CREATE ROLE "{app_role}" LOGIN PASSWORD \'{password}\' NOSUPERUSER NOBYPASSRLS'
        )
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{app_role}"')
        app_dsn = _postgres_dsn(
            admin_dsn,
            user=app_role,
            password=password,
            database=database,
        )
        registry = PostgresLegacyRegistry(app_dsn)
        await registry.initialize()
        await registry.close()
        registry = None
        repeated = PostgresLegacyRegistry(app_dsn)
        await repeated.initialize()

        version, relationship = _snapshots()
        grant = await repeated.issue(
            owner_account_id="owner-a",
            grantee=RegisteredGranteeSnapshot("grantee-a", NOW),
            version=version,
            relationship_profile=relationship,
            allowed_items=(LegacyManifestItemRef("memory_claim", "memory-1"),),
            visibility="family",
            voice_allowed=False,
            expires_at=NOW + timedelta(days=30),
            idempotency_key="issue-1",
            now=NOW,
        )
        duplicate = await repeated.issue(
            owner_account_id="owner-a",
            grantee=RegisteredGranteeSnapshot("grantee-a", NOW),
            version=version,
            relationship_profile=relationship,
            allowed_items=(LegacyManifestItemRef("memory_claim", "memory-1"),),
            visibility="family",
            voice_allowed=False,
            expires_at=NOW + timedelta(days=30),
            idempotency_key="issue-1",
            now=NOW,
        )
        assert duplicate == grant

        connection = await asyncpg.connect(app_dsn)
        try:
            assert await connection.fetchval(
                "SELECT NOT rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.actor_account_id', 'owner-a', true)"
                )
                with pytest.raises(asyncpg.PostgresError):
                    await connection.execute(
                        "UPDATE legacy_grants SET version_number=8 WHERE grant_id=$1",
                        uuid.UUID(grant.grant_id),
                    )
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.actor_account_id', 'owner-a', true)"
                )
                with pytest.raises(asyncpg.PostgresError):
                    await connection.execute(
                        "UPDATE legacy_audit_events SET reason='revoked' WHERE grant_id=$1",
                        uuid.UUID(grant.grant_id),
                    )
        finally:
            await connection.close()
    finally:
        if registry is not None:
            await registry.close()
        if repeated is not None:
            await repeated.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{app_role}"')
        await admin.close()
