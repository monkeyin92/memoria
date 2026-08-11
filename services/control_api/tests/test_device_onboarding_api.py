from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from services.common.miniprogram_gateway_ticket import verify_device_gateway_ticket
from services.control_api.app.routes import device_onboarding, media
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.device_fleet.bootstrap_domain import (
    b64url_encode,
    canonical_json_bytes,
    sha256_hex,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.tests.test_bootstrap_vertical_slice import (
    _fixture,
    _online,
)


def _app(service: DeviceOnboardingService | None, actor: str = "person_a") -> FastAPI:
    app = FastAPI()
    app.include_router(device_onboarding.router)
    app.include_router(media.device_router)
    if service is not None:
        app.state.device_onboarding_service = service
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedUser(
        user_id=actor, session_id="test-session", jti="test-jti"
    )
    return app


def _activate_device(
    service: DeviceOnboardingService,
    store: object,
    device_key: Ed25519PrivateKey,
    payload: object,
) -> dict[str, object]:
    _online(service, store, device_key, payload)  # type: ignore[arg-type]
    session = store.find_session_by_client(  # type: ignore[attr-defined]
        actor_id="person_a",
        client_onboarding_id="client_a",
    )
    assert session is not None
    claim = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=session.onboarding_session_id,
        device_id=session.device_id,
        idempotency_key="api-media-claim",
        expected_state_version=session.state_version,
    )
    result = service.create_binding(
        actor_id="person_a",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=session.onboarding_session_id,
        initialization={
            "declared_mode": "self_use",
            "account_owner_person_id": "person_a",
            "primary_subject": {"person_id": "person_a", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        },
        idempotency_key="api-media-binding",
    )
    manifest = result["manifest"]
    assert isinstance(manifest, dict)
    unsigned: dict[str, object] = {
        "device_id": "dev_test_01",
        "certificate_id": "cert_test_01",
        "binding_id": manifest["binding_id"],
        "binding_version": manifest["binding_version"],
        "activation_version": manifest["activation_version"],
        "config_hash": manifest["config_hash"],
        "firmware_version": "0.1.0",
        "monotonic_counter": 2,
        "applied_at": "2026-08-11T08:00:00Z",
    }
    service.accept_activation_ack(
        device_id="dev_test_01",
        ack={
            **unsigned,
            "signature": b64url_encode(
                device_key.sign(canonical_json_bytes(unsigned))
            ),
        },
    )
    return manifest


def _client_payload() -> dict[str, object]:
    return {
        "platform": "wechat-miniprogram",
        "app_version": "0.1.0",
        "base_library_version": "3.0.0",
    }


@pytest.mark.asyncio
async def test_missing_service_is_explicitly_unavailable() -> None:
    app = _app(None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/device-bootstrap/introspect",
            json={
                "qr_payload": "memoria-bootstrap:v1:x.y",
                "client_onboarding_id": "client_a",
                "client": _client_payload(),
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "device_onboarding_unavailable"


@pytest.mark.asyncio
async def test_offline_registration_maps_public_key_contract_to_service() -> None:
    service, _store, _device_key, _payload = _fixture()
    app = _app(service)
    new_key = Ed25519PrivateKey.generate()
    public_key = b64url_encode(new_key.public_key().public_bytes_raw())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/device-fleet/offline-mock/devices",
            json={
                "device_id": "dev_http_registration",
                "certificate_id": "cert_http_registration",
                "public_key": public_key,
                "product_model": "memoria-atk-dnesp32s3-v1",
                "hardware_revision": "rev-a",
                "firmware_version": "0.1.0",
                "firmware_security_version": 1,
                "capability_manifest_hash": sha256_hex(b"http-registration"),
                "minimum_firmware_security_version": 1,
            },
        )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "device_id": "dev_http_registration",
        "certificate_id": "cert_http_registration",
        "public_key": public_key,
        "lifecycle_status": "manufactured",
    }


@pytest.mark.asyncio
async def test_real_api_response_shapes_match_miniprogram_contracts() -> None:
    service, store, device_key, payload = _fixture()
    app = _app(service)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        from services.device_fleet.bootstrap_domain import encode_bootstrap_qr

        introspected = await client.post(
            "/v1/device-bootstrap/introspect",
            json={
                "qr_payload": encode_bootstrap_qr(payload, device_key),
                "client_onboarding_id": "api-client-a",
                "client": _client_payload(),
            },
        )
        assert introspected.status_code == 200, introspected.text
        onboarding = introspected.json()
        assert set(onboarding) == {
            "onboarding_session_id",
            "state",
            "state_version",
            "expires_at",
            "device",
            "provisioning",
            "mobile_nonce",
        }
        assert set(onboarding["device"]) == {
            "device_id",
            "display_tail",
            "model",
            "firmware_version",
            "claim_status",
        }
        assert onboarding["device"]["claim_status"] in {
            "unclaimed",
            "reserved",
            "bound",
            "suspended",
            "revoked",
        }
        session_id = onboarding["onboarding_session_id"]
        session_response = await client.get(f"/v1/device-bootstrap/{session_id}")
        assert session_response.status_code == 200
        assert set(session_response.json()) == {
            "onboarding_session_id",
            "state",
            "state_version",
            "expires_at",
            "device",
            "provisioning",
        }

    # Complete the device proof through the service seam, then exercise the
    # public user claim endpoint with the state_version returned by the store.
    _online(
        service,
        store,
        device_key,
        replace(payload, bootstrap_nonce="ZmVkY2JhOTg3NjU0MzIxMA"),
        actor_id="person_a",
        client_id="api-client-b",
    )
    second = store.find_session_by_client(actor_id="person_a", client_onboarding_id="api-client-b")
    assert second is not None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        claimed = await client.post(
            "/v1/device-claims",
            json={
                "onboarding_session_id": second.onboarding_session_id,
                "device_id": second.device_id,
                "idempotency_key": "api-claim-01",
                "expected_state_version": second.state_version,
            },
        )
        assert claimed.status_code == 201, claimed.text
        claim = claimed.json()
        assert set(claim) == {
            "claim_id",
            "onboarding_session_id",
            "device_id",
            "status",
            "expires_at",
        }
        assert "binding_version" not in claim
        assert "state_version" not in claim
        resumed_claim = await client.get(
            f"/v1/device-bootstrap/{second.onboarding_session_id}"
        )
        assert resumed_claim.status_code == 200
        assert resumed_claim.json()["claim_id"] == claim["claim_id"]

        # The development slice uses the explicit offline authority.  The
        # user-facing activation query is still exercised over HTTP.
        service.create_binding(
            actor_id="person_a",
            claim_id=claim["claim_id"],
            onboarding_session_id=second.onboarding_session_id,
            initialization={
                "declared_mode": "self_use",
                "account_owner_person_id": "person_a",
                "primary_subject": {"person_id": "person_a", "relationship": "self"},
                "persona_selection": "companion_x",
                "service_preferences": {},
                "consent_offer_ids": [],
            },
            idempotency_key="api-binding-01",
        )
        activation = await client.get("/v1/device-activations/dev_test_01")
        assert activation.status_code == 200, activation.text
        assert set(activation.json()) == {
            "activation_id",
            "device_id",
            "binding_id",
            "binding_version",
            "activation_version",
            "status",
            "config_hash",
            "acknowledged_at",
        }
        assert "issued_at" not in activation.json()
        assert "expires_at" not in activation.json()
        assert "downloaded_at" not in activation.json()
        assert "applied_at" not in activation.json()
        assert "ack_counter" not in activation.json()
        resumed_binding = await client.get(
            f"/v1/device-bootstrap/{second.onboarding_session_id}"
        )
        assert resumed_binding.status_code == 200
        assert resumed_binding.json()["binding_id"] == activation.json()["binding_id"]
        assert resumed_binding.json()["activation_status"] == "manifest_ready"


@pytest.mark.asyncio
async def test_device_media_session_uses_fleet_proof_and_device_only_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    app = _app(service)
    secret = "device-ticket-secret-material-that-is-long-enough"
    app.state.settings = SimpleNamespace(
        device_media_gateway_url="wss://media.example/v1/device/media",
        memoria_device_gateway_ticket_secret=SecretStr(secret),
        device_gateway_ticket_ttl_s=300,
        livekit_agent_name="duplex-zh-agent",
    )

    class Memory:
        @staticmethod
        def is_account_unavailable(*, user_id: str) -> bool:
            assert user_id == "person_a"
            return False

    class AccountOperations:
        @asynccontextmanager
        async def write(self, account_id: str):
            assert account_id == "person_a"
            yield

    app.state.memory_store = Memory()
    app.state.account_operations = AccountOperations()

    async def fake_create(
        body: object,
        request: object,
        user: AuthenticatedUser,
        *,
        device_id: str | None = None,
        binding_version: int | None = None,
    ) -> media.MediaSessionResponse:
        del body, request
        assert user.user_id == "person_a"
        assert device_id == "dev_test_01"
        assert binding_version is None
        return media.MediaSessionResponse(
            session_id="session-device-api",
            media_runtime="livekit",
            stream_epoch=1,
            fallback={"media_runtime": "livekit"},
            livekit={
                "url": "wss://livekit.example",
                "room_name": "voice-session-device-api",
                "participant_token": "must-not-leak",
            },
            device_id=device_id,
        )

    monkeypatch.setattr(media, "_create", fake_create)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        challenge_response = await client.post(
            "/v1/devices/dev_test_01/media-challenge",
            headers={
                "X-Device-Certificate-ID": "cert_test_01",
                "X-Client-ID": "esp-installation-1",
            },
        )
        assert challenge_response.status_code == 200, challenge_response.text
        challenge = challenge_response.json()
        issued_at = datetime.fromisoformat(
            challenge["issued_at"].replace("Z", "+00:00")
        )
        signed_payload = service.media_challenge_signing_payload(
            challenge_id=challenge["challenge_id"],
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="esp-installation-1",
            nonce=challenge["nonce"],
            issued_at=issued_at,
        )
        session_response = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(
                    device_key.sign(canonical_json_bytes(signed_payload))
                ),
                "client_id": "esp-installation-1",
            },
        )
        replay = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(
                    device_key.sign(canonical_json_bytes(signed_payload))
                ),
                "client_id": "esp-installation-1",
            },
        )
    assert session_response.status_code == 200, session_response.text
    response = session_response.json()
    assert response["websocket_url"] == "wss://media.example/v1/device/media"
    assert response["uplink"] == {
        "codec": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_ms": 20,
    }
    assert response["downlink"]["sample_rate"] == 24000
    assert "participant_token" not in session_response.text
    claims = verify_device_gateway_ticket(
        response["media_token"],
        secret=secret,
    )
    assert claims.device_id == "dev_test_01"
    assert claims.client_id == "esp-installation-1"
    assert claims.binding_id == manifest["binding_id"]
    assert claims.binding_version == manifest["binding_version"]
    assert claims.stream_epoch == 1
    assert replay.status_code == 409
