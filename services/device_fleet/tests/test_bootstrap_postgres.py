from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.device_fleet.bootstrap_domain import (
    ActivationStatus,
    BindingInitialization,
    BindingRecord,
    BootstrapQRPayload,
    BootstrapSession,
    BootstrapState,
    ChallengeReplay,
    ClaimConflict,
    ClaimReservation,
    ClaimStatus,
    DeviceChallenge,
    DeviceLifecycle,
    DeviceMediaChallenge,
    DeviceOnlineProof,
    DeviceRecord,
    b64url_encode,
    canonical_json_bytes,
    encode_bootstrap_qr,
    hash_b64url,
    sha256_hex,
)
from services.device_fleet.bootstrap_postgres_store import (
    PostgresBootstrapStore,
    PostgresBootstrapStoreContextError,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService

SCHEMA_PATH = Path(__file__).parents[1] / "bootstrap_postgres_schema.sql"
ADMIN_DSN = os.getenv(
    "MEMORIA_TEST_POSTGRES_DSN",
    "postgresql://memoria_test:memoria_test_local@127.0.0.1:55439/postgres",
)
ROLE_PASSWORD = "memoria_device_onboarding_local_test"
NOW = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)


def _dsn_with(dsn: str, *, database: str, user: str, password: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = f"{quote(user)}:{quote(password)}"
    return urlunsplit((parsed.scheme, f"{credentials}@{host}", f"/{database}", "", ""))


def _device(device_id: str) -> DeviceRecord:
    return DeviceRecord(
        device_id=device_id,
        certificate_id=f"cert_{device_id}",
        public_key=bytes(range(32)),
        product_model="memoria-test",
        hardware_revision="rev-a",
        firmware_version="1.0.0",
        firmware_security_version=1,
        capability_manifest_hash=sha256_hex(b"capabilities"),
        minimum_firmware_security_version=1,
        lifecycle_status=DeviceLifecycle.MANUFACTURED,
        last_monotonic_counter=0,
        binding_id=None,
        binding_version=None,
        actor_id=None,
        activation_version=0,
        last_activation_counter=0,
    )


def _session(*, session_id: str, device_id: str, actor_id: str) -> BootstrapSession:
    return BootstrapSession(
        onboarding_session_id=session_id,
        device_id=device_id,
        actor_id=actor_id,
        client_onboarding_id=f"client_{session_id}",
        qr_nonce_hash=sha256_hex(session_id.encode("utf-8")),
        pop_hash=sha256_hex(f"pop:{session_id}"),
        mobile_nonce_hash=sha256_hex(f"mobile:{session_id}"),
        protocol_version=1,
        ble_name="MEM-TEST",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        state=BootstrapState.DEVICE_ONLINE,
        state_version=1,
        first_seen_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        proximity_verified_at=NOW,
        wifi_connected_at=NOW,
        device_online_at=NOW,
        cancelled_at=None,
        consumed_at=None,
        failure_code=None,
    )


def _claim(*, claim_id: str, session: BootstrapSession) -> ClaimReservation:
    return ClaimReservation(
        claim_id=claim_id,
        onboarding_session_id=session.onboarding_session_id,
        device_id=session.device_id,
        actor_id=session.actor_id,
        status=ClaimStatus.RESERVED,
        idempotency_key=f"idem_{claim_id}",
        reserved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        binding_id=None,
        binding_version=None,
        committed_at=None,
        released_at=None,
    )


def _manifest(*, device_id: str, binding_id: str, activation_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "activation_id": activation_id,
        "activation_version": 1,
        "device_id": device_id,
        "binding_id": binding_id,
        "binding_version": 1,
        "persona_assignment_id": "companion_x",
        "service_profile_version": "self_use-v1",
        "policy_bundle_version": "multi-subject-v1",
        "runtime_profile_version": 1,
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "display": {
            "robot_name": "Memoria",
            "primary_subject_display_name": "主要使用者",
        },
        "endpoints": {
            "control_api": "https://control.example.test",
            "device_media": "wss://media.example.test",
        },
        "config_hash": "4" * 64,
        "issued_at": "2026-08-11T08:00:00Z",
        "expires_at": "2026-09-10T08:00:00Z",
        "signature": b64url_encode(b"s" * 64),
    }


@pytest.fixture
async def postgres_onboarding() -> AsyncIterator[tuple[PostgresBootstrapStore, PostgresBootstrapStore, str]]:
    database = f"device_onboarding_{uuid.uuid4().hex[:10]}"
    try:
        admin = await asyncpg.connect(ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL integration DSN unavailable: {exc}")
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    admin_db_dsn = urlunsplit(
        (
            urlsplit(ADMIN_DSN).scheme,
            urlsplit(ADMIN_DSN).netloc,
            f"/{database}",
            "",
            "",
        )
    )
    api_dsn = _dsn_with(
        admin_db_dsn,
        database=database,
        user="memoria_device_onboarding_api",
        password=ROLE_PASSWORD,
    )
    maintenance_dsn = _dsn_with(
        admin_db_dsn,
        database=database,
        user="memoria_device_onboarding_maintenance",
        password=ROLE_PASSWORD,
    )
    maintenance = PostgresBootstrapStore(maintenance_dsn, role="maintenance")
    api = PostgresBootstrapStore(api_dsn)
    try:
        maintenance.initialize(bootstrap_dsn=admin_db_dsn, role_password=ROLE_PASSWORD)
        api.initialize()
        yield api, maintenance, admin_db_dsn
    finally:
        api.close()
        maintenance.close()
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            await admin.close()


def test_schema_is_forward_only_and_forces_rls_on_every_onboarding_table() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8").lower()
    assert "drop table" not in sql
    assert "drop policy" not in sql
    assert "truncate" not in sql
    assert "memoria_device_onboarding_api" in sql
    assert "nobypassrls" in sql
    tables = (
        "device_onboarding_devices",
        "device_onboarding_sessions",
        "device_onboarding_events",
        "device_onboarding_challenges",
        "device_media_challenges",
        "device_onboarding_claims",
        "device_onboarding_bindings",
        "device_onboarding_activations",
    )
    for table in tables:
        assert f"alter table {table} enable row level security" in sql
        assert f"alter table {table} force row level security" in sql
    assert "current_setting('app.device_onboarding.actor_id', true)" in sql
    assert "current_setting('app.device_onboarding.device_id', true)" in sql
    assert "current_setting('app.device_onboarding.lookup_kind', true)" in sql


@pytest.mark.asyncio
async def test_postgres_challenge_is_single_use(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],
) -> None:
    api, maintenance, _admin_dsn = postgres_onboarding
    device = _device("dev_challenge")
    maintenance.register_manufactured_device(device)
    session = _session(session_id="onb_challenge", device_id=device.device_id, actor_id="actor_a")
    api.create_session(session)
    challenge = DeviceChallenge(
        challenge_id="challenge_once",
        onboarding_session_id=session.onboarding_session_id,
        device_id=device.device_id,
        nonce_hash="5" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        used_at=None,
    )
    api.issue_challenge(challenge)
    accepted = api.accept_online_proof(
        challenge_id=challenge.challenge_id,
        onboarding_session_id=session.onboarding_session_id,
        device_id=device.device_id,
        monotonic_counter=1,
        firmware_version=device.firmware_version,
        firmware_security_version=device.firmware_security_version,
        now=NOW,
    )
    assert accepted.state is BootstrapState.DEVICE_ONLINE
    with pytest.raises(ChallengeReplay):
        api.accept_online_proof(
            challenge_id=challenge.challenge_id,
            onboarding_session_id=session.onboarding_session_id,
            device_id=device.device_id,
            monotonic_counter=2,
            firmware_version=device.firmware_version,
            firmware_security_version=device.firmware_security_version,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_postgres_schema_bootstrap_is_repeatable(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],
) -> None:
    _api, _maintenance, admin_dsn = postgres_onboarding
    database = urlsplit(admin_dsn).path.lstrip("/")
    repeat = PostgresBootstrapStore(
        _dsn_with(
            admin_dsn,
            database=database,
            user="memoria_device_onboarding_maintenance",
            password=ROLE_PASSWORD,
        ),
        role="maintenance",
    )
    try:
        repeat.initialize(bootstrap_dsn=admin_dsn, role_password=ROLE_PASSWORD)
    finally:
        repeat.close()


@pytest.mark.asyncio
async def test_postgres_store_preserves_device_onboarding_service_api(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],
) -> None:
    api, maintenance, _admin_dsn = postgres_onboarding
    device_key = Ed25519PrivateKey.generate()
    device = replace(
        _device("dev_service_contract"),
        public_key=device_key.public_key().public_bytes_raw(),
    )
    maintenance.register_manufactured_device(device)
    service = DeviceOnboardingService(
        api,
        offline_mock=True,
        now_fn=lambda: NOW,
        minimum_firmware_security_version=1,
    )
    payload = BootstrapQRPayload(
        typ="memoria-device-bootstrap",
        ver=1,
        device_id=device.device_id,
        bootstrap_nonce=b64url_encode(b"0123456789abcdef"),
        ble_name="MEM-SERVICE",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        certificate_id=device.certificate_id,
        provisioning_protocol="memoria-provisioning/1",
        firmware_version=device.firmware_version,
        pop=b64url_encode(b"0123456789abcdef-pop"),
    )
    session = service.introspect(
        actor_id="actor_service",
        qr_payload=encode_bootstrap_qr(payload, device_key),
        client_onboarding_id="client_service",
        client={
            "platform": "wechat-miniprogram",
            "app_version": "1.0.0",
            "base_library_version": "3.0.0",
        },
    )
    challenge = service.issue_challenge(
        onboarding_session_id=str(session["onboarding_session_id"]),
        device_id=device.device_id,
        certificate_id=device.certificate_id,
    )
    unsigned: dict[str, object] = {
        "device_id": device.device_id,
        "certificate_id": device.certificate_id,
        "bootstrap_nonce_hash": hash_b64url(payload.bootstrap_nonce),
        "mobile_nonce_hash": hash_b64url(str(session["mobile_nonce"])),
        "challenge_id": challenge["challenge_id"],
        "challenge_nonce": challenge["nonce"],
        "firmware_version": device.firmware_version,
        "firmware_security_version": device.firmware_security_version,
        "capability_manifest_hash": device.capability_manifest_hash,
        "network_result": {"got_ip": True, "dns_ready": True, "tls_ready": True},
        "monotonic_counter": 1,
    }
    online = service.submit_online_proof(
        onboarding_session_id=str(session["onboarding_session_id"]),
        proof=DeviceOnlineProof.from_mapping(
            {**unsigned, "signature": b64url_encode(device_key.sign(canonical_json_bytes(unsigned)))}
        ),
    )
    claim = service.reserve_claim(
        actor_id="actor_service",
        onboarding_session_id=str(session["onboarding_session_id"]),
        device_id=device.device_id,
        idempotency_key="claim_service",
        expected_state_version=int(online["state_version"]),
    )
    committed = service.create_binding(
        actor_id="actor_service",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=str(session["onboarding_session_id"]),
        initialization={
            "declared_mode": "self_use",
            "account_owner_person_id": "actor_service",
            "primary_subject": {"person_id": "actor_service", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        },
        idempotency_key="binding_service",
    )
    manifest = dict(committed["manifest"])
    ack_unsigned: dict[str, object] = {
        "device_id": device.device_id,
        "certificate_id": device.certificate_id,
        "binding_id": manifest["binding_id"],
        "binding_version": manifest["binding_version"],
        "activation_version": manifest["activation_version"],
        "config_hash": manifest["config_hash"],
        "firmware_version": device.firmware_version,
        "monotonic_counter": 2,
        "applied_at": "2026-08-11T08:00:00Z",
    }
    status = service.accept_activation_ack(
        device_id=device.device_id,
        ack={
            **ack_unsigned,
            "signature": b64url_encode(device_key.sign(canonical_json_bytes(ack_unsigned))),
        },
    )
    assert status["status"] == ActivationStatus.READY_FOR_CONVERSATION.value
    media = service.issue_media_challenge(
        device_id=device.device_id,
        certificate_id=device.certificate_id,
        client_id="service-client",
    )
    signing_payload = service.media_challenge_signing_payload(
        challenge_id=str(media["challenge_id"]),
        device_id=device.device_id,
        certificate_id=device.certificate_id,
        client_id="service-client",
        nonce=str(media["nonce"]),
        issued_at=datetime.fromisoformat(str(media["issued_at"]).replace("Z", "+00:00")),
    )
    authenticated = service.authenticate_media_challenge(
        device_id=device.device_id,
        certificate_id=device.certificate_id,
        client_id="service-client",
        challenge_id=str(media["challenge_id"]),
        nonce=str(media["nonce"]),
        signature=device_key.sign(canonical_json_bytes(signing_payload)),
    )
    assert authenticated["actor_id"] == "actor_service"


@pytest.mark.asyncio
async def test_postgres_claim_competition_has_one_winner(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],
) -> None:
    api, maintenance, _admin_dsn = postgres_onboarding
    device = _device("dev_claim_race")
    maintenance.register_manufactured_device(device)
    first = _session(session_id="onb_claim_a", device_id=device.device_id, actor_id="actor_a")
    second = _session(session_id="onb_claim_b", device_id=device.device_id, actor_id="actor_b")
    api.create_session(first)
    api.create_session(second)

    def reserve(session: BootstrapSession, claim_id: str) -> ClaimReservation | Exception:
        try:
            return api.reserve_claim(
                _claim(claim_id=claim_id, session=session),
                expected_state_version=1,
                now=NOW,
            )
        except Exception as exc:  # noqa: BLE001 - race result is asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: reserve(*args),
                ((first, "claim_a"), (second, "claim_b")),
            )
        )
    assert sum(isinstance(result, ClaimReservation) for result in results) == 1
    assert sum(isinstance(result, ClaimConflict) for result in results) == 1


