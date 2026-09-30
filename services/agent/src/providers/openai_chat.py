"""Streaming OpenAI-compatible chat client for the reply pipeline.

It replaces ``livekit.plugins.openai.LLM`` and keeps the request and stream
semantics the bridge relied on:

- one ``chat.completions`` request per attempt with the serialized chat
  context, ``stream_options.include_usage``, the configured temperature and
  ``extra_body``, and a 10 s per-request timeout (the connect options'
  timeout, which overrides the client-level ``httpx.Timeout``); no tools are
  offered, so ``tool_choice`` is not sent;
- up to ``max_retry`` further attempts (0.1 s, then ``retry_interval`` apart)
  while the failing attempt has not yielded visible text; a 4xx other than
  408/429 is final, 499 ends the stream quietly, and exhausting the attempts
  raises ``APIConnectionError``;
- ``<think>...</think>`` spans are stripped from the streamed text;
- ``prewarm()`` lists models once in the background to open the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, cast

import httpx
import openai
from openai.types.chat import ChatCompletionMessageParam

from services.agent.src.llm_types import ChatChunk, ChatContext, ChoiceDelta
from services.agent.src.providers.provider_errors import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIError,
    APIStatusError,
    APITimeoutError,
)

logger = logging.getLogger(__name__)

THINK_TAG_START = "<think>"
THINK_TAG_END = "</think>"


@dataclass(slots=True)
class ThinkingFilter:
    """Streaming state that hides ``<think>...</think>`` spans across chunks."""

    _buffer: str = ""
    _inside: bool = False

    def feed(self, content: str | None, *, final: bool) -> str | None:
        if content is not None:
            self._buffer += content
        visible: list[str] = []
        while self._buffer:
            if self._inside:
                index = self._buffer.find(THINK_TAG_END)
                if index < 0:
                    keep = _partial_marker_length(self._buffer, THINK_TAG_END)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    break
                self._buffer = self._buffer[index + len(THINK_TAG_END) :]
                self._inside = False
                continue
            index = self._buffer.find(THINK_TAG_START)
            if index >= 0:
                visible.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(THINK_TAG_START) :]
                self._inside = True
                continue
            # Hold back a trailing prefix of the start tag until the next chunk.
            keep = _partial_marker_length(self._buffer, THINK_TAG_START)
            visible.append(self._buffer[:-keep] if keep else self._buffer)
            self._buffer = self._buffer[-keep:] if keep else ""
            break
        if final:
            if not self._inside:
                visible.append(self._buffer)
            self._buffer = ""
            self._inside = False
        result = "".join(visible)
        return result or ("" if content == "" else None)


def _partial_marker_length(content: str, marker: str) -> int:
    return max(
        (
            length
            for length in range(1, min(len(content), len(marker) - 1) + 1)
            if content.endswith(marker[:length])
        ),
        default=0,
    )


class OpenAIChatModel:
    """One OpenAI-compatible chat model with its own HTTP connection pool."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        timeout: httpx.Timeout,
        extra_body: dict[str, Any],
        max_retries: int = 0,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI-compatible chat model requires an API key")
        self._model = model
        self._temperature = temperature
        self._extra_body = dict(extra_body)
        self._conn_options = conn_options
        self._client = openai.AsyncClient(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            http_client=httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=50,
                    keepalive_expiry=120,
                ),
            ),
        )
        self._prewarm_task: asyncio.Task[None] | None = None

    @property
    def model(self) -> str:
        return self._model

    def chat(self, *, chat_ctx: ChatContext, tools: Sequence[Any] = ()) -> OpenAIChatStream:
        if tools:
            raise ValueError("the reply chat model does not offer tools")
        return OpenAIChatStream(self, chat_ctx.to_openai_messages())

    def prewarm(self) -> None:
        """Open the provider connection in the background, once per model."""

        if self._prewarm_task is not None:
            return

        async def _prewarm() -> None:
            # A failed warmup only means the first reply opens the connection.
            with contextlib.suppress(Exception):
                await self._client.models.list()

        self._prewarm_task = asyncio.get_event_loop().create_task(_prewarm())

    async def aclose(self) -> None:
        if self._prewarm_task is not None:
            self._prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._prewarm_task
        await self._client.close()

    async def _attempt(self, messages: list[dict[str, str]]) -> AsyncGenerator[ChatChunk, None]:
        """One request; raises ``APIError`` with ``retryable`` false once text was yielded."""

        retryable = True
        try:
            stream = await self._client.chat.completions.create(
                messages=cast(list[ChatCompletionMessageParam], messages),
                model=self._model,
                stream_options={"include_usage": True},
                stream=True,
                timeout=httpx.Timeout(self._conn_options.timeout),
                extra_body=self._extra_body,
                temperature=self._temperature,
            )
            thinking = ThinkingFilter()
            async with stream:
                async for chunk in stream:
                    for choice in chunk.choices:
                        delta = choice.delta
                        if delta is None:
                            continue
                        content = thinking.feed(
                            delta.content,
                            final=choice.finish_reason is not None,
                        )
                        if not content:
                            continue
                        retryable = False
                        yield ChatChunk(id=chunk.id, delta=ChoiceDelta(content=content))
        except openai.APITimeoutError:
            raise APITimeoutError(retryable=retryable) from None
        except httpx.TimeoutException as exc:
            # A timeout while reading the streamed body is not mapped by openai.
            raise APITimeoutError(retryable=retryable) from exc
        except openai.APIStatusError as exc:
            raise APIStatusError(
                exc.message,
                status_code=exc.status_code,
                request_id=exc.request_id,
                body=exc.body,
                retryable=retryable,
            ) from None
        except Exception as exc:
            raise APIConnectionError(retryable=retryable) from exc

    async def _stream(self, messages: list[dict[str, str]]) -> AsyncGenerator[ChatChunk, None]:
        options = self._conn_options
        for attempt in range(options.max_retry + 1):
            try:
                # Close the attempt's HTTP stream when the consumer stops early.
                async with contextlib.aclosing(self._attempt(messages)) as chunks:
                    async for chunk in chunks:
                        yield chunk
                return
            except APIError as exc:
                if isinstance(exc, APIStatusError) and exc.status_code == 499:
                    return
                if options.max_retry == 0 or not exc.retryable:
                    raise
                if attempt == options.max_retry:
                    raise APIConnectionError(
                        f"failed to generate LLM completion after {options.max_retry + 1} attempts",
                    ) from exc
                interval = options.interval_for_retry(attempt)
                logger.warning(
                    "failed to generate LLM completion: %s, retrying in %ss attempt=%s",
                    exc,
                    interval,
                    attempt + 1,
                )
                if interval > 0:
                    await asyncio.sleep(interval)


class OpenAIChatStream:
    """``async with model.chat(...) as stream: async for chunk in stream``."""

    def __init__(self, model: OpenAIChatModel, messages: list[dict[str, str]]) -> None:
        self._chunks = model._stream(messages)

    def __aiter__(self) -> AsyncIterator[ChatChunk]:
        return self._chunks

    async def __aenter__(self) -> OpenAIChatStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._chunks.aclose()


__all__ = ["OpenAIChatModel", "OpenAIChatStream", "ThinkingFilter"]
