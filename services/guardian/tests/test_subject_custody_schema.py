import os
import uuid
from pathlib import Path

import asyncpg
import pytest

_SCHEMA = Path(__file__).parents[1].joinpath("postgres_schema.sql")
_DSN = os.getenv(
    "MEMORIA_TEST_POSTGRES_DSN",
    "postgresql://memoria_test:memoria_test_local@127.0.0.1:55439/postgres",
)

_INSERT = """
INSERT INTO guardian_person_consents(consent_id, subject_person_id, grantor_person_id,
  consent_kind, policy_version, granted_at, expires_at, evidence_event_id)
VALUES ($1, 's', 'g', $2, 'v1', now(), $3, $4)
"""


@pytest.fixture
async def conn():
    try:
        admin = await asyncpg.connect(_DSN)
    except Exception as exc:  # pragma: no cover - environment
        pytest.skip(f"postgres unavailable: {exc}")
    name = f"gc_{uuid.uuid4().hex[:10]}"
    await admin.execute(f"CREATE DATABASE {name}")
    dsn = _DSN.rsplit("/", 1)[0] + "/" + name
    c = await asyncpg.connect(dsn)
    try:
        yield c
    finally:
        await c.close()
        await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        await admin.close()


async def _insert(c, kind, expires):
    await c.execute(_INSERT, uuid.uuid4(), kind, expires, uuid.uuid4().hex)


async def test_custody_needs_expiry_and_schema_is_reapplicable(conn) -> None:
    sql = _SCHEMA.read_text(encoding="utf-8")
    await conn.execute(sql)
    await conn.execute(sql)  # idempotent
    future = await conn.fetchval("SELECT now() + interval '90 days'")
    await _insert(conn, "subject_custody", future)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, "subject_custody", None)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, "memory_retention", future)
    await _insert(conn, "memory_retention", None)


async def test_upgrade_from_previous_schema(conn) -> None:
    import subprocess

    old = subprocess.run(
        ["git", "show", "c0cd19f6:services/guardian/postgres_schema.sql"],
        capture_output=True, text=True, check=True,
    ).stdout
    await conn.execute(old)
    await conn.execute(_SCHEMA.read_text(encoding="utf-8"))
    future = await conn.fetchval("SELECT now() + interval '90 days'")
    await _insert(conn, "subject_custody", future)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, "subject_custody", None)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, "child_archive", future)
