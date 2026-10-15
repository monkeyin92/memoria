"""Guardian pin write and the interrupt mark for a crisis script.

Lives outside interaction.py so that module stays inside its line budget.
The body of the interrupt mark is a crisis event id. Child speech is not accepted.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class CrisisScriptInterruptedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crisis_event_id: str = Field(min_length=1, max_length=64)


def attach_crisis_script_routes(
    router: APIRouter,
    *,
    require_token: Any,
    notifications: Any,
) -> None:
    @router.post("/crisis-script-interrupted")
    async def crisis_script_interrupted(
        request: Request,
        body: CrisisScriptInterruptedBody,
        _: Annotated[None, Depends(require_token)],
    ) -> dict[str, Any]:
        template_id = await notifications(request).mark_script_interrupted(
            crisis_event_id=body.crisis_event_id,
        )
        if template_id is None:
            raise HTTPException(status_code=404, detail="crisis pin was not found")
        return {"reply_template_id": template_id, "script_interrupted": True}


async def write_guardian_crisis_pin(
    request: Request,
    notifications: Any,
    body: Any,
    now: datetime,
    crisis: Any,
    script: Any,
    *,
    notify: bool,
    active_subject_id: str | None,
    is_minor_student: bool,
    contacts: tuple[str, ...],
) -> tuple[bool, str | None]:
    if not (
        notify
        and script is not None
        and active_subject_id is not None
        and (is_minor_student or contacts)
    ):
        return False, None
    try:
        receipt = await notifications(request).record_minor_crisis(
            minor_user_id=active_subject_id,
            session_id=body.session_id,
            turn_id=body.fence.turn_id,
            generation_id=body.fence.generation_id,
            tool_epoch=body.fence.tool_epoch,
            script_version=script.template_id,
            reply_template_id=script.template_id,
            occurred_at=now,
            declared_guardian_ids=contacts,
            attempt_push=crisis.attempt_push,
            possibly_story_retell=crisis.possibly_story_retell,
        )
    except Exception:
        logger.exception(
            "minor crisis notification enqueue failed; preserving fixed safety reply "
            "session_id=%s turn_id=%s",
            body.session_id,
            body.fence.turn_id,
        )
        return False, None
    return True, receipt.crisis_event_id
