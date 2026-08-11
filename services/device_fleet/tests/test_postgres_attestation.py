from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceAttestation,
    DeviceCapability,
    DeviceCertificateStatus,
    DeviceLifecycleStatus,
    DeviceTrust,
    OtaBootStatus,
    OtaSlotName,
    PhysicalMuteState,
    PrivacyLightState,
    SimLifecycleStatus,
)

from services.device_fleet.authority import (
    CommandAuthorizationRejected,
    DeviceCommandAction,
    DeviceCommandAuthorization,
    DeviceCommandWriteCallback,
)
from services.device_fleet.crypto import sign_device_attestation
from services.device_fleet.domain import (
    AttestationRejected,
    DeviceFleetContext,
    DeviceTrustAuthorityFacts,
)
from services.device_fleet.postgres_store import PostgresDeviceFleetStore
from services.device_fleet.service import DeviceFleetService

ADMIN_DSN = os.getenv(
    "MEMORIA_TEST_POSTGRES_DSN",
    "postgresql://memoria_test:memoria_test_local@127.0.0.1:55439/postgres",
)
ROLE_PASSWORD = "memoria_device_fleet_local_test"
ROLE_NAMES = {
    "api": "memoria_device_fleet_api",
    "projector": "memoria_device_fleet_projector",
    "worker": "memoria_device_fleet_worker",
    "maintenance": "memoria_device_fleet_maintenance",
    "action_executor": "memoria_action_executor",
}


@dataclass(frozen=True, slots=True)
class AuthenticatedTestPrincipal:
    actor_id: str
    binding_role: str
    authenticated: bool = True


AUTHORIZED_OWNER = AuthenticatedTestPrincipal("owner-1", "account_owner")


class _TestDeviceCommandAuthority:
    def __init__(self, *, authenticated_actor: str | None = None) -> None:
        self._authenticated_actor = authenticated_actor

    async def execute_authorized(
        self,
        connection: asyncpg.Connection,
        action: DeviceCommandAction,
        authority_input: object,
        callback: DeviceCommandWriteCallback[object],
    ) -> object:
        if await connection.fetchval("SELECT current_user") != "memoria_action_executor":
            raise CommandAuthorizationRejected("action executor connection required")
        if (
            not isinstance(authority_input, AuthenticatedTestPrincipal)
            or not authority_input.authenticated
            or authority_input.binding_role not in action.allowed_binding_roles
        ):
            raise CommandAuthorizationRejected
        await connection.execute(
            "SELECT set_config('app.authenticated_actor', $1, true)",
            self._authenticated_actor or authority_input.actor_id,
        )
        receipt_id = f"test-policy-{action.canonical_hash}"
        action_fence_hash = action.canonical_hash
        await connection.execute(
            "SELECT device_fleet_test_mint_policy_receipt($1::jsonb)",
            json.dumps(
                {
                    "receipt_id": receipt_id,
                    "actor_id": authority_input.actor_id,
                    "device_id": action.device_id,
                    "binding_id": action.binding_id,
                    "binding_version": action.binding_version,
                    "effect": "allow",
                    "exact_fence": True,
                    "device_trust": "verified",
                    "expires_at": _rfc3339(datetime.now(UTC) + timedelta(minutes=5)),
                    "action_fence_hash": action_fence_hash,
                    "action_resource_fence": {
                        "action_resource_id": action.action_resource_id,
                        "action_revision": action.action_revision,
                    },
                },
                separators=(",", ":"),
            ),
        )
        authorization = DeviceCommandAuthorization(
            action=action,
            actor_id=authority_input.actor_id,
            binding_role=authority_input.binding_role,
            authority_receipt_id=receipt_id,
            action_fence_hash=action_fence_hash,
        )
        return await callback(connection, authorization)


type PgServices = tuple[
    DeviceFleetService,
    DeviceFleetService,
    DeviceFleetService,
    DeviceFleetService,
    DeviceFleetService,
]


