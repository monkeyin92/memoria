"""Shared fixtures, helpers and schema constants for the split identity Postgres tests."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.identity.authority import (
    DeterministicConsentSnapshotResolver,
)
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.service import IdentityService
from services.identity.testing_authorities import TestTransferAuthority

_DSN = os.getenv("MEMORIA_TEST_POSTGRES_DSN", "")


_ROLE_PASSWORD = "memoria_identity_test_password"


_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "postgres_schema.sql"


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _DSN,
        reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL identity contract",
    ),
]


def _with_credentials(
    dsn: str, *, username: str, password: str, database: str | None = None
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = f"{quote(username)}:{quote(password)}"
    path = f"/{database}" if database else parsed.path
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", path, parsed.query, "")
    )


async def _ensure_role(admin: asyncpg.Connection, name: str) -> None:
    exists = await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    if not exists:
        await admin.execute(
            f"CREATE ROLE {name} LOGIN NOSUPERUSER NOBYPASSRLS "
            f"PASSWORD '{_ROLE_PASSWORD}'"
        )
    else:
        await admin.execute(
            f"ALTER ROLE {name} LOGIN NOSUPERUSER NOBYPASSRLS "
            f"PASSWORD '{_ROLE_PASSWORD}'"
        )


async def _bootstrap(database: str) -> dict[str, str]:
    """Create the database, roles and schema; return role-scoped DSNs."""
    admin = await asyncpg.connect(_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        for role in (
            "memoria_identity",
            "memoria_identity_registration",
            "memoria_identity_outbox",
            "memoria_identity_migration",
        ):
            await _ensure_role(admin, role)
    finally:
        await admin.close()
    admin_user = urlsplit(_DSN).username or "postgres"
    admin_password = urlsplit(_DSN).password or ""
    admin_dsn = _with_credentials(
        _DSN, username=admin_user, password=admin_password, database=database
    )
    schema_admin = await asyncpg.connect(admin_dsn)
    try:
        await schema_admin.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        await schema_admin.close()
    return {
        "admin": admin_dsn,
        "api": _with_credentials(
            _DSN, username="memoria_identity", password=_ROLE_PASSWORD, database=database
        ),
        "registration": _with_credentials(
            _DSN,
            username="memoria_identity_registration",
            password=_ROLE_PASSWORD,
            database=database,
        ),
        "outbox": _with_credentials(
            _DSN,
            username="memoria_identity_outbox",
            password=_ROLE_PASSWORD,
            database=database,
        ),
        "migration": _with_credentials(
            _DSN,
            username="memoria_identity_migration",
            password=_ROLE_PASSWORD,
            database=database,
        ),
    }


async def _service(
    dsns: dict[str, str],
) -> tuple[PostgresIdentityStore, IdentityService, TestTransferAuthority]:
    store = PostgresIdentityStore(
        dsns["api"], registration_dsn=dsns["registration"]
    )
    await store.initialize(expected_role="memoria_identity")
    authority = TestTransferAuthority(b"pg-test-secret-that-is-at-least-32-bytes")
    service = IdentityService(
        store,
        transfer_verifier=authority,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )
    return store, service, authority


async def _guardian(service: IdentityService, *, guardian: str, ward: str, now: datetime) -> str:
    proposed = await service.propose_relationship(
        source_person_id=guardian,
        target_person_id=ward,
        relation_type="guardian_of",
        established_evidence_id="evidence-guardian",
        actor_person_id=guardian,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id, person_id=guardian, now=now
    )
    return (
        await service.confirm_relationship(
            relationship_id=proposed.relationship_id, person_id=ward, now=now
        )
    ).relationship_id


async def _drop_database(database: str) -> None:
    admin = await asyncpg.connect(_DSN)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()
