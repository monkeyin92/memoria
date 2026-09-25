"""``GET /v1/devices/{device_id}/display-profile``: the idle mascot poll.

① a bound device authenticates exactly like the activation manifest and reads
   the companion its next session would use, uncached and write-free;
② a pick in the mini program changes the companion and the display version;
③ bad, missing, foreign or cross-endpoint signatures, an unknown device and an
   unbound device fail like the manifest route;
④ personas without mascot art fall back to their base companion, then the
   account companion, then the shipped default.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_display_profile import (
    DeviceDisplayProfile,
    mascot_for_persona,
)
from services.control_api.tests.test_device_onboarding_binding_integration import (
    _app,
    _online_claim,
)
from services.control_api.tests.test_persona_companion_readends import _custom_record
from services.device_fleet.bootstrap_domain import b64url_encode, canonical_json_bytes
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.identity.domain import IdentityNotFoundError

DEVICE_ID = "dev_onboarding_integration"
CERTIFICATE_ID = "cert_onboarding_integration"
PATH = f"/v1/devices/{DEVICE_ID}/display-profile"
MASCOTS = {"starlight", "taoxi", "mianmian", "axu", "xuanmo"}


def _signed_headers(
    key: Ed25519PrivateKey,
    *,
    path: str = PATH,
    certificate_id: str = CERTIFICATE_ID,
    device_id: str = DEVICE_ID,
) -> dict[str, str]:
    payload = {
        "certificate_id": certificate_id,
        "device_id": device_id,
        "method": "GET",
        "path": path,
    }
    # The firmware's exact bytes: keys sorted, no whitespace.
    assert canonical_json_bytes(payload) == (
        f'{{"certificate_id":"{certificate_id}","device_id":"{device_id}",'
        f'"method":"GET","path":"{path}"}}'
    ).encode()
    return {
        "X-Device-Certificate-ID": certificate_id,
        "X-Device-Signature": b64url_encode(key.sign(canonical_json_bytes(payload))),
    }


async def _bound_device(
    client: AsyncClient, app: Any, key: Ed25519PrivateKey
) -> dict[str, Any]:
    service = app.state.device_onboarding_service
    assert isinstance(service, DeviceOnboardingService)
    registered = await client.post(
        "/v1/auth/register",
        json={"username": "display-owner", "password": "safe-password"},
    )
    assert registered.status_code == 201
    owner = registered.json()
    session_id, claim_id = _online_claim(service, actor_id=owner["user_id"], device_key=key)
    bound = await client.post(
        "/v1/device-bindings",
        headers={
            "Authorization": f"Bearer {owner['access_token']}",
            "Idempotency-Key": "display-binding-01",
        },
        json={
            "claim_id": claim_id,
            "onboarding_session_id": session_id,
            "declared_mode": "self_use",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {"person_id": owner["user_id"], "relationship": "self"},
            "persona_selection": "starlight",
            "service_preferences": {
                "memory_level": "personal",
                "interview_frequency": "low",
            },
            "consent_offer_ids": ["offer_self_memory_retention_v1"],
        },
    )
    assert bound.status_code == 201, bound.text
    return owner


@pytest.mark.asyncio
async def test_bound_device_reads_its_companion_and_follows_the_picker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _bound_device(client, app, key)
        auth = {"Authorization": f"Bearer {owner['access_token']}"}

        # A poll is read-only: it must never mark the manifest downloaded.
        service = app.state.device_onboarding_service

        def no_writes(**_kwargs: object) -> None:
            raise AssertionError("display poll must not write")

        monkeypatch.setattr(service.store, "mark_activation_downloaded", no_writes)

        first = await client.get(PATH, headers=_signed_headers(key))
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "no-store"
        body = first.json()
        assert set(body) == {"schema_version", "device_id", "companion_id", "display_version"}
        assert body["schema_version"] == 1
        assert body["device_id"] == DEVICE_ID
        assert body["companion_id"] == "starlight"
        assert isinstance(body["display_version"], str) and 0 < len(body["display_version"]) <= 32

        again = await client.get(PATH, headers=_signed_headers(key))
        assert again.json() == body, "an unchanged companion keeps its display version"

        picked = await client.put(
            f"/v1/memory/profile/{owner['user_id']}",
            headers=auth,
            json={"companion_id": "axu"},
        )
        assert picked.status_code == 200
        after = (await client.get(PATH, headers=_signed_headers(key))).json()
        assert after["companion_id"] == "axu"
        assert after["display_version"] != body["display_version"]

        # A tutor persona has no mascot art: the account companion stands in.
        tutor = await client.put(
            f"/v1/devices/{DEVICE_ID}/persona-assignments/{owner['user_id']}",
            headers=auth,
            json={"persona_selection": "zhiyao"},
        )
        assert tutor.status_code == 200, tutor.text
        fallback = (await client.get(PATH, headers=_signed_headers(key))).json()
        assert fallback["companion_id"] == "axu"
        assert fallback["display_version"] == after["display_version"]


@pytest.mark.asyncio
async def test_display_profile_authenticates_like_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _bound_device(client, app, key)

        missing_certificate = await client.get(
            PATH, headers={"X-Device-Signature": _signed_headers(key)["X-Device-Signature"]}
        )
        assert missing_certificate.status_code == 401
        assert missing_certificate.json()["detail"]["code"] == "device_certificate_required"
        assert missing_certificate.headers["cache-control"] == "no-store"

        foreign_key = await client.get(
            PATH, headers=_signed_headers(Ed25519PrivateKey.generate())
        )
        assert foreign_key.status_code == 422
        assert foreign_key.json()["detail"]["code"] == "DEVICE_PROOF_INVALID"

        # A manifest signature never authenticates the display endpoint.
        cross_endpoint = await client.get(
            PATH,
            headers=_signed_headers(
                key, path=f"/v1/devices/{DEVICE_ID}/activation-manifest"
            ),
        )
        assert cross_endpoint.status_code == 422

        wrong_certificate = await client.get(
            PATH, headers=_signed_headers(key, certificate_id="cert_someone_else")
        )
        assert wrong_certificate.status_code == 422

        malformed = await client.get(
            PATH,
            headers={"X-Device-Certificate-ID": CERTIFICATE_ID, "X-Device-Signature": "nope"},
        )
        assert malformed.status_code == 422

        unknown = await client.get(
            "/v1/devices/dev_unknown/display-profile",
            headers=_signed_headers(
                key,
                device_id="dev_unknown",
                path="/v1/devices/dev_unknown/display-profile",
            ),
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

        # Offline mock tolerates an unsigned read exactly as the manifest does;
        # outside it, the signature is mandatory.
        service = app.state.device_onboarding_service
        monkeypatch.setattr(service, "offline_mock", False)
        unsigned = await client.get(
            PATH, headers={"X-Device-Certificate-ID": CERTIFICATE_ID}
        )
        assert unsigned.status_code == 422
        assert unsigned.json()["detail"]["code"] == "DEVICE_PROOF_INVALID"
        signed = await client.get(PATH, headers=_signed_headers(key))
        assert signed.status_code == 200


@pytest.mark.asyncio
async def test_unbound_device_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    service = app.state.device_onboarding_service
    _online_claim(service, actor_id="someone", device_key=key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH, headers=_signed_headers(key))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BINDING_CONFLICT"


class _Identity:
    def __init__(self, records: dict[str, Any] | None = None) -> None:
        self._records = records or {}

    async def get_custom_persona(
        self,
        persona_id: str,
        *,
        owner_person_id: str,
        actor_person_id: str | None = None,
    ) -> Any:
        del actor_person_id
        record = self._records.get(persona_id)
        if record is None or record.owner_person_id != owner_person_id:
            raise IdentityNotFoundError(persona_id)
        return record


class _Profiles:
    def __init__(self, companion_id: str | None) -> None:
        self.companion_id = companion_id

    def get_subject_profile(self, *, user_id: str) -> dict[str, Any] | None:
        del user_id
        return None if self.companion_id is None else {"companion_id": self.companion_id}


async def _mascot(persona_id: str, *, identity: Any, account: str | None) -> str:
    return await mascot_for_persona(
        persona_id,
        identity=identity,
        profiles=_Profiles(account),
        owner_id="owner",
        actor_id="owner",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("persona_id", sorted(MASCOTS))
async def test_mascot_personas_map_to_themselves(persona_id: str) -> None:
    assert await _mascot(persona_id, identity=_Identity(), account="taoxi") == persona_id


@pytest.mark.asyncio
async def test_personas_without_art_fall_back_in_order() -> None:
    custom_id = "cu_0123456789abcdef"
    tutor_based_id = "cu_fedcba9876543210"
    identity = _Identity(
        {
            custom_id: _custom_record(custom_id, owner_person_id="owner"),  # base xuanmo
            tutor_based_id: replace(
                _custom_record(tutor_based_id, owner_person_id="owner"),
                fallback_designed_voice="zhiyao",
            ),
        }
    )

    # A custom persona shows its base built-in companion.
    assert await _mascot(custom_id, identity=identity, account="taoxi") == "xuanmo"
    # A custom base without art, an unreadable custom persona, a tutor persona
    # and an unknown id all fall back to the account's companion ...
    assert await _mascot(tutor_based_id, identity=identity, account="taoxi") == "taoxi"
    assert await _mascot("cu_ffffffffffffffff", identity=identity, account="axu") == "axu"
    assert await _mascot("zhiyao", identity=identity, account="mianmian") == "mianmian"
    assert await _mascot("yanxi", identity=None, account="mianmian") == "mianmian"
    assert await _mascot("no-such-persona", identity=identity, account="axu") == "axu"
    # ... and to the shipped default when that has no art either.
    assert await _mascot("zhiyao", identity=identity, account="yanxi") == "starlight"
    assert await _mascot("zhiyao", identity=identity, account=None) == "starlight"


def test_display_version_changes_with_the_companion_only() -> None:
    def version(binding_id: str, companion_id: str, persona_id: str) -> str:
        return DeviceDisplayProfile(
            device_id="dev",
            binding_id=binding_id,
            persona_id=persona_id,
            companion_id=companion_id,
        ).display_version

    assert version("bd_1", "axu", "axu") == version("bd_1", "axu", "cu_x")
    assert version("bd_1", "axu", "axu") != version("bd_1", "taoxi", "taoxi")
    assert version("bd_1", "axu", "axu") != version("bd_2", "axu", "axu")
