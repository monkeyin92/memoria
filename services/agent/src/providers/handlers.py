"""Construction seam for the production remote voice providers."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from services.agent.src.config import AgentSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceProviderHandlers:
    """LiveKit-compatible provider adapters used by one cascade session."""

    asr: Any
    language_model: Any
    speech_synthesis: Any
    realtime_search_resolver: Any | None = None
    realtime_search_model: str | None = None


async def build_voice_provider_handlers(
    *,
    settings: AgentSettings,
    llm_factory: Callable[..., Any],
    asr_factory: Callable[[], Any] | None = None,
    tts_factory: Callable[[], Any] | None = None,
    realtime_search_factory: Callable[..., Any] | None = None,
) -> VoiceProviderHandlers:
    """Build all remote provider adapters without exposing vendor setup to entrypoint."""

    if asr_factory is None:
        from services.agent.src.providers.funasr_stt import FunASRSTT

        asr_factory = FunASRSTT.from_env
    if tts_factory is None:
        from services.agent.src.providers.doubao_tts import DoubaoTTS

        tts_factory = DoubaoTTS.from_env

    asr = asr_factory()
    speech_synthesis = tts_factory()
    warm = getattr(getattr(speech_synthesis, "pool", None), "warm", None)
    if callable(warm):
        try:
            await warm()
        except Exception as exc:
            logger.warning("Doubao TTS pool warm failed (will open on demand): %s", exc)

    extra_body: dict[str, Any] = {
        "thinking": {"type": "disabled"},
        "max_tokens": int(os.getenv("DEEPSEEK_FAST_MAX_TOKENS", "240")),
    }
    language_model = llm_factory(
        model=settings.llm_fast_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=float(os.getenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")),
        tool_choice="auto",
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body=extra_body,
    )
    realtime_search_resolver = None
    realtime_search_model = None
    if getattr(settings, "llm_provider", "qwen") == "qwen":
        from services.agent.src.providers.qwen_realtime_search import (
            QwenRealtimeSearch,
            QwenRealtimeSearchConfig,
        )

        realtime_search_model = str(getattr(settings, "qwen_deep_model", "qwen-plus"))
        if settings.llm_api_key.strip() and realtime_search_model.strip():
            factory = realtime_search_factory or QwenRealtimeSearch
            realtime_search_resolver = factory(
                QwenRealtimeSearchConfig(
                    api_key=settings.llm_api_key,
                    base_url=settings.llm_base_url,
                    model=realtime_search_model,
                )
            )
        else:
            logger.warning("Qwen realtime search disabled because credentials or model are missing")
    return VoiceProviderHandlers(
        asr=asr,
        language_model=language_model,
        speech_synthesis=speech_synthesis,
        realtime_search_resolver=realtime_search_resolver,
        realtime_search_model=realtime_search_model,
    )
