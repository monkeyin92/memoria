from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.providers.crisis_reply_model import (
    CrisisReplyModel,
    CrisisReplyModelConfig,
)


def _model(handler: httpx.MockTransport | object, *, thinking: str = "dashscope") -> CrisisReplyModel:
    client = httpx.AsyncClient(transport=handler)  # type: ignore[arg-type]
    return CrisisReplyModel(
        CrisisReplyModelConfig(
            api_key="test-secret",
            base_url="https://provider.test/v1",
            model="qwen-flash",
            thinking_mode=thinking,  # type: ignore[arg-type]
        ),
        client=client,
    )


@pytest.mark.asyncio
async def test_draft_and_review_use_one_fast_model() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = "PASS" if body["max_tokens"] == 8 else "去找身边的大人。"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    model = _model(httpx.MockTransport(handler))
    draft = await model.generate(query="我不想活了", tier="crisis_self", failures=())
    accepted = await model.accept(query="我不想活了", tier="crisis_self", reply=draft or "")
    await model.aclose()

    assert draft == "去找身边的大人。"
    assert accepted is True
    assert [body["model"] for body in seen] == ["qwen-flash", "qwen-flash"]
    assert seen[0]["enable_thinking"] is False
    assert "幺二三五六" in str(seen[0]["messages"])
    assert seen[1]["max_tokens"] == 8


@pytest.mark.asyncio
async def test_review_rejects_anything_but_pass() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "FAIL"}}]})

    model = _model(httpx.MockTransport(handler))
    assert await model.accept(query="有人打我", tier="support_victim", reply="去找大人。") is False
    await model.aclose()


@pytest.mark.asyncio
async def test_deepseek_draft_disables_thinking() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "好。"}}]})

    model = _model(httpx.MockTransport(handler), thinking="deepseek")
    await model.generate(query="帮帮他", tier="support_relay", failures=("adult",))
    await model.aclose()

    assert seen[0]["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in seen[0]
    assert "没有请孩子找身边的大人" in str(seen[0]["messages"])
