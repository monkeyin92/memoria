"""Roles and DSN helper shared by the memory_scope PostgreSQL tests."""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

ACTION_EXECUTOR_ROLE = "memoria_action_executor"
API_ROLE = "memoria_memory_api"
WORKER_ROLE = "memoria_memory_worker"


def dsn_with(
    dsn: str, *, database: str, user: str | None = None, password: str | None = None
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(user or parsed.username or "postgres")
    secret = quote(password if password is not None else parsed.password or "")
    credentials = f"{username}:{secret}" if secret else username
    return urlunsplit((parsed.scheme, f"{credentials}@{host}", f"/{database}", parsed.query, ""))
