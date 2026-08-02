"""DeepSeek OpenAI-compatible client: fast path + deep path (ch.13)."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.reliability import CircuitBreaker


@dataclass
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    fast_model: str = "deepseek-v4-flash"
    deep_model: str = "deepseek-v4-flash"
    fast_first_token_timeout_s: float = 3.0
    fast_total_timeout_s: float = 12.0
    fast_max_tokens: int = 240
    fast_temperature: float = 0.45
    deep_total_timeout_s: float = 90.0
    thinking_mode: Literal["dashscope", "deepseek"] = "dashscope"


@dataclass
class StreamChunk:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None


class DeepSeekClient:
    """Async streaming client. Never sends parallel_tool_calls or max_completion_tokens."""

    def __init__(self, config: DeepSeekConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=3.0,
                read=config.fast_total_timeout_s,
                write=5.0,
                pool=3.0,
            ),
        )
        self._breaker = CircuitBreaker()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _fast_body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._config.fast_model,
            "messages": messages,
            "stream": True,
            "temperature": self._config.fast_temperature,
            "max_tokens": self._config.fast_max_tokens,
        }
        body.update(self._thinking_body(enabled=False))
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        # MUST NOT include parallel_tool_calls or max_completion_tokens
        return body

    def _deep_body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._config.deep_model,
            "messages": messages,
            "stream": True,
            "reasoning_effort": "high",
        }
        body.update(self._thinking_body(enabled=True))
        if tools:
            body["tools"] = tools
        return body

    def _thinking_body(self, *, enabled: bool) -> dict[str, Any]:
        if self._config.thinking_mode == "dashscope":
            return {"enable_thinking": enabled}
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}

    async def stream_fast(
        self,
        messages: list[dict[str, Any]],
        *,
        fence: GenerationFence,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[tuple[GenerationFence, StreamChunk]]:
        body = self._fast_body(messages, tools)
        async for chunk in self._stream(
            body,
            total_timeout=self._config.fast_total_timeout_s,
            first_token_timeout=self._config.fast_first_token_timeout_s,
        ):
            yield fence, chunk

    async def stream_deep(
        self,
        messages: list[dict[str, Any]],
        *,
        fence: GenerationFence,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[tuple[GenerationFence, StreamChunk]]:
        body = self._deep_body(messages, tools)
        async for chunk in self._stream(
            body,
            total_timeout=self._config.deep_total_timeout_s,
            first_token_timeout=None,
        ):
            yield fence, chunk

    async def _stream(
        self,
        body: dict[str, Any],
        *,
        total_timeout: float,
        first_token_timeout: float | None,
    ) -> AsyncIterator[StreamChunk]:
        self._breaker.before_request()
        yielded = False
        for attempt in range(2):
            iterator = self._stream_once(body, total_timeout=total_timeout)
            buffered: list[StreamChunk] = []
            first_received = first_token_timeout is None
            loop = asyncio.get_running_loop()
            total_deadline = loop.time() + total_timeout
            first_deadline = loop.time() + (first_token_timeout or total_timeout)
            try:
                while True:
                    deadline = total_deadline if first_received else min(total_deadline, first_deadline)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        if first_received:
                            raise DeepSeekTotalTimeoutError()
                        raise DeepSeekFirstTokenTimeoutError()
                    try:
                        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                    except TimeoutError:
                        if first_received:
                            raise DeepSeekTotalTimeoutError() from None
                        raise DeepSeekFirstTokenTimeoutError() from None
                    except StopAsyncIteration:
                        if not first_received:
                            raise DeepSeekFirstTokenTimeoutError() from None
                        self._breaker.record_success()
                        return
                    if not first_received:
                        buffered.append(chunk)
                        if not chunk.content and not chunk.tool_calls:
                            continue
                        first_received = True
                        for pending in buffered:
                            yielded = True
                            yield pending
                        buffered.clear()
                    else:
                        yielded = True
                        yield chunk
            except (DeepSeekFirstTokenTimeoutError, DeepSeekServerError, httpx.TransportError):
                await iterator.aclose()
                if attempt == 0 and not yielded:
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                    continue
                self._breaker.record_failure()
                raise
            except (asyncio.CancelledError, GeneratorExit):
                await iterator.aclose()
                raise
            except BaseException:
                await iterator.aclose()
                self._breaker.record_failure()
                raise

        raise AssertionError("unreachable")

    async def _stream_once(
        self,
        body: dict[str, Any],
        *,
        total_timeout: float,
    ) -> AsyncGenerator[StreamChunk, None]:
        timeout = httpx.Timeout(connect=3.0, read=total_timeout, write=5.0, pool=3.0)
        async with self._client.stream(
            "POST",
            "/chat/completions",
            json=body,
            timeout=timeout,
        ) as resp:
            if resp.status_code == 429:
                raise DeepSeekRateLimitError(resp.headers.get("Retry-After"))
            if resp.status_code >= 500:
                raise DeepSeekServerError(resp.status_code)
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                else:
                    data = line.strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise DeepSeekBadJSONError(str(exc)) from exc
                yield parse_completion_chunk(obj)


def parse_completion_chunk(obj: dict[str, Any]) -> StreamChunk:
    choices = obj.get("choices") or []
    if not choices:
        return StreamChunk()
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content") or ""
    reasoning = delta.get("reasoning_content") or ""
    tool_calls = delta.get("tool_calls")
    finish = choice.get("finish_reason")
    return StreamChunk(
        content=content if isinstance(content, str) else "",
        reasoning_content=reasoning if isinstance(reasoning, str) else "",
        tool_calls=tool_calls if isinstance(tool_calls, list) else None,
        finish_reason=finish if isinstance(finish, str) else None,
    )


def filter_content_for_tts(chunk: StreamChunk) -> str:
    """Only content goes to TTS; never reasoning_content."""
    return chunk.content


class DeepSeekRateLimitError(Exception):
    def __init__(self, retry_after: str | None) -> None:
        super().__init__(f"DeepSeek 429 retry_after={retry_after}")
        self.retry_after = retry_after


class DeepSeekServerError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"DeepSeek server error {status}")
        self.status = status


class DeepSeekBadJSONError(Exception):
    """Raised when the provider stream returns invalid JSON chunks."""


class DeepSeekFirstTokenTimeoutError(TimeoutError):
    """Raised when the fast path produces no content before its deadline."""


class DeepSeekTotalTimeoutError(TimeoutError):
    """Raised when a streaming request exceeds its total deadline."""


def validate_no_forbidden_fields(body: dict[str, Any]) -> None:
    if "parallel_tool_calls" in body:
        raise ValueError("must not send parallel_tool_calls")
    if "max_completion_tokens" in body:
        raise ValueError("must not send max_completion_tokens; use max_tokens")
