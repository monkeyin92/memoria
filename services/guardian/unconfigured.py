"""The guardian store when no PostgreSQL DSN is configured.

Guardian links, consents and a minor's corpus live only in PostgreSQL. Live
Control API startup requires ``MEMORIA_GUARDIAN_DATABASE_URL``; eager test
wiring, which never opens a connection, gets this placeholder so apps whose
tests never touch guardian data still build. Every call fails loudly: a
missing store must never read as "no consent".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn


class GuardianStoreNotConfiguredError(RuntimeError):
    """A guardian operation ran without a PostgreSQL guardian store."""


class UnconfiguredGuardianStore:
    def __getattr__(self, name: str) -> Callable[..., NoReturn]:
        def unavailable(*_args: object, **_kwargs: object) -> NoReturn:
            raise GuardianStoreNotConfiguredError(
                f"guardian store {name!r} needs PostgreSQL (MEMORIA_GUARDIAN_DATABASE_URL)"
            )

        return unavailable
