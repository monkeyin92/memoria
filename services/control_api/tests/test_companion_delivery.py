from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.common.companions import COMPANIONS
from services.control_api.app.companion_delivery import freeze_companion_delivery
from services.control_api.app.mode_policy import companion_personal_voice_contract_valid
from services.voice_profile.domain import VoiceResolution

_CUSTOM_BIO = "[memoria.custom_persona.v1]\nname: 小北\n---\n说话短一点，像朋友。"


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

    async def resolve(self, *, account_id: str) -> VoiceResolution:
        assert account_id
        return self.resolution


@pytest.mark.asyncio
async def test_catalog_companion_does_not_freeze_a_personal_clone() -> None:
    frozen = await freeze_companion_delivery(
        companion=COMPANIONS["taoxi"],
        session_focus="chat",
        bio="",
        account_id="owner-a",
        store=_AdultStore(),
        voice_manager=_ResolutionStub(
            VoiceResolution(
                mode="active",
                profile_id="voice-profile-personal",
                version_number=2,
                provider="alibaba_model_studio",
                voice_kind="personal",
                model="cosyvoice-v3.5-flash",
                resource_id="cosyvoice-v3.5-flash",
                voice_id="cosyvoice-v3.5-flash-clone-owner001",
            )
        ),
    )

    assert frozen.companion_style_id == "taoxi"
    assert frozen.voice_profile_id is None
    assert companion_personal_voice_contract_valid(frozen) is False


@pytest.mark.asyncio
async def test_custom_persona_freezes_a_cosyvoice_clone_with_designed_fallback() -> None:
    frozen = await freeze_companion_delivery(
        companion=COMPANIONS["taoxi"],
        session_focus="chat",
        bio=_CUSTOM_BIO,
        account_id="owner-a",
        store=_AdultStore(),
        voice_manager=_ResolutionStub(
            VoiceResolution(
                mode="active",
                profile_id="voice-profile-personal",
                version_number=2,
                provider="alibaba_model_studio",
                voice_kind="personal",
                model="cosyvoice-v3.5-flash",
                resource_id="cosyvoice-v3.5-flash",
                voice_id="cosyvoice-v3.5-flash-clone-owner001",
            )
        ),
    )

    assert companion_personal_voice_contract_valid(frozen) is True
    assert frozen.voice_provider == "alibaba_model_studio"
    assert frozen.voice_model == "cosyvoice-v3.5-flash"
    assert frozen.fallback_voice_profile_id == "bright_peer"
    assert frozen.fallback_voice_model == "seed-tts-2.0"


@pytest.mark.asyncio
async def test_custom_persona_freezes_a_doubao_icl_clone() -> None:
    expires = datetime(2027, 7, 23, tzinfo=UTC)
    frozen = await freeze_companion_delivery(
        companion=COMPANIONS["taoxi"],
        session_focus="chat",
        bio=_CUSTOM_BIO,
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
        companion=COMPANIONS["taoxi"],
        session_focus="chat",
        bio=_CUSTOM_BIO,
        account_id="child-a",
        store=_MinorStore(),
        voice_manager=_ResolutionStub(
            VoiceResolution(
                mode="active",
                profile_id="voice-profile-personal",
                version_number=2,
                provider="alibaba_model_studio",
                voice_kind="personal",
                model="cosyvoice-v3.5-flash",
                resource_id="cosyvoice-v3.5-flash",
                voice_id="cosyvoice-v3.5-flash-clone-owner001",
            )
        ),
    )

    assert frozen.voice_profile_id is None
    assert companion_personal_voice_contract_valid(frozen) is False