@pytest.mark.asyncio
async def test_postgres_binding_activation_ack_and_media_replay(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],
) -> None:
    api, maintenance, _admin_dsn = postgres_onboarding
    device = _device("dev_activation")
    maintenance.register_manufactured_device(device)
    session = _session(session_id="onb_activation", device_id=device.device_id, actor_id="actor_a")
    api.create_session(session)
    claim = _claim(claim_id="claim_activation", session=session)
    api.reserve_claim(claim, expected_state_version=1, now=NOW)
    initialization = BindingInitialization.from_mapping(
        {
            "declared_mode": "self_use",
            "account_owner_person_id": "actor_a",
            "primary_subject": {"person_id": "actor_a", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        }
    )
    binding = BindingRecord(
        binding_id="binding_activation",
        claim_id=claim.claim_id,
        device_id=device.device_id,
        actor_id="actor_a",
        binding_version=1,
        status="draft",
        initialization=initialization,
        created_at=NOW,
        committed_at=None,
    )
    api.begin_binding(
        binding=binding,
        idempotency_key="binding_idem",
        expected_state_version=2,
        now=NOW,
    )
    manifest = _manifest(
        device_id=device.device_id,
        binding_id=binding.binding_id,
        activation_id="activation_one",
    )
    _committed_binding, activation = api.commit_binding(
        claim_id=claim.claim_id,
        actor_id="actor_a",
        binding_id=binding.binding_id,
        binding_version=1,
        manifest=manifest,
        manifest_hash=sha256_hex(b"manifest"),
        activation_id="activation_one",
        activation_version=1,
        activation_expires_at=NOW + timedelta(days=30),
        now=NOW,
    )
    assert activation.status is ActivationStatus.MANIFEST_READY
    ack = {
        "device_id": device.device_id,
        "certificate_id": device.certificate_id,
        "binding_id": binding.binding_id,
        "binding_version": 1,
        "activation_version": 1,
        "config_hash": manifest["config_hash"],
        "firmware_version": device.firmware_version,
        "monotonic_counter": 1,
        "applied_at": "2026-08-11T08:00:00Z",
    }
    ready = api.accept_activation_ack(ack_payload=ack, now=NOW)
    assert ready.status is ActivationStatus.READY_FOR_CONVERSATION
    assert api.accept_activation_ack(ack_payload=ack, now=NOW).status is ActivationStatus.READY_FOR_CONVERSATION
    changed = dict(ack)
    changed["monotonic_counter"] = 2
    with pytest.raises(ChallengeReplay):
        api.accept_activation_ack(ack_payload=changed, now=NOW)
    media = DeviceMediaChallenge(
        challenge_id="media_once",
        device_id=device.device_id,
        certificate_id=device.certificate_id,
        client_id="client_media",
        nonce_hash="6" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        used_at=None,
    )
    api.issue_media_challenge(media)
    assert api.consume_media_challenge(
        challenge_id=media.challenge_id,
        device_id=device.device_id,
        nonce_hash=media.nonce_hash,
        now=NOW,
    ).used_at is not None
    with pytest.raises(ChallengeReplay):
        api.consume_media_challenge(
            challenge_id=media.challenge_id,
            device_id=device.device_id,
            nonce_hash=media.nonce_hash,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_postgres_rls_fails_closed_and_isolates_actor_and_device_contexts(
    postgres_onboarding: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str],
) -> None:
    api, _maintenance, admin_dsn = postgres_onboarding
    database = urlsplit(admin_dsn).path.lstrip("/")
    maintenance_connection = await asyncpg.connect(
        _dsn_with(
            admin_dsn,
            database=database,
            user="memoria_device_onboarding_maintenance",
            password=ROLE_PASSWORD,
        )
    )
    await maintenance_connection.execute(
        """
        INSERT INTO device_onboarding_devices (
            device_id, certificate_id, public_key_b64, product_model,
            hardware_revision, firmware_version, firmware_security_version,
            capability_manifest_hash, minimum_firmware_security_version,
            lifecycle_status, last_monotonic_counter, binding_id, binding_version,
            actor_id, activation_version, last_activation_counter, created_at, updated_at
        ) VALUES
            ('dev_actor_a', 'cert_actor_a', $1, 'model', 'rev', '1.0', 1, $2, 1,
             'bound', 0, 'binding_a', 1, 'actor_a', 1, 0, $3, $3),
            ('dev_actor_b', 'cert_actor_b', $1, 'model', 'rev', '1.0', 1, $2, 1,
             'bound', 0, 'binding_b', 1, 'actor_b', 1, 0, $3, $3),
            ('dev_unbound', 'cert_unbound', $1, 'model', 'rev', '1.0', 1, $2, 1,
             'manufactured', 0, NULL, NULL, NULL, 0, 0, $3, $3)
        """,
        base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("="),
        "7" * 64,
        NOW,
    )
    await maintenance_connection.close()
    connection = await asyncpg.connect(
        _dsn_with(
            admin_dsn,
            database=database,
            user="memoria_device_onboarding_api",
            password=ROLE_PASSWORD,
        )
    )
    try:
        assert await connection.fetchval("SELECT COUNT(*) FROM device_onboarding_devices") == 0
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.device_onboarding.actor_id', 'actor_a', true)"
            )
            assert await connection.fetchval(
                "SELECT COUNT(*) FROM device_onboarding_devices"
            ) == 1
            await connection.execute(
                "SELECT set_config('app.device_onboarding.actor_id', 'actor_b', true)"
            )
            assert await connection.fetchval(
                "SELECT COUNT(*) FROM device_onboarding_devices"
            ) == 1
            assert await connection.fetchval(
                "SELECT device_id FROM device_onboarding_devices"
            ) == "dev_actor_b"
            await connection.execute(
                "SELECT set_config('app.device_onboarding.actor_id', '', true)"
            )
            await connection.execute(
                "SELECT set_config('app.device_onboarding.device_id', 'dev_actor_a', true)"
            )
            assert await connection.fetchval(
                "SELECT device_id FROM device_onboarding_devices"
            ) == "dev_actor_a"
            await connection.execute(
                "SELECT set_config('app.device_onboarding.device_id', 'dev_actor_b', true)"
            )
            assert await connection.fetchval(
                "SELECT device_id FROM device_onboarding_devices"
            ) == "dev_actor_b"
    finally:
        await connection.close()


def test_store_rejects_unscoped_operation_before_database_io() -> None:
    # Constructor only starts the private loop; no DB is needed to verify the
    # fail-closed contract of the context resolver.
    store = object.__new__(PostgresBootstrapStore)
    with pytest.raises(PostgresBootstrapStoreContextError):
        store._scope()  # type: ignore[attr-defined]
