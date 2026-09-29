"""A device turn travels Agent -> Archive -> 回顾 under the profile Control signs.

The Agent reads the real ``/v1/interaction/session-policy`` response, claims
the device-bound owner, and hands its archive evidence to the real
``/v1/archive/session-events`` route; the owner then reads it back through the
review endpoints the mini program calls.  The profile never carries
``memory_capture`` (an action-time capability), exactly as in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.media_agent_factory import ProductionMediaSessionFactory
from services.agent.src.mode_policy_client import parse_mode_policy
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.control_api.app.bound_subject import DEVICE_BOUND_SUBJECT_REASON
from services.control_api.app.main import create_app
from services.control_api.tests.test_archive_api import (
    _configure,
    _register_verified_adult,
)
from services.control_api.tests.test_interaction_api import (
    _RUNTIME_PROFILE_SIGNING_KEY,
    _attach_signed_runtime_profile,
)

_ARCHIVE = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
_POLICY_TOKEN = "interaction-policy-token-that-is-long-enough"
_USER_TEXT = "我今天学会骑自行车了。"
_REPLY_TEXT = "太棒了，骑车时记得戴头盔。"


def _device_runtime(session_id: str, *, account_id: str) -> DuplexRuntime:
    factory = ProductionMediaSessionFactory(
        settings=SimpleNamespace(
            use_paralinguistic_tags=False,
            speaker_enroll_speech_ms=1_000,
            speaker_enroll_timeout_ms=10_000,
            speaker_accept_threshold=0.8,
            speaker_min_verify_speech_ms=800,
        ),
        llm_factory=object(),
    )
    # The ticket facts _attach_signed_runtime_profile signs into the profile.
    identity = SessionIdentity(
        session_id,
        account_id=account_id,
        device_id="device-test-1",
        client_type="device",
        subject_id=account_id,
        binding_id="binding-test-1",
        binding_version=1,
        runtime_profile_version=1,
    )
    return factory._new_runtime(
        session_id,
        SimpleNamespace(pool=object()),
        device_id=identity.device_id,
        identity=identity,
    )


async def _agent_turn(
    client: AsyncClient, *, session_id: str, account_id: str
) -> list[dict[str, Any]]:
    """Run one committed device turn in the Agent and return its evidence."""

    policy = await client.post(
        "/v1/interaction/session-policy",
        headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
        json={"session_id": session_id},
    )
    assert policy.status_code == 200, policy.text
    assert "memory_capture" not in policy.json()["runtime_profile"]["capabilities"]
    runtime = _device_runtime(session_id, account_id=account_id)
    runtime.set_mode_policy(
        parse_mode_policy(
            policy.json(),
            runtime_profile_verify_key=_RUNTIME_PROFILE_SIGNING_KEY.decode(),
        )
    )
    runtime.set_device_conversation_controls(True)
    await runtime.await_speaker_classification()
    evidence: list[dict[str, Any]] = []

    async def capture(event: dict[str, Any]) -> None:
        evidence.append(event)

    runtime.set_evidence_publisher(capture)
    fence = runtime.fence.bump_turn()
    runtime.publish_transcript(speaker="user", text=_USER_TEXT, final=True, fence=fence)
    runtime.publish_transcript(
        speaker="assistant", text=_REPLY_TEXT, final=True, heard=True, fence=fence
    )
    await asyncio.sleep(0)
    await runtime.close()
    return evidence


@pytest.mark.asyncio
async def test_device_bound_turn_reaches_the_owners_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", _POLICY_TOKEN)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="device-review-owner")
        account_id = str(owner["user_id"])
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        # A trusted, consented one-to-one device: Control signs the memory
        # grant for the bound subject and never memory_capture.
        _attach_signed_runtime_profile(app, user_id=account_id, session_id=session_id)

        evidence = await _agent_turn(client, session_id=session_id, account_id=account_id)
        assert [event["event_type"] for event in evidence] == [
            "speech.utterance_finalized",
            "assistant.playout_stopped",
        ]
        assert evidence[0]["speaker_class"] == "owner"
        assert evidence[0]["payload"]["speaker_reason_code"] == DEVICE_BOUND_SUBJECT_REASON
        for event in evidence:
            stored = await client.post("/v1/archive/session-events", headers=_ARCHIVE, json=event)
            assert stored.status_code == 201, stored.text

        sessions = await client.get("/v1/archive/conversation-sessions", headers=headers)
        history = await client.get(
            "/v1/archive/conversation-history",
            headers=headers,
            params={"session_id": session_id},
        )

    assert sessions.status_code == 200, sessions.text
    assert [item["session_id"] for item in sessions.json()["items"]] == [session_id]
    assert history.status_code == 200, history.text
    assert [
        (turn["owner_text"], turn["assistant_text"]) for turn in history.json()["turns"]
    ] == [(_USER_TEXT, _REPLY_TEXT)]


@pytest.mark.asyncio
async def test_untrusted_device_profile_sends_nothing_to_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Production today: without a device attestation Control denies
    # memory_recall_private (device_untrusted) and signs a chat-only profile.
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", _POLICY_TOKEN)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(client, app, username="device-review-untrusted")
        account_id = str(owner["user_id"])
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=account_id,
            session_id=session_id,
            capabilities=("chat", "tutor", "english_practice"),
        )

        evidence = await _agent_turn(client, session_id=session_id, account_id=account_id)
        sessions = await client.get("/v1/archive/conversation-sessions", headers=headers)

    assert evidence == []
    assert sessions.status_code == 200, sessions.text
    assert sessions.json()["items"] == []
