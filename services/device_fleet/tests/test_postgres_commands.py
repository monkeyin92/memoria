from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceTrust,
    PhysicalMuteState,
    PrivacyLightState,
    RemoteDeviceCommand,
    RemoteDeviceCommandExecution,
    RemoteDeviceCommandType,
)

from services.device_fleet.authority import CommandAuthorizationRejected
from services.device_fleet.crypto import (
    sign_device_attestation,
    verify_remote_device_command_signature,
)
from services.device_fleet.dispatcher import (
    RemoteCommandDispatcherPort,
    RemoteCommandDispatchWorker,
)
from services.device_fleet.domain import (
    CommandDispatchReadiness,
    CommandReconcileResult,
    DeviceLifecycleAuthorityFacts,
    RemoteCommandDispatch,
    RemoteCommandRejected,
    RemoteCommandState,
)
from services.device_fleet.service import DeviceFleetService
from services.device_fleet.tests.test_postgres_attestation import (
    AUTHORIZED_OWNER,
    PgServices,
    _attestation_payload,
    _provision_bound_device,
    pg_services,  # noqa: F401 - pytest discovers imported fixtures by name
)


@dataclass(slots=True)
class RecordingDispatcher(RemoteCommandDispatcherPort):
    fail: bool = False
    calls: list[tuple[RemoteDeviceCommand, str]] = field(default_factory=list)

    async def send(
        self,
        command: RemoteDeviceCommand,
        *,
        idempotency_key: str,
    ) -> None:
        self.calls.append((command, idempotency_key))
        if self.fail:
            raise TimeoutError("transport timeout")


async def _dispatch_command(
    worker: DeviceFleetService,
    context: object,
    command: RemoteDeviceCommand,
    *,
    now: datetime,
) -> RemoteCommandDispatch:
    dispatcher = RecordingDispatcher()
    runtime = RemoteCommandDispatchWorker(
        worker,
        dispatcher=dispatcher,
        dispatcher_id="dispatcher-1",
    )
    result = await runtime.dispatch_once(
        context,  # type: ignore[arg-type]
        now=now,
        limit=1,
    )
    assert result.claimed == 1
    assert result.dispatched == 1
    assert result.failed == 0
    assert dispatcher.calls == [(command, command.idempotency_key)]
    assert len(result.deliveries) == 1
    return result.deliveries[0]


