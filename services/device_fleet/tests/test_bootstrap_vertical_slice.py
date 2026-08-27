from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.device_fleet.bootstrap_domain import (
    BootstrapQRPayload,
    ChallengeReplay,
    ClaimConflict,
    DeviceLifecycle,
    DeviceMediaChallengeRateLimited,
    DeviceMediaNotReady,
    DeviceOnlineProof,
    DeviceRecord,
    IntegrationUnavailable,
    InvalidDeviceProof,
    ProximityRequired,
    QRFormatError,
    SessionExpired,
    b64url_encode,
    canonical_json_bytes,
    encode_bootstrap_qr,
    hash_b64url,
    sha256_hex,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.bootstrap_store import SQLiteBootstrapStore

NOW = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)


def _fixture(
    *, now_fn=None, device_key: Ed25519PrivateKey | None = None
) -> tuple[DeviceOnboardingService, SQLiteBootstrapStore, Ed25519PrivateKey, BootstrapQRPayload]:
    device_key = device_key or Ed25519PrivateKey.generate()
    store = SQLiteBootstrapStore()
    capability_hash = sha256_hex(b"capabilities")
    store.register_manufactured_device(
        DeviceRecord(
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            public_key=device_key.public_key().public_bytes_raw(),
            product_model="memoria-devkit",
            hardware_revision="rev-a",
            firmware_version="0.1.0",
            firmware_security_version=1,
            capability_manifest_hash=capability_hash,
            minimum_firmware_security_version=1,
            lifecycle_status=DeviceLifecycle.MANUFACTURED,
            last_monotonic_counter=0,
            binding_id=None,
            binding_version=None,
            actor_id=None,
            activation_version=0,
            last_activation_counter=0,
        )
    )
    service = DeviceOnboardingService(
        store,
        offline_mock=True,
        now_fn=now_fn or (lambda: NOW),
        minimum_firmware_security_version=1,
    )
    payload = BootstrapQRPayload(
        typ="memoria-device-bootstrap",
        ver=1,
        device_id="dev_test_01",
        bootstrap_nonce=b64url_encode(b"0123456789abcdef"),
        ble_name="MEM-TEST",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        certificate_id="cert_test_01",
        provisioning_protocol="memoria-provisioning/1",
        firmware_version="0.1.0",
        pop=b64url_encode(b"0123456789abcdef-pop"),
    )
    return service, store, device_key, payload


def _online(
    service: DeviceOnboardingService,
    store: SQLiteBootstrapStore,
    device_key: Ed25519PrivateKey,
    payload: BootstrapQRPayload,
    *,
    actor_id: str = "person_a",
    client_id: str = "client_a",
    counter: int = 1,
) -> tuple[dict[str, object], dict[str, object]]:
    qr = encode_bootstrap_qr(payload, device_key)
    session = service.introspect(
        actor_id=actor_id,
        qr_payload=qr,
        client_onboarding_id=client_id,
        client={
            "platform": "wechat-miniprogram",
            "app_version": "0.1.0",
            "base_library_version": "3.0.0",
        },
    )
    challenge = service.issue_challenge(
        onboarding_session_id=str(session["onboarding_session_id"]),
        device_id="dev_test_01",
        certificate_id="cert_test_01",
    )
    session_row = store.get_session(str(session["onboarding_session_id"]))
    assert session_row is not None
    mobile_nonce = str(session["mobile_nonce"])
    challenge_nonce = str(challenge["nonce"])
    proof_unsigned: dict[str, object] = {
        "device_id": "dev_test_01",
        "certificate_id": "cert_test_01",
        "bootstrap_nonce_hash": hash_b64url(payload.bootstrap_nonce),
        "mobile_nonce_hash": hash_b64url(mobile_nonce),
        "challenge_id": challenge["challenge_id"],
        "challenge_nonce": challenge_nonce,
        "firmware_version": "0.1.0",
        "firmware_security_version": 1,
        "capability_manifest_hash": sha256_hex(b"capabilities"),
        "network_result": {"got_ip": True, "dns_ready": True, "tls_ready": True},
        "monotonic_counter": counter,
    }
    proof = {
        **proof_unsigned,
        "signature": b64url_encode(device_key.sign(canonical_json_bytes(proof_unsigned))),
    }
    online = service.submit_online_proof(
        onboarding_session_id=str(session["onboarding_session_id"]),
        proof=DeviceOnlineProof.from_mapping(proof),
    )
    return session, online


