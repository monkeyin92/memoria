"""Backend-neutral contract helpers shared by the long-term memory catalog.

The catalog itself is :class:`services.archive.postgres_memory_catalog.PostgresMemoryCatalog`;
archive-family data is PostgreSQL-only.
"""

from __future__ import annotations


class SubjectCategoryUnresolved(Exception):
    """The evidence names another person whose category cannot be classified.

    ``None`` from a resolver is "category unknown", and unknown still compiles
    through the non-minor path.  Only a caller that looked and could not decide
    -- identity down, or the person missing -- raises this.  The catalog then
    records an ignored receipt and leaves the archive evidence unprojected.
    """
