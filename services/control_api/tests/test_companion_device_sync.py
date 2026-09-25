"""The companion picker is authoritative for the devices an account runs.

① a pick becomes the primary subject's persona override on the owner's binding
   and reaches the next Runtime Profile read;
② replaying the same pick writes nothing and projects nothing;
③ a family binding pins the primary subject, not the account owner;
④ a failing identity write never fails the profile save;
⑤ the projection runs with next-session semantics, and the persona-assignment
   route now projects too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.tests.test_persona_assignment_api import (
    _auth,
    _bind_family,
    _bind_self,
    _env,
    _path,
    _register,
)


class _Invalidations:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **payload: object) -> dict[str, object]:
        self.calls.append(dict(payload))
        return {"delivered": True}


def _install_invalidator(app: Any) -> _Invalidations:
    recorder = _Invalidations()
    app.state.device_runtime_invalidator = recorder
    return recorder


async def _pick(client: AsyncClient, user: dict, companion_id: str) -> dict:
    response = await client.put(
        f"/v1/memory/profile/{user['user_id']}",
        headers=_auth(user),
        json={"companion_id": companion_id, "display_name": "我"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["companion_id"] == companion_id
    return response.json()


async def _verified_adult(app: Any, user: dict) -> None:
    """Age facts a confirmed adult subject needs; without them it stays unknown-safe."""
    await app.state.identity_service.register_person(
        person_id=user["user_id"],
        display_name="我",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=datetime.now(UTC),
    )


async def _confirm_subject(
    client: AsyncClient, user: dict, *, device_id: str, subject_id: str
) -> None:
    """Confirm who uses the device, as production's sole-subject session does.

    The PostgreSQL control session confirms a one-to-one binding's only
    subject on its own; the in-memory control used here starts unconfirmed
    and would otherwise resolve the binding default for "nobody".
    """
    headers = _auth(user)
    first = await client.get(f"/v1/devices/{device_id}/runtime-profile", headers=headers)
    assert first.status_code == 200
    confirmed = await client.post(
        f"/v1/sessions/{first.json()['session_id']}/active-subject",
        headers=headers,
        json={"person_id": subject_id, "confirmation_method": "app_confirm"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["active_subject_id"] == subject_id


def _override_for(listing: dict, subject_id: str) -> dict | None:
    return next(
        (item for item in listing["assignments"] if item["subject_id"] == subject_id),
        None,
    )


@pytest.mark.asyncio
async def test_pick_applies_to_owner_binding_primary_subject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "picker-self")
    invalidations = _install_invalidator(app)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "picker-owner")
        await _verified_adult(app, owner)
        await _bind_self(
            client, app, owner=owner, device_id="dev-picker", nonce="nonce-picker"
        )
        headers = _auth(owner)
        await _confirm_subject(
            client, owner, device_id="dev-picker", subject_id=owner["user_id"]
        )
        invalidations.calls.clear()

        await _pick(client, owner, "taoxi")

        listing = (await client.get(_path("dev-picker"), headers=headers)).json()
        override = _override_for(listing, owner["user_id"])
        assert override is not None
        assert override["assignment_id"] == "taoxi:v1"
        # The rotated profile already carries the pick; the device is told to
        # take it at the next session, never mid-reply.
        profile = await client.get("/v1/devices/dev-picker/runtime-profile", headers=headers)
        assert profile.json()["persona_assignment_id"] == "taoxi:v1"
        assert profile.json()["persona"]["persona_id"] == "taoxi"
        assert invalidations.calls, "the persona change must be projected"
        assert {call["apply_at"] for call in invalidations.calls} == {"next_session"}
        assert {call["device_id"] for call in invalidations.calls} == {"dev-picker"}

        # ② Replaying the same pick is silent: no new write, no projection.
        invalidations.calls.clear()
        await _pick(client, owner, "taoxi")
        replay = (await client.get(_path("dev-picker"), headers=headers)).json()
        assert _override_for(replay, owner["user_id"]) == override
        assert invalidations.calls == []

        # Picking the binding default again still wins over the old override.
        await _pick(client, owner, "starlight")
        back = (await client.get(_path("dev-picker"), headers=headers)).json()
        assert _override_for(back, owner["user_id"])["assignment_id"] == "starlight:v1"
        profile = await client.get("/v1/devices/dev-picker/runtime-profile", headers=headers)
        assert profile.json()["persona"]["persona_id"] == "starlight"


@pytest.mark.asyncio
async def test_pick_without_companion_field_leaves_devices_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "picker-unrelated")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "unrelated-owner")
        await _bind_self(
            client, app, owner=owner, device_id="dev-unrelated", nonce="nonce-unrelated"
        )
        saved = await client.put(
            f"/v1/memory/profile/{owner['user_id']}",
            headers=_auth(owner),
            json={"display_name": "只改名字"},
        )
        assert saved.status_code == 200
        listing = (await client.get(_path("dev-unrelated"), headers=_auth(owner))).json()
        assert listing["assignments"] == []


@pytest.mark.asyncio
async def test_family_binding_pins_the_primary_subject_not_the_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "picker-family")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "picker-family-owner")
        child_id = await _bind_family(
            client, app, owner=owner, device_id="dev-family", nonce="nonce-family"
        )
        await _pick(client, owner, "mianmian")
        listing = (await client.get(_path("dev-family"), headers=_auth(owner))).json()
        assert [
            (item["subject_id"], item["assignment_id"]) for item in listing["assignments"]
        ] == [(child_id, "mianmian:v1")]


@pytest.mark.asyncio
async def test_identity_failure_never_fails_the_profile_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "picker-failure")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "picker-failure-owner")
        await _bind_self(
            client, app, owner=owner, device_id="dev-failure", nonce="nonce-failure"
        )

        async def broken(**_kwargs: object) -> None:
            raise RuntimeError("identity authority is down")

        monkeypatch.setattr(app.state.identity_service, "set_persona_assignment", broken)
        saved = await _pick(client, owner, "axu")
        assert saved["companion_id"] == "axu"
        listing = (await client.get(_path("dev-failure"), headers=_auth(owner))).json()
        assert listing["assignments"] == []


@pytest.mark.asyncio
async def test_persona_assignment_route_projects_next_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _env(monkeypatch, tmp_path, "picker-route-projection")
    invalidations = _install_invalidator(app)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "route-projection-owner")
        await _verified_adult(app, owner)
        await _bind_self(
            client, app, owner=owner, device_id="dev-route", nonce="nonce-route"
        )
        headers = _auth(owner)
        await _confirm_subject(
            client, owner, device_id="dev-route", subject_id=owner["user_id"]
        )
        invalidations.calls.clear()

        pinned = await client.put(
            _path("dev-route", owner["user_id"]),
            headers=headers,
            json={"persona_selection": "xuanmo"},
        )
        assert pinned.status_code == 200
        assert [call["apply_at"] for call in invalidations.calls] == ["next_session"]

        invalidations.calls.clear()
        removed = await client.delete(_path("dev-route", owner["user_id"]), headers=headers)
        assert removed.status_code == 200
        assert [call["apply_at"] for call in invalidations.calls] == ["next_session"]
