"""DeepSeek mock HTTP streaming integration tests."""

from __future__ import annotations

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.deepseek import (
    DeepSeekBadJSONError,
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekRateLimitError,
    DeepSeekServerError,
    DeepSeekTotalTimeoutError,
    filter_content_for_tts,
    validate_no_forbidden_fields,
)
from services.agent.src.providers.reliability import CircuitOpenError
from services.agent.tests.integration.mock_servers import MockDeepSeekServer


@pytest.mark.asyncio
async def test_deepseek_stream_content() -> None:
    srv = MockDeepSeekServer(scenario="happy")
    srv.start()
    try:
        cfg = DeepSeekConfig(api_key="test", base_url=srv.base_url, fast_model="deepseek-v4-flash")
        client = DeepSeekClient(cfg)
        fence = GenerationFence("s", 1, 1, 0)
        content = ""
        reasoning = ""
        async for f, chunk in client.stream_fast(
            [{"role": "user", "content": "请只回答“连接正常”。"}],
            fence=fence,
        ):
            assert f.matches(fence)
            content += filter_content_for_tts(chunk)
            reasoning += chunk.reasoning_content
        await client.aclose()
        assert "连接正常" in content
        assert reasoning == ""
        assert srv.last_body is not None
        assert srv.last_body.get("model") == "deepseek-v4-flash"
        assert srv.last_body.get("thinking") == {"type": "disabled"}
        validate_no_forbidden_fields(srv.last_body)
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_thinking_not_sent_to_tts() -> None:
    srv = MockDeepSeekServer(scenario="thinking")
    srv.start()
    try:
        client = DeepSeekClient(DeepSeekConfig(api_key="t", base_url=srv.base_url))
        fence = GenerationFence("s", 1, 1, 0)
        tts_text = ""
        reasoning = ""
        async for _, chunk in client.stream_fast(
            [{"role": "user", "content": "hi"}], fence=fence
        ):
            tts_text += filter_content_for_tts(chunk)
            reasoning += chunk.reasoning_content
        await client.aclose()
        assert "连接正常" in tts_text
        assert "推理" in reasoning
        assert "推理" not in tts_text
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_deepseek_429() -> None:
    srv = MockDeepSeekServer(scenario="429")
    srv.start()
    try:
        client = DeepSeekClient(DeepSeekConfig(api_key="t", base_url=srv.base_url))
        with pytest.raises(DeepSeekRateLimitError):
            async for _ in client.stream_fast(
                [{"role": "user", "content": "x"}],
                fence=GenerationFence("s", 1, 1, 0),
            ):
                pass
        await client.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_deepseek_5xx() -> None:
    srv = MockDeepSeekServer(scenario="500")
    srv.start()
    try:
        client = DeepSeekClient(DeepSeekConfig(api_key="t", base_url=srv.base_url))
        with pytest.raises(DeepSeekServerError):
            async for _ in client.stream_fast(
                [{"role": "user", "content": "x"}],
                fence=GenerationFence("s", 1, 1, 0),
            ):
                pass
        await client.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["500_once", "timeout_once"])
async def test_deepseek_retries_once_before_first_content(scenario: str) -> None:
    srv = MockDeepSeekServer(scenario=scenario)
    srv.start()
    try:
        client = DeepSeekClient(
            DeepSeekConfig(
                api_key="t",
                base_url=srv.base_url,
                # Keep the timeout far below the mock's 5-second first request,
                # but leave enough headroom for the immediate retry under a
                # concurrently loaded test process.
                fast_first_token_timeout_s=0.2,
                fast_total_timeout_s=1.0,
            )
        )
        content = ""
        async for _, chunk in client.stream_fast(
            [{"role": "user", "content": "x"}],
            fence=GenerationFence("s", 1, 1, 0),
        ):
            content += chunk.content
        await client.aclose()

        assert "连接正常" in content
        assert srv.requests == 2
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_deepseek_total_timeout_does_not_replay_emitted_content() -> None:
    srv = MockDeepSeekServer(scenario="slow_after_content")
    srv.start()
    try:
        client = DeepSeekClient(
            DeepSeekConfig(
                api_key="t",
                base_url=srv.base_url,
                fast_first_token_timeout_s=0.2,
                fast_total_timeout_s=0.1,
            )
        )
        chunks = []
        with pytest.raises(DeepSeekTotalTimeoutError):
            async for _, chunk in client.stream_fast(
                [{"role": "user", "content": "x"}],
                fence=GenerationFence("s", 1, 1, 0),
            ):
                chunks.append(chunk.content)
        assert chunks == ["连"]
        assert srv.requests == 1
        await client.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_deepseek_bad_json_and_circuit_open_after_three_failures() -> None:
    bad_json = MockDeepSeekServer(scenario="bad_json")
    bad_json.start()
    try:
        client = DeepSeekClient(DeepSeekConfig(api_key="t", base_url=bad_json.base_url))
        with pytest.raises(DeepSeekBadJSONError):
            async for _ in client.stream_fast(
                [{"role": "user", "content": "x"}],
                fence=GenerationFence("s", 1, 1, 0),
            ):
                pass
        await client.aclose()
    finally:
        bad_json.stop()

    failing = MockDeepSeekServer(scenario="500")
    failing.start()
    try:
        client = DeepSeekClient(DeepSeekConfig(api_key="t", base_url=failing.base_url))
        for _ in range(3):
            with pytest.raises(DeepSeekServerError):
                async for _ in client.stream_fast(
                    [{"role": "user", "content": "x"}],
                    fence=GenerationFence("s", 1, 1, 0),
                ):
                    pass
        with pytest.raises(CircuitOpenError):
            async for _ in client.stream_fast(
                [{"role": "user", "content": "x"}],
                fence=GenerationFence("s", 1, 1, 0),
            ):
                pass
        assert failing.requests == 6
        await client.aclose()
    finally:
        failing.stop()