def test_qr_is_strict_and_screenshot_without_nearby_proof_cannot_claim() -> None:
    service, store, device_key, payload = _fixture()
    qr = encode_bootstrap_qr(payload, device_key)
    session = service.introspect(
        actor_id="person_a",
        qr_payload=qr,
        client_onboarding_id="client_a",
        client={
            "platform": "wechat-miniprogram",
            "app_version": "0.1.0",
            "base_library_version": "3.0.0",
        },
    )
    replay = service.introspect(
        actor_id="person_a",
        qr_payload=qr,
        client_onboarding_id="client_a",
        client={
            "platform": "wechat-miniprogram",
            "app_version": "0.1.0",
            "base_library_version": "3.0.0",
        },
    )
    assert replay["onboarding_session_id"] == session["onboarding_session_id"]
    assert replay["mobile_nonce"] == session["mobile_nonce"]
    retried_from_new_page = service.introspect(
        actor_id="person_a",
        qr_payload=qr,
        client_onboarding_id="client_a_retry",
        client={
            "platform": "wechat-miniprogram",
            "app_version": "0.1.0",
            "base_library_version": "3.0.0",
        },
    )
    assert retried_from_new_page["onboarding_session_id"] == session["onboarding_session_id"]
    assert retried_from_new_page["mobile_nonce"] == session["mobile_nonce"]
    session_row = store.get_session(str(session["onboarding_session_id"]))
    assert session_row is not None
    assert session_row.pop_hash != payload.pop
    with pytest.raises(ProximityRequired, match="nearby"):
        service.reserve_claim(
            actor_id="person_a",
            onboarding_session_id=session_row.onboarding_session_id,
            device_id="dev_test_01",
            idempotency_key="claim-screenshot",
            expected_state_version=session_row.state_version,
        )

    with pytest.raises(QRFormatError):
        # Reordering JSON bytes inside the QR payload breaks the canonical
        # representation even though the decoded object is equivalent.
        noncanonical_bytes = (
            b'{"ver":1,"typ":"memoria-device-bootstrap","device_id":"dev_test_01",'
            b'"bootstrap_nonce":"MDEyMzQ1Njc4OWFiY2RlZg","ble_name":"MEM-TEST",'
            b'"ble_service_uuid":"12345678-1234-5678-1234-567812345678",'
            b'"certificate_id":"cert_test_01","provisioning_protocol":"memoria-provisioning/1",'
            b'"firmware_version":"0.1.0","pop":"MDEyMzQ1Njc4OWFiY2RlZi1wb3A"}'
        )
        noncanonical = b64url_encode(noncanonical_bytes)
        service.introspect(
            actor_id="person_b",
            qr_payload=f"memoria-bootstrap:v1:{noncanonical}.{b64url_encode(device_key.sign(noncanonical_bytes))}",
            client_onboarding_id="client_b",
            client={
                "platform": "wechat-miniprogram",
                "app_version": "0.1.0",
                "base_library_version": "3.0.0",
            },
        )


