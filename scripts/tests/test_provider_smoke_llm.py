"""Provider smoke LLM path must honor LLM_FAILOVER_ENABLED."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any

import pytest
from services.agent.src.llm_types import ChatChunk, ChatContext, ChoiceDelta
from services.agent.src.providers.llm_failover import FailoverChatModel
from services.agent.src.providers.provider_errors import APIConnectionError


class _FakeStream:
    def __init__(self, chunks: list[ChatChunk]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[ChatChunk]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ChatChunk]:
        for chunk in self._chunks:
            yield chunk


class _FakeModel:
    def __init__(
        self,
        *,
        name: str,
        chunks: list[ChatChunk] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.model = name
        self._chunks = chunks or []
        self._error = error
        self.calls = 0

    def chat(self, *, chat_ctx: ChatContext, tools: Sequence[Any] = ()) -> _FakeStream:
        del chat_ctx, tools
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _FakeStream(self._chunks)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_smoke_llm_uses_failover_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", "0.05")
    monkeypatch.setenv("LLM_INTER_TOKEN_TIMEOUT_S", "4")

    primary = _FakeModel(
        name="deepseek-flash",
        error=APIConnectionError("primary first token timeout", retryable=True),
    )
    backup = _FakeModel(
        name="qwen3.8-flash",
        chunks=[ChatChunk(id="b1", delta=ChoiceDelta(content="连接正常"))],
    )
    built: list[Any] = []

    def factory(**kwargs: Any) -> _FakeModel:
        model_name = str(kwargs["model"])
        if "qwen" in model_name:
            built.append(("backup", kwargs))
            return backup
        built.append(("primary", kwargs))
        return primary

    import scripts.provider_smoke_test as smoke

    monkeypatch.setattr(smoke, "OpenAIChatModel", factory)
    monkeypatch.setattr(
        smoke,
        "build_language_model_handler",
        lambda **kwargs: __import__(
            "services.agent.src.providers.handlers", fromlist=["build_language_model_handler"]
        ).build_language_model_handler(**kwargs),
    )

    label = await smoke.smoke_llm()
    assert label == "DeepSeek"
    assert primary.calls == 1
    assert backup.calls == 1
    assert any(role == "backup" for role, _ in built)


@pytest.mark.asyncio
async def test_smoke_llm_flag_off_stays_on_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")

    primary = _FakeModel(
        name="deepseek-flash",
        chunks=[ChatChunk(id="p1", delta=ChoiceDelta(content="连接正常"))],
    )
    backup = _FakeModel(
        name="qwen3.8-flash",
        chunks=[ChatChunk(id="b1", delta=ChoiceDelta(content="备用"))],
    )

    def factory(**kwargs: Any) -> _FakeModel:
        if "qwen" in str(kwargs["model"]):
            return backup
        return primary

    import scripts.provider_smoke_test as smoke

    monkeypatch.setattr(smoke, "OpenAIChatModel", factory)

    label = await smoke.smoke_llm()
    assert label == "DeepSeek"
    assert primary.calls == 1
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_failover_stream_survives_primary_first_token_timeout() -> None:
    """Readiness depends on FailoverChatModel absorbing primary first-token timeout."""

    primary = _FakeModel(
        name="deepseek-flash",
        error=APIConnectionError("primary first token timeout", retryable=True),
    )
    backup = _FakeModel(
        name="qwen3.8-flash",
        chunks=[ChatChunk(id="b1", delta=ChoiceDelta(content="连接正常"))],
    )
    model = FailoverChatModel(
        primary=primary,
        backup=backup,
        first_token_timeout_s=0.2,
        inter_token_timeout_s=4.0,
    )
    chat_ctx = ChatContext.empty()
    chat_ctx.add_message(role="user", content="请只回答“连接正常”。")
    texts: list[str] = []
    async with model.chat(chat_ctx=chat_ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                texts.append(chunk.delta.content)
    assert "".join(texts) == "连接正常"
