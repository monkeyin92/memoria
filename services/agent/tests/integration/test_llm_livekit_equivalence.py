"""Transitional: the local LLM port against the livekit-agents 1.8.1 code it replaces.

Deleted together with the livekit dependency; kept in history as the evidence
that the request, the streamed text, retries and errors did not change.
"""

from __future__ import annotations

import random
from typing import Any

import httpx
import pytest
from livekit.agents import APIConnectOptions as LKConnectOptions
from livekit.agents import llm as lk_llm
from livekit.agents.llm import utils as lk_llm_utils
from livekit.plugins import openai as lk_openai
from services.agent.src import context_assembler as assembler
from services.agent.src.llm_types import ChatContext
from services.agent.src.orchestration.context_manager import ChatMessage as SessionTurn
from services.agent.src.providers.openai_chat import OpenAIChatModel, ThinkingFilter
from services.agent.src.providers.provider_errors import APIConnectOptions
from services.agent.tests.integration.test_openai_chat import (
    EXTRA_BODY,
    USAGE_CHUNK,
    MockChatServer,
    content_chunk,
    server_factory,  # noqa: F401 - fixture
    sse,
    stall,
    status,
)

_ROLES = ("system", "user", "assistant")


def _script(rng: random.Random) -> list[tuple[str, list[str]]]:
    messages: list[tuple[str, list[str]]] = []
    for index in range(rng.randint(1, 9)):
        role = rng.choice(_ROLES)
        parts = [f"{role}-{index}-{part}" for part in range(rng.randint(1, 2))]
        messages.append((role, parts))
    return messages


def _both(script: list[tuple[str, list[str]]]) -> tuple[lk_llm.ChatContext, ChatContext]:
    old = lk_llm.ChatContext.empty()
    new = ChatContext.empty()
    for role, parts in script:
        old.add_message(role=role, content=list(parts))  # type: ignore[arg-type]
        new.add_message(role=role, content=list(parts))  # type: ignore[arg-type]
    return old, new


def _old_messages(ctx: lk_llm.ChatContext) -> list[dict[str, Any]]:
    messages, _ = ctx.to_provider_format("openai")
    return messages


@pytest.mark.parametrize("seed", range(200))
def test_context_assembly_serializes_identically(seed: int) -> None:
    rng = random.Random(seed)
    script = _script(rng)
    heard = [f"heard-{index}" for index in range(rng.randint(0, 3))]
    turns = [
        SessionTurn(rng.choice(("user", "assistant")), f"turn-{index}", rng.choice(("public", "owner")))
        for index in range(rng.randint(0, 4))
    ]
    operations = {
        "heard_only": lambda ctx: assembler.heard_only_chat_context(ctx, list(heard)),
        "current_user_only": assembler.current_user_only_chat_context,
        "scoped_working": lambda ctx: assembler.scoped_working_chat_context(
            ctx, turns, speaker_scope="public"
        ),
        "interrupted_owner": lambda ctx: assembler.interrupted_reply_chat_context(
            ctx, list(heard), include_previous_user=True
        ),
        "interrupted_guest": lambda ctx: assembler.interrupted_reply_chat_context(
            ctx, list(heard), include_previous_user=False
        ),
    }
    for name, operation in operations.items():
        old, new = _both(script)
        old_result = operation(old)
        new_result = operation(new)
        assert _old_messages(old_result) == new_result.to_openai_messages(), name
        # The shallow-copy edits reach the original the same way.
        assert _old_messages(old) == new.to_openai_messages(), name


@pytest.mark.parametrize("seed", range(300))
def test_thinking_filter_matches_the_library(seed: int) -> None:
    rng = random.Random(seed)
    pieces = ["答", "案", "<think>", "推理", "</think>", "<", "t", "hink>", "</", "think", ">", "。"]
    text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
    cuts = sorted(rng.sample(range(len(text) + 1), k=min(len(text) + 1, rng.randint(0, 5))))
    chunks = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]
    chunks.append("")
    old_state = lk_llm_utils.ThinkingTokenFilter()
    new_state = ThinkingFilter()
    for index, chunk in enumerate(chunks):
        final = index == len(chunks) - 1
        old = lk_llm_utils.strip_thinking_tokens(chunk, old_state, final=final)
        new = new_state.feed(chunk, final=final)
        assert old == new, (text, chunks, index)


def _old_model(server: MockChatServer) -> lk_openai.LLM:
    # Exactly the arguments build_language_model_handler passed before.
    return lk_openai.LLM(
        model="deepseek-v4-flash",
        api_key="secret-key",
        base_url=server.base_url,
        temperature=0.45,
        tool_choice="auto",
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body=dict(EXTRA_BODY),
    )


