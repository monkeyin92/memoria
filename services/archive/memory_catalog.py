"""Backend-neutral contract helpers shared by the long-term memory catalog.

The catalog itself is :class:`services.archive.postgres_memory_catalog.PostgresMemoryCatalog`;
archive-family data is PostgreSQL-only.
"""

from __future__ import annotations


class SubjectCategoryUnresolved(Exception):
    """Identity was down, or the named person is missing.

    The compiler records an ignored receipt with no reason and leaves the
    archive evidence unprojected. A resolver that returns a category does not
    raise this. extraction_gate extracts only the string adult.
    """
