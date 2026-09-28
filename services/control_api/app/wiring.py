"""One build of the Control API object graph, eager or live (see ``Wiring``)."""

from __future__ import annotations

from asyncio import to_thread
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import FastAPI

from services.control_api.app.config import ControlSettings
from services.control_api.app.lifespan_resources import LifespanResources


class Worker(Protocol):
    def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True)
class Wiring:
    """One build of the object graph: eager (``create_app``) or live (lifespan).

    Live wiring picks PostgreSQL wherever a URL is set, initializes schemas,
    starts workers and registers every closeable.  Eager wiring is what ASGI
    test clients see without a lifespan: SQLite, no network, nothing started,
    and only the schemas ``create_app`` has always initialized inline.
    """

    app: FastAPI
    settings: ControlSettings
    live: bool
    resources: LifespanResources = field(default_factory=LifespanResources)
    # Test-only: eager wiring also builds the PostgreSQL stores; each opens its pool lazily.
    eager_postgres: bool = False

    def url(self, value: str) -> str:
        """A store's backend URL; plain eager wiring ignores it and stays on SQLite."""
        return value if self.live or self.eager_postgres else ""

    @property
    def schema_external(self) -> bool:  # the admin init script owns the schemas
        return self.settings.environment == "production" or self.eager_postgres

    def on_close(self, close: Callable[[], object]) -> None:
        if self.live or self.eager_postgres:
            self.resources.add(close)

    async def open(
        self,
        initialize: Callable[[], Awaitable[object]],
        close: Callable[[], object],
    ) -> None:
        """Initialize a live resource, then register its closer."""
        if self.live:
            await initialize()
            self.resources.add(close)
        elif self.eager_postgres:
            self.resources.add(close)  # initializes lazily on first use

    async def init_blocking(
        self,
        initialize: Callable[[], object],
        *,
        eager: bool = False,
    ) -> None:
        """Blocking schema setup: off-thread when live; inline only if ``eager``."""
        if self.live:
            await to_thread(initialize)
        elif eager:
            initialize()

    def start(self, worker: Worker) -> None:
        worker.start()
        self.resources.add(worker.stop)


def run_eagerly(wiring: Coroutine[object, None, None]) -> None:
    """Finish eager wiring synchronously, inside or outside a running loop.

    Eager ``Wiring`` never awaits I/O, so the coroutine completes on its
    first step; suspending would mean live-only work leaked into eager mode.
    """
    try:
        wiring.send(None)
    except StopIteration:
        return
    wiring.close()
    raise RuntimeError("eager Control API wiring must not suspend")
