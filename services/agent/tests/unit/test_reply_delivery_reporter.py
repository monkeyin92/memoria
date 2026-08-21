from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from services.agent.src.voice_core.reply_delivery_reporter import (
    ReplyDeliveryReporter,
    ReplyDeliveryReporterConfig,
)


@pytest.mark.asyncio
async def test_reporter_posts_fenced_payload_without_blocking_submit(tmp_path) -> None:
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reporter = ReplyDeliveryReporter(
        ReplyDeliveryReporterConfig(
            endpoint="http://control-api:8000/v1/internal/media-runtime/reply-delivery",
            token="reply-delivery-token-material-that-is-long-enough",
            spool_path=tmp_path / "reply.spool",
            spool_key=Fernet.generate_key().decode("ascii"),
        ),
        client=client,
    )
    try:
        payload = {"event_id": "a" * 64, "event_type": "first_frame_sent"}
        assert reporter.submit(payload)
        await reporter.flush()
    finally:
        await reporter.close()
        await client.aclose()

    assert seen == [payload]
    assert reporter.published_count == 1
    assert reporter.failed_count == 0


def test_reporter_rejects_non_local_plaintext_endpoint() -> None:
    with pytest.raises(ValueError, match="plaintext"):
        ReplyDeliveryReporterConfig(
            endpoint="http://example.test/v1/internal/media-runtime/reply-delivery",
            token="reply-delivery-token-material-that-is-long-enough",
            spool_path=Path("/tmp/reply.spool"),
            spool_key=Fernet.generate_key().decode("ascii"),
        )


@pytest.mark.asyncio
async def test_reporter_spools_retryable_http_failure_and_replays(tmp_path) -> None:
    responses = [503, 202]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(responses.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reporter = ReplyDeliveryReporter(
        ReplyDeliveryReporterConfig(
            endpoint="http://control-api:8000/v1/internal/media-runtime/reply-delivery",
            token="reply-delivery-token-material-that-is-long-enough",
            spool_path=tmp_path / "reply.spool",
            spool_key=Fernet.generate_key().decode("ascii"),
        ),
        client=client,
    )
    try:
        assert reporter.submit({"event_id": "b" * 64})
        await reporter.flush()
        assert reporter.config.spool_path.exists()
        assert await reporter.replay() == 1
        assert reporter.config.spool_path.read_bytes() == b""
    finally:
        await reporter.close()
        await client.aclose()
