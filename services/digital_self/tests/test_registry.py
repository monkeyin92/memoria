"""Digital Self versions on PostgreSQL, as the production archive role."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import pytest
import pytest_asyncio
from services.archive.domain import EvidenceEvent
from services.archive.postgres_archive import PostgresLifeArchive
from services.digital_self.compiler import (
    canonical_json_bytes,
    canonical_manifest_bytes,
    entry_dict,
    sha256_hex,
)
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    EmptyDigitalSelfSourceError,
    InvalidVersionTransitionError,
    ManifestIntegrityError,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
    SourceSnapshotConflictError,
    VersionNotFoundError,
)
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.governance.account_data import PostgresAccountRepository
from services.self_model.postgres_registry import PostgresSelfModelRegistry

if TYPE_CHECKING:
    from testing.postgres_harness import TestDatabase

_OCCURRED_AT = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
_OWNER_CLAIM_ID = "00000000-0000-0000-0000-000000000000"
_PERSONA_VERSION_ID = "20000000-0000-0000-0000-000000000001"
_PERSON_ID = "30000000-0000-0000-0000-000000000001"
_RELATIONSHIP_ID = "30000000-0000-0000-0000-000000000002"
_LEGACY_VERSION_ID = "40000000-0000-0000-0000-000000000001"


@pytest_asyncio.fixture
async def archive(postgres_database: TestDatabase) -> AsyncIterator[PostgresLifeArchive]:
    store = PostgresLifeArchive(postgres_database.role_dsn("memoria_app"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def registry(postgres_database: TestDatabase) -> AsyncIterator[PostgresDigitalSelfRegistry]:
    store = PostgresDigitalSelfRegistry(postgres_database.role_dsn("memoria_app"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def self_model(postgres_database: TestDatabase) -> AsyncIterator[PostgresSelfModelRegistry]:
    store = PostgresSelfModelRegistry(postgres_database.role_dsn("memoria_app"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def _as_owner(database: TestDatabase, *statements: tuple[str, tuple[object, ...]]) -> None:
    """Run statements as the cluster admin: rows no store API writes, or corruption."""

    connection = await asyncpg.connect(database.owner_dsn())
    try:
        async with connection.transaction():
            for sql, args in statements:
                await connection.execute(sql, *args)
    finally:
        await connection.close()


_INSERT_MEMORY_CLAIM = """
INSERT INTO memory_claims (
    claim_id, account_id, category, domain_category, subject_key, predicate,
    value, confidence, status, sensitive_domain, extractor_version,
    source_event_id, valid_at, observed_at
) VALUES ($1, $2, $3, $3, 'owner', $4, $5, $6, $7, 'personal', 'extractor-v1', $8, $9, $9)
"""

_INSERT_PERSONA_TRAIT = """
INSERT INTO persona_traits (
    trait_id, account_id, category, normalized_key, description,
    context, counterexample, confidence, status, observation_count
) VALUES ($1, $2, $3, $4, $5, 'conversation', $6, 0.9, 'confirmed', 3)
"""


async def _seed_sources(
    archive: PostgresLifeArchive,
    database: TestDatabase,
    *,
    account_id: str = "owner-account",
) -> None:
    for speaker_class in ("owner", "guest", "uncertain", "assistant"):
        await archive.record(
            EvidenceEvent(
                event_id=f"{account_id}-{speaker_class}-source",
                account_id=account_id,
                event_type=(
                    "assistant.final"
                    if speaker_class == "assistant"
                    else "speech.utterance_finalized"
                ),
                occurred_at=_OCCURRED_AT,
                speaker_class=speaker_class,  # type: ignore[arg-type]
                source="registry-test",
                payload={
                    "text": f"{speaker_class} material",
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": speaker_class == "owner",
                },
            )
        )
    await archive.record(
        EvidenceEvent(
            event_id=f"{account_id}-companion-source",
            account_id=account_id,
            event_type="assistant.final",
            occurred_at=_OCCURRED_AT,
            speaker_class="owner",
            source="companion-runtime",
            payload={
                "text": "companion output misclassified as owner",
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": False,
            },
        )
    )

    statements: list[tuple[str, tuple[object, ...]]] = []
    for index, speaker_class in enumerate(("owner", "guest", "uncertain", "assistant")):
        statements.append(
            (
                _INSERT_MEMORY_CLAIM,
                (
                    uuid.UUID(f"00000000-0000-0000-0000-00000000000{index}"),
                    account_id,
                    "life_story",
                    "preference",
                    f"{speaker_class} memory",
                    0.9,
                    "confirmed",
                    f"{account_id}-{speaker_class}-source",
                    _OCCURRED_AT,
                ),
            )
        )
    statements.append(
        (
            _INSERT_MEMORY_CLAIM,
            (
                uuid.UUID("00000000-0000-0000-0000-000000000099"),
                account_id,
                "daily_life",
                "candidate",
                "not confirmed",
                0.7,
                "candidate",
                f"{account_id}-owner-source",
                _OCCURRED_AT,
            ),
        )
    )
    statements.append(
        (
            _INSERT_MEMORY_CLAIM,
            (
                uuid.UUID("00000000-0000-0000-0000-000000000098"),
                account_id,
                "daily_life",
                "companion",
                "misclassified companion memory",
                0.9,
                "confirmed",
                f"{account_id}-companion-source",
                _OCCURRED_AT,
            ),
        )
    )

    snapshot: list[dict[str, object]] = []
    for index, speaker_class in enumerate(("owner", "guest", "uncertain", "assistant")):
        trait_id = f"10000000-0000-0000-0000-00000000000{index}"
        description = f"{speaker_class} trait"
        statements.append(
            (
                _INSERT_PERSONA_TRAIT,
                (uuid.UUID(trait_id), account_id, "verbal_tic", speaker_class, description, ""),
            )
        )
        snapshot.append(
            {
                "trait_id": trait_id,
                "category": "verbal_tic",
                "description": description,
                "context": "conversation",
                "counterexample": "",
                "confidence": 0.9,
                "source_event_ids": [f"{account_id}-{speaker_class}-source"],
            }
        )
    legacy_trait_id = "10000000-0000-0000-0000-000000000099"
    statements.append(
        (
            _INSERT_PERSONA_TRAIT,
            (
                uuid.UUID(legacy_trait_id),
                account_id,
                "decision_habit",
                "legacy-decision",
                "legacy decision candidate",
                "有时会先照顾家人",
            ),
        )
    )
    snapshot.append(
        {
            "trait_id": legacy_trait_id,
            "category": "decision_habit",
            "description": "legacy decision candidate",
            "context": "conversation",
            "counterexample": "有时会先照顾家人",
            "confidence": 0.9,
            "source_event_ids": [f"{account_id}-owner-source"],
        }
    )
    statements.append(
        (
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason, snapshot
            ) VALUES ($1, $2, 1, 'active', 'test', $3::jsonb)
            """,
            (uuid.UUID(_PERSONA_VERSION_ID), account_id, json.dumps(snapshot, ensure_ascii=False)),
        )
    )
    await _as_owner(database, *statements)


