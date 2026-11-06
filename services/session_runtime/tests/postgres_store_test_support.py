"""Shared fixtures, helpers and schema constants for the split session-runtime Postgres tests."""

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest_asyncio
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
    RuntimeProfileV2,
    SessionEvent,
)
from services.consent.evidence import (
    BindingEvidence,
    ConsentEvidence,
    ConsentParams,
    ConsentSnapshot,
)
from services.policy.action_fence import (
    build_default_action_resource_fence,
)
from services.policy.postgres_receipt_repository import (
    POLICY_RECEIPTS_V2_SCHEMA_SQL,
)
from services.session_runtime.postgres_store import (
    SESSION_RUNTIME_SCHEMA_SQL,
    PostgresSessionRuntimeStore,
)
from services.session_runtime.profile_service import sign_runtime_profile_payload

_DEFAULT_POSTGRES_DSN = (
    "postgresql://memoria_test:memoria_test_local@127.0.0.1:55439/postgres"
)


_IDENTITY_SCHEMA_SQL = Path("services/identity/postgres_schema.sql").read_text(
    encoding="utf-8"
)


_DEVICE_FLEET_SCHEMA_SQL = Path(
    "services/device_fleet/postgres_schema.sql"
).read_text(encoding="utf-8")


_CONSENT_SCHEMA_SQL = Path("services/consent/postgres_schema.sql").read_text(
    encoding="utf-8"
)


_SIGNING_KEY = b"postgres-session-runtime-signing-key"


def _dsn_with(
    dsn: str,
    *,
    database: str,
    user: str | None = None,
    password: str | None = None,
) -> str:
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


def _signed_unknown_profile(
    *,
    session_id: str,
    actor_id: str,
    device_id: str,
    binding_id: str,
    profile_id: str,
    issued_at: datetime,
) -> RuntimeProfileSignedV2:
    payload: dict[str, object] = {
        "signature_schema": "runtime-profile-v2",
        "runtime_profile_id": profile_id,
        "device_id": device_id,
        "session_id": session_id,
        "actor_id": actor_id,
        "binding_id": binding_id,
        "binding_version": 1,
        "active_subject_id": None,
        "subject_revision": 0,
        "subject_category": "unknown",
        "age_band": "unknown",
        "speaker_state": "unconfirmed",
        "speaker_confidence": None,
        "service_mode": "unknown_safe",
        "persona_assignment_id": "starlight:v1",
        "persona": {
            "persona_id": "starlight",
            "version": 1,
            "relationship_stage": "new",
        },
        "policy_bundle_version": "multi-subject-v2",
        "capabilities": ["chat"],
        "obligations": [
            {
                "code": code,
                "params": {
                    "max_session_seconds": None,
                    "retention_ttl_seconds": None,
                    "quiet_hours": None,
                    "extras": [],
                },
            }
            for code in (
                "DO_NOT_PERSIST",
                "DO_NOT_WRITE_LEARNING_PROGRESS",
                "NO_MODEL_TRAINING",
                "REQUIRE_SPEAKER_CONFIRMATION",
            )
        ],
        "policy_receipt_ids": [],
        "session_epoch": 1,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(minutes=5)).isoformat(),
    }
    canonical = RuntimeProfileV2.model_validate(payload).model_dump(mode="json")
    return RuntimeProfileSignedV2.model_validate(
        {
            **canonical,
            "signature": sign_runtime_profile_payload(
                canonical,
                signing_key=_SIGNING_KEY,
            ),
        }
    )


def _profile_event(
    profile: RuntimeProfileSignedV2,
    *,
    event_type: str = "profile_rotated",
    event_sequence: int = 1,
    generation_id: int = 0,
    turn_id: int = 0,
    tool_epoch: int = 0,
) -> SessionEvent:
    return SessionEvent.model_validate(
        {
            "event_id": f"event-{profile.runtime_profile_id}",
            "event_type": event_type,
            "session_id": profile.session_id,
            "session_epoch": profile.session_epoch,
            "device_id": profile.device_id,
            "binding_id": profile.binding_id,
            "binding_version": profile.binding_version,
            "generation_id": generation_id,
            "turn_id": turn_id,
            "tool_epoch": tool_epoch,
            "event_sequence": event_sequence,
            "active_subject_id": profile.active_subject_id,
            "runtime_profile_id": profile.runtime_profile_id,
            "actor_id": profile.actor_id,
            "subject_revision": profile.subject_revision,
            "occurred_at": profile.issued_at.isoformat(),
            "payload": {"reason": "session_started"},
        }
    )


