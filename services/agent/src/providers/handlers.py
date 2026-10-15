"""Construction seam for the production remote voice providers."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from services.agent.src.providers.llm_failover import FailoverChatModel
from services.agent.src.providers.open_meteo_weather import (
    OpenMeteoWeather,
    is_weather_query,
)
from services.agent.src.providers.qwen_realtime_search import (
    QwenRealtimeSearch,
    QwenRealtimeSearchConfig,
)
from services.common.llm_failover import DASHSCOPE_CN_COMPATIBLE_BASE_URL
from services.common.llm_thinking import thinking_disabled, thinking_mode_for

if TYPE_CHECKING:
    from services.agent.src.config import AgentSettings


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


def _reply_extra_body(*, llm_provider: str) -> dict[str, Any]:
    extra_body: dict[str, Any] = {
        "max_tokens": int(os.getenv("DEEPSEEK_FAST_MAX_TOKENS", "240")),
    }
    extra_body.update(thinking_disabled(thinking_mode_for(llm_provider)))
    if thinking_mode_for(llm_provider) == "dashscope":
        # qwen3.8-flash defaults to thinking; keep both keys for Bailian gateways.
        extra_body["enable_thinking"] = False
        extra_body["thinking"] = {"type": "disabled"}
    return extra_body


def _build_chat_model(
    *,
    llm_factory: Callable[..., Any],
    model: str,
    api_key: str,
    base_url: str,
    llm_provider: str,
) -> Any:
    return llm_factory(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=float(os.getenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")),
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body=_reply_extra_body(llm_provider=llm_provider),
    )


def build_language_model_handler(
    *,
    settings: AgentSettings,
    llm_factory: Callable[..., Any],
) -> Any:
    """Build the session's streaming chat model (``OpenAIChatModel`` in production).

    No tools are offered to the reply model, so no ``tool_choice`` is sent.
    When ``LLM_FAILOVER_ENABLED`` is on, wrap the primary with a Beijing
    DashScope backup (``qwen3.8-flash``, thinking off).
    """

    primary = _build_chat_model(
        llm_factory=llm_factory,
        model=settings.llm_fast_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        llm_provider=settings.llm_provider,
    )
    if not settings.llm_failover_enabled:
        return primary
    backup = _build_chat_model(
        llm_factory=llm_factory,
        model=settings.llm_backup_model,
        api_key=settings.dashscope_api_key,
        base_url=DASHSCOPE_CN_COMPATIBLE_BASE_URL,
        llm_provider="qwen",
    )
    return FailoverChatModel(
        primary=primary,
        backup=backup,
        first_token_timeout_s=settings.llm_first_token_timeout_s,
        inter_token_timeout_s=settings.llm_inter_token_timeout_s,
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
                        DASHSCOPE_CN_COMPATIBLE_BASE_URL,
                    )
                ),
                model=str(getattr(settings, "qwen_deep_model", "qwen-plus")),
            )
        )
    return PublicRealtimeSearch(weather=OpenMeteoWeather(), qwen=qwen)