def _dsn_with(dsn: str, *, database: str, user: str | None = None, password: str | None = None) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(user or parsed.username or "postgres")
    secret = quote(password if password is not None else parsed.password or "")
    credentials = f"{username}:{secret}" if secret else username
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", f"/{database}", parsed.query, "")
    )


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
async def pg_services() -> AsyncIterator[PgServices]:
    database = f"device_fleet_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    admin_db_dsn = _dsn_with(ADMIN_DSN, database=database)
    stores = {
        role: PostgresDeviceFleetStore(
            _dsn_with(
                admin_db_dsn,
                database=database,
                user=role_name,
                password=ROLE_PASSWORD,
            ),
            role=role,  # type: ignore[arg-type]
        )
        for role, role_name in ROLE_NAMES.items()
    }
    command_key = Ed25519PrivateKey.generate()
    try:
        await stores["maintenance"].initialize(
            bootstrap_dsn=admin_db_dsn,
            role_password=ROLE_PASSWORD,
        )
        bridge = await asyncpg.connect(admin_db_dsn)
        try:
            await bridge.execute(
                """
                CREATE TABLE device_fleet_test_policy_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    payload JSONB NOT NULL
                );
                CREATE OR REPLACE FUNCTION device_fleet_test_mint_policy_receipt(
                    p_receipt JSONB
                ) RETURNS VOID
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog, public
                AS $test_mint$
                BEGIN
                    INSERT INTO device_fleet_test_policy_receipts(receipt_id, payload)
                    VALUES (p_receipt ->> 'receipt_id', p_receipt)
                    ON CONFLICT (receipt_id) DO UPDATE SET payload = EXCLUDED.payload;
                END
                $test_mint$;
                CREATE OR REPLACE FUNCTION action_policy_lock_receipt(
                    p_receipt_id TEXT
                ) RETURNS JSONB
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog, public
                AS $test_lock$
                DECLARE
                    result JSONB;
                BEGIN
                    SELECT payload INTO result
                    FROM device_fleet_test_policy_receipts
                    WHERE receipt_id = p_receipt_id
                    FOR SHARE;
                    RETURN result;
                END
                $test_lock$;
                REVOKE ALL ON FUNCTION device_fleet_test_mint_policy_receipt(JSONB)
                    FROM PUBLIC;
                REVOKE ALL ON FUNCTION action_policy_lock_receipt(TEXT)
                    FROM PUBLIC;
                GRANT EXECUTE ON FUNCTION device_fleet_test_mint_policy_receipt(JSONB)
                    TO memoria_action_executor;
                GRANT EXECUTE ON FUNCTION action_policy_lock_receipt(TEXT)
                    TO memoria_action_executor, memoria_device_fleet_maintenance;
                """
            )
        finally:
            await bridge.close()
        for role in ("api", "projector", "worker", "action_executor"):
            await stores[role].initialize()
        yield tuple(
            DeviceFleetService(
                stores[role],
                command_signing_key=command_key,
                command_signer_key_id="device-fleet-command-key-1",
                command_authority=_TestDeviceCommandAuthority(),
            )
            for role in (
                "maintenance",
                "api",
                "projector",
                "worker",
                "action_executor",
            )
        )  # type: ignore[misc]
    finally:
        for store in stores.values():
            await store.close()
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            await admin.close()


async def _provision_bound_device(
    maintenance: DeviceFleetService,
    *,
    private_key: Ed25519PrivateKey,
    now: datetime,
    provision_sim: bool = True,
) -> DeviceFleetContext:
    capabilities = (
        DeviceCapability.DEVICE_CAPABILITY_DEVICE_CERTIFICATE,
        DeviceCapability.DEVICE_CAPABILITY_SECURE_ELEMENT,
        DeviceCapability.DEVICE_CAPABILITY_PHYSICAL_MICROPHONE_CUT,
        DeviceCapability.DEVICE_CAPABILITY_HARDWARE_PRIVACY_LIGHT,
        DeviceCapability.DEVICE_CAPABILITY_AB_OTA,
        DeviceCapability.DEVICE_CAPABILITY_ANTI_ROLLBACK,
        DeviceCapability.DEVICE_CAPABILITY_REMOTE_ATTESTATION,
    )
    await maintenance.provision_device(
        device_id="device-1",
        capability_manifest_hash="2" * 64,
        capabilities=capabilities,
        firmware_version="1.0.0",
        firmware_security_version=10,
        firmware_sha256="1" * 64,
        bootloader_version="1.0.0",
        anti_rollback_floor_version="1.0.0",
        anti_rollback_floor_security_version=10,
        now=now,
    )
    await maintenance.issue_certificate(
        device_id="device-1",
        certificate_id="certificate-1",
        public_key_b64=_public_key_b64(private_key),
        valid_until=now + timedelta(days=90),
        now=now,
    )
    context = await maintenance.bind_device(
        device_id="device-1",
        family_space_id="family-1",
        binding_id="binding-1",
        binding_version=1,
        now=now,
    )
    if provision_sim:
        await maintenance.provision_sim_authority(
            context,
            sim_id="esim-profile-default",
            provider="test-carrier",
            profile_kind="esim",
            provider_status="active",
            now=now,
        )
    return context


