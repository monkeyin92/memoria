"""Map recall-eligible child-archive facts into dialogue grounded items.

Parent summaries (`dialogue_summary`) never enter this path. Only
`ChildArchiveFactView` rows with `recall_eligible` (parent_confirmed) become
`GroundedItem` facts for companion recall.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from services.digital_self.response_planner import GroundedItem, SourceRef
from services.guardian.child_archive_review import ChildArchiveFactView

ARCHIVE_FIELD_TITLES: Final[dict[str, str]] = {
    "appellation": "称呼",
    "preference": "喜好",
    "pet_name": "小名",
}

FACT_VERSION_PREFIX: Final = "fact_version:"
DEFAULT_ARCHIVE_RECALL_LIMIT: Final = 4


def recall_ready_archive_views(
    views: Iterable[ChildArchiveFactView],
) -> tuple[ChildArchiveFactView, ...]:
    """Keep parent-confirmed archive facts only; drop pending/withdrawn/expired."""

    ready: list[ChildArchiveFactView] = []
    for view in views:
        if not view.recall_eligible:
            continue
        if view.display_lifecycle != "parent_confirmed":
            continue
        text = view.text.strip()
        if not text:
            continue
        ready.append(view)
    return tuple(ready)


def grounded_items_from_archive_views(
    views: Sequence[ChildArchiveFactView],
    *,
    limit: int = DEFAULT_ARCHIVE_RECALL_LIMIT,
) -> tuple[GroundedItem, ...]:
    """Build grounded memory claims with record id and fact version provenance."""

    if limit <= 0:
        return ()
    items: list[GroundedItem] = []
    for view in recall_ready_archive_views(views):
        title = ARCHIVE_FIELD_TITLES.get(view.field, view.field)
        items.append(
            GroundedItem(
                item_id=view.record_id,
                kind="memory_claim",
                content=f"{title}：{view.text.strip()}",
                source_refs=(
                    SourceRef(
                        "memory_claim",
                        view.record_id,
                        (f"{FACT_VERSION_PREFIX}{view.fact_version}",),
                    ),
                ),
            )
        )
        if len(items) >= limit:
            break
    return tuple(items)


__all__ = [
    "ARCHIVE_FIELD_TITLES",
    "DEFAULT_ARCHIVE_RECALL_LIMIT",
    "FACT_VERSION_PREFIX",
    "grounded_items_from_archive_views",
    "recall_ready_archive_views",
]