def test_challenge_is_single_use_and_wrong_key_does_not_consume_it() -> None:
    service, store, device_key, payload = _fixture()
    session, _ = _online(service, store, device_key, payload)
    # A second proof for the first challenge is a replay, not a new online proof.
    # Recreate the proof through the service path so the challenge is consumed.
    session_row = store.get_session(str(session["onboarding_session_id"]))
    assert session_row is not None
    challenge = service.issue_challenge(
        onboarding_session_id=session_row.onboarding_session_id,
        device_id="dev_test_01",
        certificate_id="cert_test_01",
    )
    mobile_nonce_hash = session["mobile_nonce"]
    unsigned: dict[str, object] = {
        "device_id": "dev_test_01",
        "certificate_id": "cert_test_01",
        "bootstrap_nonce_hash": hash_b64url(payload.bootstrap_nonce),
        "mobile_nonce_hash": hash_b64url(str(mobile_nonce_hash)),
        "challenge_id": challenge["challenge_id"],
        "challenge_nonce": challenge["nonce"],
        "firmware_version": "0.1.0",
        "firmware_security_version": 1,
        "capability_manifest_hash": sha256_hex(b"capabilities"),
        "network_result": {"got_ip": True, "dns_ready": True, "tls_ready": True},
        "monotonic_counter": 2,
    }
    wrong = Ed25519PrivateKey.generate()
    wrong_proof = {**unsigned, "signature": b64url_encode(wrong.sign(canonical_json_bytes(unsigned)))}
    with pytest.raises(InvalidDeviceProof):
        service.submit_online_proof(
            onboarding_session_id=session_row.onboarding_session_id,
            proof=wrong_proof,
        )
    good = {**unsigned, "signature": b64url_encode(device_key.sign(canonical_json_bytes(unsigned)))}
    service.submit_online_proof(
        onboarding_session_id=session_row.onboarding_session_id,
        proof=good,
    )
    with pytest.raises(ChallengeReplay):
        service.submit_online_proof(
            onboarding_session_id=session_row.onboarding_session_id,
            proof=good,
        )


def test_two_accounts_conflict_and_claim_idempotency() -> None:
    service, store, device_key, payload = _fixture()
    first, _ = _online(service, store, device_key, payload, actor_id="person_a", client_id="a")
    second, _ = _online(
        service,
        store,
        device_key,
        replace(payload, bootstrap_nonce=b64url_encode(b"fedcba9876543210")),
        actor_id="person_b",
        client_id="b",
        counter=2,
    )
    first_row = store.get_session(str(first["onboarding_session_id"]))
    assert first_row is not None
    first_claim = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=first_row.onboarding_session_id,
        device_id="dev_test_01",
        idempotency_key="claim-same-key",
        expected_state_version=first_row.state_version,
    )
    duplicate = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=first_row.onboarding_session_id,
        device_id="dev_test_01",
        idempotency_key="claim-same-key",
        expected_state_version=first_row.state_version + 1,
    )
    assert duplicate["claim_id"] == first_claim["claim_id"]

    second_row = store.get_session(str(second["onboarding_session_id"]))
    assert second_row is not None
    with pytest.raises(ClaimConflict):
        service.reserve_claim(
            actor_id="person_b",
            onboarding_session_id=second_row.onboarding_session_id,
            device_id="dev_test_01",
            idempotency_key="claim-other",
            expected_state_version=second_row.state_version,
        )


def test_session_expiry_is_fail_closed() -> None:
    clock = [NOW]
    service, store, device_key, payload = _fixture(now_fn=lambda: clock[0])
    session = service.introspect(
        actor_id="person_a",
        qr_payload=encode_bootstrap_qr(payload, device_key),
        client_onboarding_id="client_a",
        client={
            "platform": "wechat-miniprogram",
            "app_version": "0.1.0",
            "base_library_version": "3.0.0",
        },
    )
    clock[0] = NOW + timedelta(minutes=16)
    with pytest.raises(SessionExpired):
        service.reserve_claim(
            actor_id="person_a",
            onboarding_session_id=str(session["onboarding_session_id"]),
            device_id="dev_test_01",
            idempotency_key="expired-claim",
            expected_state_version=1,
        )