@pytest.mark.asyncio
async def test_force_mute_command_is_signed_idempotent_and_replay_safe_without_faking_hardware(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=device_key, now=now)
    nonce = await api.issue_attestation_nonce(context, now=now)
    attestation = sign_device_attestation(
        _attestation_payload(nonce=nonce, counter=1, now=now), device_key
    )
    await api.accept_attestation(context, attestation, now=now)

    issued = await action.issue_remote_command(
        context,
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
        reason_code="privacy_requested",
        parameters={"muted": True},
        idempotency_key="mute-once",
        now=now,
    )
    replayed_issue = await action.issue_remote_command(
        context,
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
        reason_code="privacy_requested",
        parameters={"muted": True},
        idempotency_key="mute-once",
        now=now,
    )

    assert type(issued) is RemoteDeviceCommand
    assert replayed_issue == issued
    assert issued.command_sequence == 1
    assert issued.target_certificate_id == "certificate-1"
    assert issued.expected_binding_id == "binding-1"
    assert issued.expected_binding_version == 1
    assert issued.expected_monotonic_counter == 1
    assert issued.expected_firmware_security_version == 10
    assert verify_remote_device_command_signature(
        issued, action.command_verification_key
    )
    with pytest.raises(ValueError, match="parameters"):
        await action.issue_remote_command(
            context,
            authority_input=AUTHORIZED_OWNER,
            command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
            reason_code="privacy_requested",
            parameters={"muted": False},
            idempotency_key="mute-once",
            now=now,
        )

    execution = await worker.accept_remote_command_execution(
        context,
        issued,
        now=now + timedelta(seconds=1),
    )

    assert type(execution) is RemoteDeviceCommandExecution
    assert execution.command == issued
    assert execution.current_attestation == attestation
    state = await projector.get_remote_command_state(context, issued.command_id)
    assert type(state) is RemoteCommandState
    assert state.status == "accepted"
    assert state.hardware_confirmed_at is None
    facts_before_ack = await projector.resolve_device_trust(
        context, now=now + timedelta(seconds=1)
    )
    assert facts_before_ack is not None
    assert facts_before_ack.attestation.physical_mute_state.value == "disengaged"
    readiness = await worker.command_dispatch_readiness(
        context, now=now + timedelta(seconds=1)
    )
    assert type(readiness) is CommandDispatchReadiness
    assert readiness.ready is False
    assert readiness.accepted_not_dispatched == 1

    await _dispatch_command(
        worker, context, issued, now=now + timedelta(seconds=2)
    )
    ack_nonce = await api.issue_attestation_nonce(
        context, now=now + timedelta(seconds=3)
    )
    ack_payload = {
        **_attestation_payload(
            nonce=ack_nonce,
            counter=2,
            now=now + timedelta(seconds=3),
        ),
        "attestation_id": "attestation-command-ack-1",
        "last_command_sequence": 1,
        "attested_capabilities": tuple(
            _attestation_payload(nonce=ack_nonce, counter=2, now=now)["capabilities"]
        ),
        "physical_mute_state": PhysicalMuteState.PHYSICAL_MUTE_STATE_ENGAGED,
        "privacy_light_state": PrivacyLightState.PRIVACY_LIGHT_STATE_ON,
    }
    await api.accept_attestation(
        context,
        sign_device_attestation(ack_payload, device_key),
        now=now + timedelta(seconds=3),
    )
    state = await projector.get_remote_command_state(context, issued.command_id)
    assert state is not None
    assert state.status == "hardware_confirmed"
    assert state.device_acknowledged_at == now + timedelta(seconds=3)
    assert state.hardware_confirmed_at == now + timedelta(seconds=3)
    with pytest.raises(RemoteCommandRejected, match="remote command rejected"):
        await worker.accept_remote_command_execution(
            context,
            issued,
            now=now + timedelta(seconds=4),
        )
    facts = await projector.resolve_device_trust(context, now=now + timedelta(seconds=4))
    assert facts is not None
    assert facts.attestation.physical_mute_state.value == "engaged"


