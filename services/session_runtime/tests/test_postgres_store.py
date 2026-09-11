from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
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
    build_action_resource_fence,
    build_default_action_resource_fence,
)
from services.policy.context import PolicyContext
from services.policy.engine import PolicyEngine
from services.policy.postgres_receipt_repository import (
    POLICY_RECEIPTS_V2_SCHEMA_SQL,
)
from services.session_runtime.postgres_store import (
    SESSION_RUNTIME_SCHEMA_SQL,
    PostgresSessionRuntimeStore,
    SessionRuntimeAuthorityUnavailable,
    SessionRuntimeConflict,
    SessionRuntimeContext,
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


def _next_confirmed_profile(
    previous: RuntimeProfileSignedV2,
    *,
    profile_id: str,
    issued_at: datetime,
) -> RuntimeProfileSignedV2:
    payload = previous.model_dump(mode="json", exclude={"signature"})
    payload.update(
        {
            "runtime_profile_id": profile_id,
            "active_subject_id": previous.actor_id,
            "subject_revision": 1,
            "subject_category": "adult",
            "age_band": "adult",
            "speaker_state": "confirmed",
            "speaker_confidence": 1.0,
            "service_mode": "adult_companion",
            "session_epoch": previous.session_epoch + 1,
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(minutes=5)).isoformat(),
        }
    )
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


def _action_fence_payload(
    context: SessionRuntimeContext,
    *,
    fence_kind: str,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    occurred_at: datetime,
) -> dict[str, object]:
    return {
        "session_id": context.session_id,
        "runtime_profile_id": context.current_runtime_profile_id,
        "actor_id": context.actor_id,
        "device_id": context.device_id,
        "binding_id": context.binding_id,
        "binding_version": context.binding_version,
        "session_epoch": context.session_epoch,
        "active_subject_id": context.active_subject_id,
        "subject_revision": context.subject_revision,
        "fence_kind": fence_kind,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": tool_epoch,
        "occurred_at": occurred_at.isoformat(),
    }


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


async def _advance_fence_raw(
    store: PostgresSessionRuntimeStore,
    payload: dict[str, object],
    *,
    actor_id: str = "actor-a",
    device_id: str = "device-a",
) -> str:
    async with store.action_transaction(
        actor_id=actor_id,
        device_id=device_id,
    ) as connection:
        try:
            await connection.fetchval(
                "SELECT session_runtime_advance_action_fence($1::jsonb)",
                json.dumps(payload),
            )
        except asyncpg.PostgresError as exc:
            return exc.sqlstate or ""
    raise AssertionError("illegal action fence advance was accepted")


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


async def _revoke_self_consent(
    connection: asyncpg.Connection,
    *,
    evidence: ConsentEvidence,
    snapshot: ConsentSnapshot,
    now: datetime,
) -> None:
    revoked_snapshot_id = f"{snapshot.snapshot_id}-revoked"
    revoked = replace(
        evidence,
        version=evidence.version + 1,
        snapshot_id=revoked_snapshot_id,
        status="revoked",
        evidence_id=f"{evidence.evidence_id}-revoked",
        idempotency_key=None,
        canonical_hash="",
    )
    revoked_snapshot = ConsentSnapshot(
        snapshot_id=revoked_snapshot_id,
        version=snapshot.version + 1,
        subject_id=snapshot.subject_id,
        binding_id=snapshot.binding_id,
        binding_version=snapshot.binding_version,
        policy_version=snapshot.policy_version,
        created_at=now,
        grants=(),
        relationships=snapshot.relationships,
        binding=snapshot.binding,
    )
    await connection.execute(
        """
        INSERT INTO consent_evidence (
            consent_id, version, actor_id, subject_id, binding_id,
            binding_version, status, evidence_json
        ) VALUES ($1, $2, $3, $4, $5, $6, 'revoked', $7::jsonb)
        """,
        revoked.consent_id,
        revoked.version,
        revoked.actor_id,
        revoked.subject_id,
        revoked.binding_id,
        revoked.binding_version,
        json.dumps(revoked.to_canonical_dict()),
    )
    await connection.execute(
        """
        INSERT INTO consent_snapshot (
            snapshot_id, version, actor_id, subject_id, binding_id,
            binding_version, snapshot_json
        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        """,
        revoked_snapshot.snapshot_id,
        revoked_snapshot.version,
        revoked.actor_id,
        revoked_snapshot.subject_id,
        revoked_snapshot.binding_id,
        revoked_snapshot.binding_version,
        json.dumps(revoked_snapshot.to_canonical_dict()),
    )
    await connection.execute(
        """
        UPDATE consent_evidence_head
        SET current_consent_id = $1,
            current_revision = $2,
            current_hash = $3,
            updated_at = $4
        WHERE actor_id = $5
          AND subject_id = $6
          AND binding_id = $7
          AND binding_version = $8
          AND capability = $9
          AND purpose = $10
        """,
        revoked.consent_id,
        revoked.version,
        revoked.canonical_hash,
        now,
        revoked.actor_id,
        revoked.subject_id,
        revoked.binding_id,
        revoked.binding_version,
        revoked.capability,
        revoked.purpose,
    )
    await connection.execute(
        """
        UPDATE consent_snapshot_head
        SET current_snapshot_id = $1,
            current_revision = $2,
            current_hash = $3,
            updated_at = $4
        WHERE subject_id = $5
          AND binding_id = $6
          AND binding_version = $7
        """,
        revoked_snapshot.snapshot_id,
        revoked_snapshot.version,
        revoked_snapshot.canonical_hash,
        now,
        revoked_snapshot.subject_id,
        revoked_snapshot.binding_id,
        revoked_snapshot.binding_version,
    )


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


@pytest.mark.asyncio
async def test_authorize_action_reuses_same_transaction_consent_discovery(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        evidence, snapshot = await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
        policy=PolicyEngine(receipt_ttl=timedelta(seconds=30)),
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-consent",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-consent",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _current_profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )

    receipt = await service.authorize_action(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile_id=profile.runtime_profile_id,
        capability="voice_clone_use",
        session_epoch=current.session_epoch,
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        data_classification="biometric",
        safety_state="normal",
        now=now + timedelta(seconds=2),
    )
    replayed = await service.authorize_action(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile_id=profile.runtime_profile_id,
        capability="voice_clone_use",
        session_epoch=current.session_epoch,
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        data_classification="biometric",
        safety_state="normal",
        now=now + timedelta(seconds=2),
    )

    assert receipt.effect.value == "allow_with_obligations"
    assert receipt.reason_code == "adult_subject_authorized"
    assert receipt.consent_snapshot_ids == (snapshot.snapshot_id,)
    assert replayed == receipt
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _revoke_self_consent(
            admin,
            evidence=evidence,
            snapshot=snapshot,
            now=now + timedelta(seconds=3),
        )
    finally:
        await admin.close()
    with pytest.raises(PersistentSessionDenied, match="current Consent"):
        await service.authorize_action(
            actor_id="actor-a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="voice_clone_use",
            session_epoch=current.session_epoch,
            generation_id=current.generation_id + 1,
            turn_id=current.turn_id + 1,
            tool_epoch=current.tool_epoch,
            data_classification="biometric",
            safety_state="normal",
            now=now + timedelta(seconds=4),
        )
    with pytest.raises(PersistentSessionDenied, match="receipt is expired"):
        await service.authorize_action(
            actor_id="actor-a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="voice_clone_use",
            session_epoch=current.session_epoch,
            generation_id=current.generation_id + 1,
            turn_id=current.turn_id + 1,
            tool_epoch=current.tool_epoch,
            data_classification="biometric",
            safety_state="normal",
            now=now + timedelta(seconds=40),
        )