def _attestation_payload(
    *,
    nonce: str,
    counter: int,
    now: datetime,
    certificate_id: str = "certificate-1",
) -> dict[str, object]:
    capabilities = (
        DeviceCapability.DEVICE_CAPABILITY_DEVICE_CERTIFICATE,
        DeviceCapability.DEVICE_CAPABILITY_SECURE_ELEMENT,
        DeviceCapability.DEVICE_CAPABILITY_PHYSICAL_MICROPHONE_CUT,
        DeviceCapability.DEVICE_CAPABILITY_HARDWARE_PRIVACY_LIGHT,
        DeviceCapability.DEVICE_CAPABILITY_AB_OTA,
        DeviceCapability.DEVICE_CAPABILITY_ANTI_ROLLBACK,
        DeviceCapability.DEVICE_CAPABILITY_REMOTE_ATTESTATION,
    )
    return {
        "attestation_schema": "device-attestation-v1",
        "attestation_id": f"attestation-{counter}",
        "device_id": "device-1",
        "certificate_id": certificate_id,
        "certificate_status": DeviceCertificateStatus.DEVICE_CERTIFICATE_STATUS_ACTIVE,
        "device_lifecycle_status": DeviceLifecycleStatus.DEVICE_LIFECYCLE_STATUS_BOUND,
        "binding_id": "binding-1",
        "binding_version": 1,
        "firmware_version": "1.0.0",
        "firmware_security_version": 10,
        "firmware_sha256": "1" * 64,
        "bootloader_version": "1.0.0",
        "capability_manifest_hash": "2" * 64,
        "capabilities": capabilities,
        "attested_capabilities": (
            DeviceCapability.DEVICE_CAPABILITY_DEVICE_CERTIFICATE,
            DeviceCapability.DEVICE_CAPABILITY_SECURE_ELEMENT,
            DeviceCapability.DEVICE_CAPABILITY_AB_OTA,
            DeviceCapability.DEVICE_CAPABILITY_ANTI_ROLLBACK,
            DeviceCapability.DEVICE_CAPABILITY_REMOTE_ATTESTATION,
        ),
        "physical_mute_state": PhysicalMuteState.PHYSICAL_MUTE_STATE_DISENGAGED,
        "privacy_light_state": PrivacyLightState.PRIVACY_LIGHT_STATE_OFF,
        "sim_status": SimLifecycleStatus.SIM_LIFECYCLE_STATUS_ACTIVE,
        "active_ota_slot": OtaSlotName.OTA_SLOT_NAME_A,
        "ota_boot_status": OtaBootStatus.OTA_BOOT_STATUS_CONFIRMED,
        "anti_rollback_floor_version": "1.0.0",
        "anti_rollback_floor_security_version": 10,
        "monotonic_counter": counter,
        "last_command_sequence": 0,
        "nonce": nonce,
        "occurred_at": _rfc3339(now),
        "expires_at": _rfc3339(now + timedelta(minutes=2)),
        "signer_key_id": certificate_id,
        "signature_algorithm": "ed25519",
    }


@pytest.mark.asyncio
async def test_signed_attestation_is_single_use_and_projects_locked_authority_facts(
    pg_services: PgServices,
) -> None:
    maintenance, api, projector, _worker, _action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=device_key, now=now)
    nonce = await api.issue_attestation_nonce(context, now=now)
    attestation = sign_device_attestation(
        _attestation_payload(nonce=nonce, counter=1, now=now),
        device_key,
    )

    facts = await api.accept_attestation(context, attestation, now=now)

    assert type(facts) is DeviceTrustAuthorityFacts
    assert type(facts.attestation) is DeviceAttestation
    assert facts.family_space_id == "family-1"
    assert facts.binding_id == "binding-1"
    assert facts.binding_version == 1
    assert facts.attestation.attestation_id == "attestation-1"
    assert not hasattr(facts, "allowed")
    projected = await projector.resolve_device_trust(context, now=now)
    assert projected == facts

    with pytest.raises(AttestationRejected, match="attestation rejected"):
        await api.accept_attestation(context, attestation, now=now)


