"""Real PostgreSQL lifecycle checks for the PR-14 production action port.

These tests deliberately exercise the public production executor.  They do
not call the narrow Memory functions as a substitute for the executor and do
not install fake Policy/Identity/Consent/Session/Device authorities.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import asyncpg
import pytest
from services.consent.evidence import (
    BindingEvidence,
    ConsentEvidence,
    ConsentParams,
    ConsentSnapshot,
)
from services.memory_scope.domain import WriteFence
from services.memory_scope.repository import MemoryAuthoritySnapshot
from services.memory_scope.shared_actions import (
    FamilySharedActionDeniedError,
    FamilySharedActionInput,
    PostgresFamilySharedActionExecutor,
)
from services.memory_scope.tests.test_cross_domain_sensitive_write import (
    _dsn_with,
    _install_schemas,
)

pytest_plugins = ("services.memory_scope.tests.test_cross_domain_sensitive_write",)

ACTION_EXECUTOR_ROLE: Final[str] = "memoria_action_executor"
API_ROLE: Final[str] = "memoria_memory_api"
FAMILY_SPACE_ID: Final[str] = "family-pr14"
BINDING_ID: Final[str] = "binding-pr14"
DEVICE_ID: Final[str] = "device-pr14"
PROPOSER_ID: Final[str] = "person-a"
CO_SUBJECT_ID: Final[str] = "person-b"
UNAUTHORIZED_ID: Final[str] = "person-c"
SESSION_ID_BY_SUBJECT: Final[dict[str, str]] = {
    PROPOSER_ID: "session-pr14-proposer",
    CO_SUBJECT_ID: "session-pr14-co-subject",
    UNAUTHORIZED_ID: "session-pr14-unauthorized",
}
PROFILE_ID_BY_SUBJECT: Final[dict[str, str]] = {
    PROPOSER_ID: "profile-pr14-proposer",
    CO_SUBJECT_ID: "profile-pr14-co-subject",
    UNAUTHORIZED_ID: "profile-pr14-unauthorized",
}
SOURCE_EVIDENCE_ID: Final[str] = "capture-evidence-pr14"
MEMBERSHIP_SNAPSHOT_ID: Final[str] = "membership-pr14-v1"
MEMBERSHIP_HASH: Final[str] = "d" * 64


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
        # Session's action_device_lock_trust bridge is conditional on Device
        # Fleet being present, so replay the idempotent Session schema after it.
        await admin.execute(
            (root / "services/session_runtime/postgres_schema.sql").read_text(
                encoding="utf-8"
            )
        )
        await admin.execute("RESET ROLE")
    finally:
        await admin.close()


def _consent_evidence(
    *,
    subject_id: str,
    snapshot_id: str,
    capability: str,
    now: datetime,
) -> ConsentEvidence:
    return ConsentEvidence(
        consent_id=f"consent-pr14-{subject_id}-{capability}",
        version=1,
        snapshot_id=snapshot_id,
        status="active",
        subject_id=subject_id,
        resource_owner_id=PROPOSER_ID,
        actor_id=subject_id,
        actor_kind="subject",
        device_id=DEVICE_ID,
        binding_id=BINDING_ID,
        binding_version=1,
        capability=capability,  # type: ignore[arg-type]
        purpose=capability,  # type: ignore[arg-type]
        policy_version="policy-v2",
        evidence_id=f"evidence-pr14-{subject_id}-{capability}",
        offer_id=f"offer-pr14-{subject_id}-{capability}",
        idempotency_key=f"idempotency-pr14-{subject_id}-{capability}",
        params=ConsentParams(),
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=4),
        supersedes_consent_id=None,
        superseded_by_consent_id=None,
    )


async def _seed_authorities(admin_dsn: str) -> None:
    now = datetime.now(UTC)
    binding = BindingEvidence(
        binding_id=BINDING_ID,
        version=1,
        device_id=DEVICE_ID,
        status="active",
        declared_mode="family_shared",
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=4),
    )
    snapshot_by_subject: dict[str, ConsentSnapshot] = {}
    evidence_by_subject: dict[str, tuple[ConsentEvidence, ...]] = {}
    for subject_id in (PROPOSER_ID, CO_SUBJECT_ID):
        snapshot_id = f"consent-snapshot-pr14-{subject_id}"
        grants = tuple(
            _consent_evidence(
                subject_id=subject_id,
                snapshot_id=snapshot_id,
                capability=capability,
                now=now,
            )
            for capability in (
                "family_shared_memory_proposal",
                "family_shared_memory_approval",
                "family_shared_memory_promotion",
            )
        )
        snapshot_by_subject[subject_id] = ConsentSnapshot(
            snapshot_id=snapshot_id,
            version=1,
            subject_id=subject_id,
            binding_id=BINDING_ID,
            binding_version=1,
            policy_version="policy-v2",
            created_at=now,
            grants=grants,
            relationships=(),
            binding=binding,
        )
        evidence_by_subject[subject_id] = grants

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            """
            INSERT INTO identity_persons (
                person_id, display_name, subject_category, age_band,
                age_evidence_status, locale, timezone, status, created_at,
                updated_at
            ) VALUES
                ('person-b', 'Person B', 'adult', 'adult', 'verified',
                 'zh-CN', 'Asia/Shanghai', 'active', $1, $1)
            """,
            now,
        )
        await admin.execute(
            """
            UPDATE identity_device_bindings
            SET family_space_id = $1,
                declared_mode = 'family_shared',
                account_owner_person_id = $2,
                valid_until = $3
            WHERE binding_id = 'binding-pr14'
            """,
            FAMILY_SPACE_ID,
            PROPOSER_ID,
            now + timedelta(hours=4),
        )
        await admin.execute(
            """
            INSERT INTO identity_device_bindings (
                binding_id, device_id, declared_mode, family_space_id,
                account_owner_person_id, binding_version, status, reason,
                valid_from, valid_until, service_profile_version,
                policy_bundle_version, created_at
            ) VALUES (
                $1, $2, 'family_shared', $3, $4, 1, 'active', 'create',
                $5, $6, 'v1', 'v1', $5
            )
            """,
            BINDING_ID,
            DEVICE_ID,
            FAMILY_SPACE_ID,
            PROPOSER_ID,
            now - timedelta(hours=1),
            now + timedelta(hours=4),
        )
        await admin.execute(
            """
            INSERT INTO identity_device_binding_roles (
                binding_id, person_id, role, status, permissions_json,
                granted_at
            ) VALUES
                ($1, $2, 'primary_subject', 'active', '[]'::jsonb, $3),
                ($1, $4, 'member', 'active', '[]'::jsonb, $3)
            """,
            BINDING_ID,
            PROPOSER_ID,
            now - timedelta(hours=1),
            CO_SUBJECT_ID,
        )
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
                $1, $2, $3, 1, 1, 'bound',
                $4, '[]'::jsonb, '1.0.0', 1, $5, '1.0.0',
                'disengaged', 'off', 'active', 'a', 'confirmed',
                '1.0.0', 1, 'certificate-pr14', 1, $6, $6
            )
            """,
            DEVICE_ID,
            FAMILY_SPACE_ID,
            BINDING_ID,
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
                'certificate-pr14', $1, $2, $3, 1, $4, 'ed25519',
                'active', $5, $6, $5
            )
            """,
            DEVICE_ID,
            FAMILY_SPACE_ID,
            BINDING_ID,
            "A" * 43,
            now - timedelta(minutes=5),
            now + timedelta(hours=4),
        )
        await admin.execute(
            """
            INSERT INTO device_fleet_attestation_challenges (
                nonce, device_id, family_space_id, binding_id,
                binding_version, issued_at, expires_at, consumed_at
            ) VALUES (
                'nonce-pr14-00000001', $1, $2, $3, 1, $4, $5, $6
            )
            """,
            DEVICE_ID,
            FAMILY_SPACE_ID,
            BINDING_ID,
            now - timedelta(minutes=5),
            now + timedelta(hours=4),
            now,
        )
        await admin.execute(
            """
            INSERT INTO device_fleet_attestations (
                attestation_id, device_id, certificate_id, family_space_id,
                binding_id, binding_version, monotonic_counter, nonce,
                payload, occurred_at, expires_at, accepted_at
            ) VALUES (
                'attestation-pr14', $1, 'certificate-pr14', $2, $3, 1, 1,
                'nonce-pr14-00000001', '{}'::jsonb, $4, $5, $6
            )
            """,
            DEVICE_ID,
            FAMILY_SPACE_ID,
            BINDING_ID,
            now - timedelta(minutes=5),
            now + timedelta(hours=4),
            now,
        )

        await admin.execute("SET session_replication_role = replica")
        try:
            for subject_id, profile_id in PROFILE_ID_BY_SUBJECT.items():
                session_id = SESSION_ID_BY_SUBJECT[subject_id]
                signature = {PROPOSER_ID: "a", CO_SUBJECT_ID: "b", UNAUTHORIZED_ID: "c"}[
                    subject_id
                ]
                await admin.execute(
                    """
                    INSERT INTO session_runtime_profiles (
                        runtime_profile_id, session_id, profile_revision,
                        session_epoch, actor_id, binding_id, binding_version,
                        active_subject_id, subject_revision, payload_json,
                        signature, issued_at, expires_at
                    ) VALUES (
                        $1, $2, 1, 1, $3, $4, 1, $3, 1, '{}'::jsonb,
                        repeat($5, 64), $6, $7
                    )
                    """,
                    profile_id,
                    session_id,
                    subject_id,
                    BINDING_ID,
                    signature,
                    now - timedelta(minutes=1),
                    now + timedelta(hours=3),
                )
                await admin.execute(
                    """
                    INSERT INTO session_runtime_contexts (
                        session_id, actor_id, device_id, binding_id,
                        binding_version, active_subject_id, subject_revision,
                        session_epoch, profile_revision,
                        current_runtime_profile_id, generation_id, turn_id,
                        tool_epoch, state, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, 1, $2, 1, 1, 1, $5, 0, 1, 0,
                        'active', $6, $6
                    )
                    """,
                    session_id,
                    subject_id,
                    DEVICE_ID,
                    BINDING_ID,
                    profile_id,
                    now,
                )
        finally:
            await admin.execute("RESET session_replication_role")

        for subject_id, grants in evidence_by_subject.items():
            for evidence in grants:
                await admin.execute(
                    """
                    INSERT INTO consent_evidence (
                        consent_id, version, actor_id, subject_id, binding_id,
                        binding_version, status, evidence_json
                    ) VALUES ($1, 1, $2, $2, $3, 1, 'active', $4::jsonb)
                    """,
                    evidence.consent_id,
                    subject_id,
                    BINDING_ID,
                    json.dumps(evidence.to_canonical_dict()),
                )
                await admin.execute(
                    """
                    INSERT INTO consent_evidence_head (
                        actor_id, subject_id, binding_id, binding_version,
                        capability, purpose, current_consent_id,
                        current_revision, current_hash, updated_at
                    ) VALUES ($1, $1, $2, 1, $3, $3, $4, 1, $5, $6)
                    """,
                    subject_id,
                    BINDING_ID,
                    evidence.capability,
                    evidence.consent_id,
                    evidence.canonical_hash,
                    now,
                )
            snapshot = snapshot_by_subject[subject_id]
            await admin.execute(
                """
                INSERT INTO consent_snapshot (
                    snapshot_id, version, actor_id, subject_id, binding_id,
                    binding_version, snapshot_json
                ) VALUES ($1, 1, $2, $2, $3, 1, $4::jsonb)
                """,
                snapshot.snapshot_id,
                subject_id,
                BINDING_ID,
                json.dumps(snapshot.to_canonical_dict()),
            )
            await admin.execute(
                """
                INSERT INTO consent_snapshot_head (
                    subject_id, binding_id, binding_version,
                    current_snapshot_id, current_revision, current_hash,
                    updated_at
                ) VALUES ($1, $2, 1, $3, 1, $4, $5)
                """,
                subject_id,
                BINDING_ID,
                snapshot.snapshot_id,
                snapshot.canonical_hash,
                now,
            )

        await admin.execute(
            """
            INSERT INTO memory_shared_membership_snapshots (
                snapshot_id, family_space_id, revision, canonical_hash,
                status, family_owner_subject_id, subject_ids, binding_id,
                binding_version, valid_from, valid_until
            ) VALUES (
                $1, $2, 1, $3, 'active', $4, $5::jsonb, $6, 1, $7, $8
            )
            """,
            MEMBERSHIP_SNAPSHOT_ID,
            FAMILY_SPACE_ID,
            MEMBERSHIP_HASH,
            PROPOSER_ID,
            json.dumps([PROPOSER_ID, CO_SUBJECT_ID]),
            BINDING_ID,
            now - timedelta(hours=1),
            now + timedelta(hours=4),
        )
        await admin.execute(
            """
            INSERT INTO memory_capture_evidence (
                evidence_id, revision, canonical_hash, status, subject_id,
                binding_id, binding_version, valid_from, valid_until
            ) VALUES ($1, 1, $2, 'active', $3, $4, 1, $5, $6)
            """,
            SOURCE_EVIDENCE_ID,
            "c" * 64,
            PROPOSER_ID,
            BINDING_ID,
            now - timedelta(hours=1),
            now + timedelta(hours=4),
        )
    finally:
        await admin.close()


def _authority_snapshot(subject_id: str) -> MemoryAuthoritySnapshot:
    now = datetime.now(UTC)
    role = "primary_subject" if subject_id == PROPOSER_ID else "member"
    session_id = SESSION_ID_BY_SUBJECT[subject_id]
    profile_id = PROFILE_ID_BY_SUBJECT[subject_id]
    fence = WriteFence(
        session_id=session_id,
        epoch=1,
        binding_id=BINDING_ID,
        binding_role=role,  # type: ignore[arg-type]
        runtime_profile_id=profile_id,
        actor_subject_id=subject_id,
        active_subject_id=subject_id,
        binding_version=1,
        device_id=DEVICE_ID,
        subject_revision=1,
        family_space_id=FAMILY_SPACE_ID,
        generation_id="0",
        turn_id=1,
        valid_until=now + timedelta(minutes=10),
    )
    return MemoryAuthoritySnapshot(
        fence=fence,
        active_subject_id=subject_id,
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        registered=True,
        actor_binding_role=role,  # type: ignore[arg-type]
        actor_binding_roles=(role,),  # type: ignore[arg-type]
        family_space_id=FAMILY_SPACE_ID,
        consent_snapshot_id=f"consent-snapshot-pr14-{subject_id}",
        profile_revision=1,
        generation_id=0,
        turn_id=1,
        tool_epoch=0,
        policy_bundle_version="v1",
        policy_receipt_ids=(),
        service_mode="family_shared",
    )


@dataclass(frozen=True, slots=True)
class _ProductionFixture:
    executor_dsn: str
    admin_dsn: str
    password: str
    database: str


async def _prepare_production_fixture(
    cross_db: tuple[str, str, str, str],
) -> _ProductionFixture:
    executor_dsn, admin_dsn, password, database = cross_db
    await _install_schemas(admin_dsn, password)
    await _install_device_bridge(admin_dsn)
    await _seed_authorities(admin_dsn)
    return _ProductionFixture(executor_dsn, admin_dsn, password, database)


async def _propose_and_promote(
    action_executor: PostgresFamilySharedActionExecutor,
    *,
    title: str,
) -> str:
    proposed = await action_executor.execute(
        snapshot=_authority_snapshot(PROPOSER_ID),
        actor_subject_id=PROPOSER_ID,
        action=FamilySharedActionInput.propose(
            title=title,
            content="全家一起去公园",
            source_evidence_ids=(SOURCE_EVIDENCE_ID,),
            co_subject_ids=(CO_SUBJECT_ID,),
        ),
    )
    assert proposed.status == "pending"
    proposal_id = proposed.proposal_id
    proposer_vote = await action_executor.execute(
        snapshot=_authority_snapshot(PROPOSER_ID),
        actor_subject_id=PROPOSER_ID,
        action=FamilySharedActionInput.command(
            "confirm", proposal_id=proposal_id
        ),
    )
    assert proposer_vote.status == "pending"
    promoted = await action_executor.execute(
        snapshot=_authority_snapshot(CO_SUBJECT_ID),
        actor_subject_id=CO_SUBJECT_ID,
        action=FamilySharedActionInput.command(
            "confirm", proposal_id=proposal_id
        ),
    )
    assert promoted.status == "promoted"
    return proposal_id


async def _counts(admin_dsn: str, proposal_id: str) -> tuple[int, ...]:
    admin = await asyncpg.connect(admin_dsn)
    try:
        row = await admin.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM memory_shared_proposals
                 WHERE proposal_id = $1) AS proposals,
                (SELECT count(*) FROM memory_shared_votes
                 WHERE proposal_id = $1) AS votes,
                (SELECT count(*) FROM memory_records
                 WHERE shared_proposal_id = $1) AS records,
                (SELECT count(*) FROM memory_status_events e
                 JOIN memory_records r ON r.record_id = e.record_id
                 WHERE r.shared_proposal_id = $1) AS statuses,
                (SELECT count(*) FROM memory_outbox
                 WHERE event_id LIKE $1 || ':%') AS outbox,
                (SELECT count(*) FROM memory_audit_events
                 WHERE proposal_id = $1) AS audits,
                (SELECT count(*) FROM policy_receipts_v2
                 WHERE action_resource_fence ->> 'proposal_id' = $1) AS receipts
            """,
            proposal_id,
        )
        assert row is not None
        return tuple(int(value) for value in row)
    finally:
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the production PR-14 contract",
)
async def test_production_executor_runs_full_shared_lifecycle_and_privacy_commands(
    cross_db: tuple[str, str, str, str],
) -> None:
    fixture = await _prepare_production_fixture(cross_db)
    action_executor = PostgresFamilySharedActionExecutor(fixture.executor_dsn)
    await action_executor.initialize()
    assert action_executor.available
    try:
        object_proposal = await action_executor.execute(
            snapshot=_authority_snapshot(PROPOSER_ID),
            actor_subject_id=PROPOSER_ID,
            action=FamilySharedActionInput.propose(
                title="家庭记忆对象",
                content="全家一起去公园",
                source_evidence_ids=(SOURCE_EVIDENCE_ID,),
                co_subject_ids=(CO_SUBJECT_ID,),
            ),
        )
        proposal_id = object_proposal.proposal_id
        proposer_vote = await action_executor.execute(
            snapshot=_authority_snapshot(PROPOSER_ID),
            actor_subject_id=PROPOSER_ID,
            action=FamilySharedActionInput.command(
                "confirm", proposal_id=proposal_id
            ),
        )
        assert proposer_vote.status == "pending"
        objected = await action_executor.execute(
            snapshot=_authority_snapshot(CO_SUBJECT_ID),
            actor_subject_id=CO_SUBJECT_ID,
            action=FamilySharedActionInput.command(
                "object", proposal_id=proposal_id
            ),
        )
        assert objected.status == "frozen"
        object_counts = await _counts(fixture.admin_dsn, proposal_id)
        assert object_counts == (1, 2, 0, 0, 1, 3, 3)

        withdraw_proposal_id = await _propose_and_promote(
            action_executor, title="家庭记忆撤回"
        )
        withdrawn = await action_executor.execute(
            snapshot=_authority_snapshot(CO_SUBJECT_ID),
            actor_subject_id=CO_SUBJECT_ID,
            action=FamilySharedActionInput.command(
                "withdraw", proposal_id=withdraw_proposal_id
            ),
        )
        assert withdrawn.status == "withdrawn"
        withdraw_counts = await _counts(fixture.admin_dsn, withdraw_proposal_id)
        assert withdraw_counts == (1, 2, 1, 2, 2, 5, 5)

        admin = await asyncpg.connect(fixture.admin_dsn)
        try:
            record = await admin.fetchrow(
                """
                SELECT record_id, scope, resource_owner_id, family_space_id,
                       shared_proposal_id, payload
                FROM memory_records WHERE shared_proposal_id = $1
                """,
                withdraw_proposal_id,
            )
            assert record is not None
            assert tuple(record[1:5]) == (
                "family_shared",
                FAMILY_SPACE_ID,
                FAMILY_SPACE_ID,
                withdraw_proposal_id,
            )
            payload = record["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            assert payload == {
                "title": "家庭记忆撤回",
                "content": "全家一起去公园",
            }
            assert await admin.fetchval(
                """
                SELECT status FROM memory_status_events
                WHERE record_id = $1 ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                record["record_id"],
            ) == "revoked"
        finally:
            await admin.close()
    finally:
        await action_executor.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the production PR-14 contract",
)
async def test_production_executor_denies_unauthorized_and_admin_cannot_bypass(
    cross_db: tuple[str, str, str, str],
) -> None:
    fixture = await _prepare_production_fixture(cross_db)
    action_executor = PostgresFamilySharedActionExecutor(fixture.executor_dsn)
    await action_executor.initialize()
    assert action_executor.available
    try:
        with pytest.raises(FamilySharedActionDeniedError):
            await action_executor.execute(
                snapshot=_authority_snapshot(UNAUTHORIZED_ID),
                actor_subject_id=UNAUTHORIZED_ID,
                action=FamilySharedActionInput.propose(
                    title="越权提案",
                    content="不应落库",
                    source_evidence_ids=(SOURCE_EVIDENCE_ID,),
                    co_subject_ids=(CO_SUBJECT_ID,),
                ),
            )

        api_dsn = _dsn_with(
            fixture.admin_dsn,
            database=fixture.database,
            user=API_ROLE,
            password=fixture.password,
        )
        api = await asyncpg.connect(api_dsn)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await api.fetchval(
                    "SELECT memory_shared_action_propose('{}'::jsonb)"
                )
            # The API role may use the scoped read surface, but FORCE RLS
            # with no request context must expose no proposal rows.
            assert await api.fetchval(
                "SELECT count(*) FROM memory_shared_proposals"
            ) == 0
        finally:
            await api.close()

    finally:
        await action_executor.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the production PR-14 contract",
)
async def test_production_executor_rolls_back_failed_promotion_without_partial_rows(
    cross_db: tuple[str, str, str, str],
) -> None:
    fixture = await _prepare_production_fixture(cross_db)
    action_executor = PostgresFamilySharedActionExecutor(fixture.executor_dsn)
    await action_executor.initialize()
    assert action_executor.available
    try:
        # This is intentionally a production proposal call.  If the proposal
        # authority incorrectly tries to lock a not-yet-created proposal, the
        # test remains red and reports that product defect instead of seeding a
        # proposal behind the executor's back.
        proposed = await action_executor.execute(
            snapshot=_authority_snapshot(PROPOSER_ID),
            actor_subject_id=PROPOSER_ID,
            action=FamilySharedActionInput.propose(
                title="回滚提案",
                content="最终确认失败也不能留下半成品",
                source_evidence_ids=(SOURCE_EVIDENCE_ID,),
                co_subject_ids=(CO_SUBJECT_ID,),
            ),
        )
        proposal_id = proposed.proposal_id
        await action_executor.execute(
            snapshot=_authority_snapshot(PROPOSER_ID),
            actor_subject_id=PROPOSER_ID,
            action=FamilySharedActionInput.command(
                "confirm", proposal_id=proposal_id
            ),
        )

        # The last confirm writes a vote first, then automatically attempts a
        # promotion.  Remove only the co-subject's current promotion consent;
        # the resulting policy denial must roll back that vote, its receipt,
        # status transition, audit and outbox together.
        admin = await asyncpg.connect(fixture.admin_dsn)
        try:
            await admin.execute(
                """
                UPDATE consent_evidence_head
                SET current_consent_id = NULL, current_revision = 0,
                    current_hash = NULL, updated_at = now()
                WHERE actor_id = $1 AND subject_id = $1
                  AND binding_id = $2 AND binding_version = 1
                  AND capability = 'family_shared_memory_promotion'
                  AND purpose = 'family_shared_memory_promotion'
                """,
                CO_SUBJECT_ID,
                BINDING_ID,
            )
        finally:
            await admin.close()

        with pytest.raises(FamilySharedActionDeniedError):
            await action_executor.execute(
                snapshot=_authority_snapshot(CO_SUBJECT_ID),
                actor_subject_id=CO_SUBJECT_ID,
                action=FamilySharedActionInput.command(
                    "confirm", proposal_id=proposal_id
                ),
            )

        counts = await _counts(fixture.admin_dsn, proposal_id)
        assert counts == (1, 1, 0, 0, 1, 2, 2)
        admin = await asyncpg.connect(fixture.admin_dsn)
        try:
            status = await admin.fetchval(
                "SELECT status FROM memory_shared_proposals WHERE proposal_id = $1",
                proposal_id,
            )
            assert status == "pending"
            assert await admin.fetchval(
                """
                SELECT count(*) FROM memory_shared_votes
                WHERE proposal_id = $1 AND subject_id = $2
                """,
                proposal_id,
                CO_SUBJECT_ID,
            ) == 0
            assert await admin.fetchval(
                "SELECT count(*) FROM memory_records WHERE shared_proposal_id = $1",
                proposal_id,
            ) == 0
        finally:
            await admin.close()
    finally:
        await action_executor.close()