def _session_fence_receipt(
    *,
    receipt_id: str,
    profile: RuntimeProfileSignedV2,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    issued_at: datetime,
) -> PolicyReceiptV2:
    action_fence = build_default_action_resource_fence(
        capability="chat",
        purpose="user_request",
        actor_id=profile.actor_id,
        subject_id=profile.active_subject_id,
        resource_owner_id=profile.active_subject_id,
        device_id=profile.device_id,
        binding_id=profile.binding_id,
        binding_version=profile.binding_version,
        session_id=profile.session_id,
        session_epoch=profile.session_epoch,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=tool_epoch,
        evaluated_at=issued_at,
    )
    return PolicyReceiptV2.model_validate(
        {
            "receipt_id": receipt_id,
            "actor_id": profile.actor_id,
            "subject_id": profile.active_subject_id,
            "resource_owner_id": profile.active_subject_id,
            "device_id": profile.device_id,
            "capability": "chat",
            "purpose": "user_request",
            "effect": "allow",
            "reason_code": "adult_subject_authorized",
            "obligations": [],
            "policy_version": "multi-subject-v2",
            "context_hash": "0" * 64,
            "action_resource_fence": action_fence,
            "action_fence_hash": action_fence.canonical_hash,
            "consent_snapshot_ids": [],
            "consent_snapshot_revisions": [],
            "relationship_snapshot_ids": [],
            "relationship_snapshot_revisions": [],
            "binding_id": profile.binding_id,
            "binding_version": profile.binding_version,
            "binding_canonical_hash": "2" * 64,
            "session_id": profile.session_id,
            "session_epoch": profile.session_epoch,
            "runtime_profile_id": profile.runtime_profile_id,
            "subject_revision": profile.subject_revision,
            "device_trust": "trusted",
            "data_classification": "ephemeral",
            "safety_state": "normal",
            "jurisdiction": "CN",
            "created_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(minutes=5)).isoformat(),
            "exact_fence": True,
        }
    )


async def _seed_binding(
    connection: asyncpg.Connection,
    *,
    actor_id: str,
    device_id: str,
    binding_id: str,
) -> None:
    now = datetime.now(UTC)
    await connection.execute(
        """
        INSERT INTO identity_persons (
            person_id, display_name, subject_category, age_band,
            age_evidence_status, locale, timezone, status, created_at, updated_at
        ) VALUES ($1, $1, 'adult', 'adult', 'verified', 'zh-CN',
                  'Asia/Shanghai', 'active', $2, $2)
        """,
        actor_id,
        now,
    )
    await connection.execute(
        """
        INSERT INTO identity_device_bindings (
            binding_id, device_id, declared_mode, family_space_id,
            account_owner_person_id, binding_version, status, reason,
            valid_from, valid_until, supersedes_binding_id,
            service_profile_version, policy_bundle_version,
            consent_snapshot_id, persona_assignment_id, created_at
        ) VALUES ($1, $2, 'self_use', NULL, $3, 1, 'active', 'create',
                  $4, NULL, NULL, 'self-v1', 'multi-subject-v2', NULL,
                  'starlight:v1', $4)
        """,
        binding_id,
        device_id,
        actor_id,
        now,
    )
    await connection.execute(
        """
        INSERT INTO identity_device_binding_roles (
            binding_id, person_id, role, status, permissions_json,
            granted_at, ended_at
        ) VALUES ($1, $2, 'account_owner', 'active',
                  '["binding.manage"]'::jsonb, $3, NULL)
        """,
        binding_id,
        actor_id,
        now,
    )
    await connection.execute(
        """
        INSERT INTO identity_device_binding_roles (
            binding_id, person_id, role, status, permissions_json,
            granted_at, ended_at
        ) VALUES ($1, $2, 'primary_subject', 'active',
                  '["content.read"]'::jsonb, $3, NULL)
        """,
        binding_id,
        actor_id,
        now,
    )
    await connection.execute(
        """
        INSERT INTO device_fleet_devices (
            device_id, family_space_id, binding_id, binding_version,
            lifecycle_status, capability_manifest_hash, capabilities,
            firmware_version, firmware_security_version, firmware_sha256,
            bootloader_version, anti_rollback_floor_version,
            anti_rollback_floor_security_version, created_at, updated_at
        ) VALUES (
            $1, $2, $3, 1, 'bound', $4, '[]'::jsonb,
            'test-fw', 1, $4, 'test-boot', 'test-fw', 1, $5, $5
        )
        """,
        device_id,
        f"family-{binding_id}",
        binding_id,
        hashlib.sha256(f"manifest:{device_id}".encode()).hexdigest(),
        now,
    )