@pytest.mark.asyncio
async def test_expired_remote_command_and_unmute_are_fail_closed(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, _projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
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
    command = await action.issue_remote_command(
        context,
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_COLLECT_DIAGNOSTICS,
        reason_code="support_requested",
        parameters={"scope": "minimal"},
        idempotency_key="diagnostics-once",
        ttl=timedelta(seconds=1),
        now=now,
    )

    with pytest.raises(RemoteCommandRejected, match="remote command rejected"):
        await worker.accept_remote_command_execution(
            context,
            command,
            now=now + timedelta(seconds=1),
        )
    with pytest.raises(ValueError):
        RemoteDeviceCommandType("unmute")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_type", "expected_lifecycle", "expected_certificate_status"),
    [
        (
            RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_REVOKE_DEVICE,
            "revoked",
            "revoked",
        ),
        (
            RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_SECURE_WIPE,
            "wipe_pending",
            "active",
        ),
    ],
)
async def test_governance_command_transitions_are_authoritative_but_never_fake_wipe(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
    command_type: RemoteDeviceCommandType,
    expected_lifecycle: str,
    expected_certificate_status: str,
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
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
    command = await action.issue_remote_command(
        context,
        authority_input=AUTHORIZED_OWNER,
        command_type=command_type,
        reason_code="owner_requested",
        parameters={},
        idempotency_key=f"governance-{command_type.value}",
        now=now,
    )

    await worker.accept_remote_command_execution(
        context, command, now=now + timedelta(seconds=1)
    )
    before_ack = await projector.resolve_device_lifecycle(context)
    assert before_ack is not None and before_ack.lifecycle_status == "bound"
    await _dispatch_command(
        worker, context, command, now=now + timedelta(seconds=2)
    )
    ack_nonce = await api.issue_attestation_nonce(
        context, now=now + timedelta(seconds=3)
    )
    ack = sign_device_attestation(
        {
            **_attestation_payload(
                nonce=ack_nonce,
                counter=2,
                now=now + timedelta(seconds=3),
            ),
            "attestation_id": f"ack-{command_type.value}",
            "last_command_sequence": 1,
        },
        device_key,
    )
    await api.accept_attestation(
        context, ack, now=now + timedelta(seconds=3)
    )
    lifecycle = await projector.resolve_device_lifecycle(context)
    assert type(lifecycle) is DeviceLifecycleAuthorityFacts
    assert lifecycle.lifecycle_status == expected_lifecycle
    assert lifecycle.certificate_status == expected_certificate_status
    assert lifecycle.lifecycle_status != "wiped"
    trust = await projector.resolve_device_trust(
        context, now=now + timedelta(seconds=1)
    )
    assert trust is not None
    expected_trust = (
        DeviceTrust.DEVICE_TRUST_REVOKED
        if command_type
        is RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_REVOKE_DEVICE
        else DeviceTrust.DEVICE_TRUST_UNTRUSTED
    )
    assert trust.trust is expected_trust


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_type", "parameters"),
    [
        (RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE, {"muted": False}),
        (RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE, {}),
        (
            RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
            {"muted": True, "extra": True},
        ),
        (RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_REVOKE_DEVICE, {"force": True}),
        (RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_SECURE_WIPE, {"confirm": True}),
        (
            RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_COLLECT_DIAGNOSTICS,
            {"scope": "full"},
        ),
        (
            RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_COLLECT_DIAGNOSTICS,
            {"scope": "minimal", "include_audio": True},
        ),
    ],
)
async def test_remote_command_parameters_are_strictly_closed_before_authorization(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
    command_type: RemoteDeviceCommandType,
    parameters: dict[str, object],
) -> None:
    maintenance, api, _projector, _worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
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

    with pytest.raises(ValueError, match="parameters"):
        await action.issue_remote_command(
            context,
            authority_input=AUTHORIZED_OWNER,
            command_type=command_type,
            reason_code="negative_case",
            parameters=parameters,
            idempotency_key=f"negative-{command_type.value}",
            now=now,
        )


