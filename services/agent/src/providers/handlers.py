"""Construction seam for the production remote voice providers."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from services.agent.src.providers.open_meteo_weather import (
    OpenMeteoWeather,
    is_weather_query,
)
from services.agent.src.providers.qwen_realtime_search import (
    QwenRealtimeSearch,
    QwenRealtimeSearchConfig,
)

if TYPE_CHECKING:
    from services.agent.src.config import AgentSettings

logger = logging.getLogger(__name__)


class PublicRealtimeSearch:
    """Route weather to a deterministic public API and other topics to Qwen."""

    def __init__(
        self,
        *,
        weather: OpenMeteoWeather,
        qwen: QwenRealtimeSearch | None,
    ) -> None:
        self.weather = weather
        self.qwen = qwen
        self.model = (
            f"{weather.model}+{qwen.model}" if qwen is not None else weather.model
        )

    async def resolve(self, *, query: str) -> str | None:
        if is_weather_query(query):
            weather = await self.weather.resolve(query=query)
            if weather:
                return weather
        if self.qwen is None:
            return None
        return await self.qwen.resolve(query=query)

    async def aclose(self) -> None:
        await self.weather.aclose()
        if self.qwen is not None:
            await self.qwen.aclose()


def build_language_model_handler(
    *,
    settings: AgentSettings,
    llm_factory: Callable[..., Any],
) -> Any:
    """Build the shared cascade LLM without constructing transport-specific providers."""

    extra_body: dict[str, Any] = {
        "max_tokens": int(os.getenv("DEEPSEEK_FAST_MAX_TOKENS", "240")),
    }
    if settings.llm_provider == "deepseek":
        extra_body["thinking"] = {"type": "disabled"}
    else:
        extra_body["enable_thinking"] = False
        extra_body["thinking"] = {"type": "disabled"}
    return llm_factory(
        model=settings.llm_fast_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=float(os.getenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")),
        tool_choice="auto",
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body=extra_body,
    )


def build_realtime_search_resolver(*, settings: AgentSettings) -> PublicRealtimeSearch:
    """Build public live lookup without changing the conversational LLM."""

    api_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
    qwen = None
    if api_key:
        qwen = QwenRealtimeSearch(
            QwenRealtimeSearchConfig(
                api_key=api_key,
                base_url=str(
                    getattr(
                        settings,
                        "dashscope_compatible_base_url",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    )
                ),
                model=str(getattr(settings, "qwen_deep_model", "qwen-plus")),
            )
        )
    return PublicRealtimeSearch(weather=OpenMeteoWeather(), qwen=qwen)
