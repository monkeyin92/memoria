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


def merge_archive_ahead_of_catalog(
    archive_items: Sequence[GroundedItem],
    catalog_items: Sequence[GroundedItem],
) -> tuple[GroundedItem, ...]:
    """Archive whitelist precedes catalog claims for planner truncation order."""

    return tuple(archive_items) + tuple(catalog_items)


__all__ = [
    "ARCHIVE_FIELD_TITLES",
    "DEFAULT_ARCHIVE_RECALL_LIMIT",
    "FACT_VERSION_PREFIX",
    "grounded_items_from_archive_views",
    "merge_archive_ahead_of_catalog",
    "recall_ready_archive_views",
]