async def _seed_delegated_binding(
    connection: asyncpg.Connection,
    *,
    actor_id: str,
    subject_id: str,
    device_id: str,
    binding_id: str,
    declared_mode: str,
    subject_category: str,
    age_band: str,
) -> None:
    now = datetime.now(UTC)
    await connection.executemany(
        """
        INSERT INTO identity_persons (
            person_id, display_name, subject_category, age_band,
            age_evidence_status, locale, timezone, status, created_at, updated_at
        ) VALUES ($1, $1, $2, $3, $4, 'zh-CN', 'Asia/Shanghai',
                  'active', $5, $5)
        """,
        (
            (actor_id, "adult", "adult", "verified", now),
            (
                subject_id,
                subject_category,
                age_band,
                "verified" if subject_category == "adult" else "unverified",
                now,
            ),
        ),
    )
    await connection.execute(
        """
        INSERT INTO identity_device_bindings (
            binding_id, device_id, declared_mode, family_space_id,
            account_owner_person_id, binding_version, status, reason,
            valid_from, valid_until, supersedes_binding_id,
            service_profile_version, policy_bundle_version,
            consent_snapshot_id, persona_assignment_id, created_at
        ) VALUES ($1, $2, $3, $4, $5, 1, 'active', 'create',
                  $6, NULL, NULL, 'delegated-v1', 'multi-subject-v2', NULL,
                  'starlight:v1', $6)
        """,
        binding_id,
        device_id,
        declared_mode,
        f"family-{binding_id}",
        actor_id,
        now,
    )
    roles = [
        (binding_id, actor_id, "account_owner", '["binding.manage"]', now),
        (binding_id, actor_id, "device_admin", '["binding.manage"]', now),
        (binding_id, subject_id, "primary_subject", '["content.read"]', now),
        (binding_id, subject_id, "member", '["family.shared.read"]', now),
    ]
    if subject_category == "minor":
        roles.append(
            (binding_id, actor_id, "guardian", '["guardian.manage"]', now)
        )
    await connection.executemany(
        """
        INSERT INTO identity_device_binding_roles (
            binding_id, person_id, role, status, permissions_json,
            granted_at, ended_at
        ) VALUES ($1, $2, $3, 'active', $4::jsonb, $5, NULL)
        """,
        roles,
    )
    await connection.execute(
        """
        INSERT INTO device_fleet_devices (
            device_id, family_space_id, binding_id, binding_version,
            lifecycle_status, capability_manifest_hash, capabilities,
            firmware_version, firmware_security_version, firmware_sha256,
            bootloader_version, anti_rollback_floor_version,
            anti_rollback_floor_security_version, created_at, updated_at
        ) VALUES ($1, $2, $3, 1, 'bound', $4, '[]'::jsonb,
                  'test-fw', 1, $4, 'test-boot', 'test-fw', 1, $5, $5)
        """,
        device_id,
        f"family-{binding_id}",
        binding_id,
        hashlib.sha256(f"manifest:{device_id}".encode()).hexdigest(),
        now,
    )


