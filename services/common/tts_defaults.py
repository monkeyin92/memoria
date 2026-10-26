"""Shared TTS defaults that both runtime config and provider adapters read.

Keeping these in one place makes ``.env.example``, ``AgentSettings`` and the
vendor adapters agree on the same model and voice without duplicating literals.
"""

from __future__ import annotations

from typing import Final

from services.common.companions import CHILD_STORY_SPEAKER

STEPFUN_TTS_MODEL: Final = "stepaudio-2.5-tts"
STEPFUN_TTS_VOICE: Final = "wenrounansheng"
STEPFUN_PROVIDER: Final = "stepfun"
STEPFUN_ARCHIVE_MODELS: Final[frozenset[str]] = frozenset(
    {STEPFUN_TTS_MODEL, "stepaudio-3-tts"}
)
DOUBAO_TTS_RESOURCE_ID: Final = "seed-tts-2.0"
#: Designed female voice for the Doubao backup in StepFun-primary mode. Product
#: chose it on 2026-10-20 19:09; operators may still override the env var. It is
#: deliberately not the pure-Doubao warm_companion default.
DOUBAO_TTS_FALLBACK_SPEAKER: Final = CHILD_STORY_SPEAKER

__all__ = [
    "DOUBAO_TTS_FALLBACK_SPEAKER",
    "DOUBAO_TTS_RESOURCE_ID",
    "STEPFUN_ARCHIVE_MODELS",
    "STEPFUN_PROVIDER",
    "STEPFUN_TTS_MODEL",
    "STEPFUN_TTS_VOICE",
]
