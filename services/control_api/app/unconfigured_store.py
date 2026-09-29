"""A store the Control API has no PostgreSQL DSN for.

Guardian and archive-family data live only in PostgreSQL. Live startup
requires their DSNs; eager wiring (``create_app()`` without a lifespan, which
never opens a connection) gets this placeholder so the app object still
builds. Every call fails loudly: a missing store must never read as "no
consent" or "no memories".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn


class StoreNotConfiguredError(RuntimeError):
    """A store operation ran without its PostgreSQL DSN."""


class UnconfiguredStore:
    def __init__(self, store: str, setting: str) -> None:
        self._store = store
        self._setting = setting

    def __getattr__(self, name: str) -> Callable[..., NoReturn]:
        def unavailable(*_args: object, **_kwargs: object) -> NoReturn:
            raise StoreNotConfiguredError(
                f"{self._store}.{name} needs PostgreSQL ({self._setting})"
            )

        return unavailable