async def _seed_verified_device(
    connection: asyncpg.Connection,
    *,
    device_id: str,
    binding_id: str,
    now: datetime,
) -> None:
    certificate_id = f"certificate-{device_id}"
    nonce = f"attestation-nonce-{device_id}"
    await connection.execute(
        """
        INSERT INTO device_fleet_certificates (
            certificate_id, device_id, family_space_id, binding_id,
            binding_version, public_key_b64, key_algorithm, status,
            valid_from, valid_until, revoked_at, revocation_reason_code,
            created_at
        ) VALUES ($1, $2, $3, $4, 1, $5, 'ed25519', 'active',
                  $6, $7, NULL, NULL, $6)
        """,
        certificate_id,
        device_id,
        f"family-{binding_id}",
        binding_id,
        "A" * 44,
        now - timedelta(hours=1),
        now + timedelta(hours=1),
    )
    await connection.execute(
        """
        INSERT INTO device_fleet_attestation_challenges (
            nonce, device_id, family_space_id, binding_id, binding_version,
            issued_at, expires_at, consumed_at
        ) VALUES ($1, $2, $3, $4, 1, $5, $6, $7)
        """,
        nonce,
        device_id,
        f"family-{binding_id}",
        binding_id,
        now - timedelta(minutes=5),
        now + timedelta(minutes=15),
        now - timedelta(minutes=4),
    )
    await connection.execute(
        """
        INSERT INTO device_fleet_attestations (
            attestation_id, device_id, certificate_id, family_space_id,
            binding_id, binding_version, monotonic_counter, nonce, payload,
            occurred_at, expires_at, accepted_at
        ) VALUES ($1, $2, $3, $4, $5, 1, 1, $6, '{}'::jsonb,
                  $7, $8, $9)
        """,
        f"attestation-{device_id}",
        device_id,
        certificate_id,
        f"family-{binding_id}",
        binding_id,
        nonce,
        now - timedelta(minutes=4),
        now + timedelta(minutes=15),
        now - timedelta(minutes=3),
    )
    await connection.execute(
        """
        UPDATE device_fleet_devices
        SET current_certificate_id = $2,
            last_attestation_counter = 1,
            updated_at = $3
        WHERE device_id = $1
        """,
        device_id,
        certificate_id,
        now,
    )


async def _seed_self_consent(
    connection: asyncpg.Connection,
    *,
    actor_id: str,
    device_id: str,
    binding_id: str,
    capability: str,
    purpose: str,
    now: datetime,
    evidence_status: str = "active",
    evidence_valid_from: datetime | None = None,
    evidence_valid_until: datetime | None = None,
) -> tuple[ConsentEvidence, ConsentSnapshot]:
    snapshot_id = f"snapshot-{capability}-{actor_id}"
    evidence = ConsentEvidence(
        consent_id=f"consent-{capability}-{actor_id}",
        version=1,
        snapshot_id=snapshot_id,
        status=evidence_status,  # type: ignore[arg-type]
        subject_id=actor_id,
        resource_owner_id=actor_id,
        actor_id=actor_id,
        actor_kind="subject",
        device_id=device_id,
        binding_id=binding_id,
        binding_version=1,
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,  # type: ignore[arg-type]
        policy_version="multi-subject-v2",
        evidence_id=f"evidence-{capability}-{actor_id}",
        offer_id=f"offer-{capability}-{actor_id}",
        idempotency_key=f"grant-{capability}-{actor_id}",
        params=ConsentParams(extras=(("data_classification", "biometric"),)),
        valid_from=evidence_valid_from or now - timedelta(hours=1),
        valid_until=evidence_valid_until or now + timedelta(hours=1),
        supersedes_consent_id=None,
        superseded_by_consent_id=None,
    )
    snapshot = ConsentSnapshot(
        snapshot_id=snapshot_id,
        version=1,
        subject_id=actor_id,
        binding_id=binding_id,
        binding_version=1,
        policy_version="multi-subject-v2",
        created_at=now - timedelta(minutes=30),
        grants=(evidence,),
        relationships=(),
        binding=BindingEvidence(
            binding_id=binding_id,
            version=1,
            device_id=device_id,
            status="active",
            declared_mode="self_use",
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(hours=1),
        ),
    )
    await connection.execute(
        """
        INSERT INTO consent_evidence (
            consent_id, version, actor_id, subject_id, binding_id,
            binding_version, status, evidence_json
        ) VALUES ($1, 1, $2, $2, $3, 1, $4, $5::jsonb)
        """,
        evidence.consent_id,
        actor_id,
        binding_id,
        evidence_status,
        json.dumps(evidence.to_canonical_dict()),
    )
    await connection.execute(
        """
        INSERT INTO consent_snapshot (
            snapshot_id, version, actor_id, subject_id, binding_id,
            binding_version, snapshot_json
        ) VALUES ($1, 1, $2, $2, $3, 1, $4::jsonb)
        """,
        snapshot.snapshot_id,
        actor_id,
        binding_id,
        json.dumps(snapshot.to_canonical_dict()),
    )
    await connection.execute(
        """
        INSERT INTO consent_evidence_head (
            actor_id, subject_id, binding_id, binding_version, capability,
            purpose, current_consent_id, current_revision, current_hash,
            updated_at
        ) VALUES ($1, $1, $2, 1, $3, $4, $5, 1, $6, $7)
        """,
        actor_id,
        binding_id,
        capability,
        purpose,
        evidence.consent_id,
        evidence.canonical_hash,
        now,
    )
    await connection.execute(
        """
        INSERT INTO consent_snapshot_head (
            subject_id, binding_id, binding_version, current_snapshot_id,
            current_revision, current_hash, updated_at
        ) VALUES ($1, $2, 1, $3, 1, $4, $5)
        """,
        actor_id,
        binding_id,
        snapshot.snapshot_id,
        snapshot.canonical_hash,
        now,
    )
    return evidence, snapshot