@pytest.mark.asyncio
async def test_certificate_rotation_invalidates_old_attestation_and_revocation_clears_trust(
    pg_services: PgServices,
) -> None:
    maintenance, api, projector, _worker, _action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=old_key, now=now)

    old_nonce = await api.issue_attestation_nonce(context, now=now)
    old_attestation = sign_device_attestation(
        _attestation_payload(nonce=old_nonce, counter=1, now=now), old_key
    )
    await api.accept_attestation(context, old_attestation, now=now)
    pending_old_nonce = await api.issue_attestation_nonce(context, now=now)

    await maintenance.rotate_certificate(
        device_id="device-1",
        certificate_id="certificate-2",
        public_key_b64=_public_key_b64(new_key),
        valid_until=now + timedelta(days=90),
        now=now,
    )

    rotated = await projector.resolve_device_trust(context, now=now)
    assert rotated is not None
    assert rotated.trust is DeviceTrust.DEVICE_TRUST_UNTRUSTED
    assert "attestation_fence_stale" in rotated.reasons
    stale = sign_device_attestation(
        _attestation_payload(nonce=pending_old_nonce, counter=2, now=now), old_key
    )
    with pytest.raises(AttestationRejected, match="attestation rejected"):
        await api.accept_attestation(context, stale, now=now)

    new_nonce = await api.issue_attestation_nonce(context, now=now)
    new_payload = _attestation_payload(
        nonce=new_nonce,
        counter=2,
        now=now,
        certificate_id="certificate-2",
    )
    current = sign_device_attestation(new_payload, new_key)
    accepted = await api.accept_attestation(context, current, now=now)
    assert accepted.attestation.certificate_id == "certificate-2"

    await maintenance.emergency_revoke_device(
        device_id="device-1",
        reason_code="owner_requested",
        now=now,
    )
    revoked = await projector.resolve_device_trust(context, now=now)
    assert revoked is not None
    assert revoked.trust is DeviceTrust.DEVICE_TRUST_REVOKED
    with pytest.raises(AttestationRejected, match="attestation rejected"):
        await api.issue_attestation_nonce(context, now=now)


@pytest.mark.asyncio
async def test_attestation_counter_cas_and_cross_family_rls_are_fail_closed(
    pg_services: PgServices,
) -> None:
    maintenance, api, projector, _worker, _action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=device_key, now=now)
    first_nonce = await api.issue_attestation_nonce(context, now=now)
    await api.accept_attestation(
        context,
        sign_device_attestation(
            _attestation_payload(nonce=first_nonce, counter=1, now=now), device_key
        ),
        now=now,
    )
    nonce_a = await api.issue_attestation_nonce(context, now=now)
    nonce_b = await api.issue_attestation_nonce(context, now=now)
    competing = (
        sign_device_attestation(
            {
                **_attestation_payload(nonce=nonce_a, counter=2, now=now),
                "attestation_id": "attestation-2a",
            },
            device_key,
        ),
        sign_device_attestation(
            {
                **_attestation_payload(nonce=nonce_b, counter=2, now=now),
                "attestation_id": "attestation-2b",
            },
            device_key,
        ),
    )

    results = await asyncio.gather(
        *(api.accept_attestation(context, item, now=now) for item in competing),
        return_exceptions=True,
    )

    assert sum(isinstance(item, DeviceTrustAuthorityFacts) for item in results) == 1
    assert sum(isinstance(item, AttestationRejected) for item in results) == 1
    current = await projector.resolve_device_trust(context, now=now)
    assert current is not None and current.attestation.monotonic_counter == 2

    wrong_family = DeviceFleetContext("device-1", "binding-1", 1, "family-2")
    assert await projector.resolve_device_trust(wrong_family, now=now) is None
    with pytest.raises(AttestationRejected, match="attestation rejected"):
        await api.issue_attestation_nonce(wrong_family, now=now)
    async with api.store.transaction(wrong_family) as connection:
        assert await connection.fetchval("SELECT count(*) FROM device_fleet_devices") == 0


