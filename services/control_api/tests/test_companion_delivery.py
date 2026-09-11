"""Which companion a frozen session speaks and sounds as.

A personal clone is bound by the **persona**, not by the legacy ``bio``
marker: a built-in companion carries its own designed voice, and only a custom
persona owns a clone.  The resolution is asked for that persona's dimension, so
one custom persona's clone can never stand in for another's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.common.companions import COMPANIONS, CompanionDefinition
from services.control_api.app.companion_delivery import freeze_companion_delivery
from services.control_api.app.mode_policy import companion_personal_voice_contract_valid
from services.voice_profile.domain import VoiceResolution

_CUSTOM_PERSONA_ID = "cu_deadbeefdeadbeef"

#: A custom persona carrying taoxi's designed voice as its fallback.
_CUSTOM = CompanionDefinition(
    companion_id=_CUSTOM_PERSONA_ID,
    display_name="小北",
    style_description="说话短一点，像朋友",
    warmth="warm",
    directness="gentle",
    response_length="brief",
    question_frequency="rare",
    interview_depth="light",
    designed_voice_profile=COMPANIONS["taoxi"].designed_voice_profile,
    welcome_text="嗨，我在。",
    conversation_instruction="说话短一点，像朋友。",
    voice_instruction="轻松自然",
    default_voice_emotion="neutral",
    default_voice_rate=1.0,
)


class _AdultStore:
    def get_subject_profile(self, *, user_id: str) -> dict[str, str]:
        assert user_id
        return {"subject_category": "adult"}


class _MinorStore:
    def get_subject_profile(self, *, user_id: str) -> dict[str, str]:
        assert user_id
        return {"subject_category": "minor"}


class _ResolutionStub:
    def __init__(self, resolution: VoiceResolution) -> None:
        self.resolution = resolution
        self.seen_personas: list[str | None] = []

    async def resolve(
        self, *, account_id: str, custom_persona_id: str | None = None
    ) -> VoiceResolution:
        assert account_id
        self.seen_personas.append(custom_persona_id)
        return self.resolution


def _cosyvoice_resolution() -> VoiceResolution:
    return VoiceResolution(
        mode="active",
        profile_id="voice-profile-personal",
        version_number=2,
        provider="alibaba_model_studio",
        voice_kind="personal",
        model="cosyvoice-v3.5-flash",
        resource_id="cosyvoice-v3.5-flash",
        voice_id="cosyvoice-v3.5-flash-clone-owner001",
    )


@pytest.mark.asyncio
async def test_catalog_companion_does_not_freeze_a_personal_clone() -> None:
    manager = _ResolutionStub(_cosyvoice_resolution())
    frozen = await freeze_companion_delivery(
        companion=COMPANIONS["taoxi"],
        session_focus="chat",
        account_id="owner-a",
        store=_AdultStore(),
        voice_manager=manager,
    )

    assert frozen.companion_style_id == "taoxi"
    assert frozen.voice_profile_id is None
    assert companion_personal_voice_contract_valid(frozen) is False
    # A built-in companion never reaches for a clone at all.
    assert manager.seen_personas == []


@pytest.mark.asyncio
async def test_custom_persona_freezes_a_cosyvoice_clone_with_designed_fallback() -> None:
    manager = _ResolutionStub(_cosyvoice_resolution())
    frozen = await freeze_companion_delivery(
        companion=_CUSTOM,
        session_focus="chat",
        account_id="owner-a",
        store=_AdultStore(),
        voice_manager=manager,
    )

    assert companion_personal_voice_contract_valid(frozen) is True
    assert frozen.voice_provider == "alibaba_model_studio"
    assert frozen.voice_model == "cosyvoice-v3.5-flash"
    assert frozen.fallback_voice_profile_id == "bright_peer"
    assert frozen.fallback_voice_model == "seed-tts-2.0"
    # The clone is looked up in this persona's dimension, not the account's.
    assert manager.seen_personas == [_CUSTOM_PERSONA_ID]


@pytest.mark.asyncio
async def test_custom_persona_freezes_a_doubao_icl_clone() -> None:
    expires = datetime(2027, 7, 23, tzinfo=UTC)
    frozen = await freeze_companion_delivery(
        companion=_CUSTOM,
        session_focus="chat",
        account_id="owner-a",
        store=_AdultStore(),
        voice_manager=_ResolutionStub(
            VoiceResolution(
                mode="active",
                profile_id="voice-profile-personal",
                version_number=3,
                provider="volcengine_doubao",
                voice_kind="personal",
                model="seed-icl-2.0",
                resource_id="seed-icl-2.0",
                voice_id="S_personal_custom",
                provider_expires_at=expires,
            )
        ),
    )

    assert companion_personal_voice_contract_valid(frozen) is True
    assert frozen.voice_provider == "volcengine_doubao"
    assert frozen.voice_model == "seed-icl-2.0"
    assert frozen.voice_provider_expires_at == expires.isoformat()


@pytest.mark.asyncio
async def test_minor_keeps_designed_companion_voice() -> None:
    frozen = await freeze_companion_delivery(
        companion=_CUSTOM,
        session_focus="chat",
        account_id="child-a",
        store=_MinorStore(),
        voice_manager=_ResolutionStub(_cosyvoice_resolution()),
    )

    assert frozen.voice_profile_id is None
    assert companion_personal_voice_contract_valid(frozen) is False
