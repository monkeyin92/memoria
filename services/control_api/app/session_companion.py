"""Resolve the companion a session should speak and sound as.

The account's own companion (``profiles.companion_id``) is what a non-device
session still uses, but a device conversation must follow the **subject**, not
the account: the signed Runtime Profile names the persona the active subject
was assigned, and that persona carries the designed voice that goes with it.
Reading only the account field is what pinned every subject on a device to one
voice.

``self_preview`` and ``legacy`` deliberately do not call this -- those modes do
not switch by subject, so their callers keep resolving the account field.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.common.companions import (
    DEFAULT_COMPANION_ID,
    CompanionDefinition,
    companion_definition,
)
from services.identity.domain import CustomPersonaRecord, IdentityNotFoundError
from services.identity.service import IdentityService
from services.persona.custom_persona_fields import custom_persona_definition

#: Custom persona ids share the catalogue namespace behind this prefix.
CUSTOM_PERSONA_PREFIX = "cu_"


def persona_id_from_runtime_profile(runtime_profile: object | None) -> str | None:
    """The persona a signed Runtime Profile names, if it names one."""
    persona = getattr(runtime_profile, "persona", None)
    persona_id = getattr(persona, "persona_id", None)
    if isinstance(persona_id, str) and persona_id:
        return persona_id
    return None


async def _own_custom_persona(
    identity: IdentityService | None,
    *,
    account_id: str,
    persona_id: str,
) -> CustomPersonaRecord | None:
    """The account's own record, or ``None`` when it cannot be read.

    A caller without the identity authority wired -- a media-only app, say --
    gets the account default rather than a hard failure.  Custom personas are an
    enrichment here, and the media path never required identity before.
    """
    if identity is None:
        return None
    try:
        return await identity.get_custom_persona(
            persona_id,
            owner_person_id=account_id,
            actor_person_id=account_id,
        )
    except IdentityNotFoundError:
        return None


async def session_companion(
    *,
    identity: IdentityService | None,
    account_id: str,
    profile_row: Mapping[str, Any] | None,
    runtime_profile: object | None,
) -> CompanionDefinition | None:
    """Subject persona first, then the account's companion, then the default.

    A built-in id resolves straight from the catalogue.  A custom persona needs
    the owner's record, which is why this is async.  A persona this account
    cannot read falls back instead of failing the session: the account field is
    the documented default, so degrading to it keeps the conversation alive
    without ever borrowing someone else's voice.
    """
    persona_id = persona_id_from_runtime_profile(runtime_profile)
    if persona_id is not None:
        builtin = companion_definition(persona_id)
        if builtin is not None:
            return builtin
        if persona_id.startswith(CUSTOM_PERSONA_PREFIX):
            record = await _own_custom_persona(
                identity, account_id=account_id, persona_id=persona_id
            )
            if record is not None:
                return custom_persona_definition(record)
    name = (profile_row or {}).get("companion_id")
    return companion_definition(name or DEFAULT_COMPANION_ID)


def custom_persona_id_or_none(companion_id: object) -> str | None:
    """The id itself when it names a custom persona, else ``None``."""
    if isinstance(companion_id, str) and companion_id.startswith(CUSTOM_PERSONA_PREFIX):
        return companion_id
    return None


__all__ = [
    "CUSTOM_PERSONA_PREFIX",
    "custom_persona_id_or_none",
    "persona_id_from_runtime_profile",
    "session_companion",
]
