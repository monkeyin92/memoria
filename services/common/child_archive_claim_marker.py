"""The fact_version stamp on a parent-confirmed child-archive claim.

Archive recall is the only producer of this prefix. Catalog event ids are not
stamped this way. Callers that admit a claim import this predicate instead of
checking the prefix themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

FACT_VERSION_PREFIX: Final = "fact_version:"


def is_child_archive_confirmed_claim(source_event_ids: Sequence[str]) -> bool:
    """True when every id is a non-empty fact_version stamp."""

    return bool(source_event_ids) and all(
        bool(event_id) and event_id.startswith(FACT_VERSION_PREFIX) for event_id in source_event_ids
    )


def format_fact_version_source_id(fact_version: int) -> str:
    """Source id archive recall stamps onto one confirmed fact."""

    return f"{FACT_VERSION_PREFIX}{fact_version}"
