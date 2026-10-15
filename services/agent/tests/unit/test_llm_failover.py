"""LLM failover: first-token timeout, mid-stream stall, URL gates, thinking bodies."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any

import pytest
from pydantic import ValidationError
from services.agent.src.config import AgentSettings
from services.agent.src.llm_types import ChatChunk, ChatContext, ChoiceDelta
from services.agent.src.providers.handlers import (
    _reply_extra_body,
    build_language_model_handler,
)
from services.agent.src.providers.llm_failover import (
    FailoverChatModel,
    LlmUnavailableError,
)
from services.agent.src.providers.provider_errors import APIConnectionError
from services.common.companion_response_safety import SAFE_UNKNOWN_REPLY
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
)
from services.common.llm_failover import (
    DASHSCOPE_CN_COMPATIBLE_BASE_URL,
    LLM_UNAVAILABLE_TEXT,
    PRODUCT_FORBIDDEN_COPY,
    gate_backup_reply_copy,
    normalize_dashscope_compatible_base_url,
)
from services.common.llm_thinking import thinking_disabled
from services.control_api.app.config import ControlSettings


class _FakeStream:
    def __init__(
        self,
        chunks: list[ChatChunk],
        *,
        delay_s: float = 0.0,
        hang_after: bool = False,
    ) -> None:
        self._chunks = chunks
        self._delay_s = delay_s
        self._hang_after = hang_after
        self.closed = False

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def __aiter__(self) -> AsyncIterator[ChatChunk]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ChatChunk]:
        for chunk in self._chunks:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield chunk
        if self._hang_after:
            await asyncio.sleep(3600)


class _FakeModel:
    def __init__(
        self,
        *,
        name: str,
        chunks: list[ChatChunk] | None = None,
        delay_s: float = 0.0,
        hang_after: bool = False,
        error: BaseException | None = None,
    ) -> None:
        self.model = name
        self._chunks = chunks or []
        self._delay_s = delay_s
        self._hang_after = hang_after
        self._error = error
        self.calls: list[list[dict[str, str]]] = []
        self.closed = False

    def chat(self, *, chat_ctx: ChatContext, tools: Sequence[Any] = ()) -> _FakeStream:
        self.calls.append(chat_ctx.to_openai_messages())
        if self._error is not None:
            raise self._error
        return _FakeStream(
            self._chunks,
            delay_s=self._delay_s,
            hang_after=self._hang_after,
        )

    def prewarm(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def _text_chunk(text: str, *, chunk_id: str = "c1") -> ChatChunk:
    return ChatChunk(id=chunk_id, delta=ChoiceDelta(content=text))


def _ctx(*messages: tuple[str, str]) -> ChatContext:
    ctx = ChatContext.empty()
    for role, content in messages:
        ctx.add_message(role=role, content=content)
    return ctx


def _failover(
    primary: _FakeModel,
    backup: _FakeModel,
    *,
    first_token_timeout_s: float = 0.5,
    inter_token_timeout_s: float = 0.5,
) -> FailoverChatModel:
    return FailoverChatModel(
        primary=primary,
        backup=backup,
        first_token_timeout_s=first_token_timeout_s,
        inter_token_timeout_s=inter_token_timeout_s,
    )


@pytest.mark.asyncio
async def test_primary_success_never_touches_backup() -> None:
    primary = _FakeModel(name="deepseek-flash", chunks=[_text_chunk("你好")])
    backup = _FakeModel(name="qwen3.8-flash", chunks=[_text_chunk("备用")])
    model = _failover(primary, backup)
    chat_ctx = _ctx(("system", "规则"), ("user", "嗨"))
    async with model.chat(chat_ctx=chat_ctx) as stream:
        texts = [chunk.delta.content async for chunk in stream if chunk.delta.content]
    assert texts == ["你好"]
    assert len(primary.calls) == 1
    assert backup.calls == []


@pytest.mark.asyncio
async def test_first_token_timeout_retries_identical_messages_on_backup() -> None:
    primary = _FakeModel(
        name="deepseek-flash",
        chunks=[_text_chunk("迟到")],
        delay_s=0.2,
    )
    backup = _FakeModel(name="qwen3.8-flash", chunks=[_text_chunk("备用答复")])
    model = _failover(primary, backup, first_token_timeout_s=0.05)
    system = "儿童系统提示与内容安全规则"
    chat_ctx = _ctx(("system", system), ("user", "讲个故事"))
    async with model.chat(chat_ctx=chat_ctx) as stream:
        texts = [chunk.delta.content async for chunk in stream if chunk.delta.content]
    assert texts == ["备用答复"]
    assert primary.calls[0] == backup.calls[0]
    assert backup.calls[0][0]["content"] == system


@pytest.mark.asyncio
async def test_mid_stream_stall_ends_without_backup_stitch() -> None:
    primary = _FakeModel(
        name="deepseek-flash",
        chunks=[_text_chunk("半句")],
        hang_after=True,
    )
    backup = _FakeModel(name="qwen3.8-flash", chunks=[_text_chunk("拼接坏了")])
    model = _failover(primary, backup, inter_token_timeout_s=0.05)
    with pytest.raises(LlmUnavailableError, match="mid-stream"):
        async with model.chat(chat_ctx=_ctx(("user", "嗨"))) as stream:
            texts: list[str] = []
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    texts.append(chunk.delta.content)
    assert texts == ["半句"]
    assert backup.calls == []


@pytest.mark.asyncio
async def test_both_endpoints_fail_raise_llm_unavailable() -> None:
    primary = _FakeModel(
        name="deepseek-flash",
        error=APIConnectionError("primary down", retryable=True),
    )
    backup = _FakeModel(
        name="qwen3.8-flash",
        error=APIConnectionError("backup down", retryable=True),
    )
    model = _failover(primary, backup, first_token_timeout_s=0.2)
    with pytest.raises(LlmUnavailableError):
        async with model.chat(chat_ctx=_ctx(("user", "嗨"))) as stream:
            async for _ in stream:
                pass


@pytest.mark.asyncio
async def test_backup_forbidden_copy_is_replaced_with_safe_unknown() -> None:
    word = next(iter(PRODUCT_FORBIDDEN_COPY))
    primary = _FakeModel(
        name="deepseek-flash",
        error=APIConnectionError("primary down", retryable=True),
    )
    backup = _FakeModel(
        name="qwen3.8-flash",
        chunks=[_text_chunk(f"我们是{word}")],
    )
    model = _failover(primary, backup, first_token_timeout_s=0.2)
    async with model.chat(chat_ctx=_ctx(("user", "嗨"))) as stream:
        texts = [chunk.delta.content async for chunk in stream if chunk.delta.content]
    assert texts == [SAFE_UNKNOWN_REPLY]
    assert gate_backup_reply_copy(f"我们是{word}") == SAFE_UNKNOWN_REPLY


def test_flag_off_build_returns_primary_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    settings = AgentSettings(_env_file=None)
    captured: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> _FakeModel:
        captured.append(kwargs)
        return _FakeModel(name=kwargs["model"])

    built = build_language_model_handler(settings=settings, llm_factory=factory)
    assert isinstance(built, _FakeModel)
    assert len(captured) == 1
    assert captured[0]["model"] == settings.llm_fast_model


def test_flag_on_wraps_beijing_backup_with_thinking_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "true")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("LLM_BACKUP_MODEL", "qwen3.8-flash")
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", "5")
    monkeypatch.setenv("LLM_INTER_TOKEN_TIMEOUT_S", "8")
    settings = AgentSettings(_env_file=None)
    captured: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> _FakeModel:
        captured.append(kwargs)
        return _FakeModel(name=kwargs["model"])

    built = build_language_model_handler(settings=settings, llm_factory=factory)
    assert isinstance(built, FailoverChatModel)
    assert len(captured) == 2
    primary_kwargs, backup_kwargs = captured
    assert primary_kwargs["api_key"] == "ds-key"
    assert backup_kwargs["api_key"] == "dash-key"
    assert backup_kwargs["base_url"] == DASHSCOPE_CN_COMPATIBLE_BASE_URL
    assert backup_kwargs["model"] == "qwen3.8-flash"
    assert backup_kwargs["extra_body"]["enable_thinking"] is False
    assert backup_kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def test_normalize_rejects_intl_and_us_hosts() -> None:
    with pytest.raises(ValueError, match="China North 2"):
        normalize_dashscope_compatible_base_url(
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
    with pytest.raises(ValueError, match="China North 2"):
        normalize_dashscope_compatible_base_url(
            "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
        )


def test_deepseek_flag_off_allows_non_beijing_backup_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    settings = AgentSettings(_env_file=None)
    assert "dashscope-intl" in settings.dashscope_compatible_base_url


@pytest.mark.parametrize("provider", ["qwen", "bailian_deepseek"])
def test_agent_rejects_non_beijing_when_primary_uses_dashscope(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    with pytest.raises(ValidationError, match="China North 2"):
        AgentSettings(_env_file=None)


def test_agent_rejects_non_beijing_when_failover_backup_uses_dashscope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    )
    with pytest.raises(ValidationError, match="China North 2"):
        AgentSettings(_env_file=None)


@pytest.mark.parametrize("provider", ["qwen", "bailian_deepseek"])
def test_control_api_rejects_non_beijing_when_summaries_use_dashscope(
    provider: str,
) -> None:
    with pytest.raises(ValidationError, match="China North 2|compatible-mode"):
        ControlSettings(
            _env_file=None,
            LLM_PROVIDER=provider,
            DASHSCOPE_API_KEY="dash-key",
            DASHSCOPE_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )


def test_control_api_deepseek_allows_non_beijing_dashscope_base_url() -> None:
    settings = ControlSettings(
        _env_file=None,
        LLM_PROVIDER="deepseek",
        DEEPSEEK_API_KEY="ds-key",
        DASHSCOPE_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    assert "dashscope-intl" in settings.dashscope_base_url


def test_failover_requires_dashscope_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "true")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    with pytest.raises(ValidationError, match="DASHSCOPE_API_KEY"):
        AgentSettings(_env_file=None)


def test_backup_model_id_is_compliance_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BACKUP_MODEL", "qwen-plus")
    with pytest.raises(ValidationError, match="qwen3.8-flash"):
        AgentSettings(_env_file=None)


def test_three_tier_fixed_safety_scripts_are_model_independent() -> None:
    scripts = (CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT)
    assert len(scripts) == 3
    for script in scripts:
        assert script.display.endswith("。")
        assert "危机" not in script.display


def test_llm_unavailable_copy_is_stable() -> None:
    assert LLM_UNAVAILABLE_TEXT == "我现在有点卡住了，等一会儿再来找我聊吧。"


def test_thinking_disabled_deepseek_payload_is_exact() -> None:
    assert thinking_disabled("deepseek") == {"thinking": {"type": "disabled"}}


def test_deepseek_reply_extra_body_has_only_deepseek_thinking_switch() -> None:
    body = _reply_extra_body(llm_provider="deepseek")
    assert body["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in body


def test_qwen_reply_extra_body_disables_thinking() -> None:
    body = _reply_extra_body(llm_provider="qwen")
    assert body["enable_thinking"] is False
    assert body["thinking"] == {"type": "disabled"}