@pytest.mark.asyncio
async def test_missing_or_unauthenticated_command_authority_fails_closed(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, _projector, _worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
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
    missing = DeviceFleetService(
        action.store,
        command_signing_key=Ed25519PrivateKey.generate(),
        command_signer_key_id="missing-authority-key",
    )
    with pytest.raises(CommandAuthorizationRejected):
        await missing.issue_remote_command(
            context,
            authority_input=AUTHORIZED_OWNER,
            command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
            reason_code="privacy_requested",
            parameters={"muted": True},
            idempotency_key="missing-authority",
            now=now,
        )
    for authority_input in (
        object(),
        type(AUTHORIZED_OWNER)("owner-1", "account_owner", authenticated=False),
        type(AUTHORIZED_OWNER)("member-1", "member"),
    ):
        with pytest.raises(CommandAuthorizationRejected):
            await action.issue_remote_command(
                context,
                authority_input=authority_input,
                command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
                reason_code="privacy_requested",
                parameters={"muted": True},
                idempotency_key=f"denied-{id(authority_input)}",
                now=now,
            )


@pytest.mark.asyncio
async def test_dispatch_lease_requeues_then_ack_timeout_is_uncertain_until_signed_ack(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
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
    command = await action.issue_remote_command(
        context,
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
        reason_code="privacy_requested",
        parameters={"muted": True},
        idempotency_key="lease-reconcile",
        now=now,
    )
    await worker.accept_remote_command_execution(
        context, command, now=now + timedelta(seconds=1)
    )
    first = await worker.claim_remote_commands(
        context,
        dispatcher_id="dispatcher-1",
        now=now + timedelta(seconds=2),
        lease_ttl=timedelta(seconds=1),
    )
    assert len(first) == 1
    reconciled = await worker.reconcile_remote_commands(
        context, now=now + timedelta(seconds=4)
    )
    assert reconciled == CommandReconcileResult(requeued=1, uncertain=0)

    second = await worker.claim_remote_commands(
        context,
        dispatcher_id="dispatcher-1",
        now=now + timedelta(seconds=5),
        lease_ttl=timedelta(seconds=2),
    )
    await worker.mark_remote_command_dispatched(
        context,
        command_id=command.command_id,
        lease_token=second[0].lease_token,
        ack_timeout=timedelta(seconds=1),
        now=now + timedelta(seconds=5),
    )
    reconciled = await worker.reconcile_remote_commands(
        context, now=now + timedelta(seconds=7)
    )
    assert reconciled == CommandReconcileResult(requeued=0, uncertain=1)
    state = await projector.get_remote_command_state(context, command.command_id)
    assert state is not None and state.status == "uncertain"

    ack_nonce = await api.issue_attestation_nonce(
        context, now=now + timedelta(seconds=8)
    )
    await api.accept_attestation(
        context,
        sign_device_attestation(
            {
                **_attestation_payload(
                    nonce=ack_nonce,
                    counter=2,
                    now=now + timedelta(seconds=8),
                ),
                "attestation_id": "late-signed-ack",
                "last_command_sequence": 1,
            },
            device_key,
        ),
        now=now + timedelta(seconds=8),
    )
    state = await projector.get_remote_command_state(context, command.command_id)
    assert state is not None and state.status == "device_acknowledged"
    assert state.hardware_confirmed_at is None


@pytest.mark.asyncio
async def test_dispatcher_port_marks_only_success_and_reclaims_timeout_with_stable_key(
    pg_services: PgServices,  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    maintenance, api, projector, worker, action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
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
    command = await action.issue_remote_command(
        context,
        authority_input=AUTHORIZED_OWNER,
        command_type=RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE,
        reason_code="privacy_requested",
        parameters={"muted": True},
        idempotency_key="stable-transport-key",
        now=now,
    )
    await worker.accept_remote_command_execution(
        context, command, now=now + timedelta(seconds=1)
    )

    unavailable = RemoteCommandDispatchWorker(
        worker, dispatcher=None, dispatcher_id="missing-dispatcher"
    )
    readiness = await unavailable.readiness(
        context, now=now + timedelta(seconds=2)
    )
    assert readiness.ready is False
    assert readiness.accepted_not_dispatched == 1

    failing_port = RecordingDispatcher(fail=True)
    failing = RemoteCommandDispatchWorker(
        worker,
        dispatcher=failing_port,
        dispatcher_id="dispatcher-timeout",
        send_timeout=0.1,
        lease_ttl=timedelta(seconds=1),
    )
    failed = await failing.dispatch_once(
        context, now=now + timedelta(seconds=2), limit=1
    )
    assert (failed.claimed, failed.dispatched, failed.failed) == (1, 0, 1)
    state = await projector.get_remote_command_state(context, command.command_id)
    assert state is not None and state.status == "accepted"
    assert failing_port.calls == [(command, "stable-transport-key")]

    reconciled = await worker.reconcile_remote_commands(
        context, now=now + timedelta(seconds=4)
    )
    assert reconciled.requeued == 1
    successful_port = RecordingDispatcher()
    successful = RemoteCommandDispatchWorker(
        worker,
        dispatcher=successful_port,
        dispatcher_id="dispatcher-retry",
    )
    delivered = await successful.dispatch_once(
        context, now=now + timedelta(seconds=5), limit=1
    )
    assert (delivered.claimed, delivered.dispatched, delivered.failed) == (1, 1, 0)
    assert successful_port.calls == [(command, "stable-transport-key")]
    state = await projector.get_remote_command_state(context, command.command_id)
    assert state is not None and state.status == "dispatched"
