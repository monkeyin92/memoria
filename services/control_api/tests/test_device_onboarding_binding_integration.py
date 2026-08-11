from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.tests.identity_test_helpers import install_test_identity_authority
from services.device_fleet.bootstrap_domain import (
    BootstrapQRPayload,
    IntegrationUnavailable,
    b64url_encode,
    canonical_json_bytes,
    encode_bootstrap_qr,
    hash_b64url,
    sha256_hex,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path, *, offline_mock: bool):
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_IDENTITY_DB_PATH", str(tmp_path / "identity.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET",
        "device-onboarding-integration-auth-secret-0123456789",
    )
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        "device-onboarding-transfer-secret-0123456789",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true" if offline_mock else "false")
    app = create_app()
    install_test_identity_authority(
        app,
        secret=app.state.settings.transfer_evidence_key(),
    )
    return app


def _online_claim(
    service: DeviceOnboardingService,
    *,
    actor_id: str,
    device_key: Ed25519PrivateKey,
) -> tuple[str, str]:
    device_id = "dev_onboarding_integration"
    certificate_id = "cert_onboarding_integration"
    capability_hash = sha256_hex(b"memoria-integration-capabilities")
    service.register_offline_mock_device(
        device_id=device_id,
        certificate_id=certificate_id,
        public_key_b64=b64url_encode(device_key.public_key().public_bytes_raw()),
        product_model="memoria-atk-dnesp32s3-v1",
        hardware_revision="rev-a",
        firmware_version="0.1.0",
        firmware_security_version=1,
        capability_manifest_hash=capability_hash,
        minimum_firmware_security_version=1,
    )
    qr_payload = BootstrapQRPayload(
        typ="memoria-device-bootstrap",
        ver=1,
        device_id=device_id,
        bootstrap_nonce=b64url_encode(b"0123456789abcdef"),
        ble_name="MEMORIA-TEST",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        certificate_id=certificate_id,
        provisioning_protocol="memoria-provisioning/1",
        firmware_version="0.1.0",
        pop=b64url_encode(b"0123456789abcdef-pop"),
    )
    session = service.introspect(
        actor_id=actor_id,
        qr_payload=encode_bootstrap_qr(qr_payload, device_key),
        client_onboarding_id="integration-client-01",
        client={
            "platform": "wechat-miniprogram",
            "app_version": "0.1.0",
            "base_library_version": "3.0.0",
        },
    )
    session_id = str(session["onboarding_session_id"])
    challenge = service.issue_challenge(
        onboarding_session_id=session_id,
        device_id=device_id,
        certificate_id=certificate_id,
    )
    proof_unsigned: dict[str, object] = {
        "device_id": device_id,
        "certificate_id": certificate_id,
        "bootstrap_nonce_hash": hash_b64url(qr_payload.bootstrap_nonce),
        "mobile_nonce_hash": hash_b64url(str(session["mobile_nonce"])),
        "challenge_id": challenge["challenge_id"],
        "challenge_nonce": challenge["nonce"],
        "firmware_version": "0.1.0",
        "firmware_security_version": 1,
        "capability_manifest_hash": capability_hash,
        "network_result": {"got_ip": True, "dns_ready": True, "tls_ready": True},
        "monotonic_counter": 1,
    }
    service.submit_online_proof(
        onboarding_session_id=session_id,
        proof={
            **proof_unsigned,
            "signature": b64url_encode(
                device_key.sign(canonical_json_bytes(proof_unsigned))
            ),
        },
    )
    session_row = service.store.get_session(session_id)  # type: ignore[attr-defined]
    assert session_row is not None
    claim = service.reserve_claim(
        actor_id=actor_id,
        onboarding_session_id=session_id,
        device_id=device_id,
        idempotency_key="integration-claim-01",
        expected_state_version=session_row.state_version,
    )
    return session_id, str(claim["claim_id"])


@pytest.mark.asyncio
async def test_claim_binding_reuses_identity_and_creates_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    service = app.state.device_onboarding_service
    assert isinstance(service, DeviceOnboardingService)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "onboarding-owner", "password": "safe-password"},
        )
        assert registered.status_code == 201
        identity = registered.json()
        session_id, claim_id = _online_claim(
            service,
            actor_id=identity["user_id"],
            device_key=Ed25519PrivateKey.generate(),
        )
        headers = {
            "Authorization": f"Bearer {identity['access_token']}",
            "Idempotency-Key": "integration-binding-01",
        }
        payload = {
            "claim_id": claim_id,
            "onboarding_session_id": session_id,
            "declared_mode": "self_use",
            "account_owner_person_id": identity["user_id"],
            "primary_subject": {
                "person_id": identity["user_id"],
                "relationship": "self",
            },
            "persona_selection": "starlight",
            "service_preferences": {
                "memory_level": "personal",
                "interview_frequency": "low",
            },
            "consent_offer_ids": ["offer_self_memory_retention_v1"],
        }
        commit = service.binding_commit_with_authority

        def fail_after_identity(**_kwargs):
            raise IntegrationUnavailable("simulated Fleet projection outage")

        monkeypatch.setattr(service, "binding_commit_with_authority", fail_after_identity)
        interrupted = await client.post(
            "/v1/device-bindings", headers=headers, json=payload
        )
        assert interrupted.status_code == 503
        monkeypatch.setattr(service, "binding_commit_with_authority", commit)
        first = await client.post("/v1/device-bindings", headers=headers, json=payload)
        assert first.status_code == 201, first.text
        replay = await client.post("/v1/device-bindings", headers=headers, json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["binding_id"] == first.json()["binding_id"]
        activation = await client.get(
            "/v1/device-activations/dev_onboarding_integration",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
        )
        assert activation.status_code == 200, activation.text
        assert activation.json()["binding_id"] == first.json()["binding_id"]
        assert activation.json()["status"] == "manifest_ready"


@pytest.mark.asyncio
async def test_legacy_device_claim_token_is_offline_mock_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "legacy-owner", "password": "safe-password"},
        )
        identity = registered.json()
        token = mint_device_binding_token(
            device_id="legacy-device",
            secret=app.state.settings.device_binding_token_key(),
            now=datetime.now(UTC),
            ttl=timedelta(minutes=5),
            nonce="legacy-token-disabled",
        )
        response = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "self_use",
                "account_owner_person_id": identity["user_id"],
                "primary_subject": {
                    "person_id": identity["user_id"],
                    "relationship": "self",
                },
                "persona_selection": "starlight",
                "service_preferences": {"memory_level": "personal"},
                "consent_offer_ids": ["offer_self_memory_retention_v1"],
            },
        )
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "legacy_device_claim_disabled"
