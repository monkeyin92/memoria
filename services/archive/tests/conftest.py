"""PostgreSQL archive stores on a clone of the production-shaped template.

Archive-family data is PostgreSQL-only. The stores connect as ``memoria_app``,
the archive DSN role; the memory catalog claims compile tasks as
``memoria_compiler`` exactly as production does, because the outbox is under
forced account RLS. Everything here skips without MEMORIA_TEST_POSTGRES_DSN.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
import pytest_asyncio
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.postgres_skill_catalog import PostgresSkillCatalog

if TYPE_CHECKING:
    from testing.postgres_harness import TestDatabase

APP_ROLE = "memoria_app"
COMPILER_ROLE = "memoria_compiler"


@pytest.fixture
def archive_dsn(postgres_database: TestDatabase) -> str:
    return postgres_database.role_dsn(APP_ROLE)


@pytest.fixture
def compiler_dsn(postgres_database: TestDatabase) -> str:
    return postgres_database.role_dsn(COMPILER_ROLE)


@pytest_asyncio.fixture
async def archive(archive_dsn: str) -> AsyncIterator[PostgresLifeArchive]:
    store = PostgresLifeArchive(archive_dsn)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def make_catalog(
    archive_dsn: str, compiler_dsn: str
) -> AsyncIterator[Callable[..., Awaitable[PostgresMemoryCatalog]]]:
    catalogs: list[PostgresMemoryCatalog] = []

    async def make(**kwargs: Any) -> PostgresMemoryCatalog:
        kwargs.setdefault("compiler_dsn", compiler_dsn)
        kwargs.setdefault("compiler_role", COMPILER_ROLE)
        catalog = PostgresMemoryCatalog(archive_dsn, **kwargs)
        await catalog.initialize()
        catalogs.append(catalog)
        return catalog

    try:
        yield make
    finally:
        for catalog in catalogs:
            await catalog.close()


@pytest_asyncio.fixture
async def skill_catalog(archive_dsn: str) -> AsyncIterator[PostgresSkillCatalog]:
    catalog = PostgresSkillCatalog(archive_dsn)
    await catalog.initialize()
    try:
        yield catalog
    finally:
        await catalog.close()


@pytest.fixture
def owner_sql(postgres_database: TestDatabase) -> Callable[..., list[tuple[Any, ...]]]:
    """Run one statement on the clone as the cluster admin (RLS bypassed).

    For inspecting or seeding rows no store API exposes; returns fetched rows.
    """

    def run(statement: str, *parameters: object) -> list[tuple[Any, ...]]:
        with psycopg.connect(postgres_database.owner_dsn(), autocommit=True) as connection:
            cursor = connection.execute(statement, parameters or None)
            return cursor.fetchall() if cursor.description is not None else []

    return run
