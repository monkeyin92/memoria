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

    def llm_factory(**kwargs: Any) -> object:
        llm_calls.append(kwargs)
        return object()

    handlers = await build_voice_provider_handlers(
        settings=SimpleNamespace(
            llm_fast_model="qwen-fast",
            llm_api_key="secret",
            llm_base_url="https://llm.example.com/v1",
        ),
        llm_factory=llm_factory,
        asr_factory=lambda: asr,
        tts_factory=lambda: tts,
    )

    assert handlers.asr is asr
    assert handlers.speech_synthesis is tts
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