@pytest_asyncio.fixture
async def postgres_runtime() -> AsyncIterator[
    tuple[PostgresSessionRuntimeStore, str]
]:
    root_dsn = os.getenv("MEMORIA_TEST_POSTGRES_DSN", _DEFAULT_POSTGRES_DSN)
    database = f"memoria_session_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(root_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()

    bootstrap_dsn = _dsn_with(root_dsn, database=database)
    admin = await asyncpg.connect(bootstrap_dsn)
    app_password = uuid.uuid4().hex
    action_password = uuid.uuid4().hex
    try:
        await admin.execute(_DEVICE_FLEET_SCHEMA_SQL)
        await admin.execute(_IDENTITY_SCHEMA_SQL)
        await admin.execute("RESET ROLE")
        await admin.execute(POLICY_RECEIPTS_V2_SCHEMA_SQL)
        await _seed_binding(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
        )
        await _seed_binding(
            admin,
            actor_id="actor-b",
            device_id="device-b",
            binding_id="binding-b",
        )
    finally:
        await admin.close()

    app_dsn = _dsn_with(
        root_dsn,
        database=database,
        user="memoria_session_api",
        password=app_password,
    )
    action_dsn = _dsn_with(
        root_dsn,
        database=database,
        user="memoria_action_executor",
        password=action_password,
    )
    store = PostgresSessionRuntimeStore(
        dsn=app_dsn,
        action_dsn=action_dsn,
        bootstrap_dsn=bootstrap_dsn,
    )
    # The Session schema owns creation of the cluster roles. Install it on the
    # bootstrap connection before assigning per-test passwords; initialize()
    # can then verify both the idempotent schema and the runtime logins.
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(SESSION_RUNTIME_SCHEMA_SQL)
        await admin.execute(
            f"ALTER ROLE memoria_session_api PASSWORD '{app_password}'"
        )
        await admin.execute(
            f"ALTER ROLE memoria_action_executor PASSWORD '{action_password}'"
        )
    finally:
        await admin.close()
    await store.initialize()
    try:
        yield store, bootstrap_dsn
    finally:
        await store.close()
        admin = await asyncpg.connect(root_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=$1",
                database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            await admin.close()


@pytest_asyncio.fixture
async def postgres_runtime_with_consent(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> AsyncIterator[tuple[PostgresSessionRuntimeStore, str]]:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(_CONSENT_SCHEMA_SQL)
    finally:
        await admin.close()

    assert (
        await store.readiness()
    )["action_executor_consent_discover_exec"] == "ready"
    yield store, bootstrap_dsn


# ---------------------------------------------------------------------------
# Device trust follows the onboarding authority (2026-09-28).  A rebind from
# the Mini Program commits a new binding in Identity and device_onboarding_*
# only; device_fleet_* keeps the older binding.  Session must follow
# onboarding, not refuse the device with device_binding_mismatch.

_DEVICE_ONBOARDING_SCHEMA_SQL = Path(
    "services/device_fleet/bootstrap_postgres_schema.sql"
).read_text(encoding="utf-8")
