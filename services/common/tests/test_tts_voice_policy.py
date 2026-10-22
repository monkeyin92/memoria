"""Clone/ICL voices must never be selected on the preset voice path."""

from __future__ import annotations

import pytest
from services.common.tts_defaults import STEPFUN_TTS_VOICE
from services.common.tts_voice_policy import is_clone_or_icl_voice, is_male_speaker


@pytest.mark.parametrize(
    ("voice", "voice_kind", "resource_id", "provider"),
    [
        ("zh_female_kailangjiejie_uranus_bigtts", "designed", "seed-tts-2.0", "volcengine_doubao"),
        (STEPFUN_TTS_VOICE, "designed", "seed-tts-2.0", "stepfun"),
    ],
)
def test_designed_presets_are_allowed(
    voice: str, voice_kind: str, resource_id: str, provider: str
) -> None:
    assert not is_clone_or_icl_voice(
        voice=voice,
        voice_kind=voice_kind,
        resource_id=resource_id,
        provider=provider,
    )


@pytest.mark.parametrize(
    ("voice", "voice_kind", "resource_id", "provider"),
    [
        ("S_personal_synth_ready", "personal", "seed-icl-2.0", "volcengine_doubao"),
        ("S_personal_synth_ready", None, "seed-tts-2.0", None),
        ("designed-voice", None, "seed-icl-2.0", None),
        ("cosyvoice-clone-1", "designed", "cosyvoice-v3", "alibaba_model_studio"),
        ("cosyvoice-clone-1", "designed", "cosyvoice-v3", "cosyvoice"),
    ],
)
def test_clone_and_icl_identities_are_rejected(
    voice: str, voice_kind: str | None, resource_id: str | None, provider: str | None
) -> None:
    assert is_clone_or_icl_voice(
        voice=voice,
        voice_kind=voice_kind,
        resource_id=resource_id,
        provider=provider,
    )


def test_male_presets_are_never_the_companion_backup() -> None:
    assert is_male_speaker("zh_male_test_voice_uranus_bigtts") is True
    assert is_male_speaker("zh_female_kailangjiejie_uranus_bigtts") is False
