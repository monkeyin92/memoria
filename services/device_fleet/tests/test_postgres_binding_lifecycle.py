from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceTrust,
    RemoteDeviceCommandType,
)

from services.device_fleet.crypto import sign_device_attestation
from services.device_fleet.domain import (
    AttestationRejected,
    DeviceFleetConflict,
    DeviceFleetContext,
)
from services.device_fleet.tests.test_postgres_attestation import (
    AUTHORIZED_OWNER,
    PgServices,
    _attestation_payload,
    _provision_bound_device,
    _public_key_b64,
    pg_services,  # noqa: F401 - pytest discovers imported fixtures by name
)


@pytest.mark.asyncio
async def test_binding_transfer_cas_invalidates_all_old_fences_and_requires_new_certificate(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    old_context = await _provision_bound_device(maintenance, private_key=old_key, now=now)
    nonce = await api.issue_attestation_nonce(old_context, now=now)
    await api.accept_attestation(
        old_context,
        sign_device_attestation(
            _attestation_payload(nonce=nonce, counter=1, now=now), old_key
        ),
        now=now,
    )
    stale_nonce = await api.issue_attestation_nonce(old_context, now=now)
    command = await action.issue_remote_command(
        old_context,
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
        reason_code="privacy_requested",
        parameters={"muted": True},
        idempotency_key="old-binding-command",
        now=now,
    )
    await worker.accept_remote_command_execution(
        old_context, command, now=now + timedelta(seconds=1)
    )
    await action.issue_ota_assignment(
        old_context,
        authority_input=AUTHORIZED_OWNER,
        assignment_id="old-binding-ota",
        artifact_url="https://firmware.example.invalid/v2.bin",
        target_version="2.0.0",
        target_firmware_security_version=20,
        artifact_sha256="3" * 64,
        artifact_size_bytes=4096,
        min_bootloader_version="1.0.0",
        channel="stable",
        now=now,
    )

    new_context = await action.transfer_device_binding(
        old_context,
        authority_input=AUTHORIZED_OWNER,
        new_family_space_id="family-2",
        new_binding_id="binding-2",
        new_binding_version=2,
        new_certificate_id="certificate-2",
        new_public_key_b64=_public_key_b64(new_key),
        certificate_valid_until=now + timedelta(days=90),
        now=now + timedelta(seconds=2),
    )

    assert new_context == DeviceFleetContext("device-1", "binding-2", 2, "family-2")
    stale = sign_device_attestation(
        {
            **_attestation_payload(
                nonce=stale_nonce,
                counter=2,
                now=now + timedelta(seconds=2),
            ),
            "attestation_id": "stale-old-binding",
        },
        old_key,
    )
    with pytest.raises(AttestationRejected, match="attestation rejected"):
        await api.accept_attestation(old_context, stale, now=now + timedelta(seconds=2))
    assert await worker.claim_remote_commands(
        old_context,
        dispatcher_id="old-binding-dispatcher",
        now=now + timedelta(seconds=3),
    ) == ()
    assert await projector.get_latest_ota_state(old_context) is None
    transferred = await projector.resolve_device_trust(
        new_context, now=now + timedelta(seconds=3)
    )
    assert transferred is not None
    assert transferred.trust is DeviceTrust.DEVICE_TRUST_UNTRUSTED

    new_nonce = await api.issue_attestation_nonce(
        new_context, now=now + timedelta(seconds=3)
    )
    new_attestation = sign_device_attestation(
        {
            **_attestation_payload(
                nonce=new_nonce,
                counter=2,
                now=now + timedelta(seconds=3),
                certificate_id="certificate-2",
            ),
            "attestation_id": "new-binding-attestation",
            "binding_id": "binding-2",
            "binding_version": 2,
        },
        new_key,
    )
    accepted = await api.accept_attestation(
        new_context, new_attestation, now=now + timedelta(seconds=3)
    )
    assert accepted.binding_id == "binding-2"
    assert accepted.binding_version == 2

    with pytest.raises(DeviceFleetConflict, match="binding version conflict"):
        await action.transfer_device_binding(
            old_context,
            authority_input=AUTHORIZED_OWNER,
            new_family_space_id="family-3",
            new_binding_id="binding-3",
            new_binding_version=2,
            new_certificate_id="certificate-3",
            new_public_key_b64=_public_key_b64(Ed25519PrivateKey.generate()),
            certificate_valid_until=now + timedelta(days=90),
            now=now + timedelta(seconds=4),
        )
