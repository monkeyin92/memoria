from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.contracts.generated.python.multi_subject_contracts import (
    OtaAssignment,
    OtaBootStatus,
    OtaSlotName,
    OtaStateReceipt,
    RemoteDeviceCommandType,
)

from services.device_fleet.crypto import (
    sign_device_attestation,
    sign_ota_state_receipt,
    verify_ota_assignment_signature,
)
from services.device_fleet.domain import DeviceFleetContext, OtaRejected
from services.device_fleet.service import DeviceFleetService
from services.device_fleet.tests.test_postgres_attestation import (
    AUTHORIZED_OWNER,
    PgServices,
    _attestation_payload,
    _provision_bound_device,
    _rfc3339,
    pg_services,  # noqa: F401 - pytest discovers imported fixtures by name
)


def _slot(
    name: str,
    status: str,
    *,
    version: str | None,
    security_version: int | None,
    digest: str | None,
    boot_attempts: int = 0,
    confirmed_at: datetime | None = None,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "slot": name,
        "boot_status": status,
        "firmware_version": version,
        "firmware_security_version": security_version,
        "artifact_sha256": digest,
        "boot_attempts": boot_attempts,
        "confirmed_at": _rfc3339(confirmed_at) if confirmed_at is not None else None,
        "last_error_code": error,
    }


def _receipt_payload(
    *,
    receipt_id: str,
    assignment_id: str,
    counter: int,
    now: datetime,
    boot_status: str,
    active_slot: str = "a",
    pending_slot: str | None = "b",
    target_slot_status: str = "staged",
    target_attempts: int = 0,
    floor_security_version: int = 10,
    floor_version: str = "1.0.0",
    rollback_from: str | None = None,
    rollback_to: str | None = None,
) -> dict[str, object]:
    target_confirmed = now if target_slot_status == "confirmed" else None
    return {
        "receipt_id": receipt_id,
        "device_id": "device-1",
        "certificate_id": "certificate-1",
        "assignment_id": assignment_id,
        "highest_accepted_version": "2.0.0",
        "highest_accepted_firmware_security_version": 20,
        "anti_rollback_floor_version": floor_version,
        "anti_rollback_floor_security_version": floor_security_version,
        "active_slot": active_slot,
        "pending_slot": pending_slot,
        "boot_status": boot_status,
        "slots": (
            _slot(
                "a",
                "confirmed",
                version="1.0.0",
                security_version=10,
                digest="1" * 64,
                confirmed_at=now,
            ),
            _slot(
                "b",
                target_slot_status,
                version="2.0.0",
                security_version=20,
                digest="3" * 64,
                boot_attempts=target_attempts,
                confirmed_at=target_confirmed,
                error="boot_failed" if target_slot_status == "failed" else None,
            ),
        ),
        "max_boot_attempts": 2,
        "rollback_from_version": rollback_from,
        "rollback_to_version": rollback_to,
        "monotonic_counter": counter,
        "occurred_at": _rfc3339(now),
        "signer_key_id": "certificate-1",
        "signature_algorithm": "ed25519",
    }


async def _attested_device(
    maintenance: DeviceFleetService,
    api: DeviceFleetService,
    *,
    now: datetime,
) -> tuple[object, Ed25519PrivateKey]:
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=device_key, now=now)
    nonce = await api.issue_attestation_nonce(context, now=now)
    await api.accept_attestation(
        context,
        sign_device_attestation(
            _attestation_payload(nonce=nonce, counter=1, now=now), device_key
        ),
        now=now,
    )
    return context, device_key


async def _assignment(
    api: DeviceFleetService,
    context: object,
    *,
    now: datetime,
) -> OtaAssignment:
    return await api.issue_ota_assignment(
        context,  # type: ignore[arg-type]
        authority_input=AUTHORIZED_OWNER,
        assignment_id="ota-assignment-1",
        artifact_url="https://firmware.example.invalid/device-1-2.0.0.bin",
        target_version="2.0.0",
        target_firmware_security_version=20,
        artifact_sha256="3" * 64,
        artifact_size_bytes=4096,
        min_bootloader_version="1.0.0",
        channel="stable",
        now=now,
    )


