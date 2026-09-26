"""Which persona a companion turn may use: the confirmed subject's own (P1-03)."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import Request

from services.persona.domain import PersonaCapsule, PersonaEnginePort, PersonaRequest


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

    The persona follows the person using the device: the engine learns and
    stores it per (account, subject), so a child or elder on a one-to-one
    device gets their own persona and never the account holder's. The engine
    applies the same speaker gate on either side; reading another subject's
    persona is gated by the caller's memory scope (the binding's long-term
    memory grant), not by the account holder's persona consent.
    """

    engine = cast(PersonaEnginePort, request.app.state.persona_engine)
    return await engine.capsule(
        PersonaRequest(
            account_id=account_id,
            speaker_class=speaker_class,
            topic=topic,
            max_chars=1200,
            confirmed_style_only=speaker_class == "uncertain" and low_sensitivity_only,
            subject_id=None if subject_id == account_id else subject_id,
        )
    )