def _new_model(server: MockChatServer, options: APIConnectOptions) -> OpenAIChatModel:
    return OpenAIChatModel(
        model="deepseek-v4-flash",
        api_key="secret-key",
        base_url=server.base_url,
        temperature=0.45,
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body=dict(EXTRA_BODY),
        conn_options=options,
    )


_SCRIPT = [
    ("system", ["你是陪伴助手。"]),
    ("user", ["今天天气怎么样"]),
    ("assistant", ["第一段", "第二段"]),
    ("user", ["继续"]),
]


async def _run_old(server: MockChatServer, options: APIConnectOptions) -> dict[str, Any]:
    model = _old_model(server)
    old_ctx, _ = _both(_SCRIPT)
    texts: list[str] = []
    error: BaseException | None = None
    try:
        async with model.chat(
            chat_ctx=old_ctx,
            tools=[],
            conn_options=LKConnectOptions(
                max_retry=options.max_retry,
                retry_interval=options.retry_interval,
                timeout=options.timeout,
            ),
        ) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    texts.append(chunk.delta.content)
    except Exception as exc:
        error = exc
    finally:
        await model.aclose()
    return _outcome(server, texts, error)


async def _run_new(server: MockChatServer, options: APIConnectOptions) -> dict[str, Any]:
    model = _new_model(server, options)
    _, new_ctx = _both(_SCRIPT)
    texts: list[str] = []
    error: BaseException | None = None
    try:
        async with model.chat(chat_ctx=new_ctx, tools=[]) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    texts.append(chunk.delta.content)
    except Exception as exc:
        error = exc
    finally:
        await model.aclose()
    return _outcome(server, texts, error)


def _outcome(server: MockChatServer, texts: list[str], error: BaseException | None) -> dict[str, Any]:
    requests = [
        {
            "path": request["path"],
            "body": request["body"],
            # The library stamped its own User-Agent; everything else is the SDK's.
            "headers": {
                key: value
                for key, value in request["headers"].items()
                if key not in {"user-agent", "host", "content-length"}
            },
        }
        for request in server.requests
    ]
    return {
        "text": "".join(texts),
        "requests": requests,
        "error": None
        if error is None
        else (
            type(error).__name__,
            getattr(error, "message", str(error)),
            getattr(error, "retryable", None),
            getattr(error, "status_code", None),
            type(error.__cause__).__name__ if error.__cause__ else None,
        ),
    }


SCENARIOS: dict[str, tuple[list[Any], APIConnectOptions]] = {
    "happy": ([sse([content_chunk("你好，"), content_chunk("我在。", finish="stop"), USAGE_CHUNK])], APIConnectOptions()),
    "think": (
        [sse([content_chunk("<thi"), content_chunk("nk>内部</think>答"), content_chunk("案", finish="stop")])],
        APIConnectOptions(),
    ),
    "retry_500": ([status(500), sse([content_chunk("好", finish="stop")])], APIConnectOptions(max_retry=3, retry_interval=0.01)),
    "retry_429": ([status(429), sse([content_chunk("好", finish="stop")])], APIConnectOptions(max_retry=3, retry_interval=0.01)),
    "final_400": ([status(400, "bad")], APIConnectOptions(max_retry=3, retry_interval=0.01)),
    "quiet_499": ([status(499)], APIConnectOptions(max_retry=3, retry_interval=0.01)),
    "exhausted": ([status(503)], APIConnectOptions(max_retry=2, retry_interval=0.01)),
    "no_retry_allowed": ([status(503)], APIConnectOptions(max_retry=0)),
    "drop_after_text": (
        [sse([content_chunk("已经说出"), content_chunk("后半段")], delay_s=0.02, drop_after=1)],
        APIConnectOptions(max_retry=3, retry_interval=0.01),
    ),
    "timeout": ([stall(2.0)], APIConnectOptions(max_retry=0, timeout=0.2)),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_http_behavior_matches_the_livekit_plugin(server_factory: Any, scenario: str) -> None:  # noqa: F811
    script, options = SCENARIOS[scenario]
    old_server = await server_factory(*script)
    new_server = await server_factory(*script)
    old = await _run_old(old_server, options)
    new = await _run_new(new_server, options)
    for request in old["requests"] + new["requests"]:
        # The SDK stamps a per-request retry count; both use one SDK attempt.
        request["headers"].pop("x-stainless-retry-count", None)
    assert new == old
