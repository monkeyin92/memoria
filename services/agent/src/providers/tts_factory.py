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
from dataclasses import replace
from typing import Any

from services.agent.src.aigc_generation_log import AigcTtsDescriptor
from services.agent.src.generation_output_policy import frozen_companion_clone_permitted
from services.agent.src.providers.fixed_safety_audio import wrap_fixed_safety_tts
from services.common.tts_defaults import (
    DOUBAO_TTS_FALLBACK_SPEAKER,
    DOUBAO_TTS_RESOURCE_ID,
    STEPFUN_TTS_MODEL,
    STEPFUN_TTS_VOICE,
)
from services.common.tts_voice_policy import is_clone_or_icl_voice, is_male_speaker

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


def doubao_fallback_speaker(settings: Any) -> str:
    """Voice for the Doubao backup in main/backup mode; product default wins."""

    configured = str(getattr(settings, "doubao_tts_fallback_speaker", "") or "").strip()
    return configured or DOUBAO_TTS_FALLBACK_SPEAKER


def build_doubao_backup(settings: Any) -> Any:
    """Doubao backup for main/backup mode; it must not use the pure-Doubao voice.

    The pure-Doubao default (``warm_companion``) stays untouched for
    ``TTS_PROVIDER=doubao``. The backup keeps the designed fallback voice and
    never enables style/emotion control, even if the environment asks for it.
    A clone/ICL identity is rejected instead of silently reaching the device.
    """

    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig

    speaker = doubao_fallback_speaker(settings)
    if is_clone_or_icl_voice(
        voice=speaker,
        voice_kind="designed",
        resource_id=str(
            getattr(settings, "doubao_tts_resource_id", DOUBAO_TTS_RESOURCE_ID)
        ),
        provider="volcengine_doubao",
    ):
        raise ValueError(
            "DOUBAO_TTS_FALLBACK_SPEAKER must be a designed preset voice, "
            "not a clone/ICL voice"
        )
    if is_male_speaker(speaker):
        raise ValueError(
            "DOUBAO_TTS_FALLBACK_SPEAKER must be the product's female backup "
            "voice, not a male preset"
        )
    config = replace(
        DoubaoTTSConfig.from_env(),
        speaker=speaker,
        style_control_enabled=False,
        instruction=None,
    )
    return DoubaoTTS(config)


def _stepfun_setting(settings: Any, name: str, default: str) -> str:
    value = getattr(settings, name, None)
    if value is None:
        return default
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{name.upper()} must not be blank")
    return cleaned


def _stepfun_tts(settings: Any) -> Any:
    from services.agent.src.providers.stepfun_tts import StepFunTTS, StepFunTTSConfig

    config = replace(
        StepFunTTSConfig.from_env(),
        model=_stepfun_setting(settings, "stepfun_tts_model", STEPFUN_TTS_MODEL),
        voice=_stepfun_setting(settings, "stepfun_tts_voice", STEPFUN_TTS_VOICE),
    )
    return StepFunTTS(config)


def tts_runtime_descriptor(settings: Any) -> AigcTtsDescriptor:
    """Vendor/engine facts for the AIGC round log; no text, no secrets."""

    provider = _active_provider(settings)
    if provider == "stepfun_primary":
        return AigcTtsDescriptor(
            provider="stepfun",
            engine=str(getattr(settings, "stepfun_tts_model", STEPFUN_TTS_MODEL)),
            voice=str(getattr(settings, "stepfun_tts_voice", STEPFUN_TTS_VOICE)),
            fallback_provider="volcengine_doubao",
            fallback_engine=str(
                getattr(settings, "doubao_tts_resource_id", DOUBAO_TTS_RESOURCE_ID)
            ),
            fallback_voice=doubao_fallback_speaker(settings),
        )
    return AigcTtsDescriptor(
        provider="volcengine_doubao",
        engine=str(getattr(settings, "doubao_tts_resource_id", DOUBAO_TTS_RESOURCE_ID)),
        voice=str(getattr(settings, "doubao_tts_voice_profile", "")),
    )


def build_tts(settings: Any) -> Any:
    """The runtime synthesizer with pre-synthesized fixed-safety audio in front.

    Every provider goes through the wrapper.  Text that matches an approved
    template plays its stored clip; anything else reaches the same provider and
    fallback chain as before.
    """

    return wrap_fixed_safety_tts(_build_realtime_tts(settings), settings)


def _build_realtime_tts(settings: Any) -> Any:
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
        from services.agent.src.providers.tts_failover import (
            PROCESS_TTS_BREAKER,
            StepFunFailoverConfig,
            StepFunPrimaryTTS,
        )

        failover_config = StepFunFailoverConfig.from_env()
        failover_config = replace(
            failover_config,
            fixed_safety_primary_first=bool(
                getattr(
                    settings,
                    "fixed_safety_primary_first",
                    failover_config.fixed_safety_primary_first,
                )
            ),
        )
        PROCESS_TTS_BREAKER.configure(failover_config)
        return StepFunPrimaryTTS(
            primary=_stepfun_tts(settings),
            backup=build_doubao_backup(settings),
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
    "build_doubao_backup",
    "build_tts",
    "doubao_fallback_speaker",
    "tts_runtime_descriptor",
    "close_tts",
    "tts_provider_label",
    "wants_clone_tts",
    "warm_tts",
]
