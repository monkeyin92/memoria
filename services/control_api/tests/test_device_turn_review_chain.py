"""A device turn travels Agent -> Archive -> 回顾 under the profile Control signs.

The Agent reads the real ``/v1/interaction/session-policy`` response, claims
the device-bound owner, and hands its archive evidence to the real
``/v1/archive/session-events`` route; the owner then reads it back through the
review endpoints the mini program calls.  The profile never carries
``memory_capture`` (an action-time capability), exactly as in production.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
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
    _register_minor,
    _register_verified_adult,
)
from services.control_api.tests.test_interaction_api import (
    _RUNTIME_PROFILE_SIGNING_KEY,
    _attach_signed_runtime_profile,
)
from services.guardian.domain import PersonConsentRecord

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


async def _grant_guardian_consents(app: Any, *, child_id: str, kinds: tuple[str, ...]) -> None:
    """The guardian's ticks at binding: the voice session and, optionally, memory."""

    for kind in kinds:
        await app.state.guardian_store.grant_person_consent(
            PersonConsentRecord(
                consent_id=str(uuid.uuid4()),
                subject_person_id=child_id,
                grantor_person_id="device-review-guardian",
                consent_kind=kind,
                policy_version="minor-memory-v1",
                granted_at=datetime.now(UTC),
                evidence_event_id=f"device-review-{kind}-{child_id}",
            ),
            actor_person_id="device-review-guardian",
        )


@pytest.mark.asyncio
async def test_consented_child_turn_is_kept_for_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A child whose guardian ticked long-term memory: Control signs the memory
    # grant into the child's profile, the Agent archives the turn, and the
    # archive keeps the words for that child (not an aggregate).
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", _POLICY_TOKEN)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        child = await _register_minor(client, app, username="device-review-child")
        account_id = str(child["user_id"])
        headers = {"Authorization": f"Bearer {child['access_token']}"}
        await _grant_guardian_consents(
            app, child_id=account_id, kinds=("minor_voice_session", "memory_retention")
        )
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=account_id,
            session_id=session_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
        )

        evidence = await _agent_turn(client, session_id=session_id, account_id=account_id)
        assert [event["event_type"] for event in evidence] == [
            "speech.utterance_finalized",
            "assistant.playout_stopped",
        ]
        for event in evidence:
            assert event["payload"].get("aggregate_only") is not True
            stored = await client.post("/v1/archive/session-events", headers=_ARCHIVE, json=event)
            assert stored.status_code == 201, stored.text

        history = await client.get(
            "/v1/archive/conversation-history",
            headers=headers,
            params={"session_id": session_id},
        )
        kept = await app.state.life_archive.event(
            account_id=account_id, event_id=str(evidence[0]["event_id"])
        )

    assert kept is not None
    assert kept.subject_id == account_id
    assert kept.payload.get("text") == _USER_TEXT
    assert kept.payload.get("history_eligible") is True
    assert kept.payload.get("memory_retention") == "retained"
    assert history.status_code == 200, history.text
    assert [
        (turn["owner_text"], turn["assistant_text"]) for turn in history.json()["turns"]
    ] == [(_USER_TEXT, _REPLY_TEXT)]


@pytest.mark.asyncio
async def test_child_without_the_guardian_grant_sends_nothing_to_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No memory consent, or an untrusted device: Control signs a chat-only
    # profile for the child and the Agent hands nothing to the archive.
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", _POLICY_TOKEN)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        child = await _register_minor(client, app, username="device-review-child-none")
        account_id = str(child["user_id"])
        headers = {"Authorization": f"Bearer {child['access_token']}"}
        await _grant_guardian_consents(app, child_id=account_id, kinds=("minor_voice_session",))
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=account_id,
            session_id=session_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "english_practice"),
        )

        evidence = await _agent_turn(client, session_id=session_id, account_id=account_id)

    assert evidence == []
