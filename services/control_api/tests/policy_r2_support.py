"""Disposable PostgreSQL Guardian authority sharing the Session test database."""

import asyncpg
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.tests.guardian_postgres_support import _ensure_roles, _role_dsn


async def guardian_store(dsn):
    database = dsn.rsplit("/", 1)[-1]
    admin = await asyncpg.connect(dsn)
    try:
        await _ensure_roles(admin, database=database)
    finally:
        await admin.close()
    store = PostgresGuardianStore(
        dsn=_role_dsn(
            dsn, database=database, role="memoria_guardian", password="api-role-password"
        ),
        bootstrap_dsn=dsn,
        maintenance_dsn=_role_dsn(
            dsn,
            database=database,
            role="memoria_guardian_maintenance",
            password="maintenance-role-password",
        ),
        worker_dsn=_role_dsn(
            dsn, database=database, role="memoria_guardian_worker", password="worker-role-password"
        ),
    )
    await store.initialize()
    return store
