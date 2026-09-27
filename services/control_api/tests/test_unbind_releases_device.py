"""Unbinding in the Mini Program also frees the device in the fleet.

It used to revoke only the Identity binding: the board kept its activation
manifest and a fresh scan of its QR was refused as already bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from services.control_api.tests.test_device_display_profile import (
    CERTIFICATE_ID,
    DEVICE_ID,
    PATH,
    _bound_device,
    _signed_headers,
)
from services.control_api.tests.test_device_onboarding_binding_integration import _app
from services.device_fleet.bootstrap_domain import DeviceLifecycle

MANIFEST_PATH = f"/v1/devices/{DEVICE_ID}/activation-manifest"


@pytest.mark.asyncio
async def test_unbind_releases_the_fleet_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    service = app.state.device_onboarding_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _bound_device(client, app, key)
        before = service.store.get_device(DEVICE_ID)
        assert before.lifecycle_status is DeviceLifecycle.BOUND
        manifest = await client.get(
            MANIFEST_PATH, headers=_signed_headers(key, path=MANIFEST_PATH)
        )
        assert manifest.status_code == 200

        unbound = await client.post(
            f"/v1/devices/{DEVICE_ID}/binding/unbind",
            headers={"Authorization": f"Bearer {owner['access_token']}"},
            json={"reason": "unbind", "purge_subject_data": False},
        )
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["device_released"] is True

        after = service.store.get_device(DEVICE_ID)
        assert after.lifecycle_status is DeviceLifecycle.PROVISIONED
        assert (after.actor_id, after.binding_id) == (None, None)
        assert after.activation_version == before.activation_version
        # The board now learns it is released and brings its QR back.
        manifest = await client.get(
            MANIFEST_PATH, headers=_signed_headers(key, path=MANIFEST_PATH)
        )
        display = await client.get(PATH, headers=_signed_headers(key))
    assert manifest.status_code == 409
    assert display.status_code == 409
    assert after.certificate_id == CERTIFICATE_ID