async def _correct_owner_memory(database: TestDatabase, value: str) -> None:
    await _as_owner(
        database,
        (
            "UPDATE memory_claims SET value = $1 WHERE account_id = 'owner-account' "
            "AND claim_id = $2",
            (value, uuid.UUID(_OWNER_CLAIM_ID)),
        ),
    )


@pytest.mark.asyncio
async def test_build_rejects_an_empty_owner_source_snapshot(
    registry: PostgresDigitalSelfRegistry,
) -> None:
    with pytest.raises(EmptyDigitalSelfSourceError):
        await registry.build(account_id="empty-account")


@pytest.mark.asyncio
async def test_build_is_deterministic_and_only_compiles_confirmed_owner_sources(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    first = await registry.build(account_id="owner-account")
    # A second, independent history over the same sources: drop the first
    # version (no store API deletes one) and build version 1 again.
    await _as_owner(
        postgres_database,
        ("DELETE FROM digital_self_lifecycle_audit_events", ()),
        ("DELETE FROM digital_self_versions", ()),
    )
    second = await registry.build(account_id="owner-account")

    assert second.version_number == first.version_number == 1
    assert first.version_id != second.version_id
    assert first.created_at != second.created_at
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.manifest == second.manifest
    assert [type(entry) for entry in first.manifest.entries] == [
        MemoryClaimManifestEntry,
        PersonaTraitManifestEntry,
    ]
    assert [
        entry.value
        for entry in first.manifest.entries
        if isinstance(entry, MemoryClaimManifestEntry)
    ] == ["owner memory"]
    assert [
        entry.description
        for entry in first.manifest.entries
        if isinstance(entry, PersonaTraitManifestEntry)
    ] == ["owner trait"]
    assert "legacy decision candidate" not in str(first.manifest)
    assert first.manifest.source_summary.memory_claim_count == 1
    assert first.manifest.source_summary.persona_trait_count == 1
    assert first.manifest.source_summary.persona_version_id == _PERSONA_VERSION_ID


@pytest.mark.asyncio
async def test_source_correction_creates_a_new_version_without_mutating_old_manifest(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    first = await registry.build(account_id="owner-account")

    await _correct_owner_memory(postgres_database, "corrected owner memory")
    second = await registry.build(account_id="owner-account")
    reloaded_first = await registry.get(
        account_id="owner-account",
        version_id=first.version_id,
    )

    assert second.version_number == 2
    assert second.manifest.parent_version_id == first.version_id
    assert second.manifest_sha256 != first.manifest_sha256
    assert reloaded_first.manifest == first.manifest
    assert "corrected owner memory" not in str(reloaded_first.manifest)
    assert "corrected owner memory" in str(second.manifest)

    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await _as_owner(
            postgres_database,
            (
                "UPDATE digital_self_versions SET manifest_json = '{}' WHERE version_id = $1",
                (uuid.UUID(first.version_id),),
            ),
        )


@pytest.mark.asyncio
async def test_negative_owner_evidence_excludes_target_from_the_next_manifest(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    first = await registry.build(account_id="owner-account")
    await archive.record(
        EvidenceEvent(
            event_id="owner-memory-not-me",
            account_id="owner-account",
            event_type="owner.action_recorded",
            occurred_at=_OCCURRED_AT,
            speaker_class="owner",
            source="user.growth_feedback",
            payload={
                "action_type": "not_me",
                "target_kind": "memory_claim",
                "target_id": _OWNER_CLAIM_ID,
                "owner_projection_eligible": True,
            },
        )
    )

    second = await registry.build(account_id="owner-account")

    assert first.manifest.source_summary.memory_claim_count == 1
    assert second.manifest.source_summary.memory_claim_count == 0
    assert second.manifest.source_summary.persona_trait_count == 1
    assert "owner memory" not in str(second.manifest)


@pytest.mark.asyncio
async def test_state_machine_preconditions_and_rollback_are_fail_closed(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)

    with pytest.raises(SourceSnapshotConflictError):
        await registry.build(
            account_id="owner-account",
            expected_source_summary_sha256="0" * 64,
        )
    assert await registry.list(account_id="owner-account") == ()

    first = await registry.build(account_id="owner-account")
    with pytest.raises(InvalidVersionTransitionError):
        await registry.approve(
            account_id="owner-account",
            version_id=first.version_id,
            expected_manifest_sha256=first.manifest_sha256,
        )
    with pytest.raises(SourceSnapshotConflictError):
        await registry.begin_testing(
            account_id="owner-account",
            version_id=first.version_id,
            expected_manifest_sha256="0" * 64,
        )
    assert (await registry.get(account_id="owner-account", version_id=first.version_id)).status == (
        "draft"
    )

    testing = await registry.begin_testing(
        account_id="owner-account",
        version_id=first.version_id,
        expected_manifest_sha256=first.manifest_sha256,
    )
    approved = await registry.approve(
        account_id="owner-account",
        version_id=first.version_id,
        expected_manifest_sha256=first.manifest_sha256,
    )
    frozen = await registry.freeze(
        account_id="owner-account",
        version_id=first.version_id,
        expected_manifest_sha256=first.manifest_sha256,
    )
    revoked = await registry.revoke(
        account_id="owner-account",
        version_id=first.version_id,
        expected_manifest_sha256=first.manifest_sha256,
    )
    assert [testing.status, approved.status, frozen.status, revoked.status] == [
        "testing",
        "approved",
        "frozen",
        "revoked",
    ]
    assert {version.manifest_sha256 for version in (testing, approved, frozen, revoked)} == {
        first.manifest_sha256
    }
    with pytest.raises(InvalidVersionTransitionError):
        await registry.revoke(
            account_id="owner-account",
            version_id=first.version_id,
            expected_manifest_sha256=first.manifest_sha256,
        )

    await _correct_owner_memory(postgres_database, "new source value")
    second = await registry.build(
        account_id="owner-account",
        expected_source_summary_sha256=None,
    )
    with pytest.raises(SourceSnapshotConflictError):
        await registry.rollback(
            account_id="owner-account",
            target_version_id=first.version_id,
            expected_manifest_sha256="0" * 64,
        )
    assert len(await registry.list(account_id="owner-account")) == 2
    rollback = await registry.rollback(
        account_id="owner-account",
        target_version_id=first.version_id,
        expected_manifest_sha256=first.manifest_sha256,
    )
    assert rollback.status == "draft"
    assert rollback.version_number == 3
    assert rollback.manifest.parent_version_id == second.version_id
    assert rollback.manifest.rollback_target_version_id == first.version_id
    assert rollback.manifest.entries == first.manifest.entries
    assert (await registry.get(account_id="owner-account", version_id=first.version_id)).status == (
        "revoked"
    )


@pytest.mark.asyncio
async def test_cross_account_access_is_not_found_and_corruption_fails_closed(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    version = await registry.build(account_id="owner-account")

    for operation in (
        registry.get(account_id="other-account", version_id=version.version_id),
        registry.begin_testing(
            account_id="other-account",
            version_id=version.version_id,
            expected_manifest_sha256=version.manifest_sha256,
        ),
        registry.rollback(
            account_id="other-account",
            target_version_id=version.version_id,
            expected_manifest_sha256=version.manifest_sha256,
        ),
    ):
        with pytest.raises(VersionNotFoundError):
            await operation

    # Corrupt the stored digest past the immutability trigger, as only a
    # superuser with replication-role triggers disabled could.
    await _as_owner(
        postgres_database,
        ("SET LOCAL session_replication_role = replica", ()),
        (
            "UPDATE digital_self_versions SET source_summary_sha256 = $1 WHERE version_id = $2",
            ("f" * 64, uuid.UUID(version.version_id)),
        ),
    )
    with pytest.raises(ManifestIntegrityError):
        await registry.get(account_id="owner-account", version_id=version.version_id)


@pytest.mark.asyncio
async def test_account_governance_exports_and_deletes_digital_self_versions(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    version = await registry.build(account_id="owner-account")
    repository = PostgresAccountRepository.archive(postgres_database.role_dsn("memoria_app"))

    exported = await repository.export_account("owner-account")
    rows = exported["digital_self_versions"]
    assert len(rows) == 1
    assert rows[0]["manifest_sha256"] == version.manifest_sha256
    assert rows[0]["manifest"]["schema_version"] == "digital-self-manifest-v3"
    audit_rows = exported["digital_self_lifecycle_audit_events"]
    assert len(audit_rows) == 1
    assert audit_rows[0]["action"] == "build"
    assert audit_rows[0]["actor_account_id"] == "owner-account"
    assert audit_rows[0]["new_version_id"] == version.version_id

    deleted = await repository.delete_account("owner-account")
    assert deleted["digital_self_lifecycle_audit_events"] == 1
    assert deleted["digital_self_versions"] == 1
    assert await repository.remaining_account_rows("owner-account") == {}
    assert await registry.list(account_id="owner-account") == ()


@pytest.mark.asyncio
async def test_lifecycle_audit_is_account_scoped_immutable_and_complete(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    version = await registry.build(account_id="owner-account")
    await registry.begin_testing(
        account_id="owner-account",
        version_id=version.version_id,
        expected_manifest_sha256=version.manifest_sha256,
    )
    await registry.approve(
        account_id="owner-account",
        version_id=version.version_id,
        expected_manifest_sha256=version.manifest_sha256,
    )
    await registry.freeze(
        account_id="owner-account",
        version_id=version.version_id,
        expected_manifest_sha256=version.manifest_sha256,
    )
    await registry.revoke(
        account_id="owner-account",
        version_id=version.version_id,
        expected_manifest_sha256=version.manifest_sha256,
    )
    rollback = await registry.rollback(
        account_id="owner-account",
        target_version_id=version.version_id,
        expected_manifest_sha256=version.manifest_sha256,
    )

    connection = await asyncpg.connect(postgres_database.role_dsn("memoria_app"))
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.account_id', $1, true)", "owner-account"
            )
            records = await connection.fetch(
                """
                SELECT action, actor_account_id, version_id, manifest_sha256,
                       from_status, to_status, target_version_id, new_version_id, occurred_at
                FROM digital_self_lifecycle_audit_events
                WHERE account_id = $1 ORDER BY occurred_at
                """,
                "owner-account",
            )
        rows = [
            tuple(str(value) if isinstance(value, uuid.UUID) else value for value in record)
            for record in records
        ]
        assert [row[0] for row in rows] == [
            "build",
            "begin_testing",
            "approve",
            "freeze",
            "revoke",
            "rollback",
        ]
        assert all(row[1] == "owner-account" for row in rows)
        assert all(row[3] == version.manifest_sha256 for row in rows[:-1])
        assert rows[0][4:8] == (None, "draft", None, version.version_id)
        assert rows[-1][2:8] == (
            rollback.version_id,
            rollback.manifest_sha256,
            None,
            "draft",
            version.version_id,
            rollback.version_id,
        )
        with pytest.raises(asyncpg.PostgresError, match="immutable"):
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.account_id', $1, true)", "owner-account"
                )
                await connection.execute(
                    "UPDATE digital_self_lifecycle_audit_events SET action = 'build'"
                )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_transitions_require_a_well_formed_manifest_digest(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    version = await registry.build(account_id="owner-account")

    for digest in (None, "", "g" * 64, "a" * 63):
        with pytest.raises(ValueError, match="64 hexadecimal"):
            await registry.begin_testing(
                account_id="owner-account",
                version_id=version.version_id,
                expected_manifest_sha256=digest,  # type: ignore[arg-type]
            )
    with pytest.raises(TypeError):
        await registry.begin_testing(  # type: ignore[call-arg]
            account_id="owner-account", version_id=version.version_id
        )
    assert (
        await registry.get(account_id="owner-account", version_id=version.version_id)
    ).status == ("draft")


@pytest.mark.asyncio
async def test_v3_build_compiles_only_effective_self_model_entries(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    self_model: PostgresSelfModelRegistry,
    postgres_database: TestDatabase,
) -> None:
    await _seed_sources(archive, postgres_database)
    for event_id in ("owner-counterexample", "owner-negative"):
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id="owner-account",
                event_type="speech.utterance_finalized",
                occurred_at=_OCCURRED_AT,
                speaker_class="owner",
                source="registry-test",
                payload={
                    "text": event_id,
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": True,
                },
            )
        )
    await _as_owner(
        postgres_database,
        (
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES ($1, 'owner-account', 'friend:李梅', '李梅',
                      'friend', 'confirmed', 'owner-account-owner-source', $2)
            """,
            (uuid.UUID(_PERSON_ID), _OCCURRED_AT),
        ),
        (
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES ($1, 'owner-account', $2, 'friend', 'confirmed',
                      'owner-account-owner-source', $3)
            """,
            (uuid.UUID(_RELATIONSHIP_ID), uuid.UUID(_PERSON_ID), _OCCURRED_AT),
        ),
    )

    claim = await self_model.create_cognitive_claim(
        account_id="owner-account",
        claim_type="value",
        statement="家庭安全高于短期收益",
        confidence=0.95,
        idempotency_key="effective-claim",
    )
    claim = await self_model.add_source(
        account_id="owner-account",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="owner-account-owner-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=claim.version,
        idempotency_key="effective-claim-support",
    )
    claim = await self_model.add_source(
        account_id="owner-account",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="owner-counterexample",
        relation="counterexample",
        adopted=False,
        negative=False,
        expected_version=claim.version,
        idempotency_key="effective-claim-boundary",
    )
    claim = await self_model.review_cognitive_claim(
        account_id="owner-account",
        claim_id=claim.claim_id,
        status="confirmed",
        expected_version=claim.version,
        step_up_verified=True,
        idempotency_key="effective-claim-review",
    )

    decision = await self_model.create_decision_case(
        account_id="owner-account",
        kind="real",
        context="是否接受异地工作",
        options=("接受", "拒绝"),
        constraints=("家庭",),
        chosen_option="拒绝",
        rejected_options=("接受",),
        outcome="留在本地",
        reflection="家庭稳定更重要",
        still_endorsed=True,
        idempotency_key="effective-decision",
    )
    decision = await self_model.add_source(
        account_id="owner-account",
        item_kind="decision_case",
        item_id=decision.case_id,
        source_event_id="owner-account-owner-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=decision.version,
        idempotency_key="effective-decision-support",
    )
    await self_model.review_decision_case(
        account_id="owner-account",
        case_id=decision.case_id,
        status="confirmed",
        expected_version=decision.version,
        step_up_verified=False,
        idempotency_key="effective-decision-review",
    )

    hypothetical = await self_model.create_decision_case(
        account_id="owner-account",
        kind="hypothetical",
        context="假设移居海外",
        options=("去", "不去"),
        constraints=(),
        chosen_option="去",
        rejected_options=("不去",),
        outcome="",
        reflection="",
        still_endorsed=True,
        idempotency_key="hypothetical-decision",
    )
    hypothetical = await self_model.add_source(
        account_id="owner-account",
        item_kind="decision_case",
        item_id=hypothetical.case_id,
        source_event_id="owner-account-owner-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=hypothetical.version,
        idempotency_key="hypothetical-decision-support",
    )
    await self_model.review_decision_case(
        account_id="owner-account",
        case_id=hypothetical.case_id,
        status="confirmed",
        expected_version=hypothetical.version,
        step_up_verified=False,
        idempotency_key="hypothetical-decision-review",
    )

    negative = await self_model.create_cognitive_claim(
        account_id="owner-account",
        claim_type="belief",
        statement="我总会选择最稳妥的方案",
        confidence=0.7,
        idempotency_key="negative-claim",
    )
    negative = await self_model.add_source(
        account_id="owner-account",
        item_kind="cognitive_claim",
        item_id=negative.claim_id,
        source_event_id="owner-account-owner-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=negative.version,
        idempotency_key="negative-claim-support",
    )
    negative = await self_model.add_source(
        account_id="owner-account",
        item_kind="cognitive_claim",
        item_id=negative.claim_id,
        source_event_id="owner-negative",
        relation="counterexample",
        adopted=False,
        negative=True,
        expected_version=negative.version,
        idempotency_key="negative-claim-negative",
    )
    await self_model.review_cognitive_claim(
        account_id="owner-account",
        claim_id=negative.claim_id,
        status="confirmed",
        expected_version=negative.version,
        step_up_verified=False,
        idempotency_key="negative-claim-review",
    )
    candidate = await self_model.create_cognitive_claim(
        account_id="owner-account",
        claim_type="belief",
        statement="仍待本人确认的候选声明",
        confidence=0.6,
        idempotency_key="candidate-claim",
    )

    missing_counterexample = await self_model.create_cognitive_claim(
        account_id="owner-account",
        claim_type="red_line",
        statement="不为收益牺牲家人安全",
        confidence=0.9,
        idempotency_key="missing-counterexample",
    )
    missing_counterexample = await self_model.add_source(
        account_id="owner-account",
        item_kind="cognitive_claim",
        item_id=missing_counterexample.claim_id,
        source_event_id="owner-account-owner-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=missing_counterexample.version,
        idempotency_key="missing-counterexample-support",
    )
    # Confirmed and step-up verified without the owner counterexample a
    # red line needs: only a direct write can produce this row.
    await _as_owner(
        postgres_database,
        (
            """
            UPDATE self_model_cognitive_claims
            SET status = 'confirmed', owner_reviewed_at = $1,
                step_up_verified = true, version = version + 1, updated_at = $1
            WHERE claim_id = $2
            """,
            (_OCCURRED_AT, uuid.UUID(missing_counterexample.claim_id)),
        ),
    )

    profile = await self_model.create_relationship_profile(
        account_id="owner-account",
        person_id=_PERSON_ID,
        relationship_id=_RELATIONSHIP_ID,
        salutation="梅姐",
        tone="坦诚",
        advice_style="先听再建议",
        boundaries=("不谈财务细节",),
        idempotency_key="effective-profile",
        sharing_scope="family",
    )
    profile = await self_model.add_source(
        account_id="owner-account",
        item_kind="relationship_profile",
        item_id=profile.profile_id,
        source_event_id="owner-account-owner-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=profile.version_number,
        idempotency_key="effective-profile-support",
    )
    await self_model.review_relationship_profile(
        account_id="owner-account",
        profile_id=profile.profile_id,
        version_number=profile.version_number,
        status="approved",
        expected_status="candidate",
        step_up_verified=True,
        idempotency_key="effective-profile-review",
    )
    unapproved = await self_model.create_relationship_profile(
        account_id="owner-account",
        person_id=_PERSON_ID,
        relationship_id=_RELATIONSHIP_ID,
        salutation="李梅",
        tone="正式",
        advice_style="只回答问题",
        boundaries=(),
        idempotency_key="unapproved-profile",
    )

    version = await registry.build(account_id="owner-account")
    self_model_entries = tuple(
        entry
        for entry in version.manifest.entries
        if isinstance(
            entry,
            (
                CognitiveClaimManifestEntry,
                DecisionCaseManifestEntry,
                RelationshipProfileManifestEntry,
            ),
        )
    )

    assert [type(entry) for entry in self_model_entries] == [
        CognitiveClaimManifestEntry,
        DecisionCaseManifestEntry,
        RelationshipProfileManifestEntry,
    ]
    assert claim.claim_id in str(self_model_entries)
    assert decision.case_id in str(self_model_entries)
    assert profile.profile_id in str(self_model_entries)
    assert hypothetical.case_id not in str(version.manifest)
    assert negative.claim_id not in str(version.manifest)
    assert candidate.claim_id not in str(version.manifest)
    assert missing_counterexample.claim_id not in str(version.manifest)
    assert unapproved.profile_id not in str(version.manifest)
    assert version.manifest.source_summary.cognitive_claim_count == 1
    assert version.manifest.source_summary.decision_case_count == 1
    assert version.manifest.source_summary.relationship_profile_count == 1


@pytest.mark.asyncio
async def test_rollback_of_v1_creates_v3_without_mutating_old_bytes(
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    entry = MemoryClaimManifestEntry(
        claim_id="legacy-memory",
        category="life_story",
        subject_key="owner",
        predicate="prefers",
        value="tea",
        confidence=0.9,
        sensitive_domain="personal",
        extractor_version="extractor-v1",
        source_event_id="legacy-source",
        valid_at="2026-07-22T08:00:00+00:00",
    )
    source_sha256 = sha256_hex(
        canonical_json_bytes(
            {
                "entries": [entry_dict(entry)],
                "persona_version_id": None,
            }
        )
    )
    legacy_manifest = DigitalSelfManifest(
        schema_version="digital-self-manifest-v1",
        compiler_version="digital-self-compiler-v1",
        policy_version="digital-self-policy-v1",
        parent_version_id=None,
        rollback_target_version_id=None,
        entries=(entry,),
        source_summary=DigitalSelfSourceSummary(
            memory_claim_count=1,
            persona_trait_count=0,
            persona_version_id=None,
            source_summary_sha256=source_sha256,
        ),
    )
    legacy_bytes = canonical_manifest_bytes(legacy_manifest)
    legacy_digest = sha256_hex(legacy_bytes)
    await _as_owner(
        postgres_database,
        (
            """
            INSERT INTO digital_self_versions (
                version_id, account_id, version_number, status, manifest_json,
                manifest_sha256, source_summary_sha256, parent_version_id,
                rollback_target_version_id, created_at
            ) VALUES ($1, 'owner-account', 1, 'revoked', $2, $3, $4, NULL, NULL, $5)
            """,
            (
                uuid.UUID(_LEGACY_VERSION_ID),
                legacy_bytes.decode(),
                legacy_digest,
                source_sha256,
                _OCCURRED_AT,
            ),
        ),
    )

    rollback = await registry.rollback(
        account_id="owner-account",
        target_version_id=_LEGACY_VERSION_ID,
        expected_manifest_sha256=legacy_digest,
    )
    reloaded = await registry.get(
        account_id="owner-account",
        version_id=_LEGACY_VERSION_ID,
    )

    assert rollback.manifest.schema_version == "digital-self-manifest-v3"
    assert rollback.manifest.entries == (entry,)
    assert rollback.manifest.rollback_target_version_id == _LEGACY_VERSION_ID
    assert reloaded.manifest.schema_version == "digital-self-manifest-v1"
    assert canonical_manifest_bytes(reloaded.manifest) == legacy_bytes
    assert reloaded.manifest_sha256 == legacy_digest


@pytest.mark.asyncio
async def test_a_bound_childs_claims_stay_out_of_the_account_holders_digital_self(
    archive: PostgresLifeArchive,
    registry: PostgresDigitalSelfRegistry,
    postgres_database: TestDatabase,
) -> None:
    """P2-03: the child's turns live in the binder's account but are not theirs."""

    await _seed_sources(archive, postgres_database)
    await archive.record(
        EvidenceEvent(
            event_id="owner-account-child-source",
            account_id="owner-account",
            subject_id="person-child",
            event_type="speech.utterance_finalized",
            occurred_at=_OCCURRED_AT,
            speaker_class="owner",
            source="registry-test",
            payload={
                "text": "child material",
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )
    await _as_owner(
        postgres_database,
        (
            _INSERT_MEMORY_CLAIM,
            (
                uuid.UUID("00000000-0000-0000-0000-0000000000c1"),
                "owner-account",
                "life_story",
                "preference",
                "child memory",
                0.9,
                "confirmed",
                "owner-account-child-source",
                _OCCURRED_AT,
            ),
        ),
    )

    version = await registry.build(account_id="owner-account")

    values = [
        entry.value
        for entry in version.manifest.entries
        if isinstance(entry, MemoryClaimManifestEntry)
    ]
    assert values == ["owner memory"]
    assert "child memory" not in str(version.manifest)
