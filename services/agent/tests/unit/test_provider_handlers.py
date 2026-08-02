from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.providers.handlers import build_voice_provider_handlers


@pytest.mark.asyncio
async def test_bailian_deepseek_handlers_do_not_construct_qwen_search(
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
    assert handlers.realtime_search_resolver is None
    assert handlers.realtime_search_model is None
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
