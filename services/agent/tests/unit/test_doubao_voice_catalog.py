from __future__ import annotations

import json

from services.agent.src.providers.doubao_voice_catalog import (
    DOUBAO_TTS_MODEL,
    DOUBAO_VOICE_CATALOG,
    catalog_by_id,
    resolve_approved_voice,
)
from services.common.companions import DESIGNED_VOICE_SPEAKERS


def test_all_companion_voices_are_unique_and_approved() -> None:
    assert len(DOUBAO_VOICE_CATALOG) == 5
    assert len({voice.profile_id for voice in DOUBAO_VOICE_CATALOG}) == 5
    assert len({voice.speaker_id for voice in DOUBAO_VOICE_CATALOG}) == 5
    for voice in DOUBAO_VOICE_CATALOG:
        assert resolve_approved_voice(profile_id=voice.profile_id) == voice.speaker_id


def test_registry_must_match_reviewed_catalog(tmp_path) -> None:
    registry = tmp_path / "voices.json"
    registry.write_text(
        json.dumps(
            {
                "provider": "doubao",
                "model": DOUBAO_TTS_MODEL,
                "voices": {
                    "warm_companion": {
                        "speaker_id": "unreviewed-voice",
                        "model": DOUBAO_TTS_MODEL,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert (
        resolve_approved_voice(
            profile_id="warm_companion",
            registry_path=registry,
        )
        is None
    )


def test_catalog_covers_stable_profile_keys() -> None:
    assert set(catalog_by_id()) == {
        "warm_companion",
        "bright_peer",
        "soft_confidante",
        "calm_guide",
        "low_magnetic",
    }


def test_catalog_reuses_the_common_canonical_speaker_mapping() -> None:
    assert {
        voice.profile_id: voice.speaker_id for voice in DOUBAO_VOICE_CATALOG
    } == DESIGNED_VOICE_SPEAKERS
