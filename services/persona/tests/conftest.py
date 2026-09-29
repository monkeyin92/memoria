"""PostgreSQL persona stores on a clone of the production-shaped template.

Persona data is PostgreSQL-only: these fixtures connect as ``memoria_app``, the
archive DSN role, and skip without MEMORIA_TEST_POSTGRES_DSN.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

import pytest_asyncio
from services.archive.postgres_archive import PostgresLifeArchive
from services.persona.postgres_engine import PostgresPersonaEngine
from services.persona.rules import PersonaExtractor

if TYPE_CHECKING:
    from testing.postgres_harness import TestDatabase


@pytest_asyncio.fixture
async def archive(postgres_database: TestDatabase) -> AsyncIterator[PostgresLifeArchive]:
    store = PostgresLifeArchive(postgres_database.role_dsn("memoria_app"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def make_engine(
    postgres_database: TestDatabase,
) -> AsyncIterator[Callable[..., Awaitable[PostgresPersonaEngine]]]:
    engines: list[PostgresPersonaEngine] = []

    async def make(extractor: PersonaExtractor | None = None) -> PostgresPersonaEngine:
        engine = PostgresPersonaEngine(
            postgres_database.role_dsn("memoria_app"), extractor=extractor
        )
        await engine.initialize()
        engines.append(engine)
        return engine

    try:
        yield make
    finally:
        for engine in engines:
            await engine.close()
