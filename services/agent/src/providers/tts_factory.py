"""The one place that turns settings and session policy into a TTS provider.

``TTS_PROVIDER`` selects the runtime synthesizer; the Voice Core media session
factory builds it here instead of naming a vendor class. A frozen companion clone voice hosted on Alibaba Model
Studio is synthesized by CosyVoice; that choice is made here as well, from
the session's mode policy.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from services.agent.src.generation_output_policy import frozen_companion_clone_permitted

logger = logging.getLogger(__name__)

# The label each provider reports to the response planner and telemetry.
_PROVIDER_LABELS: Mapping[str, str] = {
    "doubao": "volcengine_doubao",
    "stepfun_primary": "stepfun",
}

CLONE_VOICE_PROVIDER = "alibaba_model_studio"


def _stepfun_enabled(settings: Any) -> bool:
    value = getattr(settings, "stepfun_tts_enabled", None)
    if isinstance(value, bool):
        return value
    raw = os.environ.get("STEPFUN_TTS_ENABLED", "false")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _active_provider(settings: Any) -> str:
    provider = str(getattr(settings, "tts_provider", "doubao"))
    if provider == "stepfun_primary" and not _stepfun_enabled(settings):
        return "doubao"
    return provider


def tts_provider_label(settings: Any) -> str:
    provider = _active_provider(settings)
    try:
        return _PROVIDER_LABELS[provider]
    except KeyError:
        raise ValueError(f"unsupported TTS_PROVIDER: {provider!r}") from None


def build_tts(settings: Any) -> Any:
    """The runtime synthesizer named by ``TTS_PROVIDER`` and its enable gate."""

    provider = _active_provider(settings)
    if provider == "doubao":
        if str(getattr(settings, "tts_provider", "doubao")) == "stepfun_primary":
            logger.warning(
                "TTS_PROVIDER=stepfun_primary but STEPFUN_TTS_ENABLED is false; "
                "using the existing Doubao provider"
            )
        from services.agent.src.providers.doubao_tts import DoubaoTTS

        return DoubaoTTS.from_env()
    if provider == "stepfun_primary":
        from services.agent.src.providers.doubao_tts import DoubaoTTS
        from services.agent.src.providers.stepfun_tts import StepFunTTS
        from services.agent.src.providers.tts_failover import (
            PROCESS_TTS_BREAKER,
            StepFunFailoverConfig,
            StepFunPrimaryTTS,
        )

        failover_config = StepFunFailoverConfig.from_env()
        PROCESS_TTS_BREAKER.configure(failover_config)
        return StepFunPrimaryTTS(
            primary=StepFunTTS.from_env(),
            backup=DoubaoTTS.from_env(),
            config=failover_config,
            breaker=PROCESS_TTS_BREAKER,
        )
    raise ValueError(f"unsupported TTS_PROVIDER: {provider!r}")


def wants_clone_tts(mode_policy: Any) -> bool:
    """True when this session speaks with a frozen Model Studio clone voice."""

    references = dict(getattr(mode_policy, "references", ()) or ())
    return references.get("voice_provider") == CLONE_VOICE_PROVIDER and bool(
        frozen_companion_clone_permitted(mode_policy)
    )


def build_clone_tts() -> Any:
    from services.agent.src.providers.cosyvoice_tts import CosyVoiceTTS

    return CosyVoiceTTS.from_env()


async def warm_tts(tts: Any) -> None:
    """Open the provider pool early; a failure only means it opens on demand."""

    warm = getattr(getattr(tts, "pool", None), "warm", None)
    if not callable(warm):
        return
    try:
        await warm()
    except Exception as exc:
        logger.warning(
            "TTS pool warm failed; it opens on first use provider=%s error=%s",
            type(tts).__name__,
            type(exc).__name__,
        )


async def close_tts(tts: Any) -> None:
    close = getattr(tts, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except Exception as exc:
        logger.warning(
            "TTS close failed provider=%s error=%s", type(tts).__name__, type(exc).__name__
        )


__all__ = [
    "CLONE_VOICE_PROVIDER",
    "build_clone_tts",
    "build_tts",
    "close_tts",
    "tts_provider_label",
    "wants_clone_tts",
    "warm_tts",
]
