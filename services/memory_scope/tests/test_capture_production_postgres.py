"""Real PostgreSQL E2E for the production PR-12 capture assembly."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from services.consent.evidence import (
    BindingEvidence,
    ConsentEvidence,
    ConsentParams,
    ConsentSnapshot,
)
from services.memory_scope.capture_policy import build_memory_capture_policy_assembly
from services.memory_scope.domain import (
    ConsentSnapshotInput,
    MemoryScope,
    MemoryWriteDraft,
    PolicyDecisionInput,
    ResolutionContext,
    SubjectContext,
    WriteFence,
)
from services.memory_scope.postgres_store import PostgresMemoryStore
from services.memory_scope.production import (
    MemoryCaptureConflictError,
    MemoryProductionUnavailableError,
    MemorySensitiveWriteExecutor,
)
from services.memory_scope.repository import MemoryAuthoritySnapshot
from services.memory_scope.tests.test_cross_domain_sensitive_write import (
    _install_schemas,
)

pytest_plugins = ("services.memory_scope.tests.test_cross_domain_sensitive_write",)


async def _install_device_bridge(admin_dsn: str) -> None:
    root = Path(__file__).resolve().parents[3]
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            (root / "services/device_fleet/postgres_schema.sql").read_text(
                encoding="utf-8"
            )
        )
        await admin.execute("RESET ROLE")
        # Session installs the device bridge policies conditionally, so replay
        # its idempotent schema after Device Fleet exists.
        await admin.execute(
            (root / "services/session_runtime/postgres_schema.sql").read_text(
                encoding="utf-8"
            )
        )
        await admin.execute("RESET ROLE")
    finally:
        await admin.close()


async def _seed_authorities(admin_dsn: str) -> None:
    now = datetime.now(UTC)
    binding = BindingEvidence(
        binding_id="binding-1",
        version=1,
        device_id="device-1",
        status="active",
        declared_mode="self_use",
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=2),
    )
    evidence = ConsentEvidence(
        consent_id="memory-consent-1",
        version=1,
        snapshot_id="memory-consent-snapshot-1",
        status="active",
        subject_id="person-a",
        resource_owner_id="person-a",
        actor_id="person-a",
        actor_kind="subject",
        device_id="device-1",
        binding_id="binding-1",
        binding_version=1,
        capability="memory_capture",
        purpose="memory_capture",
        policy_version="policy-v2",
        evidence_id="memory-consent-evidence-1",
        offer_id="memory-consent-offer-1",
        idempotency_key="memory-consent-idempotency-1",
        params=ConsentParams(),
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=2),
        supersedes_consent_id=None,
        superseded_by_consent_id=None,
    )
    snapshot = ConsentSnapshot(
        snapshot_id="memory-consent-snapshot-1",
        version=1,
        subject_id="person-a",
        binding_id="binding-1",
        binding_version=1,
        policy_version="policy-v2",
        created_at=now,
        grants=(evidence,),
        relationships=(),
        binding=binding,
    )
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            """
            INSERT INTO device_fleet_devices (
                device_id, family_space_id, binding_id, binding_version,
                binding_version_floor, lifecycle_status,
                capability_manifest_hash, capabilities, firmware_version,
                firmware_security_version, firmware_sha256,
                bootloader_version, physical_mute_state, privacy_light_state,
                attested_sim_status, active_ota_slot, ota_boot_status,
                anti_rollback_floor_version,
                anti_rollback_floor_security_version,
                current_certificate_id, last_attestation_counter,
                created_at, updated_at
            ) VALUES (
                'device-1', 'family-1', 'binding-1', 1, 1, 'bound',
                $1, '[]'::jsonb, '1.0.0', 1, $2, '1.0.0',
                'disengaged', 'off', 'active', 'a', 'confirmed',
                '1.0.0', 1, 'certificate-1', 1, $3, $3
            )
            """,
            "a" * 64,
            "b" * 64,
            now,
        )
        await admin.execute(
            """
            INSERT INTO device_fleet_certificates (
                certificate_id, device_id, family_space_id, binding_id,
                binding_version, public_key_b64, key_algorithm, status,
                valid_from, valid_until, created_at
            ) VALUES (
                'certificate-1', 'device-1', 'family-1', 'binding-1', 1,
                $1, 'ed25519', 'active', $2, $3, $4
            )
            """,
            "A" * 43,
            now - timedelta(minutes=5),
            now + timedelta(hours=1),
            now,
        )
        await admin.execute(
            """
            INSERT INTO device_fleet_attestation_challenges (
                nonce, device_id, family_space_id, binding_id,
                binding_version, issued_at, expires_at, consumed_at
            ) VALUES (
                'capture-e2e-nonce-0001', 'device-1', 'family-1',
                'binding-1', 1, $1, $2, $3
            )
            """,
            now - timedelta(minutes=5),
            now + timedelta(hours=1),
            now,
        )
        await admin.execute(
            """
            INSERT INTO device_fleet_attestations (
                attestation_id, device_id, certificate_id, family_space_id,
                binding_id, binding_version, monotonic_counter, nonce,
                payload, occurred_at, expires_at, accepted_at
            ) VALUES (
                'attestation-1', 'device-1', 'certificate-1', 'family-1',
                'binding-1', 1, 1, 'capture-e2e-nonce-0001', '{}'::jsonb,
                $1, $2, $3
            )
            """,
            now - timedelta(minutes=5),
            now + timedelta(hours=1),
            now,
        )
        await admin.execute(
            """
            INSERT INTO consent_evidence (
                consent_id, version, actor_id, subject_id, binding_id,
                binding_version, status, evidence_json
            ) VALUES ($1, 1, 'person-a', 'person-a', 'binding-1', 1,
                      'active', $2::jsonb)
            """,
            evidence.consent_id,
            json.dumps(evidence.to_canonical_dict()),
        )
        await admin.execute(
            """
            INSERT INTO consent_snapshot (
                snapshot_id, version, actor_id, subject_id, binding_id,
                binding_version, snapshot_json
            ) VALUES ($1, 1, 'person-a', 'person-a', 'binding-1', 1,
                      $2::jsonb)
            """,
            snapshot.snapshot_id,
            json.dumps(snapshot.to_canonical_dict()),
        )
        await admin.execute(
            """
            INSERT INTO consent_evidence_head (
                actor_id, subject_id, binding_id, binding_version,
                capability, purpose, current_consent_id, current_revision,
                current_hash, updated_at
            ) VALUES (
                'person-a', 'person-a', 'binding-1', 1,
                'memory_capture', 'memory_capture', $1, 1, $2, $3
            )
            """,
            evidence.consent_id,
            evidence.canonical_hash,
            now,
        )
        await admin.execute(
            """
            INSERT INTO consent_snapshot_head (
                subject_id, binding_id, binding_version, current_snapshot_id,
                current_revision, current_hash, updated_at
            ) VALUES ('person-a', 'binding-1', 1, $1, 1, $2, $3)
            """,
            snapshot.snapshot_id,
            snapshot.canonical_hash,
            now,
        )
    finally:
        await admin.close()


def _authority_snapshot() -> MemoryAuthoritySnapshot:
    now = datetime.now(UTC)
    fence = WriteFence(
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_role="primary_subject",
        runtime_profile_id="profile-1",
        actor_subject_id="person-a",
        active_subject_id="person-a",
        binding_version=1,
        device_id="device-1",
        subject_revision=1,
        family_space_id=None,
        generation_id="0",
        turn_id=0,
        valid_until=now + timedelta(minutes=3),
    )
    return MemoryAuthoritySnapshot(
        fence=fence,
        active_subject_id="person-a",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        registered=True,
        actor_binding_role="primary_subject",
        actor_binding_roles=("primary_subject",),
        family_space_id=None,
        consent_snapshot_id="memory-consent-snapshot-1",
        profile_revision=1,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        policy_bundle_version="v1",
        policy_receipt_ids=(),
        service_mode="adult_companion",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run production capture E2E",
)
async def test_production_capture_assembly_commits_all_rows_on_one_real_transaction(
    cross_db: tuple[str, str, str, str],
) -> None:
    executor_dsn, admin_dsn, password, _database = cross_db
    await _install_schemas(admin_dsn, password)
    await _install_device_bridge(admin_dsn)
    await _seed_authorities(admin_dsn)
    assembly = build_memory_capture_policy_assembly()
    store = PostgresMemoryStore(executor_dsn).with_role("action_executor")
    executor = MemorySensitiveWriteExecutor(
        action_executor_store=store,
        sensitive_write=assembly.sensitive_write,
        context_builder=assembly.context_builder,
    )
    await executor.initialize()
    projected_at = datetime.now(UTC)
    projection_candidate = {
        "active_subject_id": "person-a",
        "actor_id": "person-a",
        "binding_id": "binding-1",
        "binding_version": 1,
        "device_id": "device-1",
        "event_sequence": 1,
        "generation_id": 0,
        "memory_scope": "personal_private",
        "runtime_profile_id": "profile-1",
        "session_id": "session-1",
        "session_epoch": 1,
        "subject_revision": 1,
        "tool_epoch": 0,
        "turn_id": 0,
    }
    projected = await executor.project_capture_evidence(
        event_id="turn-evidence-1",
        content_sha256="d" * 64,
        occurred_at=projected_at,
        candidate=projection_candidate,
    )
    assert projected is True
    assert await executor.project_capture_evidence(
        event_id="turn-evidence-1",
        content_sha256="d" * 64,
        occurred_at=projected_at,
        candidate=projection_candidate,
    ) is True
    assert await executor.project_capture_evidence(
        event_id="forged-turn-evidence",
        content_sha256="e" * 64,
        occurred_at=projected_at,
        candidate={**projection_candidate, "active_subject_id": "person-b"},
    ) is False
    snapshot = _authority_snapshot()
    context = ResolutionContext(
        subject=SubjectContext(
            active_subject_id="person-a",
            subject_category="adult",
            speaker_state="confirmed",
            speaker_confidence=0.99,
            registered=True,
        ),
        policy=PolicyDecisionInput(),
        consent=ConsentSnapshotInput(
            snapshot_id="memory-consent-snapshot-1",
            granted=True,
            scope="memory",
            covers_subjects=("person-a",),
        ),
        requested_scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
        fence=snapshot.fence,
    )
    draft = MemoryWriteDraft(
        content="今天一起去了公园",
        source_evidence_ids=("turn-evidence-1",),
    )
    try:
        record_id = await executor.execute(
            context,
            draft,
            "person-a",
            snapshot=snapshot,
        )
        admin = await asyncpg.connect(admin_dsn)
        try:
            row = await admin.fetchrow(
                """
                SELECT record_id, confidence, retention,
                       retention_expires_at, payload, policy_receipt_id
                FROM memory_records WHERE record_id = $1
                """,
                record_id,
            )
            assert row is not None
            assert float(row["confidence"]) == 0.5
            assert row["retention"] == "ttl"
            assert row["retention_expires_at"] is not None
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            assert payload["content"] == "今天一起去了公园"
            assert len(payload["canonical_action_sha256"]) == 64
            counts = await admin.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM policy_receipts_v2
                   WHERE receipt_id = $1) AS receipt_count,
                  (SELECT count(*) FROM memory_status_events
                   WHERE record_id = $2 AND status = 'confirmed') AS status_count,
                  (SELECT count(*) FROM memory_outbox
                   WHERE event_id = $2 || ':captured') AS outbox_count,
                  (SELECT count(*) FROM memory_audit_events
                   WHERE record_id = $2) AS audit_count
                """,
                row["policy_receipt_id"],
                record_id,
            )
            assert counts is not None
            assert tuple(counts) == (1, 1, 1, 1)
        finally:
            await admin.close()

        # A retry of the same canonical action is serialized by the resource
        # lock and cannot create a second receipt/record projection.
        with pytest.raises(MemoryCaptureConflictError):
            await executor.execute(
                context,
                draft,
                "person-a",
                snapshot=snapshot,
            )
        admin = await asyncpg.connect(admin_dsn)
        try:
            assert await admin.fetchval("SELECT count(*) FROM memory_records") == 1
            assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 1
            await admin.execute(
                """
                UPDATE memory_capture_evidence SET status = 'revoked'
                WHERE evidence_id = 'turn-evidence-1'
                """
            )
        finally:
            await admin.close()

        # Current evidence revocation is checked before a new receipt is
        # minted; no partially-authorized action survives.
        with pytest.raises(MemoryProductionUnavailableError):
            await executor.execute(
                context,
                MemoryWriteDraft(
                    content="另一条记忆",
                    source_evidence_ids=("turn-evidence-1",),
                ),
                "person-a",
                snapshot=snapshot,
            )
        admin = await asyncpg.connect(admin_dsn)
        try:
            assert await admin.fetchval("SELECT count(*) FROM memory_records") == 1
            assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 1
        finally:
            await admin.close()
    finally:
        await executor.close()
