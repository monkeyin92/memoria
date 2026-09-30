"""OpenAIChatModel against a local OpenAI-compatible chat completions server."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from aiohttp import web
from services.agent.src.llm_types import ChatContext
from services.agent.src.providers.openai_chat import OpenAIChatModel, ThinkingFilter
from services.agent.src.providers.provider_errors import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
)

Behavior = Callable[[web.Request], Awaitable[web.StreamResponse]]


def content_chunk(text: str | None, *, finish: str | None = None, index: int = 0) -> dict[str, Any]:
    return {
        "id": f"chunk-{index}",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }
        ],
    }


USAGE_CHUNK: dict[str, Any] = {
    "id": "usage",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": "deepseek-v4-flash",
    "choices": [],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def sse(chunks: list[dict[str, Any]], *, delay_s: float = 0.0, drop_after: int | None = None) -> Behavior:
    async def respond(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for index, chunk in enumerate(chunks):
            if drop_after is not None and index == drop_after:
                # Abort mid-body: the client sees a broken stream, not an end.
                assert request.transport is not None
                request.transport.abort()
                return response
            if delay_s:
                await asyncio.sleep(delay_s)
            payload = json.dumps(chunk, ensure_ascii=False)
            await response.write(f"data: {payload}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    return respond


def status(code: int, message: str = "provider error") -> Behavior:
    async def respond(_request: web.Request) -> web.StreamResponse:
        return web.json_response({"error": {"message": message}}, status=code)

    return respond


def stall(seconds: float) -> Behavior:
    async def respond(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(seconds)
        return await sse([content_chunk("迟到", finish="stop")])(request)

    return respond


@dataclass
class MockChatServer:
    """Answers each request with the next scripted behavior (the last repeats)."""

    script: list[Behavior]
    requests: list[dict[str, Any]] = field(default_factory=list)
    model_lists: int = 0
    _runner: web.AppRunner | None = None
    base_url: str = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._models)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base_url = f"http://127.0.0.1:{port}/v1"
        return self.base_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(
            {
                "path": request.path,
                "headers": {name.lower(): value for name, value in request.headers.items()},
                "body": body,
            }
        )
        behavior = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        return await behavior(request)

    async def _models(self, _request: web.Request) -> web.StreamResponse:
        self.model_lists += 1
        return web.json_response({"object": "list", "data": []})


EXTRA_BODY = {"max_tokens": 240, "enable_thinking": False, "thinking": {"type": "disabled"}}


def chat_model(
    server: MockChatServer,
    *,
    conn_options: APIConnectOptions | None = None,
) -> OpenAIChatModel:
    kwargs: dict[str, Any] = {}
    if conn_options is not None:
        kwargs["conn_options"] = conn_options
    return OpenAIChatModel(
        model="deepseek-v4-flash",
        api_key="secret-key",
        base_url=server.base_url,
        temperature=0.45,
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body=dict(EXTRA_BODY),
        **kwargs,
    )


def sample_context() -> ChatContext:
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content="你是陪伴助手。")
    ctx.add_message(role="user", content="今天天气怎么样")
    ctx.add_message(role="assistant", content=["第一段", "第二段"])
    ctx.add_message(role="user", content="继续")
    return ctx


async def collect(model: OpenAIChatModel, ctx: ChatContext | None = None) -> list[str]:
    texts: list[str] = []
    async with model.chat(chat_ctx=ctx or sample_context(), tools=[]) as stream:
        async for chunk in stream:
            assert chunk.delta is not None and chunk.delta.content
            texts.append(chunk.delta.content)
    return texts


FAST_RETRY = APIConnectOptions(max_retry=3, retry_interval=0.01)


@pytest.fixture
async def server_factory() -> Any:
    servers: list[MockChatServer] = []

    async def make(*script: Behavior) -> MockChatServer:
        server = MockChatServer(list(script))
        await server.start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        await server.stop()


async def test_request_carries_the_serialized_context_and_settings(server_factory: Any) -> None:
    server = await server_factory(sse([content_chunk("好"), USAGE_CHUNK]))
    model = chat_model(server)
    try:
        assert await collect(model) == ["好"]
    finally:
        await model.aclose()

    [request] = server.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer secret-key"
    assert request["body"] == {
        "messages": [
            {"role": "system", "content": "你是陪伴助手。"},
            {"role": "user", "content": "今天天气怎么样"},
            {"role": "assistant", "content": "第一段\n第二段"},
            {"role": "user", "content": "继续"},
        ],
        "model": "deepseek-v4-flash",
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.45,
        "max_tokens": 240,
        "enable_thinking": False,
        "thinking": {"type": "disabled"},
    }


async def test_streams_text_in_order_and_skips_empty_and_usage_chunks(server_factory: Any) -> None:
    server = await server_factory(
        sse(
            [
                content_chunk(None),
                content_chunk("你好，"),
                content_chunk(""),
                content_chunk("我在。", finish="stop"),
                USAGE_CHUNK,
            ]
        )
    )
    model = chat_model(server)
    try:
        assert await collect(model) == ["你好，", "我在。"]
    finally:
        await model.aclose()


async def test_think_spans_are_stripped_across_chunk_boundaries(server_factory: Any) -> None:
    server = await server_factory(
        sse(
            [
                content_chunk("<thi"),
                content_chunk("nk>内部推理</think>答"),
                content_chunk("案<", finish=None),
                content_chunk("。", finish="stop"),
            ]
        )
    )
    model = chat_model(server)
    try:
        assert "".join(await collect(model)) == "答案<。"
    finally:
        await model.aclose()


def test_thinking_filter_holds_back_only_a_start_tag_prefix() -> None:
    state = ThinkingFilter()
    assert state.feed("前<th", final=False) == "前"
    assert state.feed("en", final=False) == "<then"
    assert state.feed("<think>隐", final=False) is None
    assert state.feed("藏</thi", final=False) is None
    assert state.feed("nk>后", final=True) == "后"
    assert ThinkingFilter().feed("", final=False) == ""
    assert ThinkingFilter().feed(None, final=False) is None
    unterminated = ThinkingFilter()
    assert unterminated.feed("<think>没有结束", final=True) is None


async def test_failure_before_text_retries_on_a_fresh_request(server_factory: Any) -> None:
    server = await server_factory(status(500), sse([content_chunk("重试成功", finish="stop")]))
    model = chat_model(server, conn_options=FAST_RETRY)
    started = time.monotonic()
    try:
        assert await collect(model) == ["重试成功"]
    finally:
        await model.aclose()
    assert len(server.requests) == 2
    assert server.requests[0]["body"] == server.requests[1]["body"]
    # The first retry waits 0.1 s regardless of retry_interval.
    assert time.monotonic() - started >= 0.1


async def test_rate_limit_is_retried_but_a_client_error_is_final(server_factory: Any) -> None:
    limited = await server_factory(status(429), sse([content_chunk("好", finish="stop")]))
    model = chat_model(limited, conn_options=FAST_RETRY)
    try:
        assert await collect(model) == ["好"]
    finally:
        await model.aclose()
    assert len(limited.requests) == 2

    rejected = await server_factory(status(400, "bad request"))
    model = chat_model(rejected, conn_options=FAST_RETRY)
    try:
        with pytest.raises(APIStatusError) as caught:
            await collect(model)
    finally:
        await model.aclose()
    assert caught.value.status_code == 400
    assert caught.value.retryable is False
    assert len(rejected.requests) == 1


async def test_exhausted_retries_raise_a_connection_error(server_factory: Any) -> None:
    server = await server_factory(status(503))
    model = chat_model(server, conn_options=APIConnectOptions(max_retry=2, retry_interval=0.01))
    try:
        with pytest.raises(APIConnectionError) as caught:
            await collect(model)
    finally:
        await model.aclose()
    assert caught.value.message == "failed to generate LLM completion after 3 attempts"
    assert isinstance(caught.value.__cause__, APIStatusError)
    assert len(server.requests) == 3


async def test_status_499_ends_the_stream_quietly(server_factory: Any) -> None:
    server = await server_factory(status(499))
    model = chat_model(server, conn_options=FAST_RETRY)
    try:
        assert await collect(model) == []
    finally:
        await model.aclose()
    assert len(server.requests) == 1


async def test_failure_after_text_is_final_and_keeps_the_delivered_text(server_factory: Any) -> None:
    server = await server_factory(
        sse([content_chunk("已经说出"), content_chunk("后半段")], delay_s=0.02, drop_after=1)
    )
    model = chat_model(server, conn_options=FAST_RETRY)
    received: list[str] = []
    try:
        with pytest.raises(APIConnectionError) as caught:
            async with model.chat(chat_ctx=sample_context(), tools=[]) as stream:
                async for chunk in stream:
                    assert chunk.delta is not None and chunk.delta.content
                    received.append(chunk.delta.content)
    finally:
        await model.aclose()
    assert received == ["已经说出"]
    assert caught.value.retryable is False
    assert len(server.requests) == 1


async def test_request_timeout_is_the_connect_options_timeout(server_factory: Any) -> None:
    server = await server_factory(stall(2.0))
    model = chat_model(server, conn_options=APIConnectOptions(max_retry=0, timeout=0.2))
    started = time.monotonic()
    try:
        with pytest.raises(APITimeoutError):
            await collect(model)
    finally:
        await model.aclose()
    # The client-level read timeout is 12 s; the per-request 0.2 s wins.
    assert time.monotonic() - started < 1.5


async def test_closing_early_stops_reading_the_response(server_factory: Any) -> None:
    server = await server_factory(
        sse([content_chunk(str(index)) for index in range(50)], delay_s=0.01)
    )
    model = chat_model(server)
    try:
        async with model.chat(chat_ctx=sample_context(), tools=[]) as stream:
            async for chunk in stream:
                assert chunk.delta is not None
                assert chunk.delta.content == "0"
                break
    finally:
        await model.aclose()
    assert len(server.requests) == 1


async def test_tools_are_rejected(server_factory: Any) -> None:
    server = await server_factory(sse([content_chunk("好")]))
    model = chat_model(server)
    try:
        with pytest.raises(ValueError):
            model.chat(chat_ctx=sample_context(), tools=[object()])
    finally:
        await model.aclose()


async def test_prewarm_lists_models_once(server_factory: Any) -> None:
    server = await server_factory(sse([content_chunk("好")]))
    model = chat_model(server)
    try:
        model.prewarm()
        model.prewarm()
        for _ in range(100):
            if server.model_lists:
                break
            await asyncio.sleep(0.01)
    finally:
        await model.aclose()
    assert server.model_lists == 1


def test_an_empty_api_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        OpenAIChatModel(
            model="m",
            api_key="",
            base_url="http://127.0.0.1:9/v1",
            temperature=0.4,
            timeout=httpx.Timeout(1.0),
            extra_body={},
        )
