from __future__ import annotations

from collections.abc import Sequence
from typing import Final

FACT_VERSION_PREFIX: Final = "fact_version:"


def is_child_archive_confirmed_claim(source_event_ids: Sequence[str]) -> bool:
    return bool(source_event_ids) and all(
        bool(event_id) and event_id.startswith(FACT_VERSION_PREFIX) for event_id in source_event_ids
    )


def format_fact_version_source_id(fact_version: int) -> str:
    return f"{FACT_VERSION_PREFIX}{fact_version}"