@pytest.mark.asyncio
async def test_profile_issue_and_decide_discover_current_consent(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="memory_recall_private",
            purpose="memory_recall",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-consented-memory-recall",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-consented-memory-recall",
            now=now,
            requested_capabilities=("chat", "memory_recall_private"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(milliseconds=100),
            requested_capabilities=("chat", "memory_recall_private"),
        )
    )

    assert "memory_recall_private" in {
        capability.value for capability in profile.capabilities
    }
    decision = await service.decide(
        runtime_profile_id=profile.runtime_profile_id,
        capability="memory_recall_private",
        actor_id="actor-a",
        data_classification="private",
        safety_state="normal",
        now=now + timedelta(seconds=1),
    )
    assert decision.effect.value == "allow_with_obligations"
    assert decision.reason_code == "private_memory_subject_authorized"


@pytest.mark.asyncio
async def test_action_receipt_subject_must_match_current_active_subject(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-subject-receipt-fence",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-subject-receipt-fence",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(milliseconds=100),
            requested_capabilities=("chat",),
        )
    )
    current_profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=1),
    )
    forged_profile = current_profile.model_copy(
        update={"active_subject_id": "actor-b"}
    )
    forged_receipt = _session_fence_receipt(
        receipt_id="receipt-forged-cross-subject",
        profile=forged_profile,
        generation_id=current.generation_id,
        turn_id=current.turn_id,
        tool_epoch=current.tool_epoch,
        issued_at=now + timedelta(seconds=1),
    )

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
        subject_id="actor-a",
    ) as connection:
        await connection.execute(
            "SELECT set_config('app.authenticated_subject', $1, true)",
            "actor-b",
        )
        with pytest.raises(asyncpg.PostgresError) as raised:
            await connection.fetchval(
                "SELECT action_policy_insert_receipt($1::jsonb)",
                json.dumps(forged_receipt.model_dump(mode="json"), default=str),
            )
    assert raised.value.sqlstate in {"SR403", "SR412"}


@pytest.mark.asyncio
async def test_concurrent_authorize_actions_have_one_fence_winner(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        SessionRuntimeConflict,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-concurrent-actions",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-concurrent-actions",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(milliseconds=100),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=1),
    )

    async def attempt(safety_state: str) -> object:
        try:
            return await service.authorize_action(
                actor_id="actor-a",
                session_id=profile.session_id,
                runtime_profile_id=profile.runtime_profile_id,
                capability="voice_clone_use",
                session_epoch=current.session_epoch,
                generation_id=current.generation_id + 1,
                turn_id=current.turn_id + 1,
                tool_epoch=current.tool_epoch,
                data_classification="biometric",
                safety_state=safety_state,
                now=now + timedelta(seconds=1),
            )
        except Exception as exc:  # collected for exact outcome assertions
            return exc

    results = await asyncio.gather(
        attempt("normal"),
        attempt("self_crisis"),
    )
    receipts = [item for item in results if isinstance(item, PolicyReceiptV2)]
    conflicts = [item for item in results if isinstance(item, SessionRuntimeConflict)]

    assert len(receipts) == 1, repr(results)
    assert len(conflicts) == 1, repr(results)


