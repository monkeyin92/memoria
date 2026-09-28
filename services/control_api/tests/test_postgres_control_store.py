"""PostgreSQL control store contract, run as the real runtime role.

The default parity run connects as a superuser, which bypasses RLS. These
tests connect as ``memoria_control`` (NOBYPASSRLS, not an owner) to prove the
store works under FORCE RLS, that other roles see nothing, and that the
SQLite-to-PostgreSQL migration copies and verifies real store data.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from services.control_api.app.database import MemoryStore, migrations
from services.control_api.app.database.backend import translate_sql
from services.control_api.app.database.schema import POSTGRES_TABLES, initialize_postgres

ADMIN_DSN = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="set MEMORIA_TEST_POSTGRES_DSN")

NOW = "2026-09-28T00:00:00+00:00"


def _with_search_path(dsn: str, schema: str) -> str:
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options=-csearch_path%3D{schema}"


def _as_role(dsn: str, role: str, password: str) -> str:
    parts = urlsplit(dsn)
    netloc = f"{quote(role)}:{quote(password)}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture()
def control_schema() -> Iterator[dict[str, str]]:
    schema = f"ctl_contract_{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(24)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    initialize_postgres(_with_search_path(ADMIN_DSN, schema), apply_schema=True).close()
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"ALTER ROLE memoria_control WITH LOGIN PASSWORD '{password}'")
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO memoria_control')
    try:
        yield {
            "schema": schema,
            "control_dsn": _with_search_path(
                _as_role(ADMIN_DSN, "memoria_control", password), schema
            ),
            "admin_dsn": _with_search_path(ADMIN_DSN, schema),
        }
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _store(dsn: str) -> MemoryStore:
    return MemoryStore(dsn=dsn, initialize_schema=False)


def test_translate_sql_rewrites_placeholders_and_rejects_ambiguous_sql() -> None:
    assert translate_sql("SELECT a FROM t WHERE b = ? AND c = ?") == (
        "SELECT a FROM t WHERE b = %s AND c = %s"
    )
    with pytest.raises(ValueError):
        translate_sql("SELECT * FROM t WHERE a LIKE 'x%'")
    with pytest.raises(ValueError):
        translate_sql("SELECT '?' FROM t")


def test_runtime_role_reads_and_writes_under_force_rls(control_schema: dict[str, str]) -> None:
    store = _store(control_schema["control_dsn"])
    try:
        store.initialize()
        user_id, created = store.bind_external_identities(
            preferred_user_id="u-contract",
            identities={"wechat_openid": "hash-openid"},
            now=NOW,
        )
        assert (user_id, created) == ("u-contract", True)
        message, replayed = store.add_message(
            user_id=user_id,
            client_message_id="m-1",
            request_fingerprint="a" * 64,
            role="user",
            text="你好",
            emotion=None,
            local_date="2026-09-28",
            created_at=NOW,
        )
        assert replayed is False
        # The unique violation is caught inside the same transaction; the
        # savepoint keeps it usable, so the replay returns the stored row.
        again, replayed = store.add_message(
            user_id=user_id,
            client_message_id="m-1",
            request_fingerprint="a" * 64,
            role="user",
            text="你好",
            emotion=None,
            local_date="2026-09-28",
            created_at=NOW,
        )
        assert replayed is True
        assert again["id"] == message["id"]
        assert store.get_profile(user_id=user_id, now=NOW)["user_id"] == user_id
    finally:
        store.close()


def test_other_roles_cannot_read_control_rows(control_schema: dict[str, str]) -> None:
    store = _store(control_schema["control_dsn"])
    try:
        store.bind_external_identities(
            preferred_user_id="u-private",
            identities={"wechat_openid": "hash-private"},
            now=NOW,
        )
    finally:
        store.close()
    outsider = f"ctl_outsider_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {outsider} LOGIN NOBYPASSRLS PASSWORD '{password}'")
        admin.execute(f'GRANT USAGE ON SCHEMA "{control_schema["schema"]}" TO {outsider}')
    try:
        dsn = _with_search_path(_as_role(ADMIN_DSN, outsider, password), control_schema["schema"])
        with psycopg.connect(dsn) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT count(*) FROM profiles").fetchone()
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'REVOKE ALL ON SCHEMA "{control_schema["schema"]}" FROM {outsider}')
            admin.execute(f"DROP ROLE {outsider}")


def test_every_table_is_owned_by_the_nologin_owner_with_force_rls(
    control_schema: dict[str, str],
) -> None:
    with psycopg.connect(control_schema["admin_dsn"]) as connection:
        rows = connection.execute(
            """
            SELECT c.relname, pg_get_userbyid(c.relowner), c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind = 'r'
            """
        ).fetchall()
    tables = {str(name): (str(owner), bool(forced)) for name, owner, forced in rows}
    assert set(tables) == {*POSTGRES_TABLES, "control_schema_migrations"}
    assert {value for value in tables.values()} == {("memoria_control_owner", True)}


@pytest.mark.real_sqlite_store
def test_migration_copies_verifies_and_refuses_a_second_run(
    control_schema: dict[str, str],
    tmp_path: Path,
) -> None:
    from scripts.migrate_control_sqlite_to_postgres import migrate

    source_path = tmp_path / "memoria.sqlite3"
    source = MemoryStore(str(source_path))
    user_id, _ = source.bind_external_identities(
        preferred_user_id="u-migrated",
        identities={"wechat_openid": "hash-migrated", "wechat_phone": "hash-phone"},
        now=NOW,
    )
    for index in range(3):
        source.add_message(
            user_id=user_id,
            client_message_id=f"m-{index}",
            request_fingerprint=f"{index}" * 64,
            role="user",
            text=f"第 {index} 句",
            emotion=None,
            local_date="2026-09-28",
            created_at=NOW,
        )
    with sqlite3.connect(source_path) as connection:
        max_id = int(connection.execute("SELECT max(id) FROM messages").fetchone()[0])

    dry = migrate(source_path, control_schema["admin_dsn"], dry_run=True)
    assert dry["tables"]["messages"]["rows"] == 3
    with psycopg.connect(control_schema["admin_dsn"]) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone() == (0,)

    applied: dict[str, Any] = migrate(source_path, control_schema["admin_dsn"], dry_run=False)
    assert applied["tables"]["external_identities"]["rows"] == 2
    assert applied["tables"]["messages"]["checksum"] == dry["tables"]["messages"]["checksum"]

    target = _store(control_schema["control_dsn"])
    try:
        latest, replayed = target.add_message(
            user_id=user_id,
            client_message_id="after-migration",
            request_fingerprint="z" * 64,
            role="assistant",
            text="迁移后",
            emotion=None,
            local_date="2026-09-28",
            created_at=NOW,
        )
        assert replayed is False
        assert latest["id"] == max_id + 1
    finally:
        target.close()

    with pytest.raises(SystemExit, match="not empty"):
        migrate(source_path, control_schema["admin_dsn"], dry_run=False)


def test_baseline_records_version_one_and_the_runtime_role_only_reads_it(
    control_schema: dict[str, str],
) -> None:
    with psycopg.connect(control_schema["control_dsn"]) as connection:
        assert connection.execute(
            "SELECT version, name FROM control_schema_migrations"
        ).fetchall() == [(1, "baseline")]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "INSERT INTO control_schema_migrations VALUES (2, 'forged', 'now')"
            )


def _write_migration(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


def test_pending_migrations_apply_once_in_order_and_gate_startup(
    control_schema: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "migrations"
    _write_migration(
        directory,
        "0002_profiles_note.sql",
        "ALTER TABLE profiles ADD COLUMN note TEXT NOT NULL DEFAULT '';",
    )
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", directory)
    # The code now needs version 2; the runtime role refuses a database at 1.
    with pytest.raises(RuntimeError, match="version 1, code needs 2"):
        initialize_postgres(control_schema["control_dsn"], apply_schema=False)

    with psycopg.connect(control_schema["admin_dsn"], autocommit=True) as admin:
        assert migrations.apply_pending(admin) == ["0002_profiles_note"]
        assert migrations.apply_pending(admin) == []
        assert migrations.applied_version(admin) == 2
    initialize_postgres(control_schema["control_dsn"], apply_schema=False).close()

    # Ahead of the code (a rolled-back release) still starts.
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", tmp_path / "none")
    initialize_postgres(control_schema["control_dsn"], apply_schema=False).close()


def test_a_failing_migration_leaves_no_trace(
    control_schema: dict[str, str],
    tmp_path: Path,
) -> None:
    directory = tmp_path / "migrations"
    _write_migration(
        directory,
        "0002_half_done.sql",
        "ALTER TABLE profiles ADD COLUMN half TEXT; SELECT 1 / 0;",
    )
    with psycopg.connect(control_schema["admin_dsn"], autocommit=True) as admin:
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrations.apply_pending(admin, directory)
        assert migrations.applied_version(admin) == 1
        assert admin.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'profiles' "
            "AND column_name = 'half'"
        ).fetchone() == (0,)


def test_migration_files_must_be_contiguous(tmp_path: Path) -> None:
    _write_migration(tmp_path, "0003_skips_two.sql", "SELECT 1;")
    with pytest.raises(RuntimeError, match="numbered"):
        migrations.migrations(tmp_path)
    _write_migration(tmp_path, "notes.sql", "SELECT 1;")
    with pytest.raises(RuntimeError, match="misnamed"):
        migrations.migrations(tmp_path)


def test_release_schema_step_applies_the_same_directory() -> None:
    release = (Path(__file__).resolve().parents[3] / "scripts" / "release_ops.sh").read_text(
        encoding="utf-8"
    )
    relative = migrations.MIGRATIONS_DIR.relative_to(Path(__file__).resolve().parents[3])
    assert f'"$R"/{relative.as_posix()}/[0-9][0-9][0-9][0-9]_*.sql' in release
    assert "psql -U memoria_admin -d memoria -1 -v ON_ERROR_STOP=1" in release
