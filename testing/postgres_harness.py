"""A production-shaped PostgreSQL for tests.

The template database is built with the real ``infra/postgres/init-memoria.sh``
(its SQL, executed by a tiny interpreter for the few psql meta-commands it
uses) and the same schema files the data compose mounts, so roles, grants,
owners and FORCE RLS match production. Stores that apply their own schema at
runtime are then initialized once, as their production roles. Each test gets
a clone of the template (``CREATE DATABASE ... TEMPLATE``, a few ms) and a
DSN per production role pointing at it.

Nothing here runs in production; it only reads the deployment files.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import psycopg.sql
import yaml

ROOT = Path(__file__).resolve().parents[1]
INIT_SCRIPT = ROOT / "infra" / "postgres" / "init-memoria.sh"
DATA_COMPOSE = ROOT / "infra" / "memoria-data.production.yml"
# Every login role gets the same throwaway password inside the test cluster.
TEST_ROLE_PASSWORD = "memoria-test-" + secrets.token_hex(8)


def _login_roles() -> tuple[str, ...]:
    from scripts.production_postgres_roles import PRODUCTION_POSTGRES_ROLES

    return tuple(role.role for role in PRODUCTION_POSTGRES_ROLES)


LOGIN_ROLES = _login_roles()


def _mounts() -> dict[str, Path]:
    """Container path -> repository file, exactly as the data compose mounts."""

    document = yaml.safe_load(DATA_COMPOSE.read_text(encoding="utf-8"))
    mounts: dict[str, Path] = {}
    for volume in document["services"]["postgres"]["volumes"]:
        if isinstance(volume, dict) and "docker-entrypoint-initdb.d" in volume.get("target", ""):
            mounts[volume["target"]] = (DATA_COMPOSE.parent / volume["source"]).resolve()
    return mounts


def _init_sql_and_variables() -> tuple[str, dict[str, str]]:
    script = INIT_SCRIPT.read_text(encoding="utf-8")
    body = script.split("<<'SQL'\n", 1)[1].rsplit("\nSQL", 1)[0]
    variables: dict[str, str] = {}
    for name, env in re.findall(r'--set=([a-z_]+)="\$([A-Z_]+)"', script):
        variables[name] = TEST_ROLE_PASSWORD if env.endswith("_PASSWORD") else env
    return body, variables


def _statements(sql: str) -> Iterator[tuple[str, str]]:
    """Yield ("sql", text) and ("meta", line) items in psql order.

    Handles single-quoted strings, $tag$ bodies and -- comments, which is
    everything init-memoria.sh uses.
    """

    buffer: list[str] = []
    index = 0
    length = len(sql)
    at_line_start = True
    while index < length:
        char = sql[index]
        if at_line_start and char == "\\":
            # A meta-command ends the query buffer, terminated or not (\gexec
            # runs an unterminated buffer).
            text = "".join(buffer).strip()
            if text:
                yield ("sql", text)
            buffer = []
            end = sql.find("\n", index)
            end = length if end < 0 else end
            yield ("meta", sql[index:end].strip())
            index = end + 1
            continue
        at_line_start = False
        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end < 0 else end
            index = end
            continue
        if char == "'":
            end = index + 1
            while True:
                end = sql.find("'", end)
                if end < 0 or not sql.startswith("''", end):
                    break
                end += 2
            buffer.append(sql[index : end + 1])
            index = end + 1
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_]*\$", sql[index:])
            if match:
                tag = match.group(0)
                end = sql.find(tag, index + len(tag))
                buffer.append(sql[index : end + len(tag)])
                index = end + len(tag)
                continue
        if char == ";":
            text = "".join(buffer).strip()
            if text:
                yield ("sql", text)
            buffer = []
            index += 1
            continue
        if char == "\n":
            at_line_start = True
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        yield ("sql", tail)


def _with_database(dsn: str, database: str, *, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rpartition("@")[2]
        netloc = f"{quote(user)}:{quote(password or '')}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{database}", parts.query, parts.fragment))


def _run_init_script(admin_dsn: str, database: str) -> None:
    body, variables = _init_sql_and_variables()
    variables["control_schema_present"] = "true"
    mounts = _mounts()
    control_schema = ROOT / "services" / "control_api" / "app" / "database" / "postgres_schema.sql"
    mounts["/docker-entrypoint-initdb.d/011-control-schema.sql"] = control_schema
    # The script targets the production database name.
    body = re.sub(r"\bDATABASE memoria\b", f'DATABASE "{database}"', body)
    body = body.replace("datname = 'memoria'", f"datname = '{database}'")
    body = body.replace("\\connect memoria", f"\\connect {database}")

    def interpolate(text: str) -> str:
        return re.sub(
            r":'([a-z_]+)'",
            lambda match: "'" + variables[match.group(1)].replace("'", "''") + "'",
            text,
        )

    connection = psycopg.connect(admin_dsn, autocommit=True)
    pending = ""
    skipping = False
    try:
        for kind, text in _statements(body):
            if kind == "meta":
                command, _, argument = text.partition(" ")
                if command == "\\if":
                    name = argument.strip().lstrip(":")
                    skipping = variables.get(name, "false").lower() != "true"
                elif command == "\\endif":
                    skipping = False
                elif skipping:
                    continue
                elif command != "\\gexec" and pending:
                    connection.execute(interpolate(pending))
                    pending = ""
                if skipping or command in {"\\if", "\\endif"}:
                    continue
                if command == "\\gexec":
                    for (statement,) in connection.execute(interpolate(pending)).fetchall():
                        if statement:
                            connection.execute(statement)
                    pending = ""
                elif command == "\\connect":
                    connection.close()
                    connection = psycopg.connect(
                        _with_database(admin_dsn, argument.strip()), autocommit=True
                    )
                elif command == "\\i":
                    path = mounts[argument.strip()]
                    connection.execute(path.read_text(encoding="utf-8"))
                else:
                    raise RuntimeError(f"unsupported psql meta-command in init script: {text}")
                continue
            if skipping:
                continue
            if pending:
                connection.execute(interpolate(pending))
            pending = text
        if pending:
            connection.execute(interpolate(pending))
    finally:
        connection.close()


@dataclass(frozen=True)
class TestDatabase:
    name: str
    admin_dsn: str

    def role_dsn(self, role: str) -> str:
        return _with_database(self.admin_dsn, self.name, user=role, password=TEST_ROLE_PASSWORD)

    def owner_dsn(self) -> str:
        """The clone as the cluster admin, for seeding rows no store API writes."""
        return _with_database(self.admin_dsn, self.name)

    def control_env(self) -> dict[str, str]:
        """Every Control API DSN, each as its production role."""

        from scripts.production_postgres_roles import (
            PRODUCTION_POSTGRES_ROLES,
            production_control_database_urls,
        )

        parts = urlsplit(self.admin_dsn)
        passwords = {role.password_env: TEST_ROLE_PASSWORD for role in PRODUCTION_POSTGRES_ROLES}
        env = production_control_database_urls(
            passwords, host=parts.hostname or "127.0.0.1", port=parts.port or 5432, database=self.name
        )
        env["MEMORIA_CONTROL_DATABASE_URL"] = self.role_dsn("memoria_control")
        return env


# Schemas the archive-family stores apply themselves on first use, in the
# order their dependencies require. Production applies them as memoria_app
# (the archive DSN role); so does the template.
RUNTIME_SCHEMAS: tuple[str, ...] = (
    "services/archive/postgres_archive_schema.sql",
    "services/archive/postgres_memory_schema.sql",
    "services/archive/postgres_skill_schema.sql",
    "services/persona/postgres_schema.sql",
    "services/self_model/postgres_schema.sql",
    "services/digital_self/postgres_schema.sql",
    "services/legacy/postgres_schema.sql",
    "services/speaker/postgres_schema.sql",
    "services/voice_profile/postgres_schema.sql",
)


def _initialize_runtime_schemas(database: TestDatabase) -> None:
    with psycopg.connect(database.role_dsn("memoria_app"), autocommit=True) as connection:
        for relative in RUNTIME_SCHEMAS:
            connection.execute((ROOT / relative).read_text(encoding="utf-8"))


def build_template(admin_dsn: str) -> TestDatabase:
    name = f"memoria_tpl_{uuid.uuid4().hex[:10]}"
    _run_init_script(admin_dsn, name)
    template = TestDatabase(name=name, admin_dsn=admin_dsn)
    _initialize_runtime_schemas(template)
    _terminate(admin_dsn, name)
    return template


def _terminate(admin_dsn: str, database: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        )


@contextmanager
def cloned_database(template: TestDatabase) -> Iterator[TestDatabase]:
    name = f"memoria_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(template.admin_dsn, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template.name}"')
        # Roles are cluster-wide and other PostgreSQL tests set their own
        # passwords on them; restore the harness password every time.
        for role in LOGIN_ROLES:
            connection.execute(
                psycopg.sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    psycopg.sql.Identifier(role), psycopg.sql.Literal(TEST_ROLE_PASSWORD)
                )
            )
    clone = TestDatabase(name=name, admin_dsn=template.admin_dsn)
    try:
        yield clone
    finally:
        _terminate(template.admin_dsn, name)
        with psycopg.connect(template.admin_dsn, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


def drop_template(template: TestDatabase) -> None:
    _terminate(template.admin_dsn, template.name)
    with psycopg.connect(template.admin_dsn, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{template.name}"')


def environment_for(database: TestDatabase) -> Mapping[str, str]:
    return database.control_env()


__all__ = [
    "TEST_ROLE_PASSWORD",
    "TestDatabase",
    "build_template",
    "cloned_database",
    "drop_template",
    "environment_for",
]
