from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.contracts.generated.python.multi_subject_contracts import DeviceTrust

from services.device_fleet.authority import CommandAuthorizationRejected
from services.device_fleet.crypto import sign_device_attestation
from services.device_fleet.domain import DeviceFleetConflict
from services.device_fleet.service import DeviceFleetService
from services.device_fleet.tests.test_postgres_attestation import (
    AUTHORIZED_OWNER,
    PgServices,
    _attestation_payload,
    _provision_bound_device,
    _TestDeviceCommandAuthority,
    pg_services,  # noqa: F401 - pytest discovers imported fixtures by name
)


@pytest.mark.asyncio
async def test_server_sim_suspend_cannot_be_overridden_by_old_or_new_device_active_claim(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, _worker, action_service = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(
        maintenance, private_key=device_key, now=now, provision_sim=False
    )
    initial_sim = await maintenance.provision_sim_authority(
        context,
        sim_id="esim-profile-1",
        provider="carrier-a",
        profile_kind="esim",
        provider_status="active",
        now=now,
    )
    assert initial_sim.revision == 1
    nonce = await api.issue_attestation_nonce(context, now=now)
    await api.accept_attestation(
        context,
        sign_device_attestation(
            _attestation_payload(nonce=nonce, counter=1, now=now), device_key
        ),
        now=now,
    )
    trusted = await projector.resolve_device_trust(context, now=now)
    assert trusted is not None
    assert trusted.trust is DeviceTrust.DEVICE_TRUST_VERIFIED
    assert trusted.server_sim_status == "active"
    assert trusted.attested_sim_status == "active"

    suspended = await action_service.transition_sim_authority(
        context,
        authority_input=AUTHORIZED_OWNER,
        expected_sim_id="esim-profile-1",
        expected_revision=1,
        action="suspend",
        now=now + timedelta(seconds=1),
    )
    assert (suspended.status, suspended.revision) == ("suspended", 2)
    stale = await projector.resolve_device_trust(
        context, now=now + timedelta(seconds=1)
    )
    assert stale is not None
    assert stale.trust is DeviceTrust.DEVICE_TRUST_UNTRUSTED
    assert stale.server_sim_status == "suspended"
    assert stale.attested_sim_status == "active"

    nonce = await api.issue_attestation_nonce(
        context, now=now + timedelta(seconds=2)
    )
    await api.accept_attestation(
        context,
        sign_device_attestation(
            {
                **_attestation_payload(
                    nonce=nonce,
                    counter=2,
                    now=now + timedelta(seconds=2),
                ),
                "attestation_id": "attestation-self-reactivate-sim",
            },
            device_key,
        ),
        now=now + timedelta(seconds=2),
    )
    still_suspended = await projector.resolve_device_trust(
        context, now=now + timedelta(seconds=2)
    )
    assert still_suspended is not None
    assert still_suspended.trust is DeviceTrust.DEVICE_TRUST_UNTRUSTED
    assert still_suspended.server_sim_status == "suspended"


@pytest.mark.asyncio
async def test_sim_action_rejects_forged_authenticated_actor(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, _api, _projector, _worker, action_service = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    context = await _provision_bound_device(
        maintenance,
        private_key=Ed25519PrivateKey.generate(),
        now=now,
        provision_sim=False,
    )
    await maintenance.provision_sim_authority(
        context,
        sim_id="forged-actor-sim",
        provider="carrier-a",
        profile_kind="esim",
        provider_status="active",
        now=now,
    )
    forged = DeviceFleetService(
        action_service.store,
        command_signing_key=Ed25519PrivateKey.generate(),
        command_signer_key_id="forged-actor-test",
        command_authority=_TestDeviceCommandAuthority(
            authenticated_actor="attacker-person"
        ),
    )

    with pytest.raises(
        (DeviceFleetConflict, CommandAuthorizationRejected),
    ):
        await forged.transition_sim_authority(
            context,
            authority_input=AUTHORIZED_OWNER,
            expected_sim_id="forged-actor-sim",
            expected_revision=1,
            action="suspend",
            now=now + timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_sim_replace_revoke_expire_are_revision_cas_and_auditable(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, _api, _projector, _worker, action_service = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    context = await _provision_bound_device(
        maintenance,
        private_key=Ed25519PrivateKey.generate(),
        now=now,
        provision_sim=False,
    )
    await maintenance.provision_sim_authority(
        context,
        sim_id="iccid-1",
        provider="carrier-a",
        profile_kind="physical",
        provider_status="active",
        now=now,
    )
    replacement = await action_service.transition_sim_authority(
        context,
        authority_input=AUTHORIZED_OWNER,
        expected_sim_id="iccid-1",
        expected_revision=1,
        action="replace",
        replacement_sim_id="esim-2",
        replacement_provider="carrier-b",
        replacement_profile_kind="esim",
        now=now + timedelta(seconds=1),
    )
    assert (replacement.sim_id, replacement.status, replacement.revision) == (
        "esim-2",
        "active",
        2,
    )
    revoked = await action_service.transition_sim_authority(
        context,
        authority_input=AUTHORIZED_OWNER,
        expected_sim_id="esim-2",
        expected_revision=2,
        action="revoke",
        now=now + timedelta(seconds=2),
    )
    assert (revoked.status, revoked.revision) == ("revoked", 3)
    with pytest.raises(DeviceFleetConflict, match="SIM revision conflict"):
        await action_service.transition_sim_authority(
            context,
            authority_input=AUTHORIZED_OWNER,
            expected_sim_id="esim-2",
            expected_revision=2,
            action="expire",
            now=now + timedelta(seconds=3),
        )