@pytest.mark.asyncio
async def test_revocation_race_never_leaves_current_device_trust(
    pg_services: PgServices,
) -> None:
    maintenance, api, projector, _worker, _action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=device_key, now=now)
    nonce = await api.issue_attestation_nonce(context, now=now)
    pending = sign_device_attestation(
        _attestation_payload(nonce=nonce, counter=1, now=now), device_key
    )

    attestation_result, revoke_result = await asyncio.gather(
        api.accept_attestation(context, pending, now=now),
        maintenance.emergency_revoke_device(
            device_id="device-1",
            reason_code="security_incident",
            now=now,
        ),
        return_exceptions=True,
    )

    assert revoke_result is None
    assert attestation_result is None or isinstance(
        attestation_result, (DeviceTrustAuthorityFacts, AttestationRejected)
    )
    revoked = await projector.resolve_device_trust(context, now=now)
    assert revoked is not None
    assert revoked.trust is DeviceTrust.DEVICE_TRUST_REVOKED


@pytest.mark.asyncio
async def test_locked_trust_callback_holds_revocation_until_callback_finishes(
    pg_services: PgServices,
) -> None:
    maintenance, api, projector, _worker, _action = pg_services
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
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()

    async def policy_callback(
        connection: asyncpg.Connection,
        facts: DeviceTrustAuthorityFacts | None,
    ) -> DeviceTrustAuthorityFacts:
        assert connection.is_in_transaction()
        assert facts is not None
        assert facts.trust is DeviceTrust.DEVICE_TRUST_VERIFIED
        callback_entered.set()
        await release_callback.wait()
        return facts

    callback_task = asyncio.create_task(
        projector.execute_with_locked_device_trust(
            context, policy_callback, now=now
        )
    )
    await callback_entered.wait()
    revoke_task = asyncio.create_task(
        maintenance.emergency_revoke_device(
            device_id="device-1",
            reason_code="concurrent_security_incident",
            now=now + timedelta(seconds=1),
        )
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.1)
    release_callback.set()
    locked = await callback_task
    assert locked.trust is DeviceTrust.DEVICE_TRUST_VERIFIED
    await revoke_task
    current = await projector.resolve_device_trust(
        context, now=now + timedelta(seconds=1)
    )
    assert current is not None
    assert current.trust is DeviceTrust.DEVICE_TRUST_REVOKED


@pytest.mark.asyncio
async def test_required_capability_and_physical_privacy_mismatch_are_untrusted(
    pg_services: PgServices,
) -> None:
    maintenance, api, projector, _worker, _action = pg_services
    now = datetime.now(UTC).replace(microsecond=0)
    device_key = Ed25519PrivateKey.generate()
    context = await _provision_bound_device(maintenance, private_key=device_key, now=now)
    nonce = await api.issue_attestation_nonce(context, now=now)
    await api.accept_attestation(
        context,
        sign_device_attestation(
            {
                **_attestation_payload(nonce=nonce, counter=1, now=now),
                "physical_mute_state": PhysicalMuteState.PHYSICAL_MUTE_STATE_ENGAGED,
                "privacy_light_state": PrivacyLightState.PRIVACY_LIGHT_STATE_OFF,
            },
            device_key,
        ),
        now=now,
    )
    mismatched = await projector.resolve_device_trust(
        context,
        now=now,
        required_capabilities=(DeviceCapability.DEVICE_CAPABILITY_SECURE_WIPE,),
    )
    assert mismatched is not None
    assert mismatched.trust is DeviceTrust.DEVICE_TRUST_UNTRUSTED
    assert "physical_privacy_state_mismatch" in mismatched.reasons
    assert "required_capability_missing" in mismatched.reasons


@pytest.mark.asyncio
async def test_runtime_roles_are_real_nobypassrls_nonowners(
    pg_services: PgServices,
) -> None:
    for service, expected in zip(
        pg_services,
        (
            "memoria_device_fleet_maintenance",
            "memoria_device_fleet_api",
            "memoria_device_fleet_projector",
            "memoria_device_fleet_worker",
            "memoria_action_executor",
        ),
        strict=True,
    ):
        async with service.store.transaction() as connection:
            row = await connection.fetchrow(
                """
                SELECT current_user AS name, rolsuper, rolbypassrls,
                       EXISTS (
                         SELECT 1 FROM pg_class
                         WHERE relname = 'device_fleet_devices'
                           AND relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                       ) AS owns_table
                FROM pg_roles WHERE rolname = current_user
                """
            )
        assert row is not None
        assert (row["name"], row["rolsuper"], row["rolbypassrls"], row["owns_table"]) == (
            expected,
            False,
            False,
            False,
        )
