"""Companion dialogue recall of parent-confirmed child-archive facts."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from fastapi import Request

from services.archive.memory_domain import MemorySearchResult
from services.digital_self.response_planner import GroundedItem, SourceRef
from services.guardian.child_archive_recall import (
    grounded_items_from_archive_views,
    merge_archive_ahead_of_catalog,
)
from services.guardian.domain import GuardianStorePort
from services.memory_scope.child_layers import ChildLayerStore

__all__ = [
    "merge_companion_memory_claims",
    "start_child_archive_recall_task",
]


def _child_layer_store(request: Request) -> ChildLayerStore | None:
    store = getattr(request.app.state, "child_layer_store", None)
    if store is None:
        return None
    return cast(ChildLayerStore, store)


async def _child_archive_consent_active(
    request: Request,
    *,
    subject_id: str,
    subject_category: str | None,
) -> bool:
    if subject_category != "minor":
        return False
    return (
        await cast(GuardianStorePort, request.app.state.guardian_store).active_consent(
            minor_user_id=subject_id,
            consent_kind="child_archive",
        )
        is not None
    )


async def _fetch_child_archive_grounded(
    request: Request,
    *,
    subject_id: str,
) -> tuple[GroundedItem, ...]:
    layers = _child_layer_store(request)
    if layers is None or not hasattr(layers, "list_archive_views"):
        return ()
    views = await layers.list_archive_views(
        subject_id=subject_id,
        viewer_subject_id=subject_id,
        limit=20,
        include_withdrawn=False,
    )
    return grounded_items_from_archive_views(views)


def start_child_archive_recall_task(
    request: Request,
    *,
    private_readable: bool,
    subject_id: str | None,
    subject_category: str | None,
) -> asyncio.Task[tuple[GroundedItem, ...]] | None:
    if not private_readable or subject_id is None:
        return None
    subject_for_archive = subject_id

    async def fetch_archive() -> tuple[GroundedItem, ...]:
        if not await _child_archive_consent_active(
            request,
            subject_id=subject_for_archive,
            subject_category=subject_category,
        ):
            return ()
        return await _fetch_child_archive_grounded(
            request, subject_id=subject_for_archive
        )

    return asyncio.create_task(fetch_archive())


def merge_companion_memory_claims(
    *,
    archive_result: Any,
    memory_result: Any,
    logger: logging.Logger,
) -> list[GroundedItem]:
    archive_items: tuple[GroundedItem, ...] = ()
    if archive_result is not None and not isinstance(archive_result, BaseException):
        archive_items = cast(tuple[GroundedItem, ...], archive_result)
    elif isinstance(archive_result, BaseException):
        logger.warning(
            "response planner child-archive recall unavailable",
            exc_info=archive_result,
        )
    catalog_items: list[GroundedItem] = []
    if memory_result is not None and not isinstance(memory_result, BaseException):
        for item in cast(MemorySearchResult, memory_result).items:
            catalog_items.append(
                GroundedItem(
                    item_id=item.item_id,
                    kind="memory_claim",
                    content=f"{item.title}：{item.snippet}",
                    source_refs=(
                        SourceRef(
                            "memory_claim",
                            item.item_id,
                            item.source_event_ids,
                        ),
                    ),
                )
            )
    elif isinstance(memory_result, BaseException):
        logger.warning(
            "response planner memory context unavailable",
            exc_info=memory_result,
        )
    return list(merge_archive_ahead_of_catalog(archive_items, catalog_items))
