"""Shared test fixtures for memory_scope tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from services.memory_scope.tests.postgres_helpers import (
    ACTION_EXECUTOR_ROLE,
    API_ROLE,
    WORKER_ROLE,
    dsn_with,
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
    admin_db = dsn_with(dsn, database=db_name)
    executor = dsn_with(
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
