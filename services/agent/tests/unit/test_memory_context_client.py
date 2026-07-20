from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src import memory_context_client as memory_module
from services.agent.src.memory_context_client import (
    MemoryContextClient,
    MemoryContextClientConfig,
)


@pytest.mark.asyncio
async def test_refresh_sends_only_session_scope_and_exposes_confirmed_owner_memory() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["token"] = request.headers.get("X-Memoria-Internal-Token")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "kind": "knowledge",
                        "title": "答应别人的事要做到",
                        "snippet": "我们家的家训是答应别人的事一定做到。",
                        "category": "family_principle",
                        "status": "confirmed",
                        "source_event_id": "event-memory-001",
                        "occurred_at": "2026-07-19T10:00:00+00:00",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MemoryContextClient(
            MemoryContextClientConfig(
                endpoint="https://control.test/v1/archive/session-context",
                internal_token="archive-internal-token",
                timeout_s=0.2,
                cache_ttl_s=60,
                limit=6,
            ),
            client=http_client,
        )
        assert await client.refresh(
            session_id="session-001",
            speaker_class="owner",
            topic="家训",
        )
        snapshot = client.cached(session_id="session-001", speaker_class="owner")

    assert observed == {
        "token": "archive-internal-token",
        "body": {
            "session_id": "session-001",
            "speaker_class": "owner",
            "topic": "家训",
            "limit": 6,
        },
    }
    assert snapshot is not None
    assert snapshot.items[0].source_event_id == "event-memory-001"
    assert snapshot.items[0].status == "confirmed"


@pytest.mark.asyncio
async def test_guest_or_timeout_clears_private_memory_without_blocking_on_guest() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "claim",
                            "title": "杭州",
                            "snippet": "我在杭州读过书。",
                            "category": "life_story",
                            "status": "confirmed",
                            "source_event_id": "event-001",
                            "occurred_at": "2026-07-19T10:00:00+00:00",
                        }
                    ]
                },
            )
        raise httpx.ReadTimeout("slow archive service", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MemoryContextClient(
            MemoryContextClientConfig(
                endpoint="https://control.test/v1/archive/session-context",
                internal_token="archive-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(session_id="session-001", speaker_class="owner", topic="first")
        assert client.cached(session_id="session-001", speaker_class="owner") is not None
        assert not await client.refresh(
            session_id="session-001", speaker_class="owner", topic="second"
        )
        assert client.cached(session_id="session-001", speaker_class="owner") is None
        assert not await client.refresh(
            session_id="session-001", speaker_class="guest", topic="guest"
        )

    assert attempts == 2
    assert client.cached(session_id="session-001", speaker_class="guest") is None


@pytest.mark.asyncio
async def test_stale_or_unconfirmed_memory_is_never_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    payload = {
        "items": [
            {
                "kind": "claim",
                "title": "杭州",
                "snippet": "我在杭州读过书。",
                "category": "life_story",
                "status": "confirmed",
                "source_event_id": "event-001",
                "occurred_at": "2026-07-19T10:00:00+00:00",
            }
        ]
    }
    monkeypatch.setattr(memory_module.time, "monotonic", lambda: now)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ) as http_client:
        client = MemoryContextClient(
            MemoryContextClientConfig(
                endpoint="https://control.test/v1/archive/session-context",
                internal_token="archive-internal-token",
                cache_ttl_s=30,
            ),
            client=http_client,
        )
        assert await client.refresh(session_id="session-001", speaker_class="owner", topic="")
        now = 131.0
        assert client.cached(session_id="session-001", speaker_class="owner") is None

        payload["items"][0]["status"] = "candidate"
        assert not await client.refresh(session_id="session-001", speaker_class="owner", topic="")
        assert client.cached(session_id="session-001", speaker_class="owner") is None
