"""Backend-neutral contract helpers shared by the long-term memory catalog.

The catalog itself is :class:`services.archive.postgres_memory_catalog.PostgresMemoryCatalog`;
archive-family data is PostgreSQL-only.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable


class SubjectCategoryUnresolved(Exception):
    """The evidence names another person whose category cannot be classified.

    ``None`` from a resolver is "category unknown", and unknown still compiles
    through the non-minor path.  Only a caller that looked and could not decide
    -- identity down, or the person missing -- raises this.  The catalog then
    records an ignored receipt and leaves the archive evidence unprojected.
    """


async def _await_category(value: str | None | Awaitable[str | None]) -> str | None:
    """Accept a sync category or one looked up on the compiler's own loop.

    The compiler calls this from ``async def``.  An awaitable resolver must be
    awaited here so a Postgres identity pool stays on the loop that created it.
    Opening another thread and calling ``asyncio.run`` binds that pool to the
    wrong loop and turns every other-subject lookup into a skipped projection.
    """

    if inspect.isawaitable(value):
        return await value
    return value
