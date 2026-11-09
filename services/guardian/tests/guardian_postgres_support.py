"""Shared PostgreSQL builders for the guardian store contract tests.

The DSN rewriting and role bootstrap every database-backed test needs are
the same across the behavior files, so they live here."""

from __future__ import annotations

from urllib.parse import (
    quote,
    urlsplit,
    urlunsplit,
)

import asyncpg


def _postgres_dsn(dsn: str, *, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(parsed.username or "postgres")
    password = quote(parsed.password or "")
    credentials = f"{username}:{password}" if password else username
    return urlunsplit(
        (
            parsed.scheme,
            f"{credentials}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


def _role_dsn(
    dsn: str,
    *,
    database: str,
    role: str,
    password: str,
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{role}:{password}@{host}",
            f"/{database}",
            "",
            "",
        )
    )


async def _ensure_roles(
    admin: asyncpg.Connection,
    *,
    database: str,
) -> tuple[bool, bool, bool]:
    """Create the three dedicated roles; returns created flags."""

    created = []
    for role, password in (
        ("memoria_guardian", "api-role-password"),
        ("memoria_guardian_maintenance", "maintenance-role-password"),
        ("memoria_guardian_worker", "worker-role-password"),
    ):
        exists = bool(
            await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        )
        if not exists:
            await admin.execute(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"
            )
        else:
            # Roles are cluster-wide; another suite may have set its own password.
            await admin.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{password}'")
        created.append(not exists)
    return tuple(created)  # type: ignore[return-value]

