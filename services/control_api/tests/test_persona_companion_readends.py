"""T-D acceptance: which companion a session resolves.

The signed Runtime Profile's subject-level persona wins; the account's own
companion is only the default.  No step here may fail a session -- every
unresolvable case falls back rather than raising, because the account field is
the documented default and degrading to it never borrows another account's
voice.

The read ends themselves (``session.py`` / ``media.py``) are covered by
``test_device_onboarding_api.py``; this file pins the resolver's ladder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from services.common.companions import COMPANIONS, DEFAULT_COMPANION_ID
from services.control_api.app.session_companion import session_companion
from services.identity.domain import CustomPersonaRecord, IdentityNotFoundError


def _runtime_profile(persona_id: str | None) -> Any:
    persona = (
        None
        if persona_id is None
        else SimpleNamespace(
            persona_id=persona_id, version=1, relationship_stage="new"
        )
    )
    return SimpleNamespace(persona=persona)


def _custom_record(persona_id: str, *, owner_person_id: str) -> CustomPersonaRecord:
    return CustomPersonaRecord(
        persona_id=persona_id,
        owner_person_id=owner_person_id,
        display_name="奶奶的伙伴",
        style_description="沉稳的陪伴者",
        warmth="calm",
        directness="gentle",
        response_length="balanced",
        question_frequency="occasional",
        interview_depth="light",
        welcome_text="我在这儿。",
        conversation_instruction="多问候身体，愿意听过去的事。",
        voice_instruction="沉稳清晰，语速适中。",
        default_voice_emotion="neutral",
        default_voice_rate=1.0,
        fallback_designed_voice="xuanmo",
        persona_version=1,
        source="user_created",
        created_at=datetime.now(UTC),
    )


class _Identity:
    """Stand-in for the identity facade: only what the resolver reads."""

    def __init__(self, records: dict[str, CustomPersonaRecord] | None = None) -> None:
        self._records = records or {}
        self.calls: list[str] = []

    async def get_custom_persona(
        self,
        persona_id: str,
        *,
        owner_person_id: str,
        actor_person_id: str | None = None,
    ) -> CustomPersonaRecord:
        del actor_person_id
        self.calls.append(persona_id)
        record = self._records.get(persona_id)
        if record is None or record.owner_person_id != owner_person_id:
            raise IdentityNotFoundError(persona_id)
        return record


@pytest.mark.asyncio
async def test_builtin_persona_wins_and_never_consults_identity() -> None:
    identity = _Identity()
    resolved = await session_companion(
        identity=identity,
        account_id="acct",
        profile_row={"companion_id": "xuanmo"},
        runtime_profile=_runtime_profile("taoxi"),
    )
    assert resolved is not None
    assert resolved.companion_id == "taoxi"
    assert identity.calls == []


@pytest.mark.asyncio
async def test_custom_persona_resolves_through_the_owners_record() -> None:
    record = _custom_record("cu_deadbeefdeadbeef", owner_person_id="acct")
    identity = _Identity({"cu_deadbeefdeadbeef": record})
    resolved = await session_companion(
        identity=identity,
        account_id="acct",
        profile_row={"companion_id": "taoxi"},
        runtime_profile=_runtime_profile("cu_deadbeefdeadbeef"),
    )
    assert resolved is not None
    assert resolved.companion_id == "cu_deadbeefdeadbeef"
    # The custom persona's declared fallback decides the designed voice.
    assert (
        resolved.designed_voice_profile
        == COMPANIONS["xuanmo"].designed_voice_profile
    )
    assert identity.calls == ["cu_deadbeefdeadbeef"]


@pytest.mark.asyncio
async def test_another_accounts_custom_persona_falls_back_to_the_default() -> None:
    record = _custom_record("cu_ffeeddccffeeddcc", owner_person_id="someone-else")
    identity = _Identity({"cu_ffeeddccffeeddcc": record})
    resolved = await session_companion(
        identity=identity,
        account_id="acct",
        profile_row={"companion_id": "taoxi"},
        runtime_profile=_runtime_profile("cu_ffeeddccffeeddcc"),
    )
    assert resolved is not None
    assert resolved.companion_id == "taoxi"


@pytest.mark.asyncio
async def test_absent_persona_uses_the_account_companion() -> None:
    resolved = await session_companion(
        identity=_Identity(),
        account_id="acct",
        profile_row={"companion_id": "mianmian"},
        runtime_profile=_runtime_profile(None),
    )
    assert resolved is not None
    assert resolved.companion_id == "mianmian"


@pytest.mark.asyncio
async def test_missing_runtime_profile_uses_the_account_companion() -> None:
    resolved = await session_companion(
        identity=_Identity(),
        account_id="acct",
        profile_row={"companion_id": "axu"},
        runtime_profile=None,
    )
    assert resolved is not None
    assert resolved.companion_id == "axu"


@pytest.mark.asyncio
async def test_no_identity_authority_still_resolves_builtins_but_not_custom() -> None:
    """A media-only app has no identity facade; it must not hard-fail."""
    builtin = await session_companion(
        identity=None,
        account_id="acct",
        profile_row={"companion_id": "taoxi"},
        runtime_profile=_runtime_profile("mianmian"),
    )
    assert builtin is not None
    assert builtin.companion_id == "mianmian"

    custom = await session_companion(
        identity=None,
        account_id="acct",
        profile_row={"companion_id": "taoxi"},
        runtime_profile=_runtime_profile("cu_deadbeefdeadbeef"),
    )
    assert custom is not None
    assert custom.companion_id == "taoxi"


@pytest.mark.asyncio
async def test_empty_account_row_falls_back_to_the_shipped_default() -> None:
    resolved = await session_companion(
        identity=_Identity(),
        account_id="acct",
        profile_row=None,
        runtime_profile=None,
    )
    assert resolved is not None
    assert resolved.companion_id == DEFAULT_COMPANION_ID
