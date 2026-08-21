from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from services.control_api.app.database import MemoryStore
from services.control_api.app.routes.media import internal_router


def _payload(event_type: str = "first_frame_sent") -> dict[str, object]:
    delivery_id = "session-1/epoch-3/turn-7/generation-11/tool-2"
    return {
        "schema_version": "reply-delivery-v1",
        "event_id": hashlib.sha256(f"{delivery_id}\0{event_type}".encode()).hexdigest(),
        "delivery_id": delivery_id,
        "session_id": "session-1",
        "session_epoch": 3,
        "turn_id": 7,
        "generation_id": 11,
        "tool_epoch": 2,
        "event_type": event_type,
        "terminal_event": None,
        "terminal_reason": None,
        "first_frame_sent": True,
        "provider_completed": False,
        "actual_heard": False,
        "playback_ended": False,
        "reason": "downlink_frame_accepted",
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.asyncio
async def test_reply_delivery_projection_is_idempotent_and_readable(tmp_path) -> None:
    app = FastAPI()
    app.include_router(internal_router)
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    store.initialize()
    token = "reply-delivery-token-material-that-is-long-enough"
    app.state.memory_store = store
    app.state.settings = SimpleNamespace(media_reply_delivery_token=SecretStr(token))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        body = _payload()
        headers = {"X-Media-Reply-Delivery-Token": token}
        first = await client.post(
            "/v1/internal/media-runtime/reply-delivery",
            headers=headers,
            json=body,
        )
        duplicate = await client.post(
            "/v1/internal/media-runtime/reply-delivery",
            headers=headers,
            json=body,
        )
        read = await client.get(
            "/v1/internal/media-runtime/reply-delivery",
            headers=headers,
            params={"delivery_id": body["delivery_id"]},
        )

    assert first.status_code == 200
    assert first.json()["inserted"] is True
    assert duplicate.status_code == 200
    assert duplicate.json()["inserted"] is False
    assert read.status_code == 200
    assert len(read.json()["events"]) == 1
    assert read.json()["events"][0]["first_frame_sent"] is True


@pytest.mark.asyncio
async def test_reply_delivery_projection_rejects_bad_fence(tmp_path) -> None:
    app = FastAPI()
    app.include_router(internal_router)
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    store.initialize()
    token = "reply-delivery-token-material-that-is-long-enough"
    app.state.memory_store = store
    app.state.settings = SimpleNamespace(media_reply_delivery_token=SecretStr(token))
    body = _payload()
    body["delivery_id"] = "wrong"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/internal/media-runtime/reply-delivery",
            headers={"X-Media-Reply-Delivery-Token": token},
            json=body,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "media_reply_delivery_fence_mismatch"
