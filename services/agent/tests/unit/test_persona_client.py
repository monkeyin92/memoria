from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from services.agent.src import persona_client as persona_module
from services.agent.src.persona_client import PersonaClient, PersonaClientConfig


@pytest.mark.asyncio
async def test_refresh_sends_only_session_scope_and_exposes_recent_owner_capsule() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["token"] = request.headers.get("X-Memoria-Internal-Token")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "version_id": "persona-v3",
                "version_number": 3,
                "prompt_fragment": "[人格胶囊 v3]\n- 表达观点时常用‘我觉得’自然起句",
                "delivery_rate": 0.95,
                "entries": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PersonaClient(
            PersonaClientConfig(
                endpoint="https://control.test/v1/persona/session-capsule",
                internal_token="persona-internal-token",
                timeout_s=0.2,
                cache_ttl_s=60,
            ),
            client=http_client,
        )
        assert await client.refresh(
            session_id="session-001",
            speaker_class="owner",
            topic="表达看法",
        )
        capsule = client.cached(session_id="session-001", speaker_class="owner")

    assert observed == {
        "token": "persona-internal-token",
        "body": {
            "session_id": "session-001",
            "speaker_class": "owner",
            "topic": "表达看法",
        },
    }
    assert capsule is not None
    assert capsule.version_id == "persona-v3"
    assert "我觉得" in capsule.prompt_fragment
    assert not hasattr(capsule, "delivery_rate")


@pytest.mark.asyncio
async def test_guest_or_failed_refresh_clears_private_capsule() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={
                    "version_id": "persona-v1",
                    "version_number": 1,
                    "prompt_fragment": "private capsule",
                    "delivery_rate": 1.05,
                    "entries": [],
                },
            )
        raise httpx.ReadTimeout("slow persona service", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PersonaClient(
            PersonaClientConfig(
                endpoint="https://control.test/v1/persona/session-capsule",
                internal_token="persona-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(
            session_id="session-001", speaker_class="owner", topic="first"
        )
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
async def test_empty_capsule_refresh_clears_the_previous_consent_cache() -> None:
    before: dict[str, object] = {
        "version_id": "persona-v1",
        "version_number": 1,
        "prompt_fragment": "private capsule",
        "entries": [],
    }
    after: dict[str, object] = {
        "version_id": None,
        "version_number": None,
        "prompt_fragment": "",
        "entries": [],
    }
    responses: Iterator[dict[str, object]] = iter((before, after))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=next(responses)))
    ) as http_client:
        client = PersonaClient(
            PersonaClientConfig(
                endpoint="https://control.test/v1/persona/session-capsule",
                internal_token="persona-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(
            session_id="session-001", speaker_class="owner", topic="before revoke"
        )
        assert client.cached(session_id="session-001", speaker_class="owner") is not None

        assert await client.refresh(
            session_id="session-001", speaker_class="owner", topic="after revoke"
        )

    assert client.cached(session_id="session-001", speaker_class="owner") is None


@pytest.mark.asyncio
async def test_uncertain_style_capsule_is_cached_separately_from_owner_and_guest_clears_both(
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        speaker_class = body["speaker_class"]
        return httpx.Response(
            200,
            json={
                "version_id": "persona-v2",
                "version_number": 2,
                "prompt_fragment": (
                    "owner-private-capsule"
                    if speaker_class == "owner"
                    else "confirmed-style-only"
                ),
                "delivery_rate": 1.0,
                "entries": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PersonaClient(
            PersonaClientConfig(
                endpoint="https://control.test/v1/persona/session-capsule",
                internal_token="persona-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(
            session_id="session-001", speaker_class="owner", topic="owner"
        )
        assert await client.refresh(
            session_id="session-001", speaker_class="uncertain", topic="uncertain"
        )

        owner = client.cached(session_id="session-001", speaker_class="owner")
        uncertain = client.cached(session_id="session-001", speaker_class="uncertain")
        guest = client.cached(session_id="session-001", speaker_class="guest")

    assert [request["speaker_class"] for request in requests] == ["owner", "uncertain"]
    assert owner is not None and owner.prompt_fragment == "owner-private-capsule"
    assert uncertain is not None and uncertain.prompt_fragment == "confirmed-style-only"
    assert guest is None
    assert client.cached(session_id="session-001", speaker_class="owner") is None
    assert client.cached(session_id="session-001", speaker_class="uncertain") is None


@pytest.mark.asyncio
async def test_stale_capsule_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(persona_module.time, "monotonic", lambda: now)  # type: ignore[attr-defined]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "version_id": "persona-v1",
                    "version_number": 1,
                    "prompt_fragment": "recent capsule",
                    "delivery_rate": 1.0,
                    "entries": [],
                },
            )
        )
    ) as http_client:
        client = PersonaClient(
            PersonaClientConfig(
                endpoint="https://control.test/v1/persona/session-capsule",
                internal_token="persona-internal-token",
                cache_ttl_s=30,
            ),
            client=http_client,
        )
        assert await client.refresh(
            session_id="session-001", speaker_class="owner", topic=""
        )
        now = 131.0
        assert client.cached(session_id="session-001", speaker_class="owner") is None