def test_activation_manifest_ack_is_signed_idempotent_and_replay_safe() -> None:
    service, store, device_key, payload = _fixture()
    session, _ = _online(service, store, device_key, payload)
    session_row = store.get_session(str(session["onboarding_session_id"]))
    assert session_row is not None
    claim = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=session_row.onboarding_session_id,
        device_id="dev_test_01",
        idempotency_key="claim-activation",
        expected_state_version=session_row.state_version,
    )
    result = service.create_binding(
        actor_id="person_a",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=session_row.onboarding_session_id,
        initialization={
            "declared_mode": "self_use",
            "account_owner_person_id": "person_a",
            "primary_subject": {"person_id": "person_a", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        },
        idempotency_key="binding-activation",
    )
    manifest = cast(dict[str, object], result["manifest"])
    assert manifest["signature"]
    ack_unsigned: dict[str, object] = {
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
    ack = {
        **ack_unsigned,
        "signature": b64url_encode(device_key.sign(canonical_json_bytes(ack_unsigned))),
    }
    status = service.accept_activation_ack(device_id="dev_test_01", ack=ack)
    assert status["status"] == "ready_for_conversation"
    retried = service.accept_activation_ack(device_id="dev_test_01", ack=ack)
    assert retried == status
    changed_ack_unsigned = {**ack_unsigned, "monotonic_counter": 3}
    changed_ack = {
        **changed_ack_unsigned,
        "signature": b64url_encode(
            device_key.sign(canonical_json_bytes(changed_ack_unsigned))
        ),
    }
    with pytest.raises(ChallengeReplay):
        service.accept_activation_ack(device_id="dev_test_01", ack=changed_ack)


def test_device_media_challenge_requires_ready_activation_and_is_one_shot() -> None:
    service, store, device_key, payload = _fixture()
    with pytest.raises(DeviceMediaNotReady):
        service.issue_media_challenge(
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="installation-1",
        )
    session, _ = _online(service, store, device_key, payload)
    session_row = store.get_session(str(session["onboarding_session_id"]))
    assert session_row is not None
    claim = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=session_row.onboarding_session_id,
        device_id="dev_test_01",
        idempotency_key="claim-media",
        expected_state_version=session_row.state_version,
    )
    result = service.create_binding(
        actor_id="person_a",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=session_row.onboarding_session_id,
        initialization={
            "declared_mode": "self_use",
            "account_owner_person_id": "person_a",
            "primary_subject": {"person_id": "person_a", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        },
        idempotency_key="binding-media",
    )
    manifest = cast(dict[str, object], result["manifest"])
    ack_unsigned: dict[str, object] = {
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
            **ack_unsigned,
            "signature": b64url_encode(
                device_key.sign(canonical_json_bytes(ack_unsigned))
            ),
        },
    )
    challenge = service.issue_media_challenge(
        device_id="dev_test_01",
        certificate_id="cert_test_01",
        client_id="installation-1",
    )
    service.issue_media_challenge(
        device_id="dev_test_01",
        certificate_id="cert_test_01",
        client_id="installation-1",
    )
    service.issue_media_challenge(
        device_id="dev_test_01",
        certificate_id="cert_test_01",
        client_id="installation-1",
    )
    with pytest.raises(DeviceMediaChallengeRateLimited):
        service.issue_media_challenge(
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="installation-1",
        )
    issued_at = datetime.fromisoformat(str(challenge["issued_at"]).replace("Z", "+00:00"))
    signing_payload = service.media_challenge_signing_payload(
        challenge_id=str(challenge["challenge_id"]),
        device_id="dev_test_01",
        certificate_id="cert_test_01",
        client_id="installation-1",
        nonce=str(challenge["nonce"]),
        issued_at=issued_at,
    )
    with pytest.raises(InvalidDeviceProof):
        service.authenticate_media_challenge(
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="installation-2",
            challenge_id=str(challenge["challenge_id"]),
            nonce=str(challenge["nonce"]),
            signature=device_key.sign(canonical_json_bytes(signing_payload)),
        )
    authenticated = service.authenticate_media_challenge(
        device_id="dev_test_01",
        certificate_id="cert_test_01",
        client_id="installation-1",
        challenge_id=str(challenge["challenge_id"]),
        nonce=str(challenge["nonce"]),
        signature=device_key.sign(canonical_json_bytes(signing_payload)),
    )
    assert authenticated["actor_id"] == "person_a"
    assert authenticated["binding_id"] == manifest["binding_id"]
    assert authenticated["binding_version"] == manifest["binding_version"]
    with pytest.raises(ChallengeReplay):
        service.authenticate_media_challenge(
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="installation-1",
            challenge_id=str(challenge["challenge_id"]),
            nonce=str(challenge["nonce"]),
            signature=device_key.sign(canonical_json_bytes(signing_payload)),
        )


def test_production_without_activation_key_is_fail_closed() -> None:
    _service, store, _device_key, _payload = _fixture()
    production = DeviceOnboardingService(store, offline_mock=False)
    with pytest.raises(IntegrationUnavailable):
        _ = production.activation_public_key_b64
