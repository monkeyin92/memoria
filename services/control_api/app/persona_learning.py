"""Persona learning scheduled from archive evidence (P1-03).

The persona follows the person using the device: evidence of the account holder
learns the account holder's persona under their persona consent, and evidence of
another bound subject (a child or elder on a one-to-one device) learns that
subject's persona under the binding's long-term-memory grant. The engine keys
the persona by the evidence event's subject.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from fastapi import BackgroundTasks, Request

from services.archive.domain import EvidenceEvent
from services.control_api.app.database import MemoryStore
from services.persona.domain import PersonaEnginePort, PersonaEvidence
from services.persona.rules import trusted_uncertain_profile

logger = logging.getLogger(__name__)

#: The archive route's per-account write fence, passed in to avoid a route import.
type AccountWrite = Callable[[Request, str], AbstractAsyncContextManager[None]]


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


async def observe_persona(
    request: Request,
    engine: PersonaEnginePort,
    *,
    account_write: AccountWrite,
    account_id: str,
    source_event_id: str,
    speech_duration_ms: int | None,
    pause_ratio: float | None,
    quality_score: float | None,
    subject_learning_allowed: bool = False,
    style_only: bool = False,
) -> None:
    try:
        async with account_write(request, account_id):
            # The account holder's own persona keeps its own consent; another
            # subject was already cleared by its binding's long-term-memory grant.
            allowed = subject_learning_allowed or await engine.learning_allowed(
                account_id=account_id
            )
            await engine.observe(
                PersonaEvidence(
                    account_id=account_id,
                    source_event_id=source_event_id,
                    learning_allowed=allowed,
                    speech_duration_ms=speech_duration_ms,
                    pause_ratio=pause_ratio,
                    quality_score=quality_score,
                    style_only=style_only,
                )
            )
    except Exception:
        logger.exception("persona observation failed source_event_id=%s", source_event_id)


def _bounded_metric(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    metric = float(value)
    if not math.isfinite(metric) or not minimum <= metric <= maximum:
        return None
    return metric


def _persona_metrics(
    payload: Mapping[str, Any],
) -> tuple[int | None, float | None, float | None] | None:
    speech_ms = _bounded_metric(payload, "speech_ms", minimum=1, maximum=600_000)
    pause_ratio = _bounded_metric(payload, "pause_ratio", minimum=0, maximum=1)
    quality_score = _bounded_metric(payload, "quality_score", minimum=0, maximum=1)
    if any(
        key in payload and metric is None
        for key, metric in (
            ("speech_ms", speech_ms),
            ("pause_ratio", pause_ratio),
            ("quality_score", quality_score),
        )
    ):
        return None
    return (
        int(speech_ms) if speech_ms is not None else None,
        pause_ratio,
        quality_score,
    )


def schedule_persona_observation(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    event: EvidenceEvent,
    duplicate: bool,
    account_write: AccountWrite,
    allow_uncertain_candidate: bool = False,
    subject_learning_allowed: bool = False,
    style_only: bool = False,
) -> None:
    if duplicate or event.event_type != "speech.utterance_finalized":
        return
    # The persona follows the person using the device (P1-03): the engine keys
    # it by the event's subject. The account holder's turns keep the account's
    # persona consent; another subject -- a child or elder on a one-to-one
    # device -- learns only when the signed runtime profile confirms that
    # subject with the binding's long-term-memory grant. A NULL subject is
    # nobody's persona.
    if event.subject_id is None:
        return
    other_subject = event.subject_id != event.account_id
    if other_subject and not subject_learning_allowed:
        return
    if event.payload.get("persona_eligible") is not True:
        return
    if event.speaker_class == "uncertain":
        if (
            not allow_uncertain_candidate
            or _store(request).get_account(user_id=event.account_id) is None
            or trusted_uncertain_profile(event.payload) is None
        ):
            return
    elif event.speaker_class != "owner":
        return
    engine = cast(PersonaEnginePort, request.app.state.persona_engine)
    metrics = _persona_metrics(event.payload)
    if metrics is None:
        return
    speech_duration_ms, pause_ratio, quality_score = metrics
    background_tasks.add_task(
        observe_persona,
        request,
        engine,
        account_write=account_write,
        account_id=event.account_id,
        source_event_id=event.event_id,
        speech_duration_ms=speech_duration_ms,
        pause_ratio=pause_ratio,
        quality_score=quality_score,
        subject_learning_allowed=other_subject,
        style_only=style_only,
    )


def schedule_low_sensitivity_persona_observation(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    event: EvidenceEvent,
    duplicate: bool,
    account_write: AccountWrite,
    subject_learning_allowed: bool = False,
    style_only: bool = False,
) -> None:
    """Keep shadow candidates separate from owner-history learning."""

    if (
        event.speaker_class != "uncertain"
        or event.payload.get("speaker_reason_code") != "shadow_owner_candidate"
        or event.payload.get("history_eligible") is not False
        or event.payload.get("owner_projection_eligible") is not False
    ):
        return
    schedule_persona_observation(
        request,
        background_tasks,
        event=event,
        duplicate=duplicate,
        account_write=account_write,
        allow_uncertain_candidate=True,
        subject_learning_allowed=subject_learning_allowed,
        style_only=style_only,
    )
