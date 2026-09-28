"""Read what a Control API test app persisted, whichever backend it used.

Under ``MEMORIA_TEST_APP_POSTGRES=1`` the app writes to the test's cloned
PostgreSQL database; otherwise to the SQLite file the test configured. Reads
here run as the harness admin (RLS does not apply) because they are test
assertions about stored rows, not application reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_CURRENT: list[Any] = []


def set_current(database: Any | None) -> None:
    """Called by the root conftest around each app-PostgreSQL test."""

    _CURRENT.clear()
    if database is not None:
        _CURRENT.append(database)


def postgres_active() -> bool:
    return bool(_CURRENT)


def fetch_all(sqlite_path: str | Path, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    """Rows for ``sql`` (``?`` placeholders) from the backend the app used."""

    if _CURRENT:
        import psycopg
        from services.control_api.app.database.backend import translate_sql
        from testing.postgres_harness import _with_database

        database = _CURRENT[0]
        with psycopg.connect(_with_database(database.admin_dsn, database.name)) as connection:
            return [tuple(row) for row in connection.execute(translate_sql(sql), tuple(params)).fetchall()]
    with sqlite3.connect(sqlite_path) as connection:
        return [tuple(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def execute(sqlite_path: str | Path, sql: str, params: Sequence[Any] = ()) -> None:
    """Write fixture rows (``?`` placeholders) where the app will read them."""

    if _CURRENT:
        import psycopg
        from services.control_api.app.database.backend import translate_sql
        from testing.postgres_harness import _with_database

        database = _CURRENT[0]
        with psycopg.connect(_with_database(database.admin_dsn, database.name)) as connection:
            connection.execute(translate_sql(sql), tuple(params))
        return
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(sql, tuple(params))


async def initialized(component: Any) -> None:
    """Run ``component.initialize()`` whether the backend's is sync or async."""

    import inspect

    result = component.initialize()
    if inspect.isawaitable(result):
        await result
