"""Internal ingest for session-end child summaries and whitelist archive facts."""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.config import ControlSettings
from services.guardian.child_memory import (
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
)
from services.guardian.domain import GuardianStorePort
from services.memory_scope.child_layers import ChildLayerStore, MemoryPurposeStopped

router = APIRouter(prefix="/v1/internal/child-memory", tags=["child-memory-internal"])


class ArchiveFactIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: Literal["appellation", "preference", "pet_name"]
    text: str = Field(min_length=1, max_length=40)


class SessionRecapIngest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject_id: str = Field(min_length=1, max_length=128)
    resource_owner_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=128)
    summary_text: str | None = Field(default=None, min_length=1, max_length=200)
    archive_facts: list[ArchiveFactIn] = Field(default_factory=list)
    skipped_reason: str | None = Field(default=None, max_length=64)


def _require_archive_write_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    settings = cast(ControlSettings, request.app.state.settings)
    expected = settings.internal_token("archive_write")
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal capability token required")


def _layers(request: Request) -> ChildLayerStore:
    store = getattr(request.app.state, "child_layer_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail={"code": "child_layers_unavailable"})
    return cast(ChildLayerStore, store)


def _guardian(request: Request) -> GuardianStorePort:
    return cast(GuardianStorePort, request.app.state.guardian_store)


@router.post("/session-recap")
async def ingest_session_recap(
    body: SessionRecapIngest,
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> dict[str, Any]:
    """Accept one session-end paraphrase (+ optional whitelist facts). Never stores transcripts."""

    if body.skipped_reason:
        return {
            "skipped": True,
            "reason": body.skipped_reason,
            "summary_record_id": None,
            "archive_record_ids": [],
        }
    if not body.summary_text:
        raise HTTPException(status_code=422, detail={"code": "summary_required_unless_skipped"})

    layers = _layers(request)
    guardian = _guardian(request)
    existing = await layers.list(
        subject_id=body.subject_id,
        kind=KIND_DIALOGUE_SUMMARY,
        viewer_subject_id=body.resource_owner_id,
        limit=20,
    )
    if any(item.session_id == body.session_id for item in existing):
        return {
            "skipped": True,
            "reason": "session_summary_already_written",
            "summary_record_id": next(
                item.record_id for item in existing if item.session_id == body.session_id
            ),
            "archive_record_ids": [],
        }

    memory_consent = await guardian.active_consent(
        minor_user_id=body.subject_id, consent_kind="memory_retention"
    )
    if memory_consent is None:
        raise HTTPException(
            status_code=403, detail={"code": "memory_retention_consent_required"}
        )
    try:
        summary = DialogueSummary(
            subject_id=body.subject_id,
            text=body.summary_text,
            source_event_id=body.source_event_id,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_dialogue_summary"}) from exc

    try:
        summary_record = await layers.write_summary(
            summary,
            resource_owner_id=body.resource_owner_id,
            actor_id=body.resource_owner_id,
            consent_snapshot_id=memory_consent.consent_id,
        )
    except MemoryPurposeStopped:
        return {
            "skipped": True,
            "reason": "memory_purpose_stopped",
            "summary_record_id": None,
            "archive_record_ids": [],
        }

    archive_ids: list[str] = []
    archive_consent = await guardian.active_consent(
        minor_user_id=body.subject_id, consent_kind="child_archive"
    )
    if archive_consent is not None:
        for item in body.archive_facts:
            try:
                fact = ArchiveFact(
                    subject_id=body.subject_id, field=item.field, text=item.text
                )
            except ValueError:
                continue
            try:
                written = await layers.upsert_archive_fact(
                    fact,
                    resource_owner_id=body.resource_owner_id,
                    actor_id=body.resource_owner_id,
                    consent_snapshot_id=archive_consent.consent_id,
                )
            except MemoryPurposeStopped:
                break
            archive_ids.append(written.record_id)

    activity = getattr(request.app.state, "subject_activity_store", None)
    if activity is not None:
        await activity.touch(subject_person_id=body.subject_id, source="dialogue")

    return {
        "skipped": False,
        "reason": None,
        "summary_record_id": summary_record.record_id,
        "archive_record_ids": archive_ids,
    }
