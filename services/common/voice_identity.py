"""Single source of truth for the synthesis provider identity.

Every voice reference that is frozen into a session policy, checked by the
Agent, or written into delivery provenance names this provider/model pair.
Designed (persona) voices are Qwen-Audio system voices; personal voices are
clones enrolled with ``target_model`` set to the same model, so both share
one synthesis model and one DashScope SpeechSynthesizer connection pool.

Doubao seed-tts / seed-icl and CosyVoice v3.5 identities are legacy: they
remain readable in stored history but are never issued for new sessions.
"""

from __future__ import annotations

from typing import Final

TTS_PROVIDER: Final = "alibaba_model_studio"
TTS_MODEL: Final = "qwen-audio-3.1-tts-flash"
# Clones enroll against the synthesis model (DashScope binds a cloned voice
# to its target_model); the resource id mirrors the model for provenance.
PERSONAL_VOICE_MODEL: Final = TTS_MODEL
# Enrolled voice ids are "{target_model}-{prefix}-{unique id}".
PERSONAL_VOICE_ID_PREFIX: Final = f"{TTS_MODEL}-"

LEGACY_TTS_PROVIDERS: Final = frozenset({"volcengine_doubao"})
LEGACY_TTS_MODELS: Final = frozenset({"seed-tts-2.0", "seed-icl-2.0"})
LEGACY_TTS_MODEL_PREFIXES: Final = ("cosyvoice-v3.5-",)


def is_current_voice_identity(provider: object, model: object, resource_id: object) -> bool:
    return provider == TTS_PROVIDER and model == TTS_MODEL and resource_id == TTS_MODEL


def is_personal_voice_id(voice_id: object) -> bool:
    return (
        isinstance(voice_id, str)
        and voice_id.startswith(PERSONAL_VOICE_ID_PREFIX)
        and len(voice_id) > len(PERSONAL_VOICE_ID_PREFIX)
    )


def is_legacy_voice_model(model: object) -> bool:
    return isinstance(model, str) and (
        model in LEGACY_TTS_MODELS or model.startswith(LEGACY_TTS_MODEL_PREFIXES)
    )
