"""Internal ingest for session-end child summaries and whitelist archive facts."""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.common.memory_decision_contract import decide_auto_extract
from services.control_api.app.config import ControlSettings
from services.guardian.child_memory import (
    ArchiveFact,
    DialogueSummary,
)
from services.guardian.domain import GuardianStorePort
from services.guardian.preference_extract import (
    DEFAULT_AUTO_CONFIDENCE,
    RULE_VERSION,
)
from services.memory_scope.child_layers import ChildLayerStore

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
    """Accept one session-end paraphrase (+ optional whitelist facts). Never stores transcripts.

    Summary writes are keyed by ``(subject_id, session_id)`` via a stable
    ``policy_receipt_id``. Retries resume archive upserts even when the summary
    row already exists. Response write modes follow A01 SaveReceipt vocabulary
    for what actually persisted (``sync_persisted`` vs ``not_written``).
    """

    if body.skipped_reason:
        return {
            "skipped": True,
            "reason": body.skipped_reason,
            "summary_record_id": None,
            "summary_created": False,
            "summary_write_mode": "not_written",
            "archive_record_ids": [],
            "archive_write_mode": "not_written",
        }
    if not body.summary_text:
        raise HTTPException(status_code=422, detail={"code": "summary_required_unless_skipped"})

    layers = _layers(request)
    guardian = _guardian(request)
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

    ensured = await layers.ensure_summary(
        summary,
        resource_owner_id=body.resource_owner_id,
        actor_id=body.resource_owner_id,
        consent_snapshot_id=memory_consent.consent_id,
    )

    archive_ids: list[str] = []
    archive_write_mode: Literal["sync_persisted", "not_written"] = "not_written"
    archive_consent = await guardian.active_consent(
        minor_user_id=body.subject_id, consent_kind="child_archive"
    )
    if archive_consent is not None and body.archive_facts:
        for item in body.archive_facts:
            try:
                fact = ArchiveFact(
                    subject_id=body.subject_id, field=item.field, text=item.text
                )
            except ValueError:
                continue
            record_id = f"charch:{uuid.uuid4()}"
            birth = decide_auto_extract(
                subject_key=body.subject_id,
                predicate=item.field,
                object_text=item.text,
                polarity="positive",
                rule_version=RULE_VERSION,
                lifecycle="pending_confirm",
                source_session_id=body.session_id,
                confidence=DEFAULT_AUTO_CONFIDENCE,
                record_id=record_id,
            )
            written = await layers.upsert_archive_fact(
                fact,
                resource_owner_id=body.resource_owner_id,
                actor_id=body.resource_owner_id,
                consent_snapshot_id=archive_consent.consent_id,
                birth_decision=birth,
            )
            archive_ids.append(written.record_id)
        if archive_ids:
            archive_write_mode = "sync_persisted"
    elif archive_consent is not None and not body.archive_facts:
        archive_write_mode = "not_written"

    activity = getattr(request.app.state, "subject_activity_store", None)
    if activity is not None:
        await activity.touch(subject_person_id=body.subject_id, source="dialogue")

    return {
        "skipped": False,
        "reason": None if ensured.created else "session_summary_already_written",
        "summary_record_id": ensured.record.record_id,
        "summary_created": ensured.created,
        "summary_write_mode": "sync_persisted",
        "archive_record_ids": archive_ids,
        "archive_write_mode": archive_write_mode,
    }
