"""Owner-only Wi-Fi reprovisioning of a device that stays bound."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.device_fleet.bootstrap_domain import (
    BindingConflict,
    BootstrapPurpose,
    BootstrapQRPayload,
    BootstrapState,
    ChallengeReplay,
    ClaimConflict,
    ClaimReservation,
    ClaimStatus,
    DeviceAlreadyBound,
    DeviceLifecycle,
    DeviceOnlineProof,
    ProximityRequired,
    SessionExpired,
    b64url_encode,
    canonical_json_bytes,
    encode_bootstrap_qr,
    hash_b64url,
    sha256_hex,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.bootstrap_store import SQLiteBootstrapStore
from services.device_fleet.tests.test_bootstrap_vertical_slice import NOW, _fixture, _online

CLIENT = {
    "platform": "wechat-miniprogram",
    "app_version": "0.1.0",
    "base_library_version": "3.0.0",
}


def _bound_device(
    *, now_fn=None
) -> tuple[DeviceOnboardingService, SQLiteBootstrapStore, Ed25519PrivateKey, BootstrapQRPayload]:
    """Onboard, bind and activate ``dev_test_01`` for ``person_a`` (counters 1-2)."""

    service, store, device_key, payload = _fixture(now_fn=now_fn)
    session, online = _online(service, store, device_key, payload)
    claim = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=str(session["onboarding_session_id"]),
        device_id="dev_test_01",
        idempotency_key="claim-before-move",
        expected_state_version=int(cast(int, online["state_version"])),
    )
    result = service.create_binding(
        actor_id="person_a",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=str(session["onboarding_session_id"]),
        initialization={
            "declared_mode": "self_use",
            "account_owner_person_id": "person_a",
            "primary_subject": {"person_id": "person_a", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        },
        idempotency_key="binding-before-move",
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
            "signature": b64url_encode(device_key.sign(canonical_json_bytes(ack_unsigned))),
        },
    )
    # The board reaches a new place: Wi-Fi config mode shows a fresh QR.
    moved = replace(payload, bootstrap_nonce=b64url_encode(b"new-place-nonce!"))
    return service, store, device_key, moved


def _proof(
    *,
    device_key: Ed25519PrivateKey,
    payload: BootstrapQRPayload,
    session: dict[str, object],
    challenge: dict[str, object],
    counter: int,
    mobile_nonce: str | None = None,
) -> DeviceOnlineProof:
    unsigned: dict[str, object] = {
        "device_id": "dev_test_01",
        "certificate_id": "cert_test_01",
        "bootstrap_nonce_hash": hash_b64url(payload.bootstrap_nonce),
        "mobile_nonce_hash": hash_b64url(mobile_nonce or str(session["mobile_nonce"])),
        "challenge_id": challenge["challenge_id"],
        "challenge_nonce": challenge["nonce"],
        "firmware_version": "0.1.0",
        "firmware_security_version": 1,
        "capability_manifest_hash": sha256_hex(b"capabilities"),
        "network_result": {"got_ip": True, "dns_ready": True, "tls_ready": True},
        "monotonic_counter": counter,
    }
    return DeviceOnlineProof.from_mapping(
        {**unsigned, "signature": b64url_encode(device_key.sign(canonical_json_bytes(unsigned)))}
    )


def _introspect(
    service: DeviceOnboardingService,
    device_key: Ed25519PrivateKey,
    payload: BootstrapQRPayload,
    *,
    actor_id: str = "person_a",
    client_id: str = "client_reprovision",
) -> dict[str, object]:
    return service.introspect(
        actor_id=actor_id,
        qr_payload=encode_bootstrap_qr(payload, device_key),
        client_onboarding_id=client_id,
        client=CLIENT,
    )


def _challenge(service: DeviceOnboardingService, session: dict[str, object]) -> dict[str, object]:
    return service.issue_challenge(
        onboarding_session_id=str(session["onboarding_session_id"]),
        device_id="dev_test_01",
        certificate_id="cert_test_01",
    )


def test_owner_reprovision_reaches_device_online_without_touching_the_binding() -> None:
    service, store, device_key, payload = _bound_device()
    before = store.get_device("dev_test_01")
    assert before is not None and before.lifecycle_status is DeviceLifecycle.BOUND
    activation_before = service.get_activation_status(actor_id="person_a", device_id="dev_test_01")

    session = _introspect(service, device_key, payload)
    assert session["purpose"] == "reprovision"
    assert session["state"] == "qr_verified"
    assert cast(dict[str, object], session["device"])["claim_status"] == "bound"
    assert "activation_status" not in session
    assert "claim_id" not in session

    online = service.submit_online_proof(
        onboarding_session_id=str(session["onboarding_session_id"]),
        proof=_proof(
            device_key=device_key,
            payload=payload,
            session=session,
            challenge=_challenge(service, session),
            counter=3,
        ),
    )
    assert online["purpose"] == "reprovision"
    assert online["state"] == "device_online"

    after = store.get_device("dev_test_01")
    assert after is not None
    assert after.lifecycle_status is DeviceLifecycle.BOUND
    assert (after.actor_id, after.binding_id, after.binding_version) == (
        before.actor_id,
        before.binding_id,
        before.binding_version,
    )
    assert after.activation_version == before.activation_version
    assert after.last_monotonic_counter == 3
    assert service.get_activation_status(actor_id="person_a", device_id="dev_test_01") == (
        activation_before
    )
    row = store.get_session(str(session["onboarding_session_id"]))
    assert row is not None
    assert row.purpose is BootstrapPurpose.REPROVISION
    assert row.consumed_at is not None

    # The conversation path keeps working on the untouched activation.
    service.issue_media_challenge(
        device_id="dev_test_01", certificate_id="cert_test_01", client_id="installation-1"
    )


def test_reprovision_session_can_never_claim_or_be_undone() -> None:
    clock = [NOW]
    service, store, device_key, payload = _bound_device(now_fn=lambda: clock[0])
    session = _introspect(service, device_key, payload)
    online = service.submit_online_proof(
        onboarding_session_id=str(session["onboarding_session_id"]),
        proof=_proof(
            device_key=device_key,
            payload=payload,
            session=session,
            challenge=_challenge(service, session),
            counter=3,
        ),
    )
    with pytest.raises(ClaimConflict, match="reprovision"):
        service.reserve_claim(
            actor_id="person_a",
            onboarding_session_id=str(session["onboarding_session_id"]),
            device_id="dev_test_01",
            idempotency_key="claim-after-reprovision",
            expected_state_version=int(cast(int, online["state_version"])),
        )
    row = store.get_session(str(session["onboarding_session_id"]))
    assert row is not None
    with pytest.raises(ClaimConflict, match="reprovision"):
        # The store refuses on its own, not only behind the service check.
        store.reserve_claim(
            _forged_claim(row.onboarding_session_id),
            expected_state_version=row.state_version,
            now=NOW,
        )

    cancelled = service.cancel_session(
        actor_id="person_a", onboarding_session_id=str(session["onboarding_session_id"])
    )
    assert cancelled["state"] == "device_online"

    clock[0] = NOW + timedelta(hours=1)
    later = service.get_session(
        actor_id="person_a", onboarding_session_id=str(session["onboarding_session_id"])
    )
    assert later["state"] == "device_online"

    # The same QR cannot start a second pass over a finished reprovision.
    clock[0] = NOW
    with pytest.raises(SessionExpired):
        _introspect(service, device_key, payload, client_id="client_rescan")


def _forged_claim(onboarding_session_id: str) -> ClaimReservation:
    return ClaimReservation(
        claim_id="claim_forged",
        onboarding_session_id=onboarding_session_id,
        device_id="dev_test_01",
        actor_id="person_a",
        status=ClaimStatus.RESERVED,
        idempotency_key="claim-forged",
        reserved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        binding_id=None,
        binding_version=None,
        committed_at=None,
        released_at=None,
    )


def test_only_the_bound_owner_can_introspect_a_bound_device() -> None:
    service, _store, device_key, payload = _bound_device()
    with pytest.raises(DeviceAlreadyBound):
        _introspect(service, device_key, payload, actor_id="person_b", client_id="client_b")
    # The owner's later scan of the same QR is unaffected by the refusal.
    assert _introspect(service, device_key, payload)["purpose"] == "reprovision"


def test_reprovision_keeps_proof_replay_and_proximity_rules() -> None:
    service, _store, device_key, payload = _bound_device()
    session = _introspect(service, device_key, payload)

    stale = _challenge(service, session)
    with pytest.raises(ClaimConflict, match="monotonic"):
        # Counter 2 was consumed by the activation ACK.
        service.submit_online_proof(
            onboarding_session_id=str(session["onboarding_session_id"]),
            proof=_proof(
                device_key=device_key,
                payload=payload,
                session=session,
                challenge=stale,
                counter=2,
            ),
        )

    far = _challenge(service, session)
    with pytest.raises(ProximityRequired):
        service.submit_online_proof(
            onboarding_session_id=str(session["onboarding_session_id"]),
            proof=_proof(
                device_key=device_key,
                payload=payload,
                session=session,
                challenge=far,
                counter=3,
                mobile_nonce=b64url_encode(b"x" * 32),
            ),
        )

    challenge = _challenge(service, session)
    proof = _proof(
        device_key=device_key, payload=payload, session=session, challenge=challenge, counter=3
    )
    service.submit_online_proof(onboarding_session_id=str(session["onboarding_session_id"]), proof=proof)
    with pytest.raises(ChallengeReplay):
        service.submit_online_proof(
            onboarding_session_id=str(session["onboarding_session_id"]), proof=proof
        )


def test_reprovision_stops_when_the_binding_changes_before_the_proof() -> None:
    service, store, device_key, payload = _bound_device()
    session = _introspect(service, device_key, payload)
    challenge = _challenge(service, session)

    # Transferred to another account between scan and proof.
    store._connection.execute(
        "UPDATE device_onboarding_devices SET actor_id = 'person_b' WHERE device_id = 'dev_test_01'"
    )
    with pytest.raises(DeviceAlreadyBound):
        service.submit_online_proof(
            onboarding_session_id=str(session["onboarding_session_id"]),
            proof=_proof(
                device_key=device_key,
                payload=payload,
                session=session,
                challenge=challenge,
                counter=3,
            ),
        )

    # Released: the reprovision session must not provision an unbound device.
    store._connection.execute(
        """
        UPDATE device_onboarding_devices
        SET lifecycle_status = 'provisioned', actor_id = NULL,
            binding_id = NULL, binding_version = NULL
        WHERE device_id = 'dev_test_01'
        """
    )
    with pytest.raises(BindingConflict):
        _challenge(service, session)
    row = store.get_session(str(session["onboarding_session_id"]))
    assert row is not None and row.state is not BootstrapState.DEVICE_ONLINE


def test_onboarding_qr_session_is_not_resumed_as_reprovision() -> None:
    service, store, device_key, payload = _fixture()
    session, online = _online(service, store, device_key, payload)
    # Bound through another path while this QR's onboarding session is open.
    store._connection.execute(
        """
        UPDATE device_onboarding_devices
        SET lifecycle_status = 'bound', actor_id = 'person_a',
            binding_id = 'binding_elsewhere', binding_version = 1
        WHERE device_id = 'dev_test_01'
        """
    )
    assert online["purpose"] == "onboarding"
    with pytest.raises(SessionExpired):
        _introspect(service, device_key, payload, client_id="client_again")
    assert session["purpose"] == "onboarding"