@pytest.mark.asyncio
async def test_authorize_action_replays_terminal_deny_after_consent_is_added(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-deny-replay",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-deny-replay",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _current_profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    request = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": "voice_clone_use",
        "session_epoch": current.session_epoch,
        "generation_id": current.generation_id + 1,
        "turn_id": current.turn_id + 1,
        "tool_epoch": current.tool_epoch,
        "data_classification": "biometric",
        "safety_state": "normal",
        "now": now + timedelta(seconds=2),
    }

    with pytest.raises(PersistentSessionDenied, match="subject_consent_required"):
        await service.authorize_action(**request)  # type: ignore[arg-type]

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    with pytest.raises(PersistentSessionDenied, match="subject_consent_required"):
        await service.authorize_action(**request)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authorize_action_accepts_one_step_turn_interrupt_and_tool_fences(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-fence-kinds",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-fence-kinds",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    common = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": "voice_clone_use",
        "session_epoch": current.session_epoch,
        "data_classification": "biometric",
        "safety_state": "normal",
    }

    turn = await service.authorize_action(
        **common,  # type: ignore[arg-type]
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        now=now + timedelta(seconds=2),
    )
    interrupt = await service.authorize_action(
        **common,  # type: ignore[arg-type]
        generation_id=current.generation_id + 2,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        now=now + timedelta(seconds=3),
    )
    tool = await service.authorize_action(
        **common,  # type: ignore[arg-type]
        generation_id=current.generation_id + 3,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch + 1,
        now=now + timedelta(seconds=4),
    )
    _latest_profile, latest = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=5),
    )

    assert {turn.effect.value, interrupt.effect.value, tool.effect.value} == {
        "allow_with_obligations"
    }
    assert (latest.generation_id, latest.turn_id, latest.tool_epoch) == (
        current.generation_id + 3,
        current.turn_id + 1,
        current.tool_epoch + 1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_status", ["revoked", "expired"])
async def test_authorize_action_denies_non_effective_current_consent(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
    evidence_status: str,
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    valid_from = now - timedelta(hours=2)
    valid_until = (
        now - timedelta(hours=1)
        if evidence_status == "expired"
        else now + timedelta(hours=1)
    )
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
            evidence_status=evidence_status,
            evidence_valid_from=valid_from,
            evidence_valid_until=valid_until,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id=f"session-action-{evidence_status}-consent",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key=f"start-action-{evidence_status}-consent",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )

    with pytest.raises(PersistentSessionDenied, match="subject_consent_required"):
        await service.authorize_action(
            actor_id="actor-a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="voice_clone_use",
            session_epoch=current.session_epoch,
            generation_id=current.generation_id + 1,
            turn_id=current.turn_id + 1,
            tool_epoch=current.tool_epoch,
            data_classification="biometric",
            safety_state="normal",
            now=now + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_authorize_action_rejects_resource_scoped_capability_without_resource(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )

    with pytest.raises(
        PersistentSessionDenied,
        match="resource-scoped action requires explicit resource identity",
    ):
        await service.authorize_action(
            actor_id="actor-a",
            session_id="session-resource-missing",
            runtime_profile_id="profile-resource-missing",
            capability="memory_promotion",
            session_epoch=1,
            generation_id=1,
            turn_id=1,
            tool_epoch=0,
            data_classification="private",
            safety_state="normal",
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_authorize_action_rolls_back_fence_when_terminal_authority_fails(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionUnavailable,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-rollback",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-rollback",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    request = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": "voice_clone_use",
        "session_epoch": current.session_epoch,
        "generation_id": current.generation_id + 1,
        "turn_id": current.turn_id + 1,
        "tool_epoch": current.tool_epoch,
        "data_classification": "biometric",
        "safety_state": "normal",
        "now": now + timedelta(seconds=2),
    }
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "session_runtime_action_receipt_authority(text, jsonb) "
            "FROM memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(PersistentSessionUnavailable, match="permission denied"):
        await service.authorize_action(**request)  # type: ignore[arg-type]

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "session_runtime_action_receipt_authority(text, jsonb) "
            "TO memoria_action_executor"
        )
    finally:
        await admin.close()

    receipt = await service.authorize_action(**request)  # type: ignore[arg-type]

    assert receipt.effect.value == "allow_with_obligations"


@pytest.mark.asyncio
async def test_sensitive_write_discovers_consent_on_action_executor_transaction(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.policy.production_wiring import (
        PostgresCurrentConsentAuthorityAdapter,
        ProductionAuthorityAdapters,
        build_sensitive_write_service,
    )
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        _ActionExecutorReceiptRepository,
        _PostgresIdentityAuthority,
        _PostgresPrincipalAuthority,
        build_postgres_session_runtime_service,
    )

    class _ExactActionAuthority:
        async def lock_current(
            self,
            _connection: asyncpg.Connection,
            _receipt: PolicyReceiptV2,
            request: object,
        ) -> object:
            return request.context.action_resource_fence  # type: ignore[attr-defined]

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        _evidence, snapshot = await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="memory_capture",
            purpose="memory_capture",
            now=now,
        )
    finally:
        await admin.close()

    runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await runtime_service.start(
        StartPersistentSessionCommand(
            session_id="session-sensitive-write",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-sensitive-write",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await runtime_service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await runtime_service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )

    identity = _PostgresIdentityAuthority()
    consent = PostgresCurrentConsentAuthorityAdapter()
    service = build_sensitive_write_service(
        ProductionAuthorityAdapters(
            principal=_PostgresPrincipalAuthority(),
            consent=consent,
            binding=identity,
            action=_ExactActionAuthority(),  # type: ignore[arg-type]
        ),
        repository=_ActionExecutorReceiptRepository(),  # type: ignore[arg-type]
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
        subject_id="actor-a",
    ) as connection:
        binding = await identity.lock_binding(
            connection,
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            now=now,
        )
        action_fence = build_action_resource_fence(
            capability="memory_capture",
            purpose="memory_capture",
            action_resource_id="capture:actor-a:test",
            action_revision=1,
            consent_snapshot_id=snapshot.snapshot_id,
            consent_snapshot_revision=snapshot.version,
            consent_snapshot_hash=snapshot.canonical_hash,
            generation_id=current.generation_id,
            turn_id=current.turn_id,
            tool_epoch=current.tool_epoch,
            issued_at=now + timedelta(seconds=2),
            valid_until=now + timedelta(minutes=5),
        )
        context = PolicyContext(
            actor_id="actor-a",
            subject_id="actor-a",
            resource_owner_id="actor-a",
            device_id="device-a",
            capability="memory_capture",
            purpose="memory_capture",
            declared_device_mode="self_use",
            current_session_mode="adult_companion",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            speaker_confidence=1.0,
            device_trust="verified",
            safety_state="normal",
            jurisdiction="CN",
            data_classification="private",
            binding_id="binding-a",
            binding_version=1,
            session_id=profile.session_id,
            session_epoch=profile.session_epoch,
            runtime_profile_id=profile.runtime_profile_id,
            subject_revision=profile.subject_revision,
            evaluated_at=now + timedelta(seconds=2),
            idempotency_key="capture-sensitive-write",
            binding_evidence=binding.evidence,
            generation_id=current.generation_id,
            turn_id=current.turn_id,
            tool_epoch=current.tool_epoch,
            action_resource_fence=action_fence,
        )

        async def accepted(
            _connection: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> PolicyReceiptV2:
            return receipt

        receipt = await service.execute(connection, context, accepted)

    assert receipt.effect.value == "allow_with_obligations"
    assert receipt.consent_snapshot_ids == (snapshot.snapshot_id,)


@pytest.mark.asyncio
async def test_postgres_initial_profile_roundtrip_is_generated_and_current(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-a-request").hexdigest(),
            idempotency_key="start-session-a",
        )
    async with store.read_transaction(actor_id="actor-a") as connection:
        loaded = await store.current_profile(connection, session_id="session-a")

    assert isinstance(loaded, RuntimeProfileSignedV2)
    assert loaded == profile


@pytest.mark.asyncio
async def test_postgres_force_rls_hides_other_binding_session(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    for suffix in ("a", "b"):
        profile = _signed_unknown_profile(
            session_id=f"session-{suffix}",
            actor_id=f"actor-{suffix}",
            device_id=f"device-{suffix}",
            binding_id=f"binding-{suffix}",
            profile_id=f"rp-session-{suffix}-1",
            issued_at=now,
        )
        async with store.action_transaction(
            actor_id=f"actor-{suffix}",
            device_id=f"device-{suffix}",
        ) as connection:
            await store.persist_initial(
                connection,
                context=SessionRuntimeContext.from_profile(
                    profile,
                    profile_revision=1,
                ),
                profile=profile,
                event=_profile_event(profile),
                request_hash=hashlib.sha256(
                    f"session-{suffix}-request".encode()
                ).hexdigest(),
                idempotency_key=f"start-session-{suffix}",
            )

    async with store.read_transaction(actor_id="actor-a") as connection:
        own = await store.current_profile(connection, session_id="session-a")
        other = await store.current_profile(connection, session_id="session-b")

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        forced = {
            str(row["relname"])
            for row in await admin.fetch(
                """
                SELECT relname FROM pg_class
                WHERE relname LIKE 'session_runtime_%'
                  AND relkind = 'r'
                  AND relforcerowsecurity
                """
            )
        }
        role = await admin.fetchrow(
            """
            SELECT rolsuper, rolbypassrls FROM pg_roles
            WHERE rolname = 'memoria_session_api'
            """
        )
    finally:
        await admin.close()

    assert own is not None
    assert other is None
    assert forced == {
        "session_runtime_contexts",
        "session_runtime_events",
        "session_runtime_idempotency",
        "session_runtime_outbox",
        "session_runtime_profile_receipts",
        "session_runtime_profiles",
        "session_runtime_tool_effect_intents",
        "session_runtime_tool_effect_outbox",
    }
    assert role is not None
    assert (role["rolsuper"], role["rolbypassrls"]) == (False, False)


@pytest.mark.asyncio
async def test_postgres_readiness_reports_routing_rls_policy_and_exec_state(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime

    assert await store.readiness() == {
        "session_runtime_schema": "ready",
        "session_runtime_rls": "ready",
        "session_runtime_policies": "ready",
        "session_runtime_read_role": "ready",
        "action_executor_role": "ready",
        "action_executor_exec": "ready",
        "action_executor_consent_discover_exec": "unavailable",
    }


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_force_rls(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "ALTER TABLE session_runtime_profiles NO FORCE ROW LEVEL SECURITY"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="RLS"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_required_policy(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "DROP POLICY session_runtime_api_profiles ON session_runtime_profiles"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="policies"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_action_executor_execute(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION action_policy_insert_receipt(jsonb) "
            "FROM memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_action_executor_table_privilege(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "GRANT SELECT ON TABLE identity_device_binding_roles "
            "TO memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="table privileges"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_concurrent_double_switch_has_one_cas_winner(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    initial = _signed_unknown_profile(
        session_id="session-cas",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-cas-1",
        issued_at=now,
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=SessionRuntimeContext.from_profile(
                initial,
                profile_revision=1,
            ),
            profile=initial,
            event=_profile_event(initial),
            request_hash=hashlib.sha256(b"session-cas-request").hexdigest(),
            idempotency_key="start-session-cas",
        )

    candidates = tuple(
        _next_confirmed_profile(
            initial,
            profile_id=f"rp-session-cas-{suffix}",
            issued_at=now + timedelta(seconds=1),
        )
        for suffix in ("switch-a", "switch-b")
    )

    async def attempt(profile: RuntimeProfileSignedV2) -> RuntimeProfileSignedV2:
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
            subject_id=profile.active_subject_id,
        ) as connection:
            return await store.rotate_profile(
                connection,
                expected_runtime_profile_id=initial.runtime_profile_id,
                expected_profile_revision=1,
                expected_session_epoch=1,
                profile=profile,
                event=_profile_event(
                    profile,
                    event_type="subject_switched",
                    event_sequence=2,
                    generation_id=1,
                    turn_id=1,
                    tool_epoch=1,
                ),
            )

    results = await asyncio.gather(
        *(attempt(profile) for profile in candidates),
        return_exceptions=True,
    )
    winners = [item for item in results if isinstance(item, RuntimeProfileSignedV2)]
    conflicts = [item for item in results if isinstance(item, SessionRuntimeConflict)]

    async with store.read_transaction(actor_id="actor-a") as connection:
        current = await store.current_profile(connection, session_id="session-cas")

    assert len(winners) == 1, repr(results)
    assert len(conflicts) == 1, repr(results)
    assert current == winners[0]
    assert current is not None
    assert current.session_epoch == 2


@pytest.mark.asyncio
async def test_real_create_session_route_persists_signed_v2_receipts_and_outbox(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient
    from services.control_api.app.main import create_app
    from services.session_runtime.service import (
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET",
        "test-auth-material-that-is-long-enough",
    )
    monkeypatch.setenv(
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
        _SIGNING_KEY.decode(),
    )
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "session-pg-route-test")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        identity = await client.post("/v1/auth/anonymous")
        assert identity.status_code == 200
        actor_id = str(identity.json()["user_id"])
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await _seed_binding(
                admin,
                actor_id=actor_id,
                device_id="device-route",
                binding_id="binding-route",
            )
        finally:
            await admin.close()
        response = await client.post(
            "/v1/sessions",
            headers={
                "Authorization": f"Bearer {identity.json()['access_token']}",
                "Idempotency-Key": "route-session-start-1",
            },
            json={
                "client": {
                    "platform": "web",
                    "timezone": "Asia/Shanghai",
                    "device_id": "device-route",
                    "binding_version": 1,
                }
            },
        )

    assert response.status_code == 200, response.text
    wire = response.json()["runtime_profile"]
    signed = RuntimeProfileSignedV2.model_validate(wire)
    assert signed.signature_schema == "runtime-profile-v2"
    assert signed.actor_id == actor_id
    assert signed.active_subject_id is None
    assert signed.service_mode.value == "unknown_safe"
    assert signed.session_epoch == 1
    assert signed.policy_receipt_ids

    async with store.read_transaction(actor_id=actor_id) as connection:
        current = await store.current_profile(
            connection,
            session_id=signed.session_id,
        )
    assert current == signed

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        receipt_count = await admin.fetchval(
            "SELECT count(*) FROM policy_receipts_v2 WHERE session_id = $1",
            signed.session_id,
        )
        outbox_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_outbox WHERE session_id = $1",
            signed.session_id,
        )
    finally:
        await admin.close()
    assert receipt_count >= len(signed.policy_receipt_ids)
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_postgres_start_atomically_degrades_incomplete_subject_facts(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        build_postgres_session_runtime_service,
    )
    from services.session_runtime.subject_resolver import SubjectCandidate

    store, bootstrap_dsn = postgres_runtime
    actor_id = "actor-start-facts-unverified"
    device_id = "device-start-facts-unverified"
    binding_id = "binding-start-facts-unverified"
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(
            admin,
            actor_id=actor_id,
            device_id=device_id,
            binding_id=binding_id,
        )
        await admin.execute(
            """
            UPDATE identity_persons
            SET subject_category = 'unknown', age_band = 'unknown',
                age_evidence_status = 'unverified', updated_at = $2
            WHERE person_id = $1
            """,
            actor_id,
            datetime.now(UTC),
        )
    finally:
        await admin.close()

    observed_action_subjects: list[str | None] = []
    set_action_subject = store.set_action_subject

    async def capture_action_subject(
        connection: asyncpg.Connection,
        subject_id: str | None,
    ) -> None:
        observed_action_subjects.append(subject_id)
        await set_action_subject(connection, subject_id)

    monkeypatch.setattr(store, "set_action_subject", capture_action_subject)
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    profile = await service.start(
        StartPersistentSessionCommand(
            session_id="session-start-facts-unverified",
            actor_id=actor_id,
            device_id=device_id,
            expected_binding_version=1,
            idempotency_key="start-facts-unverified",
            now=now,
            requested_capabilities=("chat",),
            candidates=(SubjectCandidate(subject_id=actor_id, confidence=0.99),),
        )
    )

    assert observed_action_subjects == [None]
    assert profile.active_subject_id is None
    assert profile.subject_revision == 0
    assert profile.subject_category.value == "unknown"
    assert profile.age_band.value == "unknown"
    assert profile.speaker_state.value == "unconfirmed"
    assert profile.speaker_confidence is None
    assert profile.service_mode.value == "unknown_safe"

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_row = await admin.fetchrow(
            "SELECT event_type, payload_json FROM session_runtime_events "
            "WHERE session_id = $1 ORDER BY event_sequence DESC LIMIT 1",
            profile.session_id,
        )
    finally:
        await admin.close()
    assert event_row is not None
    assert event_row["event_type"] == "subject_resolved"
    event_payload = event_row["payload_json"]
    if isinstance(event_payload, str):
        event_payload = json.loads(event_payload)
    assert event_payload["payload"]["reason_code"] == "subject_facts_unverified"


@pytest.mark.asyncio
async def test_postgres_switch_atomically_degrades_incomplete_subject_facts(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    actor_id = "actor-switch-facts-unverified"
    device_id = "device-switch-facts-unverified"
    binding_id = "binding-switch-facts-unverified"
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(
            admin,
            actor_id=actor_id,
            device_id=device_id,
            binding_id=binding_id,
        )
        await admin.execute(
            """
            UPDATE identity_persons
            SET subject_category = 'unknown', age_band = 'unknown',
                age_evidence_status = 'unverified', updated_at = $2
            WHERE person_id = $1
            """,
            actor_id,
            datetime.now(UTC),
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-switch-facts-unverified",
            actor_id=actor_id,
            device_id=device_id,
            expected_binding_version=1,
            idempotency_key="start-before-facts-switch",
            now=now,
            requested_capabilities=("chat",),
        )
    )

    observed_action_subjects: list[str | None] = []
    set_action_subject = store.set_action_subject

    async def capture_action_subject(
        connection: asyncpg.Connection,
        subject_id: str | None,
    ) -> None:
        observed_action_subjects.append(subject_id)
        await set_action_subject(connection, subject_id)

    monkeypatch.setattr(store, "set_action_subject", capture_action_subject)
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id=actor_id,
            subject_id=actor_id,
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )

    assert observed_action_subjects == [None]
    assert profile.active_subject_id is None
    assert profile.subject_revision == 0
    assert profile.subject_category.value == "unknown"
    assert profile.age_band.value == "unknown"
    assert profile.speaker_state.value == "unconfirmed"
    assert profile.speaker_confidence is None
    assert profile.service_mode.value == "unknown_safe"
    assert profile.session_epoch == initial.session_epoch + 1

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_row = await admin.fetchrow(
            "SELECT event_type, payload_json FROM session_runtime_events "
            "WHERE session_id = $1 ORDER BY event_sequence DESC LIMIT 1",
            profile.session_id,
        )
    finally:
        await admin.close()
    assert event_row is not None
    assert event_row["event_type"] == "epoch_bumped"
    event_payload = event_row["payload_json"]
    if isinstance(event_payload, str):
        event_payload = json.loads(event_payload)
    assert event_payload["payload"]["reason_code"] == "subject_facts_unverified"


@pytest.mark.asyncio
async def test_parent_for_child_primary_snapshot_never_defaults_to_account_owner(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="guardian-owner",
            subject_id="child-primary",
            device_id="device-parent-child",
            binding_id="binding-parent-child",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )

    snapshot = await service.binding_snapshot(
        actor_id="guardian-owner",
        device_id="device-parent-child",
        expected_binding_version=1,
        now=datetime.now(UTC),
    )

    assert snapshot.primary_subject_ids == ("child-primary",)
    assert set(snapshot.member_subject_ids) == {"guardian-owner", "child-primary"}


@pytest.mark.asyncio
async def test_postgres_device_admin_switch_keeps_actor_separate_from_parent_subject(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="device-admin-child",
            subject_id="parent-subject",
            device_id="device-parent-care",
            binding_id="binding-parent-care",
            declared_mode="child_for_parent",
            subject_category="adult",
            age_band="adult",
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-parent-care",
            actor_id="device-admin-child",
            device_id="device-parent-care",
            expected_binding_version=1,
            idempotency_key="start-parent-care",
            now=now,
            requested_capabilities=("chat", "english_practice"),
        )
    )
    switched = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="device-admin-child",
            subject_id="parent-subject",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )

    assert switched.actor_id == "device-admin-child"
    assert switched.active_subject_id == "parent-subject"
    assert switched.session_epoch == initial.session_epoch + 1
    assert "chat" in {item.value for item in switched.capabilities}
    with pytest.raises(PersistentSessionDenied, match="stale"):
        await service.decide(
            runtime_profile_id=initial.runtime_profile_id,
            capability="chat",
            actor_id="device-admin-child",
            data_classification="ephemeral",
            safety_state="normal",
            now=now + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_postgres_minor_without_consent_gets_only_audited_denies(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="guardian-no-consent",
            subject_id="minor-no-consent",
            device_id="device-minor-no-consent",
            binding_id="binding-minor-no-consent",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-minor-no-consent",
            actor_id="guardian-no-consent",
            device_id="device-minor-no-consent",
            expected_binding_version=1,
            idempotency_key="start-minor-no-consent",
            now=now,
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )
    switched = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="guardian-no-consent",
            subject_id="minor-no-consent",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )

    assert switched.active_subject_id == "minor-no-consent"
    assert switched.capabilities == ()
    assert switched.policy_receipt_ids == ()
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        effects = await admin.fetch(
            """
            SELECT effect, reason_code FROM policy_receipts_v2
            WHERE runtime_profile_id = $1 ORDER BY capability
            """,
            switched.runtime_profile_id,
        )
    finally:
        await admin.close()
    assert len(effects) == 3
    assert {str(item["effect"]) for item in effects} == {"deny"}


@pytest.mark.asyncio
async def test_real_pg_control_routes_share_start_current_resolve_and_switch_authority(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient
    from services.control_api.app.main import create_app
    from services.control_api.app.multi_subject_runtime import (
        PostgresMultiSubjectRuntimeControl,
    )
    from services.identity.domain import BindingManifest, ManifestRole, PersonSubject
    from services.session_runtime.service import (
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "control-pg.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET",
        "test-auth-material-that-is-long-enough",
    )
    monkeypatch.setenv(
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
        _SIGNING_KEY.decode(),
    )
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "session-pg-control-route-test")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    app.state.session_runtime_service = service

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        identity_response = await client.post("/v1/auth/anonymous")
        stranger_response = await client.post("/v1/auth/anonymous")
        actor_id = str(identity_response.json()["user_id"])
        parent_id = "parent-route-subject"
        now = datetime.now(UTC)
        manifest = BindingManifest(
            binding_id="binding-control-route",
            device_id="device-control-route",
            declared_mode="child_for_parent",
            binding_version=1,
            status="active",
            reason="create",
            supersedes_binding_id=None,
            family_space_id="family-control-route",
            account_owner_id=actor_id,
            device_admin_ids=(actor_id,),
            primary_subject_ids=(parent_id,),
            guardian_ids=(),
            delegate_ids=(),
            emergency_contact_ids=(),
            member_ids=(parent_id,),
            roles=(
                ManifestRole(actor_id, "account_owner", ("binding.manage",)),
                ManifestRole(actor_id, "device_admin", ("binding.manage",)),
                ManifestRole(parent_id, "primary_subject", ("content.read",)),
                ManifestRole(parent_id, "member", ("family.shared.read",)),
            ),
            service_profile_version="delegated-v1",
            policy_bundle_version="multi-subject-v2",
            consent_snapshot_id=None,
            persona_assignment_id="starlight:v1",
            valid_from=now,
            valid_until=None,
            created_at=now,
        )
        people = {
            actor_id: PersonSubject(
                actor_id,
                "设备管理员",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                created_at=now,
                updated_at=now,
            ),
            parent_id: PersonSubject(
                parent_id,
                "父母",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                created_at=now,
                updated_at=now,
            ),
        }

        class IdentityView:
            async def get_active_manifest(
                self,
                device_id: str,
                _now: datetime | None = None,
            ) -> BindingManifest | None:
                return manifest if device_id == manifest.device_id else None

            async def get_person(self, person_id: str) -> PersonSubject:
                return people[person_id]

        app.state.multi_subject_runtime = PostgresMultiSubjectRuntimeControl(
            identity=IdentityView(),  # type: ignore[arg-type]
            sessions=service,
        )
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await _seed_delegated_binding(
                admin,
                actor_id=actor_id,
                subject_id=parent_id,
                device_id=manifest.device_id,
                binding_id=manifest.binding_id,
                declared_mode="child_for_parent",
                subject_category="adult",
                age_band="adult",
            )
            # The only subject on this binding carries a persona of their own;
            # the binding default stays starlight:v1.
            await admin.execute(
                """
                INSERT INTO identity_persona_assignments (
                    binding_id, subject_id, assignment_id, persona_id,
                    persona_version, created_at, updated_at
                ) VALUES ($1, $2, 'taoxi:v1', 'taoxi', 1, $3, $3)
                """,
                manifest.binding_id,
                parent_id,
                now,
            )
        finally:
            await admin.close()

        headers = {
            "Authorization": f"Bearer {identity_response.json()['access_token']}",
            "Idempotency-Key": "control-route-start",
        }
        started = await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "client": {
                    "platform": "web",
                    "timezone": "Asia/Shanghai",
                    "device_id": manifest.device_id,
                    "binding_version": 1,
                }
            },
        )
        assert started.status_code == 200, started.text
        initial = RuntimeProfileSignedV2.model_validate(
            started.json()["runtime_profile"]
        )
        current = await client.get(
            f"/v1/devices/{manifest.device_id}/runtime-profile",
            headers=headers,
            params={"session_id": initial.session_id},
        )
        assert current.status_code == 200, current.text
        assert current.json()["runtime_profile_id"] == initial.runtime_profile_id
        # No active subject yet: the binding default applies.
        assert initial.persona_assignment_id == "starlight:v1"

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": manifest.device_id,
                "session_id": initial.session_id,
                "client_claimed_person_id": parent_id,
                "environment": {},
            },
        )
        assert resolution.status_code == 200, resolution.text
        assert resolution.json()["resolution"] == "confirmation_required"
        switched = await client.post(
            f"/v1/sessions/{initial.session_id}/active-subject",
            headers=headers,
            json={
                "person_id": parent_id,
                "confirmation_method": "app_confirm",
            },
        )
        assert switched.status_code == 200, switched.text
        switched_profile = RuntimeProfileSignedV2.model_validate(switched.json())
        assert switched_profile.actor_id == actor_id
        assert switched_profile.active_subject_id == parent_id
        assert switched_profile.session_epoch == initial.session_epoch + 1
        # The subject carries their own persona; switching to them switches it.
        assert switched_profile.persona_assignment_id == "taoxi:v1"
        assert switched_profile.persona.persona_id == "taoxi"

        stale = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": initial.runtime_profile_id,
                "capability": "chat",
            },
        )
        assert stale.status_code == 403
        stranger = await client.get(
            f"/v1/devices/{manifest.device_id}/runtime-profile",
            headers={
                "Authorization": (
                    f"Bearer {stranger_response.json()['access_token']}"
                )
            },
            params={"session_id": initial.session_id},
        )
        assert stranger.status_code == 403


