from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.providers.handlers import build_voice_provider_handlers


@pytest.mark.asyncio
async def test_provider_handlers_hide_vendor_construction_behind_one_seam(
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
    realtime_search = object()
    realtime_search_configs: list[Any] = []

    def llm_factory(**kwargs: Any) -> object:
        llm_calls.append(kwargs)
        return object()

    handlers = await build_voice_provider_handlers(
        settings=SimpleNamespace(
            llm_provider="qwen",
            llm_fast_model="qwen-fast",
            qwen_deep_model="qwen-plus",
            llm_api_key="secret",
            llm_base_url="https://llm.example.com/v1",
        ),
        llm_factory=llm_factory,
        asr_factory=lambda: asr,
        tts_factory=lambda: tts,
        realtime_search_factory=lambda config: realtime_search_configs.append(config)
        or realtime_search,
    )

    assert handlers.asr is asr
    assert handlers.speech_synthesis is tts
    assert handlers.realtime_search_resolver is realtime_search
    assert handlers.realtime_search_model == "qwen-plus"
    assert len(realtime_search_configs) == 1
    assert realtime_search_configs[0].api_key == "secret"
    assert realtime_search_configs[0].base_url == "https://llm.example.com/v1"
    assert realtime_search_configs[0].model == "qwen-plus"
    assert warmed == [True]
    assert llm_calls == [
        {
            "model": "qwen-fast",
            "api_key": "secret",
            "base_url": "https://llm.example.com/v1",
            "temperature": 0.3,
            "tool_choice": "auto",
            "max_retries": 0,
            "timeout": llm_calls[0]["timeout"],
            "extra_body": {
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
