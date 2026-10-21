"""Control API seam for subject-keyed session recall (P0-04)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request

from services.memory_scope.subject_session_recall import (
    InMemorySubjectSessionRecallStore,
    SubjectSessionRecallDenied,
    SubjectSessionRecallStore,
)

logger = logging.getLogger(__name__)


def wire_subject_session_recall_store(app: FastAPI) -> None:
    app.state.subject_session_recall_store = InMemorySubjectSessionRecallStore()


def session_epoch_from_runtime_profile(runtime_profile: Mapping[str, object]) -> int | None:
    profile_epoch = runtime_profile.get("session_epoch")
    if isinstance(profile_epoch, int) and profile_epoch >= 1:
        return profile_epoch
    return None

_RECALL_CONTEXT_MAX_ITEMS = 4
_RECALL_CONTEXT_ITEM_MAX_CHARS = 240
_RECALL_CONTEXT_TOTAL_MAX_CHARS = 960
_RECALL_CONTEXT_BUDGET_S = 0.15


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


async def record_archive_owner_utterance_if_applicable(
    request: Request,
    *,
    event_type: str,
    speaker_class: str | None,
    retention_allowed: bool,
    subject_id: str | None,
    session_id: str,
    turn_id: int | None,
    payload: Mapping[str, Any],
    memory_write_fence: Mapping[str, Any],
) -> None:
    if (
        event_type != "speech.utterance_finalized"
        or speaker_class != "owner"
        or not retention_allowed
        or subject_id is None
        or turn_id is None
    ):
        return
    utterance = payload.get("text")
    session_epoch = memory_write_fence.get("session_epoch")
    if isinstance(utterance, str) and isinstance(session_epoch, int):
        await append_owner_utterance(
            request,
            subject_id=subject_id,
            session_id=session_id,
            session_epoch=session_epoch,
            turn_id=turn_id,
            text=utterance,
        )


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
    started = time.perf_counter()
    try:
        # Session recall is an optional memory enrichment. A long session makes
        # this indexed read a queue behind more work, and it used to run outside
        # every budget on the response-plan path. Cap it and keep the client
        # items so a slow store degrades the memory context, never the plan.
        async with asyncio.timeout(_RECALL_CONTEXT_BUDGET_S):
            server_items = await store.list_recent(
                subject_id=subject_id,
                session_id=session_id,
                session_epoch=session_epoch,
                actor_subject_id=subject_id,
                limit=_RECALL_CONTEXT_MAX_ITEMS,
            )
    except SubjectSessionRecallDenied:
        return ()
    except TimeoutError:
        logger.info(
            "recall_timeout hit=false category=session_recall latency_ms=%.1f "
            "session_id=%s",
            (time.perf_counter() - started) * 1000.0,
            session_id,
        )
        return _bounded(client_items)
    merged: list[str] = []
    seen: set[str] = set()
    for item in (*server_items, *client_items):
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return _bounded(tuple(merged))