@pytest.mark.asyncio
async def test_postgres_action_fence_advances_turn_interrupt_and_tool_monotonically(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-advance",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-advance-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-advance").hexdigest(),
            idempotency_key="start-session-fence-advance",
        )
        with pytest.raises(ValueError, match="monotonic"):
            await store.advance_action_fence(
                connection,
                context=context,
                fence_kind="turn",
                next_generation_id=0,
                next_turn_id=0,
                next_tool_epoch=0,
                occurred_at=now + timedelta(seconds=1),
            )
        turned = await store.advance_action_fence(
            connection,
            context=context,
            fence_kind="turn",
            next_generation_id=1,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        )
        interrupted = await store.advance_action_fence(
            connection,
            context=turned,
            fence_kind="interrupt",
            next_generation_id=2,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=2),
        )
        tooled = await store.advance_action_fence(
            connection,
            context=interrupted,
            fence_kind="tool",
            next_generation_id=3,
            next_turn_id=1,
            next_tool_epoch=1,
            occurred_at=now + timedelta(seconds=3),
        )

    assert (
        turned.generation_id,
        turned.turn_id,
        turned.tool_epoch,
    ) == (1, 1, 0)
    assert (
        interrupted.generation_id,
        interrupted.turn_id,
        interrupted.tool_epoch,
    ) == (2, 1, 0)
    assert (tooled.generation_id, tooled.turn_id, tooled.tool_epoch) == (3, 1, 1)
    assert tooled.updated_at == now + timedelta(seconds=3)

    async with store.read_transaction(actor_id="actor-a") as connection:
        current = await store.current_context(
            connection,
            session_id="session-fence-advance",
        )
    assert current is not None
    assert (
        current.generation_id,
        current.turn_id,
        current.tool_epoch,
    ) == (3, 1, 1)
    assert current.updated_at == now + timedelta(seconds=3)


