from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.observability.metrics import GLOBAL_METRICS
from services.agent.src.providers import doubao_tts, funasr_stt, handlers
from services.agent.src.providers.handlers import (
    build_realtime_search_resolver,
    build_voice_provider_handlers,
)


@pytest.mark.asyncio
async def test_bailian_deepseek_handlers_keep_keyless_weather_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_FAST_TEMPERATURE", "0.3")
    monkeypatch.setenv("DEEPSEEK_FAST_MAX_TOKENS", "180")
    warmed: list[bool] = []

    class Pool:
        async def warm(self) -> None:
            warmed.append(True)

    asr = object()
    tts = SimpleNamespace(pool=Pool())
    llm_calls: list[dict[str, Any]] = []
    def llm_factory(**kwargs: Any) -> object:
        llm_calls.append(kwargs)
        return object()

    handlers = await build_voice_provider_handlers(
        settings=SimpleNamespace(
            llm_provider="bailian_deepseek",
            llm_fast_model="deepseek-v4-flash",
            llm_api_key="secret",
            llm_base_url="https://llm.example.com/v1",
        ),
        llm_factory=llm_factory,
        asr_factory=lambda: asr,
        tts_factory=lambda: tts,
    )

    assert handlers.asr is asr
    assert handlers.speech_synthesis is tts
    assert handlers.realtime_search_resolver is not None
    assert handlers.realtime_search_resolver.qwen is None
    assert handlers.realtime_search_model == "open-meteo"
    assert warmed == [True]
    assert llm_calls == [
        {
            "model": "deepseek-v4-flash",
            "api_key": "secret",
            "base_url": "https://llm.example.com/v1",
            "temperature": 0.3,
            "tool_choice": "auto",
            "max_retries": 0,
            "timeout": llm_calls[0]["timeout"],
            "extra_body": {
                "enable_thinking": False,
                "thinking": {"type": "disabled"},
                "max_tokens": 180,
            },
        }
    ]


@pytest.mark.asyncio
async def test_provider_handlers_do_not_send_dashscope_search_options_to_deepseek() -> None:
    calls: list[dict[str, Any]] = []

    await build_voice_provider_handlers(
        settings=SimpleNamespace(
            llm_provider="deepseek",
            llm_fast_model="deepseek-fast",
            llm_api_key="secret",
            llm_base_url="https://deepseek.example.com/v1",
        ),
        llm_factory=lambda **kwargs: calls.append(kwargs) or object(),
        asr_factory=object,
        tts_factory=object,
    )

    assert calls[0]["extra_body"] == {
        "thinking": {"type": "disabled"},
        "max_tokens": 240,
    }


@pytest.mark.asyncio
async def test_qwen_handlers_disable_thinking_for_flash_chat() -> None:
    calls: list[dict[str, Any]] = []

    await build_voice_provider_handlers(
        settings=SimpleNamespace(
            llm_provider="qwen",
            llm_fast_model="qwen3.7-flash",
            llm_api_key="secret",
            llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        llm_factory=lambda **kwargs: calls.append(kwargs) or object(),
        asr_factory=object,
        tts_factory=object,
    )

    assert calls[0]["model"] == "qwen3.7-flash"
    assert calls[0]["extra_body"] == {
        "enable_thinking": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 240,
    }


def test_default_provider_factories_share_exported_process_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        funasr_stt.FunASRConfig,
        "from_env",
        classmethod(lambda cls: funasr_stt.FunASRConfig(api_key="test", ws_url="ws://asr")),
    )
    monkeypatch.setattr(
        doubao_tts.DoubaoTTSConfig,
        "from_env",
        classmethod(
            lambda cls: doubao_tts.DoubaoTTSConfig(
                api_key="test",
                ws_url="ws://tts",
                speaker="test",
            )
        ),
    )

    assert funasr_stt.FunASRSTT.from_env().metrics is GLOBAL_METRICS
    assert doubao_tts.DoubaoTTS.from_env().pool.metrics is GLOBAL_METRICS


def test_realtime_search_resolver_uses_isolated_qwen_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    class Resolver:
        def __init__(self, config: Any) -> None:
            captured.append(config)
            self.model = config.model

    monkeypatch.setattr(handlers, "QwenRealtimeSearch", Resolver)
    resolver = build_realtime_search_resolver(
        settings=SimpleNamespace(
            dashscope_api_key="qwen-key",
            dashscope_compatible_base_url="https://qwen.example/v1",
            qwen_deep_model="qwen-deep",
        )
    )

    assert isinstance(resolver.qwen, Resolver)
    config = captured[0]
    assert config.api_key == "qwen-key"
    assert config.base_url == "https://qwen.example/v1"
    assert config.model == "qwen-deep"


@pytest.mark.asyncio
async def test_keyless_realtime_search_uses_public_weather_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Weather:
        model = "open-meteo"

        async def resolve(self, *, query: str) -> str | None:
            assert query == "南京天气"
            return "南京现在晴，气温三十三摄氏度。"

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(handlers, "OpenMeteoWeather", Weather)
    resolver = build_realtime_search_resolver(
        settings=SimpleNamespace(dashscope_api_key=""),
    )

    assert resolver.qwen is None
    assert await resolver.resolve(query="南京天气") == "南京现在晴，气温三十三摄氏度。"
    await resolver.aclose()
