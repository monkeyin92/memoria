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


# Control API, governance and companionship tests run against a
# production-shaped PostgreSQL: every test gets a clone of a template built by
# the real init script, every DSN connects as its production role, and eager
# wiring builds the PostgreSQL stores (MEMORIA_EAGER_POSTGRES). Guardian and
# archive data are PostgreSQL-only, so without MEMORIA_TEST_POSTGRES_DSN these
# tests skip; ``sqlite_only`` tests (control-store SQLite migrations) still run.
_APP_POSTGRES = bool(os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip())
_APP_POSTGRES_ROOTS = (
    "services/control_api/tests",
    "services/governance/tests",
    "services/companionship/tests",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_sqlite = pytest.mark.skip(reason="SQLite-file fixture; not a control store behavior")
    skip_app = pytest.mark.skip(reason="API tests need MEMORIA_TEST_POSTGRES_DSN")
    for item in items:
        if _CONTROL_BACKEND == "postgres" and item.get_closest_marker("sqlite_only") is not None:
            item.add_marker(skip_sqlite)
        if (
            not _APP_POSTGRES
            and any(root in str(item.path) for root in _APP_POSTGRES_ROOTS)
            and item.get_closest_marker("sqlite_only") is None
        ):
            item.add_marker(skip_app)


# One production-shaped template per run, shared by the app harness and the
# store fixtures; built on first use.
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
def postgres_database() -> Iterator[Any]:
    """A fresh clone of the production-shaped template (``TestDatabase``).

    Guardian and archive-family data are PostgreSQL-only; without
    MEMORIA_TEST_POSTGRES_DSN the test skips. Archive-family stores connect as
    ``database.role_dsn("memoria_app")`` (their schemas are in the template);
    ``database.owner_dsn()`` seeds rows no store API writes.
    """

    if not os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip():
        pytest.skip("PostgreSQL-only store tests need MEMORIA_TEST_POSTGRES_DSN")
    from testing.postgres_harness import cloned_database

    with cloned_database(_app_template()) as database:
        yield database


_GUARDIAN_ROLES = ("memoria_guardian", "memoria_guardian_maintenance", "memoria_guardian_worker")


@pytest.fixture
def dev_database_url() -> Iterator[str]:
    """A local-development database: one NOBYPASSRLS owner role, like README's setup.

    Guardian and archive data are PostgreSQL-only, so live startup needs a DSN.
    On a shared test cluster the guardian production roles may already exist;
    the owner then joins them so the guardian schema can hand functions over.
    """
    import uuid
    from urllib.parse import urlsplit, urlunsplit

    import psycopg

    admin = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if not admin:
        pytest.skip("a live PostgreSQL startup needs MEMORIA_TEST_POSTGRES_DSN")
    name = f"memoria_dev_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f"CREATE ROLE {name} LOGIN PASSWORD 'dev' NOSUPERUSER NOBYPASSRLS")
        connection.execute(f"CREATE DATABASE {name} OWNER {name}")
        existing = [
            role
            for role in _GUARDIAN_ROLES
            if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        ]
        for role in existing:
            connection.execute(f"GRANT {role} TO {name}")
    parts = urlsplit(admin)
    if existing:
        # ALTER ... OWNER TO needs the new owner to hold CREATE on the schema.
        database_admin = urlunsplit(parts._replace(path=f"/{name}"))
        with psycopg.connect(database_admin, autocommit=True) as connection:
            connection.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {', '.join(existing)}")
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    try:
        yield urlunsplit((parts.scheme, f"{name}:dev@{host}{port}", f"/{name}", "", ""))
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            connection.execute(f"DROP ROLE IF EXISTS {name}")


@pytest.fixture
async def guardian_postgres_store(postgres_database: Any) -> Any:
    """A PostgresGuardianStore on ``postgres_database``, as the production roles."""

    from services.guardian.postgres_store import PostgresGuardianStore

    env = postgres_database.control_env()
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

    @pytest.fixture(autouse=True)
    def _app_postgres(request: pytest.FixtureRequest) -> None:
        # Sync on purpose: an async autouse fixture would run an event loop
        # around every test in the repository, and its teardown would call
        # time.monotonic while tests elsewhere still have it patched.
        path = str(request.node.path)
        if any(root in path for root in _APP_POSTGRES_ROOTS) and not request.node.get_closest_marker(
            "sqlite_only"
        ):
            request.getfixturevalue("_app_postgres_harness")

    @pytest_asyncio.fixture
    async def _app_postgres_harness(monkeypatch: pytest.MonkeyPatch) -> Any:
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
