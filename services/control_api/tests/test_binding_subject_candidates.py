"""A second binding can reuse a child the account already declared."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import _bind, _owner, _RecordingConsent
from services.control_api.tests.test_guardian_api import _configure

CANDIDATES = "/v1/device-bindings/subject-candidates"


async def _rebind_existing(
    client: AsyncClient,
    app: Any,
    *,
    owner_id: str,
    headers: dict[str, str],
    device_id: str,
    child_id: str,
) -> Any:
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=datetime.now(UTC),
        ttl=timedelta(minutes=5),
        nonce=f"nonce-{device_id}",
    )
    return await client.post(
        "/v1/device-bindings",
        headers={**headers, "Idempotency-Key": f"bind-{device_id}"},
        json={
            "device_claim_token": token,
            "declared_mode": "parent_for_child",
            "account_owner_person_id": owner_id,
            "primary_subject": {"person_id": child_id, "relationship": "guardian_of"},
            "persona_selection": "taoxi",
            "service_preferences": {"memory_level": "none"},
            "consent_offer_ids": ["offer_minor_voice_session_v1"],
        },
    )


async def _bind_child(
    client: AsyncClient, app: Any, *, owner_id: str, headers: dict[str, str], device_id: str
) -> str:
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id=device_id,
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=["offer_minor_voice_session_v1"],
        preferences={"memory_level": "none"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["primary_subject_ids"][0])


@pytest.mark.asyncio
async def test_a_declared_child_is_listed_only_for_the_owner_who_declared_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.bound_subject_consent = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, headers = await _owner(client, app, "candidate-owner")
        _, other_headers = await _owner(client, app, "candidate-stranger")
        assert (await client.get(CANDIDATES, headers=headers)).json() == {"subjects": []}

        child_id = await _bind_child(
            client, app, owner_id=owner_id, headers=headers, device_id="device-candidate-a"
        )

        listed = await client.get(CANDIDATES, headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json() == {
            "subjects": [{"person_id": child_id, "display_name": "使用人", "age_band": "under_14"}]
        }
        # The child belongs to the account that declared it, nobody else's.
        assert (await client.get(CANDIDATES, headers=other_headers)).json() == {"subjects": []}
        assert (await client.get(CANDIDATES)).status_code == 401


@pytest.mark.asyncio
async def test_an_unbound_child_is_not_offered_for_rebinding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unbind clears the child (#283), so nothing of theirs is left to reuse."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.bound_subject_consent = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, headers = await _owner(client, app, "candidate-rebinder")
        child_id = await _bind_child(
            client, app, owner_id=owner_id, headers=headers, device_id="device-candidate-b1"
        )
        unbound = await client.post(
            "/v1/devices/device-candidate-b1/binding/unbind",
            headers=headers,
            json={"reason": "unbind"},
        )
        assert unbound.status_code == 200, unbound.text

        candidates = (await client.get(CANDIDATES, headers=headers)).json()["subjects"]
        assert candidates == []
        person = await app.state.identity_service.get_person(
            child_id, actor_person_id=owner_id
        )
        assert (person.display_name, person.status) == ("已删除的使用人", "disabled")


@pytest.mark.asyncio
async def test_only_a_child_that_is_still_bound_stays_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.bound_subject_consent = _RecordingConsent()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, headers = await _owner(client, app, "candidate-deleter")
        still_bound = await _bind_child(
            client, app, owner_id=owner_id, headers=headers, device_id="device-candidate-c1"
        )
        erased = await _bind_child(
            client, app, owner_id=owner_id, headers=headers, device_id="device-candidate-c2"
        )
        unbound = await client.post(
            "/v1/devices/device-candidate-c2/binding/unbind",
            headers=headers,
            json={"reason": "unbind"},
        )
        assert unbound.status_code == 200, unbound.text

        candidates = (await client.get(CANDIDATES, headers=headers)).json()["subjects"]
        assert [item["person_id"] for item in candidates] == [still_bound]
        erased_person = await app.state.identity_service.get_person(
            erased, actor_person_id=owner_id
        )
        assert erased_person.status == "disabled"
