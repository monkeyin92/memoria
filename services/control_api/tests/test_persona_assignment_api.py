"""T-C acceptance: per-subject persona assignments on a device binding.

① the owner pins a built-in persona to a subject and re-pinning is idempotent;
② deleting the override falls back to the binding default;
③ an unknown id, or another account's custom persona, is rejected as ``422``;
④ a non-member can neither read nor write;
⑤ the binding writes stay member-scoped on read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.tests.identity_test_helpers import (
    install_test_identity_authority,
)

STRUCTURED = {
    "style_description": "温和的陪伴者",
    "warmth": "warm",
    "directness": "gentle",
    "response_length": "brief",
    "question_frequency": "rare",
    "interview_depth": "light",
    "welcome_text": "嗨，我在。",
    "conversation_instruction": "说话短一点，像朋友。",
    "voice_instruction": "轻松自然",
    "default_voice_emotion": "neutral",
    "default_voice_rate": 1.0,
}


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str):
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / f"{name}.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_IDENTITY_DB_PATH", str(tmp_path / f"{name}-identity.sqlite3")
    )
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET", f"{name}-auth-secret-long-enough-0123456789"
    )
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        f"{name}-transfer-evidence-secret-32-bytes",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    install_test_identity_authority(
        app, secret=app.state.settings.transfer_evidence_key()
    )
    return app


async def _register(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    return response.json()


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {user['access_token']}"}


async def _bind_self(
    client: AsyncClient,
    app,
    *,
    owner: dict,
    device_id: str,
    nonce: str,
) -> None:
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=nonce,
    )
    response = await client.post(
        "/v1/device-bindings",
        headers=_auth(owner),
        json={
            "device_claim_token": token,
            "declared_mode": "self_use",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": owner["user_id"],
                "relationship": "self",
            },
            "persona_selection": "starlight",
            "service_preferences": {
                "memory_level": "personal",
                "interview_frequency": "low",
            },
            "consent_offer_ids": ["offer_self_memory_retention_v1"],
        },
    )
    assert response.status_code == 201


def _path(device_id: str, person_id: str | None = None) -> str:
    base = f"/v1/devices/{device_id}/persona-assignments"
    return base if person_id is None else f"{base}/{person_id}"


@pytest.mark.asyncio
async def test_owner_pins_builtin_and_delete_falls_back_to_binding_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "persona-assign-builtin")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "assign-owner")
        await _bind_self(
            client, app, owner=owner, device_id="dev-assign", nonce="nonce-assign"
        )
        headers = _auth(owner)
        subject = owner["user_id"]

        # Nothing pinned yet: no overrides, and the binding default stands.
        listing = await client.get(_path("dev-assign"), headers=headers)
        assert listing.status_code == 200
        assert listing.json()["assignments"] == []
        assert "starlight" in listing.json()["binding_default"]

        pinned = await client.put(
            _path("dev-assign", subject),
            headers=headers,
            json={"persona_selection": "taoxi"},
        )
        assert pinned.status_code == 200
        assert pinned.json()["persona_id"] == "taoxi"
        assert pinned.json()["assignment_id"] == "taoxi:v1"

        # Replaying the same persona keeps one row rather than adding a second.
        replay = await client.put(
            _path("dev-assign", subject),
            headers=headers,
            json={"persona_selection": "taoxi"},
        )
        assert replay.status_code == 200
        assert replay.json()["assignment_id"] == "taoxi:v1"
        listing = await client.get(_path("dev-assign"), headers=headers)
        assert len(listing.json()["assignments"]) == 1

        removed = await client.delete(_path("dev-assign", subject), headers=headers)
        assert removed.status_code == 200
        assert removed.json()["removed"] is True
        assert "starlight" in removed.json()["effective"]
        listing = await client.get(_path("dev-assign"), headers=headers)
        assert listing.json()["assignments"] == []


@pytest.mark.asyncio
async def test_unknown_and_foreign_personas_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "persona-assign-reject")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "reject-owner")
        await _bind_self(
            client, app, owner=owner, device_id="dev-reject", nonce="nonce-reject"
        )
        headers = _auth(owner)
        subject = owner["user_id"]

        unknown = await client.put(
            _path("dev-reject", subject),
            headers=headers,
            json={"persona_selection": "not-a-persona"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["detail"]["code"] == "persona_selection_invalid"

        # A different account's custom persona is not assignable here.
        outsider = await _register(client, "reject-outsider")
        created = await client.post(
            "/v1/personas",
            headers=_auth(outsider),
            json={
                "display_name": "别人的伙伴",
                "structured": STRUCTURED,
                "fallback_designed_voice": "starlight",
            },
        )
        assert created.status_code == 201
        foreign_id = created.json()["persona_id"]

        crossed = await client.put(
            _path("dev-reject", subject),
            headers=headers,
            json={"persona_selection": foreign_id},
        )
        assert crossed.status_code == 422
        assert crossed.json()["detail"]["code"] == "persona_selection_invalid"


@pytest.mark.asyncio
async def test_owner_can_assign_an_own_custom_persona(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "persona-assign-custom")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "custom-owner")
        await _bind_self(
            client, app, owner=owner, device_id="dev-custom", nonce="nonce-custom"
        )
        headers = _auth(owner)

        created = await client.post(
            "/v1/personas",
            headers=headers,
            json={
                "display_name": "奶奶的伙伴",
                "structured": STRUCTURED,
                "fallback_designed_voice": "xuanmo",
            },
        )
        assert created.status_code == 201
        custom_id = created.json()["persona_id"]
        assert custom_id.startswith("cu_")

        pinned = await client.put(
            _path("dev-custom", owner["user_id"]),
            headers=headers,
            json={"persona_selection": custom_id},
        )
        assert pinned.status_code == 200
        assert pinned.json()["persona_id"] == custom_id
        assert pinned.json()["assignment_id"] == f"{custom_id}:v1"


@pytest.mark.asyncio
async def test_non_member_cannot_read_or_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "persona-assign-stranger")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "stranger-owner")
        await _bind_self(
            client, app, owner=owner, device_id="dev-stranger", nonce="nonce-stranger"
        )
        stranger = await _register(client, "totally-unrelated")
        headers = _auth(stranger)

        read = await client.get(_path("dev-stranger"), headers=headers)
        assert read.status_code == 403
        assert read.json()["detail"]["code"] == "subject_not_binding_member"

        write = await client.put(
            _path("dev-stranger", stranger["user_id"]),
            headers=headers,
            json={"persona_selection": "taoxi"},
        )
        assert write.status_code == 403
        assert write.json()["detail"]["code"] == "subject_not_binding_member"