@pytest.mark.asyncio
async def test_postgres_action_fence_rejects_illegal_transitions_at_database_boundary(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-illegal",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-illegal-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-illegal").hexdigest(),
            idempotency_key="start-session-fence-illegal",
        )

    conflicting_advances = (
        ("turn", 1, 2, 0),  # turn skips a step
        ("turn", 0, 0, 0),  # no-op replay is not an advance
        ("turn", 1, 1, 1),  # turn mixed with a tool bump
        ("interrupt", 2, 1, 0),  # generation skips a step
        ("interrupt", 1, 1, 0),  # interrupt mixed with a turn bump
        ("tool", 1, 0, 2),  # tool epoch skips a step
    )
    for fence_kind, generation_id, turn_id, tool_epoch in conflicting_advances:
        sqlstate = await _advance_fence_raw(
            store,
            _action_fence_payload(
                context,
                fence_kind=fence_kind,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
                occurred_at=now + timedelta(seconds=1),
            ),
        )
        assert sqlstate == "SR409", (fence_kind, generation_id, turn_id, tool_epoch)

    unknown_kind = await _advance_fence_raw(
        store,
        _action_fence_payload(
            context,
            fence_kind="chat",
            generation_id=1,
            turn_id=1,
            tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        ),
    )
    assert unknown_kind == "SR400"

    async with store.read_transaction(actor_id="actor-a") as connection:
        current = await store.current_context(
            connection,
            session_id="session-fence-illegal",
        )
    assert current is not None
    assert (
        current.generation_id,
        current.turn_id,
        current.tool_epoch,
    ) == (0, 0, 0)


