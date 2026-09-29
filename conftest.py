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
import sys
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
    config.addinivalue_line(
        "markers",
        "guardian_postgres: needs the guardian store, which is PostgreSQL-only; "
        "runs under MEMORIA_TEST_APP_POSTGRES=1",
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


# MEMORIA_TEST_APP_POSTGRES=1 runs Control API and governance tests against a
# production-shaped PostgreSQL: every test gets a clone of a template built by
# the real init script, every DSN connects as its production role, and eager
# wiring builds the PostgreSQL stores (MEMORIA_EAGER_POSTGRES).
_APP_POSTGRES = os.environ.get("MEMORIA_TEST_APP_POSTGRES", "").strip() == "1"
_APP_POSTGRES_ROOTS = (
    "services/control_api/tests",
    "services/governance/tests",
    "services/companionship/tests",
)

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_sqlite = pytest.mark.skip(reason="SQLite-file fixture; not a control store behavior")
    skip_guardian = pytest.mark.skip(
        reason="guardian is PostgreSQL-only; run with MEMORIA_TEST_APP_POSTGRES=1"
    )
    for item in items:
        if _CONTROL_BACKEND == "postgres" and item.get_closest_marker("sqlite_only") is not None:
            item.add_marker(skip_sqlite)
        if not _APP_POSTGRES and item.get_closest_marker("guardian_postgres") is not None:
            item.add_marker(skip_guardian)


# One production-shaped template per run, shared by the app harness and the
# guardian store fixture; built on first use.
_TEMPLATE: list[Any] = []


def _app_template() -> Any:
    from testing.postgres_harness import build_template

    if not _TEMPLATE:
        admin = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
        if not admin:
            raise RuntimeError("the PostgreSQL test harness needs MEMORIA_TEST_POSTGRES_DSN")
        _TEMPLATE.append(build_template(admin))
    return _TEMPLATE[0]


def pytest_unconfigure(config: pytest.Config) -> None:
    if _TEMPLATE:
        from testing.postgres_harness import drop_template

        drop_template(_TEMPLATE.pop())


@pytest.fixture
def guardian_postgres_database() -> Iterator[Any]:
    """A fresh clone of the production-shaped template (``TestDatabase``).

    Guardian data is PostgreSQL-only; without MEMORIA_TEST_POSTGRES_DSN the test skips.
    """

    if not os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip():
        pytest.skip("guardian store tests need MEMORIA_TEST_POSTGRES_DSN")
    from testing.postgres_harness import cloned_database

    with cloned_database(_app_template()) as database:
        yield database


@pytest.fixture
async def guardian_postgres_store(guardian_postgres_database: Any) -> Any:
    """A PostgresGuardianStore on ``guardian_postgres_database``, as the production roles."""

    from services.guardian.postgres_store import PostgresGuardianStore

    env = guardian_postgres_database.control_env()
    store = PostgresGuardianStore(
        env["MEMORIA_GUARDIAN_DATABASE_URL"],
        maintenance_dsn=env["MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL"],
        worker_dsn=env["MEMORIA_GUARDIAN_WORKER_DATABASE_URL"],
        initialize_schema=False,
    )
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


if _APP_POSTGRES:
    import pytest_asyncio
    from testing.postgres_harness import cloned_database

    @pytest_asyncio.fixture(autouse=True)
    async def _app_postgres(
        request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ) -> Any:
        path = str(request.node.path)
        if not any(root in path for root in _APP_POSTGRES_ROOTS) or request.node.get_closest_marker(
            "sqlite_only"
        ):
            yield
            return
        from services.control_api.app import main as control_main

        apps: list[Any] = []
        original_create_app = control_main.create_app

        from testing import app_store

        with cloned_database(_app_template()) as database:
            app_store.set_current(database)
            # MEMORIA_TEST_APP_POSTGRES_SKIP lists DSN settings to leave unset.
            # MEMORIA_CONSENT_DATABASE_URL is not only a backend: without it the
            # bound-subject consent feature is off, which is how most tests were
            # written, so switching it is a separate step.
            skip = {
                name.strip()
                for name in os.environ.get(
                    "MEMORIA_TEST_APP_POSTGRES_SKIP", "MEMORIA_CONSENT_DATABASE_URL"
                ).split(",")
                if name.strip()
            }
            injected = {
                **{key: value for key, value in database.control_env().items() if key not in skip},
                "MEMORIA_EAGER_POSTGRES": "true",
            }

            def recording_create_app(*args: Any, **kwargs: Any) -> Any:
                # Only the app sees the DSNs: settings a test builds itself keep
                # testing what is and is not configured. Production-mode apps
                # are left alone.
                if os.environ.get("ENVIRONMENT", "").strip() == "production":
                    return original_create_app(*args, **kwargs)
                saved = {key: os.environ.get(key) for key in injected}
                os.environ.update({key: value for key, value in injected.items() if key not in os.environ})
                try:
                    app = original_create_app(*args, **kwargs)
                finally:
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
                apps.append(app)
                return app

            monkeypatch.setattr(control_main, "create_app", recording_create_app)
            for module in list(sys.modules.values()):
                if getattr(module, "create_app", None) is original_create_app and module is not control_main:
                    monkeypatch.setattr(module, "create_app", recording_create_app)
            try:
                yield
            finally:
                app_store.set_current(None)
                for app in apps:
                    resources = getattr(app.state, "eager_resources", None)
                    if resources is not None:
                        try:
                            await resources.aclose()
                        except Exception:  # noqa: BLE001 - teardown must reach the drop
                            pass
