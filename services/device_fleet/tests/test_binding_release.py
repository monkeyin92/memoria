"""Releasing the fleet binding after an Identity unbind frees the device.

Before this the fleet kept a device ``bound`` after the Mini Program unbound
it: the board still got its manifest and every new scan of its QR was
answered as already bound.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.device_fleet.bootstrap_domain import (
    BindingConflict,
    BootstrapQRPayload,
    ClaimStatus,
    DeviceLifecycle,
    b64url_encode,
    encode_bootstrap_qr,
)
from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.tests.test_bootstrap_postgres import (  # noqa: F401 - fixture
    NOW,
    _device,
    _signed_online_proof,
    postgres_onboarding,
)
from services.device_fleet.tests.test_bootstrap_reprovision import (
    CLIENT,
    _bound_device,
    _challenge,
    _introspect,
    _proof,
)

INITIALIZATION = {
    "declared_mode": "self_use",
    "primary_subject": {"relationship": "self"},
    "persona_selection": "companion_x",
    "service_preferences": {},
    "consent_offer_ids": [],
}


def _initialization(actor_id: str) -> dict[str, object]:
    return {
        **INITIALIZATION,
        "account_owner_person_id": actor_id,
        "primary_subject": {"person_id": actor_id, "relationship": "self"},
    }


def test_release_returns_the_device_to_provisioned_and_refuses_the_old_manifest() -> None:
    service, store, _device_key, _moved = _bound_device()
    bound = store.get_device("dev_test_01")
    assert bound is not None and bound.lifecycle_status is DeviceLifecycle.BOUND
    binding_id = cast(str, bound.binding_id)

    assert service.release_device_binding(device_id="dev_test_01", binding_id=binding_id)

    released = store.get_device("dev_test_01")
    assert released is not None
    assert released.lifecycle_status is DeviceLifecycle.PROVISIONED
    assert (released.actor_id, released.binding_id, released.binding_version) == (None, None, None)
    # Counters never go back: the next binding's manifest and ACK move forward.
    assert released.activation_version == bound.activation_version
    assert released.last_monotonic_counter == bound.last_monotonic_counter
    with pytest.raises(BindingConflict):
        service.get_activation_manifest(device_id="dev_test_01", certificate_id="cert_test_01")
    assert not store.is_actor_bound_to_device(actor_id="person_a", device_id="dev_test_01")


def test_release_is_idempotent_and_ignores_a_binding_that_is_not_current() -> None:
    service, store, _device_key, _moved = _bound_device()
    bound = store.get_device("dev_test_01")
    assert bound is not None

    assert not service.release_device_binding(device_id="dev_test_01", binding_id="bind_stale")
    still = store.get_device("dev_test_01")
    assert still is not None and still.lifecycle_status is DeviceLifecycle.BOUND

    binding_id = cast(str, bound.binding_id)
    assert service.release_device_binding(device_id="dev_test_01", binding_id=binding_id)
    assert not service.release_device_binding(device_id="dev_test_01", binding_id=binding_id)


def test_a_released_device_can_be_claimed_and_bound_again_by_anyone() -> None:
    service, store, device_key, moved = _bound_device()
    bound = store.get_device("dev_test_01")
    assert bound is not None
    service.release_device_binding(device_id="dev_test_01", binding_id=cast(str, bound.binding_id))

    # Not a reprovision any more: a normal onboarding session, for a new owner.
    session = _introspect(service, device_key, moved, actor_id="person_b", client_id="client_b")
    assert session["purpose"] == "onboarding"
    online = service.submit_online_proof(
        onboarding_session_id=str(session["onboarding_session_id"]),
        proof=_proof(
            device_key=device_key,
            payload=moved,
            session=session,
            challenge=_challenge(service, session),
            counter=3,
        ),
    )
    claim = service.reserve_claim(
        actor_id="person_b",
        onboarding_session_id=str(session["onboarding_session_id"]),
        device_id="dev_test_01",
        idempotency_key="claim-after-release",
        expected_state_version=int(cast(int, online["state_version"])),
    )
    result = service.create_binding(
        actor_id="person_b",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=str(session["onboarding_session_id"]),
        initialization=_initialization("person_b"),
        idempotency_key="binding-after-release",
    )
    manifest = cast(dict[str, object], result["manifest"])
    assert manifest["activation_version"] == bound.activation_version + 1
    rebound = store.get_device("dev_test_01")
    assert rebound is not None
    assert rebound.lifecycle_status is DeviceLifecycle.BOUND
    assert rebound.actor_id == "person_b"
    assert rebound.binding_id != bound.binding_id


def test_the_released_claim_and_binding_are_marked_released() -> None:
    service, store, _device_key, _moved = _bound_device()
    bound = store.get_device("dev_test_01")
    assert bound is not None
    binding_id = cast(str, bound.binding_id)
    service.release_device_binding(device_id="dev_test_01", binding_id=binding_id, reason="unbind")
    with store._read() as connection:  # noqa: SLF001
        binding = connection.execute(
            "SELECT status FROM device_onboarding_bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        claim = connection.execute(
            "SELECT status, failure_code, released_at FROM device_onboarding_claims WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
    assert binding["status"] == "released"
    assert claim["status"] == ClaimStatus.RELEASED.value
    assert claim["failure_code"] == "unbind"
    assert claim["released_at"] is not None


@pytest.mark.asyncio
async def test_postgres_release_frees_the_device_under_rls(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],  # noqa: F811
) -> None:
    api, maintenance, _admin_dsn = postgres_onboarding
    device_key = Ed25519PrivateKey.generate()
    device = replace(
        _device("dev_release"), public_key=device_key.public_key().public_bytes_raw()
    )
    maintenance.register_manufactured_device(device)
    service = DeviceOnboardingService(
        api, offline_mock=True, now_fn=lambda: NOW, minimum_firmware_security_version=1
    )
    payload = BootstrapQRPayload(
        typ="memoria-device-bootstrap",
        ver=1,
        device_id=device.device_id,
        bootstrap_nonce=b64url_encode(b"release-nonce-01"),
        ble_name="MEM-RELS",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        certificate_id=device.certificate_id,
        provisioning_protocol="memoria-provisioning/1",
        firmware_version=device.firmware_version,
        pop=b64url_encode(b"0123456789abcdef-pop"),
    )

    def bind(actor: str, qr: BootstrapQRPayload, counter: int) -> dict[str, object]:
        session = service.introspect(
            actor_id=actor,
            qr_payload=encode_bootstrap_qr(qr, device_key),
            client_onboarding_id=f"client_{actor}",
            client=CLIENT,
        )
        online = service.submit_online_proof(
            onboarding_session_id=str(session["onboarding_session_id"]),
            proof=_signed_online_proof(
                device=device,
                device_key=device_key,
                payload=qr,
                session=session,
                challenge=service.issue_challenge(
                    onboarding_session_id=str(session["onboarding_session_id"]),
                    device_id=device.device_id,
                    certificate_id=device.certificate_id,
                ),
                counter=counter,
            ),
        )
        claim = service.reserve_claim(
            actor_id=actor,
            onboarding_session_id=str(session["onboarding_session_id"]),
            device_id=device.device_id,
            idempotency_key=f"claim_{actor}",
            expected_state_version=int(cast(int, online["state_version"])),
        )
        return service.create_binding(
            actor_id=actor,
            claim_id=str(claim["claim_id"]),
            onboarding_session_id=str(session["onboarding_session_id"]),
            initialization=_initialization(actor),
            idempotency_key=f"binding_{actor}",
        )

    bind("actor_first", payload, 1)
    bound = api.get_device(device.device_id)
    assert bound is not None and bound.lifecycle_status is DeviceLifecycle.BOUND

    assert service.release_device_binding(
        device_id=device.device_id, binding_id=cast(str, bound.binding_id)
    )
    released = api.get_device(device.device_id)
    assert released is not None
    assert released.lifecycle_status is DeviceLifecycle.PROVISIONED
    assert (released.actor_id, released.binding_id) == (None, None)
    assert not service.release_device_binding(
        device_id=device.device_id, binding_id=cast(str, bound.binding_id)
    )

    moved = replace(payload, bootstrap_nonce=b64url_encode(b"release-nonce-02"))
    result = bind("actor_second", moved, 2)
    manifest = cast(dict[str, object], result["manifest"])
    assert manifest["activation_version"] == bound.activation_version + 1
    rebound = api.get_device(device.device_id)
    assert rebound is not None and rebound.actor_id == "actor_second"