@pytest.mark.asyncio
async def test_postgres_action_fence_rejects_stale_or_foreign_context(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-stale",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-stale-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-stale").hexdigest(),
            idempotency_key="start-session-fence-stale",
        )

    stale_epoch = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=99,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    foreign_profile = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id="rp-not-current",
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    unknown_session = SessionRuntimeContext(
        session_id="session-none",
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    foreign_actor = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id="actor-b",
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    foreign_binding = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id="binding-b",
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    cases = (
        (stale_epoch, "actor-a", "device-a", "SR412"),
        (foreign_profile, "actor-a", "device-a", "SR412"),
        (unknown_session, "actor-a", "device-a", "SR412"),
        (foreign_actor, "actor-a", "device-a", "SR403"),
        (foreign_binding, "actor-a", "device-a", "SR403"),
    )
    for claimed, actor_id, device_id, expected in cases:
        sqlstate = await _advance_fence_raw(
            store,
            _action_fence_payload(
                claimed,
                fence_kind="turn",
                generation_id=1,
                turn_id=1,
                tool_epoch=0,
                occurred_at=now + timedelta(seconds=1),
            ),
            actor_id=actor_id,
            device_id=device_id,
        )
        assert sqlstate == expected, (claimed.session_id, actor_id)

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        advanced = await store.advance_action_fence(
            connection,
            context=context,
            fence_kind="turn",
            next_generation_id=1,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        )
    assert (advanced.generation_id, advanced.turn_id, advanced.tool_epoch) == (
        1,
        1,
        0,
    )


@pytest.mark.asyncio
async def test_postgres_action_receipt_authority_locks_exact_fence_and_replays_idempotently(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-receipt",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-receipt-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-receipt").hexdigest(),
            idempotency_key="start-session-fence-receipt",
        )

    receipt = _session_fence_receipt(
        receipt_id="receipt-fence-1",
        profile=profile,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        issued_at=now,
    )
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        inserted = await admin.execute(
            """
            INSERT INTO policy_receipts_v2
            SELECT (jsonb_populate_record(NULL::policy_receipts_v2, $1::jsonb)).*
            """,
            json.dumps(receipt.model_dump(mode="json")),
        )
    finally:
        await admin.close()
    assert inserted == "INSERT 0 1"

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        locked = await store.lock_receipt_authority(
            connection,
            receipt=receipt,
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
        )
        replayed = await store.lock_receipt_authority(
            connection,
            receipt=receipt,
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
        )
        missing = await store.lock_receipt_authority(
            connection,
            receipt=receipt.model_copy(
                update={"receipt_id": "receipt-fence-missing"}
            ),
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
        )
        assert locked == receipt
        assert replayed == receipt
        assert missing is None

        advanced = await store.advance_action_fence(
            connection,
            context=context,
            fence_kind="turn",
            next_generation_id=1,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        )
        assert (advanced.generation_id, advanced.turn_id, advanced.tool_epoch) == (
            1,
            1,
            0,
        )

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(SessionRuntimeConflict, match="not current"):
            await store.lock_receipt_authority(
                connection,
                receipt=receipt,
                generation_id=0,
                turn_id=0,
                tool_epoch=0,
            )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(SessionRuntimeConflict, match="mismatch"):
            await store.lock_receipt_authority(
                connection,
                receipt=receipt,
                generation_id=1,
                turn_id=1,
                tool_epoch=0,
            )


