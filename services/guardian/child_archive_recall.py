from __future__ import annotations

from collections.abc import Iterable

from services.guardian.child_archive_review import ChildArchiveFactView

__all__ = ["recall_ready_archive_views"]


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
