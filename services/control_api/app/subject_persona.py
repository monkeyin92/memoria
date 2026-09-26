"""Which persona a companion turn may use: the confirmed subject's own (P1-03)."""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from fastapi import Request

from services.control_api.app.config import ControlSettings
from services.persona.domain import PersonaCapsule, PersonaEnginePort, PersonaRequest
from services.persona.engine import persona_capsule_from_snapshot
from services.persona.subject_projection import read_active_version


async def subject_persona_capsule(
    request: Request,
    *,
    account_id: str,
    subject_id: str,
    speaker_class: Literal["owner", "guest", "uncertain"],
    topic: str,
    low_sensitivity_only: bool,
) -> PersonaCapsule:
    """The persona of the turn's confirmed subject, never another person's.

    The account-keyed learner only learns from turns whose subject is the login
    account (archive ``_schedule_persona_observation``), so its traits are that
    account holder's style and answer only while the account is the subject.
    Any other subject -- a child or elder on a one-to-one device, a household
    member -- reads its own projected persona; without an active projection it
    gets an empty capsule, never the account holder's traits.
    """

    if subject_id == account_id:
        engine = cast(PersonaEnginePort, request.app.state.persona_engine)
        return await engine.capsule(
            PersonaRequest(
                account_id=account_id,
                speaker_class=speaker_class,
                topic=topic,
                max_chars=1200,
                confirmed_style_only=low_sensitivity_only,
            )
        )
    # The same speaker gate the account-side engine applies: an owner gets the
    # full capsule, an uncertain speaker only confirmed low-sensitivity style.
    style_only = speaker_class == "uncertain" and low_sensitivity_only
    if speaker_class != "owner" and not style_only:
        return PersonaCapsule()
    settings = cast(ControlSettings, request.app.state.settings)
    projected = await asyncio.to_thread(read_active_version, settings.memoria_db_path, subject_id)
    if projected is None:
        return PersonaCapsule()
    return persona_capsule_from_snapshot(
        projected["snapshot"],
        version_id=str(projected["version_id"]),
        version_number=int(projected["version_number"]),
        topic=topic,
        max_chars=1200,
        confirmed_style_only=style_only,
    )