@pytest.mark.asyncio
async def test_postgres_action_fence_ports_are_security_definer_fixed_path_and_executor_only(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        rows = await admin.fetch(
            """
            SELECT p.proname,
                   p.prosecdef,
                   pg_get_userbyid(p.proowner) AS owner,
                   p.proconfig,
                   has_function_privilege(
                       'memoria_action_executor', p.oid, 'EXECUTE'
                   ) AS executor_exec,
                   has_function_privilege('public', p.oid, 'EXECUTE')
                       AS public_exec
            FROM pg_proc p
            WHERE p.oid IN (
                to_regprocedure(
                    'public.session_runtime_advance_action_fence(jsonb)'),
                to_regprocedure(
                    'public.session_runtime_action_receipt_authority(text,jsonb)')
            )
            ORDER BY p.proname
            """
        )
        helpers = await admin.fetch(
            """
            SELECT p.proname,
                   has_function_privilege('public', p.oid, 'EXECUTE')
                       AS public_exec
            FROM pg_proc p
            WHERE p.oid IN (
                to_regprocedure(
                    'public.session_runtime_fence_json_valid(jsonb,text[])'),
                to_regprocedure(
                    'public.session_runtime_fence_text_valid('
                    'jsonb,integer,integer)'),
                to_regprocedure(
                    'public.session_runtime_fence_int_valid(jsonb,integer)')
            )
            ORDER BY p.proname
            """
        )
    finally:
        await admin.close()

    assert {str(row["proname"]) for row in rows} == {
        "session_runtime_action_receipt_authority",
        "session_runtime_advance_action_fence",
    }
    for row in rows:
        assert row["prosecdef"] is True
        assert str(row["owner"]) == "memoria_session_owner"
        config = [str(item) for item in row["proconfig"] or []]
        assert "search_path=pg_catalog, public" in config
        assert "row_security=on" in config
        assert row["executor_exec"] is True
        assert row["public_exec"] is False
    assert {str(row["proname"]) for row in helpers} == {
        "session_runtime_fence_int_valid",
        "session_runtime_fence_json_valid",
        "session_runtime_fence_text_valid",
    }
    assert all(row["public_exec"] is False for row in helpers)


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_advance_action_fence_function(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "DROP FUNCTION session_runtime_advance_action_fence(jsonb)"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="schema"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_receipt_authority_execute(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "session_runtime_action_receipt_authority(text, jsonb) "
            "FROM memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_action_executor_cannot_read_fence_tables_directly(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(asyncpg.PostgresError) as contexts_exc:
            await connection.fetchval("SELECT count(*) FROM session_runtime_contexts")
        assert contexts_exc.value.sqlstate == "42501"
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(asyncpg.PostgresError) as receipts_exc:
            await connection.fetchval("SELECT count(*) FROM policy_receipts_v2")
        assert receipts_exc.value.sqlstate == "42501"


@pytest.mark.asyncio
async def test_postgres_consent_discover_action_fence_wiring_is_conditional(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    assert (
        await store.readiness()
    )["action_executor_consent_discover_exec"] == "unavailable"

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            """
            CREATE OR REPLACE FUNCTION consent_discover_action_fence(
                text, text, text, text, text, integer, text, text, timestamptz
            ) RETURNS boolean
            LANGUAGE sql
            AS $$ SELECT true $$
            """
        )
        await admin.execute(
            "REVOKE ALL ON FUNCTION consent_discover_action_fence("
            "text, text, text, text, text, integer, text, text, timestamptz"
            ") FROM PUBLIC"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(SESSION_RUNTIME_SCHEMA_SQL)
    finally:
        await admin.close()
    report = await store.readiness()
    assert report["action_executor_consent_discover_exec"] == "ready"


def test_session_schema_never_grants_executor_consent_mutation_lock_ports() -> None:
    assert "consent_discover_action_fence" in SESSION_RUNTIME_SCHEMA_SQL
    assert not re.search(
        r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+consent_lock_(?:authority|snapshot)_head"
        r"[\s\S]{0,240}?TO\s+memoria_action_executor",
        SESSION_RUNTIME_SCHEMA_SQL,
        re.IGNORECASE,
    )


@pytest.mark.asyncio
async def test_tool_effect_commit_replay_conflict_and_reconcile_are_durable(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        CommitToolEffectCommand,
        PersistentSessionDenied,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
        policy=PolicyEngine(receipt_ttl=timedelta(seconds=30)),
    )
    started = await service.start(
        StartPersistentSessionCommand(
            session_id="session-tool-effect",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-tool-effect",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=started.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _current, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    receipt = await service.authorize_action(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile_id=profile.runtime_profile_id,
        capability="voice_clone_use",
        session_epoch=current.session_epoch,
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        data_classification="biometric",
        safety_state="normal",
        now=now + timedelta(seconds=2),
    )
    payload = {"provider": "test", "voice": "resource"}
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    generation_id = current.generation_id + 1
    turn_id = current.turn_id + 1
    fence = {
        "session_id": profile.session_id,
        "session_epoch": current.session_epoch,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": current.tool_epoch,
    }
    fence_fingerprint = hashlib.sha256(
        json.dumps(
            fence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    command = CommitToolEffectCommand(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile=profile,
        policy_receipt=receipt,
        session_epoch=current.session_epoch,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=current.tool_epoch,
        capability=receipt.capability.value,
        purpose=receipt.purpose.value,
        resource_id=receipt.action_resource_fence.action_resource_id,
        evidence_refs=(receipt.receipt_id,),
        idempotency_key="effect-key-1",
        intent="voice_clone_use",
        payload=payload,
        payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        fence_fingerprint=fence_fingerprint,
        now=now + timedelta(seconds=2),
    )

    committed = await service.commit_tool_effect(command)
    replayed = await service.commit_tool_effect(command)
    assert replayed == committed
    assert committed["idempotency_key"] == "effect-key-1"
    assert committed["intent_sha256"] == command.payload_sha256

    reconciled = await service.reconcile_tool_effect(idempotency_key="effect-key-1")
    assert reconciled["state"] == "committed"
    assert reconciled["receipt"] == committed

    conflict_payload = {"provider": "forged"}
    conflict_bytes = json.dumps(
        conflict_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    conflicting = replace(
        command,
        payload=conflict_payload,
        payload_sha256=hashlib.sha256(conflict_bytes).hexdigest(),
    )
    with pytest.raises(SessionRuntimeConflict, match="idempotency"):
        await service.commit_tool_effect(conflicting)

    def action_fingerprint(generation: int, turn: int) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "session_id": profile.session_id,
                    "turn_id": turn,
                    "generation_id": generation,
                    "tool_epoch": current.tool_epoch,
                    "session_epoch": current.session_epoch,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    stale = replace(
        command,
        generation_id=command.generation_id - 1,
        turn_id=command.turn_id - 1,
        fence_fingerprint=action_fingerprint(
            command.generation_id - 1,
            command.turn_id - 1,
        ),
    )
    with pytest.raises(SessionRuntimeConflict, match="fence"):
        await service.commit_tool_effect(stale)

    expired_receipt = receipt.model_copy(
        update={
            "expires_at": now + timedelta(seconds=1),
        }
    )
    with pytest.raises(PersistentSessionDenied, match="current evidence"):
        await service.commit_tool_effect(
            replace(command, policy_receipt=expired_receipt)
        )

    with pytest.raises(PersistentSessionDenied, match="payload mismatch"):
        await service.commit_tool_effect(replace(command, actor_id="actor-b"))

    assert (
        await service.reconcile_tool_effect(idempotency_key="effect-never-committed")
    ) == {"state": "not_found"}

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        assert await admin.fetchval(
            "SELECT count(*) FROM session_runtime_tool_effect_intents "
            "WHERE idempotency_key = $1",
            "effect-key-1",
        ) == 1
        assert await admin.fetchval(
            "SELECT count(*) FROM session_runtime_tool_effect_outbox "
            "WHERE intent_id = $1",
            committed["intent_id"],
        ) == 1
        worker_password = uuid.uuid4().hex
        await admin.execute(
            f"ALTER ROLE memoria_session_worker PASSWORD '{worker_password}'"
        )
    finally:
        await admin.close()

    worker_dsn = _dsn_with(
        bootstrap_dsn,
        database=urlsplit(bootstrap_dsn).path.lstrip("/"),
        user="memoria_session_worker",
        password=worker_password,
    )
    worker = await asyncpg.connect(worker_dsn)
    try:
        claimed_rows = await worker.fetch(
            "SELECT * FROM session_runtime_tool_effect_outbox_claim($1, $2, $3)",
            "worker-tool-effect",
            10,
            60,
        )
        assert len(claimed_rows) == 1
        claimed = claimed_rows[0][0]
        if isinstance(claimed, str):
            claimed = json.loads(claimed)
        assert isinstance(claimed, dict)
        completed = await worker.fetchval(
            "SELECT session_runtime_tool_effect_outbox_complete($1, $2, $3, $4)",
            claimed["outbox_id"],
            "worker-tool-effect",
            "delivered",
            None,
        )
        if isinstance(completed, str):
            completed = json.loads(completed)
        assert isinstance(completed, dict)
        with pytest.raises(asyncpg.PostgresError) as worker_read:
            await worker.fetchval(
                "SELECT count(*) FROM session_runtime_tool_effect_outbox"
            )
        assert worker_read.value.sqlstate == "42501"
    finally:
        await worker.close()


def _close_event(
    *,
    session_id: str,
    profile: RuntimeProfileSignedV2,
    context: SessionRuntimeContext,
    reason_code: str,
    now: datetime,
) -> SessionEvent:
    return SessionEvent.model_validate(
        {
            "event_id": f"event-close-{uuid.uuid4().hex[:12]}",
            "event_type": "session_closed",
            "session_id": session_id,
            "session_epoch": context.session_epoch + 1,
            "device_id": profile.device_id,
            "binding_id": profile.binding_id,
            "binding_version": profile.binding_version,
            "generation_id": context.generation_id + 1,
            "turn_id": context.turn_id + 1,
            "tool_epoch": context.tool_epoch + 1,
            "event_sequence": context.profile_revision + 1,
            "active_subject_id": None,
            "runtime_profile_id": profile.runtime_profile_id,
            "actor_id": profile.actor_id,
            "subject_revision": 0,
            "occurred_at": now.isoformat(),
            "payload": {
                "reason_code": reason_code,
                "invalidated_fences": [
                    "session",
                    "generation",
                    "tool",
                    "effect",
                    "tts",
                    "memory",
                    "ui",
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_postgres_close_session_applies_terminal_fence_and_outbox(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-close-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-close-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"close-a-request").hexdigest(),
            idempotency_key="start-session-close-a",
        )

    event = _close_event(
        session_id="session-close-a",
        profile=profile,
        context=context,
        reason_code="network",
        now=now + timedelta(seconds=1),
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        status = await store.close_session(
            connection,
            expected=context,
            event=event,
        )
    assert status == "closed"

    async with store.read_transaction(actor_id="actor-a") as connection:
        stored = await store.context_any_state(
            connection,
            session_id="session-close-a",
        )
    assert stored is not None
    closed_context, state = stored
    assert state == "closed"
    assert closed_context.active_subject_id is None
    assert closed_context.session_epoch == 2
    assert closed_context.generation_id == 1
    assert closed_context.turn_id == 1
    assert closed_context.tool_epoch == 1

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_row = await admin.fetchrow(
            "SELECT event_type, event_sequence, session_epoch, payload_json "
            "FROM session_runtime_events WHERE session_id = $1 "
            "ORDER BY event_sequence DESC LIMIT 1",
            "session-close-a",
        )
        outbox_row = await admin.fetchrow(
            "SELECT topic, status FROM session_runtime_outbox WHERE session_id = $1 "
            "AND topic = 'agent.session.closed'",
            "session-close-a",
        )
        event_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_events WHERE session_id = $1",
            "session-close-a",
        )
        outbox_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_outbox WHERE session_id = $1",
            "session-close-a",
        )
    finally:
        await admin.close()
    assert event_row is not None
    assert event_row["event_type"] == "session_closed"
    assert event_row["event_sequence"] == 2
    assert event_row["session_epoch"] == 2
    event_payload = event_row["payload_json"]
    if isinstance(event_payload, str):
        event_payload = json.loads(event_payload)
    assert event_payload["payload"]["reason_code"] == "network"
    assert outbox_row is not None
    assert outbox_row["topic"] == "agent.session.closed"
    assert outbox_row["status"] == "pending"
    assert event_count == 2  # subject_resolved + session_closed
    assert outbox_count == 2

    # Idempotent replay with the observed terminal epoch returns
    # already_closed and writes no second event or outbox row.
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        replayed = await store.close_session(
            connection,
            expected=closed_context,
            event=_close_event(
                session_id="session-close-a",
                profile=profile,
                context=closed_context,
                reason_code="network",
                now=now + timedelta(seconds=2),
            ),
        )
    assert replayed == "already_closed"
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_events WHERE session_id = $1",
            "session-close-a",
        )
        outbox_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_outbox WHERE session_id = $1",
            "session-close-a",
        )
    finally:
        await admin.close()
    assert event_count == 2
    assert outbox_count == 2


@pytest.mark.asyncio
async def test_postgres_close_session_conflicts_after_failure_and_missing(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-close-b",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-close-b-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"close-b-request").hexdigest(),
            idempotency_key="start-session-close-b",
        )
        failed_payload = _profile_event(profile).model_dump(mode="json")
        failed_payload.update(
            {
                "event_id": f"event-fail-{uuid.uuid4().hex[:12]}",
                "event_type": "session_failed",
                "session_epoch": context.session_epoch + 1,
                "generation_id": context.generation_id + 1,
                "turn_id": context.turn_id + 1,
                "tool_epoch": context.tool_epoch + 1,
                "event_sequence": context.profile_revision + 1,
            }
        )
        await store.fail_session(
            connection,
            expected=context,
            event=SessionEvent.model_validate(failed_payload),
        )
    # A failed authority must never relabel to closed. Each rejected function
    # call uses its own transaction because PostgreSQL intentionally aborts a
    # transaction after a SECURITY DEFINER function raises.
    with pytest.raises(SessionRuntimeConflict):
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
        ) as connection:
            await store.close_session(
                connection,
                expected=context,
                event=_close_event(
                    session_id="session-close-b",
                    profile=profile,
                    context=context,
                    reason_code="network",
                    now=now + timedelta(seconds=1),
                ),
            )
    # A stale expected profile/epoch (post-fail row) also conflicts.
    stale = replace(context, session_epoch=2)
    with pytest.raises(SessionRuntimeConflict):
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
        ) as connection:
            await store.close_session(
                connection,
                expected=stale,
                event=_close_event(
                    session_id="session-close-b",
                    profile=profile,
                    context=stale,
                    reason_code="network",
                    now=now + timedelta(seconds=1),
                ),
            )
    missing = replace(context, session_id="session-close-missing")
    with pytest.raises(SessionRuntimeConflict):
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
        ) as connection:
            await store.close_session(
                connection,
                expected=missing,
                event=_close_event(
                    session_id="session-close-missing",
                    profile=profile,
                    context=missing,
                    reason_code="network",
                    now=now + timedelta(seconds=1),
                ),
            )


@pytest.mark.asyncio
async def test_service_close_session_replays_idempotently_and_rejects_unknown(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionNotFound,
        SessionCloseResult,
        build_postgres_session_runtime_service,
    )

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-close-c",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-close-c-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"close-c-request").hexdigest(),
            idempotency_key="start-session-close-c",
        )

    first = await service.close_session(
        actor_id="actor-a",
        session_id="session-close-c",
        reason_code="superseded",
        now=now + timedelta(seconds=1),
    )
    assert first == SessionCloseResult(applied=True, already_closed=False)

    replayed = await service.close_session(
        actor_id="actor-a",
        session_id="session-close-c",
        reason_code="superseded",
        now=now + timedelta(seconds=2),
    )
    assert replayed == SessionCloseResult(applied=False, already_closed=True)

    with pytest.raises(PersistentSessionNotFound):
        await service.close_session(
            actor_id="actor-a",
            session_id="session-close-unknown",
            reason_code="network",
            now=now,
        )


@pytest.mark.asyncio
async def test_service_start_profile_ttl_is_per_session_and_bounded(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        StartPersistentSessionCommand,
        build_postgres_session_runtime_service,
    )

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-ttl-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-ttl-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"ttl-a-request").hexdigest(),
            idempotency_key="start-session-ttl-a",
        )

    direct = await service.close_session(
        actor_id="actor-a",
        session_id="session-ttl-a",
        reason_code="device_close",
        now=now + timedelta(seconds=1),
    )
    assert direct.applied is True

    # Command-level bounds: the direct path may set a positive bounded TTL,
    # everything else keeps the 5-minute service default.
    with pytest.raises(ValueError, match="positive"):
        StartPersistentSessionCommand(
            session_id="s1",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="k1",
            now=now,
            profile_ttl=timedelta(0),
        )
    with pytest.raises(ValueError, match="one-day bound"):
        StartPersistentSessionCommand(
            session_id="s1",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="k1",
            now=now,
            profile_ttl=timedelta(days=2),
        )
    command = StartPersistentSessionCommand(
        session_id="s1",
        actor_id="actor-a",
        device_id="device-a",
        expected_binding_version=1,
        idempotency_key="k1",
        now=now,
        profile_ttl=timedelta(seconds=3600),
    )
    assert command.profile_ttl == timedelta(seconds=3600)
    default = StartPersistentSessionCommand(
        session_id="s2",
        actor_id="actor-a",
        device_id="device-a",
        expected_binding_version=1,
        idempotency_key="k2",
        now=now,
    )
    assert default.profile_ttl is None


@pytest.mark.asyncio
async def test_service_close_session_allows_expired_signed_profile(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """Expiry denies use but must not strand an active Session authority."""

    from services.session_runtime.service import build_postgres_session_runtime_service

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    issued_at = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-expired-close-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-expired-close-a-1",
        issued_at=issued_at,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"expired-close-request").hexdigest(),
            idempotency_key="start-session-expired-close-a",
        )

    result = await service.close_session(
        actor_id="actor-a",
        session_id="session-expired-close-a",
        reason_code="network",
        now=issued_at + timedelta(minutes=10),
    )
    assert result.applied is True


@pytest.mark.asyncio
async def test_postgres_close_session_survives_binding_revocation(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """Revocation must not strand the already-issued terminal authority."""

    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-revoked-close-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-revoked-close-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"revoked-close-request").hexdigest(),
            idempotency_key="start-session-revoked-close-a",
        )

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "UPDATE identity_device_bindings SET status = 'revoked', "
            "valid_until = $1 WHERE binding_id = 'binding-a'",
            now + timedelta(milliseconds=1),
        )
    finally:
        await admin.close()

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        status = await store.close_session(
            connection,
            expected=context,
            event=_close_event(
                session_id="session-revoked-close-a",
                profile=profile,
                context=context,
                reason_code="device_close",
                now=now + timedelta(seconds=1),
            ),
        )
    assert status == "closed"
