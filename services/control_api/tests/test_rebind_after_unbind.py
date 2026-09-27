"""An unbound device can be claimed and bound again by the same owner."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from services.control_api.tests.test_device_display_profile import DEVICE_ID, _bound_device
from services.control_api.tests.test_device_onboarding_binding_integration import _app
from services.device_fleet.bootstrap_domain import (
    BootstrapQRPayload,
    b64url_encode,
    canonical_json_bytes,
    encode_bootstrap_qr,
    hash_b64url,
    sha256_hex,
)

CERTIFICATE_ID = "cert_onboarding_integration"


def _second_claim(service, *, actor_id: str, device_key: Ed25519PrivateKey) -> tuple[str, str]:
    payload = BootstrapQRPayload(
        typ="memoria-device-bootstrap",
        ver=1,
        device_id=DEVICE_ID,
        bootstrap_nonce=b64url_encode(b"second-scan-nonc"),
        ble_name="MEMORIA-TEST",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        certificate_id=CERTIFICATE_ID,
        provisioning_protocol="memoria-provisioning/1",
        firmware_version="0.1.0",
        pop=b64url_encode(b"0123456789abcdef-pop"),
    )
    session = service.introspect(
        actor_id=actor_id,
        qr_payload=encode_bootstrap_qr(payload, device_key),
        client_onboarding_id="second-client-01",
        client={"platform": "wechat-miniprogram", "app_version": "0.1.0", "base_library_version": "3.0.0"},
    )
    session_id = str(session["onboarding_session_id"])
    challenge = service.issue_challenge(
        onboarding_session_id=session_id, device_id=DEVICE_ID, certificate_id=CERTIFICATE_ID
    )
    unsigned: dict[str, object] = {
        "device_id": DEVICE_ID,
        "certificate_id": CERTIFICATE_ID,
        "bootstrap_nonce_hash": hash_b64url(payload.bootstrap_nonce),
        "mobile_nonce_hash": hash_b64url(str(session["mobile_nonce"])),
        "challenge_id": challenge["challenge_id"],
        "challenge_nonce": challenge["nonce"],
        "firmware_version": "0.1.0",
        "firmware_security_version": 1,
        "capability_manifest_hash": sha256_hex(b"memoria-integration-capabilities"),
        "network_result": {"got_ip": True, "dns_ready": True, "tls_ready": True},
        "monotonic_counter": 50,
    }
    service.submit_online_proof(
        onboarding_session_id=session_id,
        proof={**unsigned, "signature": b64url_encode(device_key.sign(canonical_json_bytes(unsigned)))},
    )
    row = service.store.get_session(session_id)
    claim = service.reserve_claim(
        actor_id=actor_id,
        onboarding_session_id=session_id,
        device_id=DEVICE_ID,
        idempotency_key="second-claim-01",
        expected_state_version=row.state_version,
    )
    return session_id, str(claim["claim_id"])


@pytest.mark.asyncio
async def test_owner_rebinds_after_unbinding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    service = app.state.device_onboarding_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _bound_device(client, app, key)
        auth = {"Authorization": f"Bearer {owner['access_token']}"}
        unbound = await client.post(
            f"/v1/devices/{DEVICE_ID}/binding/unbind",
            headers=auth,
            json={"reason": "unbind", "purge_subject_data": False},
        )
        assert unbound.status_code == 200, unbound.text
        session_id, claim_id = _second_claim(service, actor_id=owner["user_id"], device_key=key)
        rebound = await client.post(
            "/v1/device-bindings",
            headers={**auth, "Idempotency-Key": "display-binding-02"},
            json={
                "claim_id": claim_id,
                "onboarding_session_id": session_id,
                "declared_mode": "self_use",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {"person_id": owner["user_id"], "relationship": "self"},
                "persona_selection": "taoxi",
                "service_preferences": {"memory_level": "personal", "interview_frequency": "low"},
                "consent_offer_ids": ["offer_self_memory_retention_v1"],
            },
        )
    assert rebound.status_code == 201, rebound.text

