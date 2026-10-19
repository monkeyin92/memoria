"""Shared test fixtures for memory_scope tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

ACTION_EXECUTOR_ROLE = "memoria_action_executor"
API_ROLE = "memoria_memory_api"
WORKER_ROLE = "memoria_memory_worker"


def _dsn_with(
    dsn: str, *, database: str, user: str | None = None, password: str | None = None
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(user or parsed.username or "postgres")
    secret = quote(password if password is not None else parsed.password or "")
    credentials = f"{username}:{secret}" if secret else username
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", f"/{database}", parsed.query, "")
    )


@pytest.fixture
async def cross_db() -> AsyncIterator[tuple[str, str, str, str]]:
    """Provision a temporary database with three roles: executor, api, worker.

    Yields (executor_dsn, admin_dsn, password, db_name).
    """
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    db_name = f"memory_cross_{uuid.uuid4().hex[:8]}"
    password = "memoria_local_test_password"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
        for role in (ACTION_EXECUTOR_ROLE, API_ROLE, WORKER_ROLE):
            if not await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role
            ):
                await admin.execute(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS"
                )
            await admin.execute(
                f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOBYPASSRLS"
                f" PASSWORD '{password}'"
            )
    finally:
        await admin.close()
    admin_db = _dsn_with(dsn, database=db_name)
    executor = _dsn_with(
        admin_db, database=db_name, user=ACTION_EXECUTOR_ROLE, password=password
    )
    try:
        yield executor, admin_db, password, db_name
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.close()
