"""Opt-in backend parity for the Control API store.

``MEMORIA_TEST_CONTROL_STORE=postgres`` (with ``MEMORIA_TEST_POSTGRES_DSN``)
runs every test's ``MemoryStore(path)`` on PostgreSQL instead of SQLite. Each
path maps to its own schema, so a test that reopens the same path (a restart)
sees the same data, exactly as with the SQLite file. The default run is
unchanged.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "sqlite_only: the test reads a SQLite file directly; skipped in control-store parity runs",
    )
    config.addinivalue_line(
        "markers",
        "real_sqlite_store: MemoryStore(path) stays on SQLite even in parity runs",
    )


_CONTROL_BACKEND = os.environ.get("MEMORIA_TEST_CONTROL_STORE", "").strip().lower()

if _CONTROL_BACKEND == "postgres":
    import psycopg
    from services.control_api.app.database import MemoryStore

    _BASE_DSN = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if not _BASE_DSN:
        raise RuntimeError("MEMORIA_TEST_CONTROL_STORE=postgres needs MEMORIA_TEST_POSTGRES_DSN")
    _SCHEMAS: set[str] = set()
    _PARITY_ACTIVE = [True]
    _OPEN_STORES: list[Any] = []
    _original_init = MemoryStore.__init__

    def _schema_dsn(path: str) -> str:
        key = hashlib.sha256(str(Path(path or ".").expanduser().resolve()).encode()).hexdigest()
        schema = f"ctl_parity_{key[:24]}"
        if schema not in _SCHEMAS:
            with psycopg.connect(_BASE_DSN, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                connection.execute(f'CREATE SCHEMA "{schema}"')
            _SCHEMAS.add(schema)
        separator = "&" if "?" in _BASE_DSN else "?"
        return f"{_BASE_DSN}{separator}options=-csearch_path%3D{schema}"

    def _parity_init(
        self: Any,
        path: str = "",
        *,
        dsn: str = "",
        initialize_schema: bool = True,
    ) -> None:
        if dsn or not _PARITY_ACTIVE[0]:
            # An explicit DSN (a PostgreSQL contract test) or an opted-out test.
            _original_init(self, path, dsn=dsn, initialize_schema=initialize_schema)
        else:
            # Production-mode tests skip schema setup (the admin script owns it
            # there); each parity schema starts empty, so always apply it here.
            _original_init(self, path, dsn=_schema_dsn(path), initialize_schema=True)
        _OPEN_STORES.append(self)

    MemoryStore.__init__ = _parity_init  # type: ignore[method-assign]

    def pytest_collection_modifyitems(
        config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        skip = pytest.mark.skip(reason="SQLite-file fixture; not a control store behavior")
        for item in items:
            if item.get_closest_marker("sqlite_only") is not None:
                item.add_marker(skip)

    @pytest.fixture(autouse=True)
    def _close_parity_pools(request: pytest.FixtureRequest) -> Iterator[None]:
        # real_sqlite_store: the test needs a genuine SQLite store (for example
        # as a migration source) next to its explicit PostgreSQL one.
        _PARITY_ACTIVE[0] = request.node.get_closest_marker("real_sqlite_store") is None
        yield
        _PARITY_ACTIVE[0] = True
        while _OPEN_STORES:
            _OPEN_STORES.pop().close()

    def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
        with psycopg.connect(_BASE_DSN, autocommit=True) as connection:
            for schema in sorted(_SCHEMAS):
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