@pytest.mark.asyncio
async def test_ota_staging_boot_attempts_rollback_and_restart_recovery(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    context, device_key = await _attested_device(maintenance, api, now=now)
    assignment = await _assignment(action, context, now=now)

    assert type(assignment) is OtaAssignment
    assert assignment.target_slot is OtaSlotName.OTA_SLOT_NAME_B
    assert verify_ota_assignment_signature(assignment, action.command_verification_key)
    command = await action.issue_remote_command(
        context,  # type: ignore[arg-type]
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA,
        reason_code="security_update",
        parameters={
            "assignment_id": assignment.assignment_id,
            "artifact_url": "https://firmware.example.invalid/device-1-2.0.0.bin",
            "artifact_sha256": "3" * 64,
            "artifact_size_bytes": 4096,
        },
        idempotency_key="assign-ota-once",
        ota_assignment_id=assignment.assignment_id,
        now=now,
    )
    await worker.accept_remote_command_execution(
        context, command, now=now + timedelta(seconds=1)  # type: ignore[arg-type]
    )

    staged = sign_ota_state_receipt(
        _receipt_payload(
            receipt_id="ota-receipt-staged",
            assignment_id=assignment.assignment_id,
            counter=1,
            now=now + timedelta(seconds=2),
            boot_status="staged",
        ),
        device_key,
    )
    await worker.accept_ota_state_receipt(
        context, staged, now=now + timedelta(seconds=2)  # type: ignore[arg-type]
    )
    with pytest.raises(OtaRejected, match="OTA rejected"):
        await worker.accept_ota_state_receipt(
            context, staged, now=now + timedelta(seconds=2)  # type: ignore[arg-type]
        )
    wrong_family = DeviceFleetContext("device-1", "binding-1", 1, "family-2")
    assert await projector.get_latest_ota_state(wrong_family) is None

    await projector.store.close()
    await projector.store.initialize()
    recovered = await projector.get_latest_ota_state(context)  # type: ignore[arg-type]
    assert type(recovered) is OtaStateReceipt
    assert recovered == staged

    for counter, attempts in ((2, 1), (3, 2)):
        booting = sign_ota_state_receipt(
            _receipt_payload(
                receipt_id=f"ota-receipt-boot-{attempts}",
                assignment_id=assignment.assignment_id,
                counter=counter,
                now=now + timedelta(seconds=counter + 1),
                boot_status="booting",
                target_slot_status="booting",
                target_attempts=attempts,
            ),
            device_key,
        )
        await worker.accept_ota_state_receipt(
            context, booting, now=now + timedelta(seconds=counter + 1)  # type: ignore[arg-type]
        )

    rollback_pending = sign_ota_state_receipt(
        _receipt_payload(
            receipt_id="ota-receipt-rollback-pending",
            assignment_id=assignment.assignment_id,
            counter=4,
            now=now + timedelta(seconds=5),
            boot_status="rollback_pending",
            target_slot_status="failed",
            target_attempts=2,
        ),
        device_key,
    )
    await worker.accept_ota_state_receipt(
        context, rollback_pending, now=now + timedelta(seconds=5)  # type: ignore[arg-type]
    )
    rolled_back = sign_ota_state_receipt(
        _receipt_payload(
            receipt_id="ota-receipt-rolled-back",
            assignment_id=assignment.assignment_id,
            counter=5,
            now=now + timedelta(seconds=6),
            boot_status="rolled_back",
            pending_slot=None,
            target_slot_status="failed",
            target_attempts=2,
            rollback_from="2.0.0",
            rollback_to="1.0.0",
        ),
        device_key,
    )
    await worker.accept_ota_state_receipt(
        context, rolled_back, now=now + timedelta(seconds=6)  # type: ignore[arg-type]
    )
    latest = await projector.get_latest_ota_state(context)  # type: ignore[arg-type]
    assert latest == rolled_back
    assert latest.active_slot is OtaSlotName.OTA_SLOT_NAME_A
    assert latest.boot_status is OtaBootStatus.OTA_BOOT_STATUS_ROLLED_BACK


@pytest.mark.asyncio
async def test_ota_confirm_advances_floor_and_rejects_http_or_downgrade(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    context, device_key = await _attested_device(maintenance, api, now=now)
    with pytest.raises(OtaRejected, match="OTA rejected"):
        await action.issue_ota_assignment(
            context,  # type: ignore[arg-type]
            authority_input=AUTHORIZED_OWNER,
            assignment_id="http-assignment",
            artifact_url="http://firmware.example.invalid/unsafe.bin",
            target_version="2.0.0",
            target_firmware_security_version=20,
            artifact_sha256="3" * 64,
            artifact_size_bytes=4096,
            min_bootloader_version="1.0.0",
            channel="stable",
            now=now,
        )
    assignment = await _assignment(action, context, now=now)
    for receipt in (
        sign_ota_state_receipt(
            _receipt_payload(
                receipt_id="confirm-staged",
                assignment_id=assignment.assignment_id,
                counter=1,
                now=now + timedelta(seconds=1),
                boot_status="staged",
            ),
            device_key,
        ),
        sign_ota_state_receipt(
            _receipt_payload(
                receipt_id="confirm-booting",
                assignment_id=assignment.assignment_id,
                counter=2,
                now=now + timedelta(seconds=2),
                boot_status="booting",
                target_slot_status="booting",
                target_attempts=1,
            ),
            device_key,
        ),
        sign_ota_state_receipt(
            _receipt_payload(
                receipt_id="confirm-complete",
                assignment_id=assignment.assignment_id,
                counter=3,
                now=now + timedelta(seconds=3),
                boot_status="confirmed",
                active_slot="b",
                pending_slot=None,
                target_slot_status="confirmed",
                target_attempts=1,
                floor_security_version=20,
                floor_version="2.0.0",
            ),
            device_key,
        ),
    ):
        await worker.accept_ota_state_receipt(
            context, receipt, now=receipt.occurred_at  # type: ignore[arg-type]
        )
    latest = await projector.get_latest_ota_state(context)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.active_slot is OtaSlotName.OTA_SLOT_NAME_B
    assert latest.anti_rollback_floor_security_version == 20

    with pytest.raises(OtaRejected, match="OTA rejected"):
        await action.issue_ota_assignment(
            context,  # type: ignore[arg-type]
            authority_input=AUTHORIZED_OWNER,
            assignment_id="downgrade-assignment",
            artifact_url="https://firmware.example.invalid/downgrade.bin",
            target_version="1.5.0",
            target_firmware_security_version=15,
            artifact_sha256="4" * 64,
            artifact_size_bytes=4096,
            min_bootloader_version="1.0.0",
            channel="stable",
            now=now + timedelta(seconds=4),
        )
