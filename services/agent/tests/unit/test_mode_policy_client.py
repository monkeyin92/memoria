from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.mode_policy_client import (
    ModePolicyClient,
    ModePolicyClientConfig,
)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "interaction_mode": "companion",
        "mode_policy_version": "mode-policy-3",
        "companion_style_id": "starlight",
        "companion_style_version": "companion-v1",
        "policy_scope": "session",
        "digital_self_version_id": None,
        "relationship_profile_id": None,
        "legacy_grant_id": None,
        "capabilities": {
            "conversation": True,
            "private_memory": True,
            "persona": True,
            "persona_low_sensitivity": True,
            "tools": True,
            "history": True,
            "learning": True,
            "voice_profile": True,
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_fetch_freezes_companion_policy_from_the_authoritative_session_response() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["token"] = request.headers.get("X-Memoria-Internal-Token")
        return httpx.Response(200, json=_payload())

    client = ModePolicyClient(
        ModePolicyClientConfig(
            endpoint="https://control.test/v1/interaction/session-policy",
            internal_token="interaction-token",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    policy = await client.fetch(session_id="session-001")

    assert seen == {
        "path": "/v1/interaction/session-policy",
        "body": {"session_id": "session-001"},
        "token": "interaction-token",
    }
    assert policy.available is True
    assert policy.mode == "companion"
    assert policy.policy_version == "mode-policy-3"
    assert policy.companion_style_prompt is not None
    assert "价值观" in policy.companion_style_prompt


@pytest.mark.parametrize(
    ("companion_id", "question_frequency", "interview_depth"),
    [
        ("starlight", "occasional", "light"),
        ("taoxi", "occasional", "light"),
        ("mianmian", "rare", "light"),
        ("axu", "rare", "light"),
        ("xuanmo", "rare", "on_explicit_invitation"),
    ],
)
def test_companion_style_catalog_parity_reaches_the_agent_prompt(
    companion_id: str, question_frequency: str, interview_depth: str
) -> None:
    policy = ModePolicyClient._parse(_payload(companion_style_id=companion_id))

    assert policy.available is True
    assert policy.companion_style is not None
    assert policy.companion_style.question_frequency == question_frequency
    assert policy.companion_style.interview_depth == interview_depth


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,status",
    [
        (_payload(interaction_mode="unknown"), 200),
        (_payload(policy_scope="turn"), 200),
        (_payload(digital_self_version_id="client-selected"), 200),
        (_payload(capabilities={"private_memory": True}), 200),
        ({"interaction_mode": "companion"}, 200),
        (_payload(), 404),
    ],
)
async def test_bad_or_missing_policy_fails_closed(
    payload: dict[str, object], status: int
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    client = ModePolicyClient(
        ModePolicyClientConfig(
            endpoint="https://control.test/v1/interaction/session-policy",
            internal_token="interaction-token",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    policy = await client.fetch(session_id="session-001")

    assert policy.available is False
    assert policy.allows_private_context("owner") is False
    assert policy.allows_tools("owner") is False
    assert policy.owner_projection_eligible("owner") is False
