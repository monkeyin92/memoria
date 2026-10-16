"""Internal mark that an in-progress safety script was cancelled."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from services.guardian.crisis import CrisisNotificationStorePort

logger = logging.getLogger(__name__)

router = APIRouter()


class _ResponsePlanSettings(Protocol):
    def internal_token(self, capability: str) -> str: ...


class CrisisScriptInterrupted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crisis_event_id: UUID


def _require_response_plan_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    settings = cast(_ResponsePlanSettings, request.app.state.settings)
    expected = settings.internal_token("response_plan")
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal response plan token required")


def _store(request: Request) -> CrisisNotificationStorePort:
    return cast(CrisisNotificationStorePort, request.app.state.guardian_store)


@router.post("/crisis-script-interrupted")
async def mark_crisis_script_interrupted(
    body: CrisisScriptInterrupted,
    request: Request,
    _: Annotated[None, Depends(_require_response_plan_token)],
) -> dict[str, str]:
    template_id = await _store(request).mark_script_interrupted(
        crisis_event_id=str(body.crisis_event_id),
    )
    if template_id is None:
        raise HTTPException(status_code=404, detail="crisis event not found")
    logger.info(
        "crisis script interrupted crisis_event_id=%s reply_template_id=%s",
        body.crisis_event_id,
        template_id,
    )
    return {"crisis_event_id": str(body.crisis_event_id)}
