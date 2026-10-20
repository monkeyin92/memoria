from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from aiohttp import web
from scripts.provider_smoke_test import smoke_llm
from services.agent.src.providers.provider_errors import APIStatusError

PROMPT = "请只回答“连接正常”。"
PASS_LINE = "DeepSeek smoke: PASS (streaming content, thinking disabled)\n"
PRIMARY_BODY = {
    "messages": [{"role": "user", "content": PROMPT}],
    "model": "deepseek-flash",
    "stream": True,
    "stream_options": {"include_usage": True},
    "temperature": 0.45,
    "max_tokens": 240,
    "thinking": {"type": "disabled"},
}
BACKUP_BODY = {
    "messages": [{"role": "user", "content": PROMPT}],
    "model": "qwen3.8-flash",
    "stream": True,
    "stream_options": {"include_usage": True},
    "temperature": 0.45,
    "max_tokens": 240,
    "enable_thinking": False,
    "thinking": {"type": "disabled"},
}

ChatMode = Literal["reply", "stall", "reject"]
STALL_FIRST_TOKEN_TIMEOUT_S = "1.5"


def _chunk(text: str) -> dict[str, Any]:
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


async def _wait_for_client_disconnect(request: web.Request) -> web.StreamResponse:
    for _ in range(200):
        transport = request.transport
        if transport is None or transport.is_closing():
            return web.Response(status=499)
        await asyncio.sleep(0.05)
    return web.Response(status=504)


@dataclass
class ChatEndpoint:
    mode: ChatMode
    reply: str = "连接正常"
    requests: list[dict[str, Any]] = field(default_factory=list)
    origin: str = ""
    _runner: web.AppRunner | None = None

    @property
    def v1(self) -> str:
        return f"{self.origin}/v1"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/chat/completions", self._handle)
        app.router.add_post("/v1/chat/completions", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        self.origin = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(
            {
                "path": request.path,
                "authorization": request.headers.get("Authorization", ""),
                "body": body,
            }
        )
        if self.mode == "stall":
            return await _wait_for_client_disconnect(request)
        if self.mode == "reject":
            return web.json_response({"error": {"message": "primary down"}}, status=400)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        payload = json.dumps(_chunk(self.reply), ensure_ascii=False)
        await response.write(f"data: {payload}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failover: bool,
    primary: ChatEndpoint,
    backup: ChatEndpoint,
    first_token_timeout_s: str,
) -> None:
    monkeypatch.setenv("LLM_FAILOVER_ENABLED", "true" if failover else "false")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setenv("DEEPSEEK_API_URL", primary.origin)
    monkeypatch.setenv("DEEPSEEK_FAST_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")
    monkeypatch.setenv("DEEPSEEK_FAST_MAX_TOKENS", "240")
    monkeypatch.setenv("LLM_BACKUP_MODEL", "qwen3.8-flash")
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", first_token_timeout_s)
    monkeypatch.setenv("LLM_INTER_TOKEN_TIMEOUT_S", "4")
    monkeypatch.setenv(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setattr(
        "services.agent.src.providers.handlers.DASHSCOPE_CN_COMPATIBLE_BASE_URL",
        backup.v1,
    )


@pytest.fixture
async def endpoints() -> Any:
    started: list[ChatEndpoint] = []

    async def open_pair(
        primary_mode: ChatMode,
        backup_mode: ChatMode = "reply",
    ) -> tuple[ChatEndpoint, ChatEndpoint]:
        primary = ChatEndpoint(primary_mode)
        backup = ChatEndpoint(backup_mode, reply="连接正常")
        await primary.start()
        await backup.start()
        started.extend((primary, backup))
        return primary, backup

    yield open_pair
    for endpoint in started:
        await endpoint.stop()


@pytest.mark.asyncio
async def test_failover_smoke_passes_when_primary_stalls_and_backup_answers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    endpoints: Any,
) -> None:
    primary, backup = await endpoints("stall")
    _configure(
        monkeypatch,
        failover=True,
        primary=primary,
        backup=backup,
        first_token_timeout_s=STALL_FIRST_TOKEN_TIMEOUT_S,
    )

    assert await smoke_llm() == "DeepSeek"
    assert capsys.readouterr().out == PASS_LINE
    assert primary.requests == [
        {
            "path": "/chat/completions",
            "authorization": "Bearer ds-key",
            "body": PRIMARY_BODY,
        }
    ]
    assert backup.requests == [
        {
            "path": "/v1/chat/completions",
            "authorization": "Bearer dash-key",
            "body": BACKUP_BODY,
        }
    ]


@pytest.mark.asyncio
async def test_flag_off_smoke_uses_only_the_primary_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    endpoints: Any,
) -> None:
    primary, backup = await endpoints("reply")
    _configure(
        monkeypatch,
        failover=False,
        primary=primary,
        backup=backup,
        first_token_timeout_s="0.25",
    )

    assert await smoke_llm() == "DeepSeek"
    assert capsys.readouterr().out == PASS_LINE
    assert primary.requests == [
        {
            "path": "/chat/completions",
            "authorization": "Bearer ds-key",
            "body": PRIMARY_BODY,
        }
    ]
    assert backup.requests == []


@pytest.mark.asyncio
async def test_flag_off_primary_failure_does_not_call_backup(
    monkeypatch: pytest.MonkeyPatch,
    endpoints: Any,
) -> None:
    primary, backup = await endpoints("reject")
    _configure(
        monkeypatch,
        failover=False,
        primary=primary,
        backup=backup,
        first_token_timeout_s="0.25",
    )

    with pytest.raises(APIStatusError):
        await smoke_llm()
    assert len(primary.requests) == 1
    assert primary.requests[0]["body"]["messages"] == [{"role": "user", "content": PROMPT}]
    assert backup.requests == []
