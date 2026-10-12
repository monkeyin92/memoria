"""Control API seam for subject-keyed session recall (P0-04)."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from services.memory_scope.subject_session_recall import (
    SubjectSessionRecallDenied,
    SubjectSessionRecallStore,
)

_RECALL_CONTEXT_MAX_ITEMS = 4
_RECALL_CONTEXT_ITEM_MAX_CHARS = 240
_RECALL_CONTEXT_TOTAL_MAX_CHARS = 960


def _store(request: Request) -> SubjectSessionRecallStore | None:
    candidate = getattr(request.app.state, "subject_session_recall_store", None)
    if candidate is None:
        return None
    return cast(SubjectSessionRecallStore, candidate)


def _bounded(items: tuple[str, ...]) -> tuple[str, ...]:
    selected: list[str] = []
    total_chars = 0
    for item in items:
        text = item.strip()[:_RECALL_CONTEXT_ITEM_MAX_CHARS]
        if not text:
            continue
        if total_chars + len(text) > _RECALL_CONTEXT_TOTAL_MAX_CHARS:
            break
        selected.append(text)
        total_chars += len(text)
        if len(selected) >= _RECALL_CONTEXT_MAX_ITEMS:
            break
    return tuple(selected)


async def append_owner_utterance(
    request: Request,
    *,
    subject_id: str | None,
    session_id: str,
    session_epoch: int | None,
    turn_id: int | None,
    text: str,
) -> None:
    """Record one owner turn for later subject-scoped recall."""

    store = _store(request)
    if store is None or subject_id is None or session_epoch is None or turn_id is None:
        return
    cleaned = text.strip()
    if not cleaned:
        return
    try:
        await store.append(
            subject_id=subject_id,
            session_id=session_id,
            session_epoch=session_epoch,
            turn_id=turn_id,
            text=cleaned,
            actor_subject_id=subject_id,
        )
    except (SubjectSessionRecallDenied, ValueError):
        return


async def authoritative_recall_context(
    request: Request,
    *,
    subject_id: str | None,
    session_id: str,
    session_epoch: int | None,
    retention_allowed: bool,
    client_items: tuple[str, ...],
) -> tuple[str, ...]:
    """Merge server-side session recall with the agent snapshot, fail closed."""

    store = _store(request)
    if (
        store is None
        or not retention_allowed
        or subject_id is None
        or session_epoch is None
    ):
        return _bounded(client_items)
    try:
        server_items = await store.list_recent(
            subject_id=subject_id,
            session_id=session_id,
            session_epoch=session_epoch,
            actor_subject_id=subject_id,
            limit=_RECALL_CONTEXT_MAX_ITEMS,
        )
    except SubjectSessionRecallDenied:
        return ()
    merged: list[str] = []
    seen: set[str] = set()
    for item in (*server_items, *client_items):
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return _bounded(tuple(merged))
