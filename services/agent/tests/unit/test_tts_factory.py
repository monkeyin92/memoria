from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.providers import doubao_tts, tts_factory


def test_build_tts_follows_tts_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(doubao_tts.DoubaoTTS, "from_env", classmethod(lambda cls: sentinel))

    assert tts_factory.build_tts(SimpleNamespace(tts_provider="doubao")) is sentinel
    assert tts_factory.tts_provider_label(SimpleNamespace(tts_provider="doubao")) == (
        "volcengine_doubao"
    )


@pytest.mark.parametrize("call", [tts_factory.build_tts, tts_factory.tts_provider_label])
def test_unknown_tts_provider_fails_closed(call: Any) -> None:
    with pytest.raises(ValueError, match="unsupported TTS_PROVIDER"):
        call(SimpleNamespace(tts_provider="qwen_audio"))


def test_clone_tts_needs_the_model_studio_provider_and_a_permitted_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tts_factory, "frozen_companion_clone_permitted", lambda policy: True)
    clone = SimpleNamespace(references={"voice_provider": "alibaba_model_studio"})
    other = SimpleNamespace(references={"voice_provider": "volcengine_doubao"})
    assert tts_factory.wants_clone_tts(clone) is True
    assert tts_factory.wants_clone_tts(other) is False

    monkeypatch.setattr(tts_factory, "frozen_companion_clone_permitted", lambda policy: False)
    assert tts_factory.wants_clone_tts(clone) is False


@pytest.mark.asyncio
async def test_warm_and_close_failures_are_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Pool:
        async def warm(self) -> None:
            raise ConnectionError("provider down")

    class Provider:
        pool = Pool()

        async def aclose(self) -> None:
            raise RuntimeError("already closed")

    await tts_factory.warm_tts(Provider())
    await tts_factory.close_tts(Provider())
    await tts_factory.warm_tts(object())
    await tts_factory.close_tts(object())

    messages = [record.getMessage() for record in caplog.records]
    assert any("TTS pool warm failed" in message and "ConnectionError" in message for message in messages)
    assert any("TTS close failed" in message and "RuntimeError" in message for message in messages)
