"""What the Control API lifespan opens, and the PostgreSQL pools it shares (P2-08).

Ten stores reach the archive database with the same DSN. Each used to open its
own pool (up to 90 connections against production's ``max_connections=50``);
they now borrow one bounded pool per DSN. Sharing is safe because every store
scopes its session variables to the transaction (``set_config(..., true)``) and
none registers connection codecs or ``init`` hooks.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, cast

import asyncpg

#: Upper bound for one shared pool; the archive stores together used to allow 90.
SHARED_POOL_MAX_SIZE = 20
#: The per-statement timeout most archive stores already used.
SHARED_POOL_COMMAND_TIMEOUT_S = 15


class LifespanResources:
    """What the lifespan opened, closed newest first on shutdown.

    A resource registers once it is live, after the stores it was built from,
    so workers stop before their stores close.  Every closer runs even when an
    earlier one fails; the first failure is re-raised once all have run.
    """

    def __init__(self) -> None:
        self._closers: list[Callable[[], object]] = []

    def add(self, close: Callable[[], object]) -> None:
        self._closers.append(close)

    async def aclose(self) -> None:
        failure: BaseException | None = None
        while self._closers:
            close = self._closers.pop()
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure


class BorrowedPool:
    """A shared pool as one store sees it: everything but ``close`` delegates.

    Stores keep their own close and error paths unchanged; only the lifespan
    closes the shared pool, after every store that borrowed it has closed.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    async def close(self) -> None:
        return None


class SharedPostgresPools:
    """One pool per DSN, closed by ``resources`` after the stores that borrow it."""

    def __init__(self, resources: LifespanResources) -> None:
        self._resources = resources
        self._pools: dict[str, asyncpg.Pool] = {}

    async def borrow(self, dsn: str) -> asyncpg.Pool:
        pool = self._pools.get(dsn)
        if pool is None:
            pool = await asyncpg.create_pool(
                dsn,
                min_size=1,
                max_size=SHARED_POOL_MAX_SIZE,
                command_timeout=SHARED_POOL_COMMAND_TIMEOUT_S,
            )
            if pool is None:  # pragma: no cover - asyncpg returns a pool here
                raise RuntimeError("failed to create shared PostgreSQL pool")
            # Registered before any borrower's closer, so it closes after them.
            self._resources.add(pool.close)
            self._pools[dsn] = pool
        return cast(asyncpg.Pool, BorrowedPool(pool))
