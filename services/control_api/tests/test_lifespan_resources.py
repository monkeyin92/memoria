"""P2-08: the archive-DSN stores borrow one pool that only the lifespan closes."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from services.control_api.app import lifespan_resources
from services.control_api.app.lifespan_resources import (
    SHARED_POOL_MAX_SIZE,
    BorrowedPool,
    LifespanResources,
    SharedPostgresPools,
)


class _FakePool:
    def __init__(self, dsn: str, events: list[str]) -> None:
        self.dsn = dsn
        self._events = events

    async def close(self) -> None:
        self._events.append(f"pool-closed:{self.dsn}")


@pytest.mark.asyncio
async def test_one_pool_per_dsn_closed_once_after_its_borrowers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    created: list[dict[str, Any]] = []

    async def create_pool(dsn: str, **kwargs: Any) -> _FakePool:
        created.append({"dsn": dsn, **kwargs})
        return _FakePool(dsn, events)

    monkeypatch.setattr(lifespan_resources.asyncpg, "create_pool", create_pool)
    resources = LifespanResources()
    pools = SharedPostgresPools(resources)

    first = await pools.borrow("postgresql://archive")
    second = await pools.borrow("postgresql://archive")
    other = await pools.borrow("postgresql://other")
    # Stores register their closers after borrowing; a borrower's close is inert.
    resources.add(first.close)
    resources.add(lambda: events.append("store-closed"))

    assert [call["dsn"] for call in created] == ["postgresql://archive", "postgresql://other"]
    assert created[0]["max_size"] == SHARED_POOL_MAX_SIZE
    assert first.dsn == second.dsn == "postgresql://archive"
    assert other.dsn == "postgresql://other"

    await resources.aclose()

    assert events == [
        "store-closed",
        "pool-closed:postgresql://other",
        "pool-closed:postgresql://archive",
    ]


@pytest.mark.asyncio
async def test_resources_run_every_closer_and_reraise_the_first_failure() -> None:
    ran: list[str] = []
    resources = LifespanResources()

    def fail(name: str) -> None:
        ran.append(name)
        raise RuntimeError(name)

    resources.add(lambda: ran.append("oldest"))
    resources.add(lambda: fail("middle"))
    resources.add(lambda: fail("newest"))

    with pytest.raises(RuntimeError, match="newest"):
        await resources.aclose()
    assert ran == ["newest", "middle", "oldest"]


def _database_dsn(admin_dsn: str, database: str, *, user: str, password: str) -> str:
    parts = urlsplit(admin_dsn)
    netloc = f"{user}:{password}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit(parts._replace(netloc=netloc, path=f"/{database}"))


def _admin(admin_dsn: str) -> dict[str, str]:
    parts = urlsplit(admin_dsn)
    return {"user": parts.username or "postgres", "password": parts.password or ""}


_ARCHIVE_STORES = (
    "life_archive",
    "memory_catalog",
    "skill_catalog",
    "persona_engine",
    "digital_self_registry",
    "self_model_registry",
    "legacy_registry",
    "growth_reader",
    "voice_profile_manager",
    "speaker_authority",
)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the shared archive pool contract",
)
async def test_live_wiring_shares_one_archive_pool_without_leaking_session_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    database, role, password = f"memoria_shared_pool_{suffix}", f"memoria_pool_{suffix}", suffix
    admin = await asyncpg.connect(admin_dsn)
    try:
        # Like production: the stores' role owns the schema but cannot bypass RLS.
        await admin.execute(
            f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{role}"')
    finally:
        await admin.close()
    admin = await asyncpg.connect(_database_dsn(admin_dsn, database, **_admin(admin_dsn)))
    try:
        await admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await admin.close()
    archive_dsn = _database_dsn(admin_dsn, database, user=role, password=password)
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("MEMORIA_ARCHIVE_DATABASE_URL", archive_dsn)

    from services.control_api.app.main import create_app

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            borrowed = [getattr(app.state, name)._pool for name in _ARCHIVE_STORES]
            assert all(isinstance(pool, BorrowedPool) for pool in borrowed)
            shared = borrowed[0]._pool
            assert all(pool._pool is shared for pool in borrowed)
            assert shared.get_max_size() == SHARED_POOL_MAX_SIZE

            # Store transactions scope app.account_id locally; the next borrower
            # of the same connection must not see it.
            async with shared.acquire() as connection, connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', 'a-1', true)")
            async with shared.acquire() as connection:
                leaked = await connection.fetchval(
                    "SELECT current_setting('app.account_id', true)"
                )
            assert leaked in (None, "")

            # A borrower closing its pool leaves the shared pool open.
            await app.state.skill_catalog.close()
            async with shared.acquire() as connection:
                assert await connection.fetchval("SELECT 1") == 1
        assert shared._closed
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        finally:
            await admin.close()
