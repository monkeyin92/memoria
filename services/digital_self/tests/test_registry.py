from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.digital_self.domain import (
    EmptyDigitalSelfSourceError,
    InvalidVersionTransitionError,
    ManifestIntegrityError,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    SourceSnapshotConflictError,
    VersionNotFoundError,
)
from services.digital_self.registry import DigitalSelfRegistry
from services.governance.account_data import SqliteAccountRepository

_OCCURRED_AT = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


async def _seed_sources(path: Path, *, account_id: str = "owner-account") -> None:
    archive = LifeArchive.sqlite(path)
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

    now = _OCCURRED_AT.isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for index, speaker_class in enumerate(("owner", "guest", "uncertain", "assistant")):
            connection.execute(
                """
                INSERT INTO memory_claims (
                    claim_id, account_id, category, subject_key, predicate,
                    value, confidence, status, sensitive_domain,
                    extractor_version, source_event_id, valid_at, created_at
                ) VALUES (?, ?, 'life_story', 'owner', 'preference', ?, 0.9,
                          'confirmed', 'personal', 'extractor-v1', ?, ?, ?)
                """,
                (
                    f"00000000-0000-0000-0000-00000000000{index}",
                    account_id,
                    f"{speaker_class} memory",
                    f"{account_id}-{speaker_class}-source",
                    now,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO memory_claims (
                claim_id, account_id, category, subject_key, predicate,
                value, confidence, status, sensitive_domain,
                extractor_version, source_event_id, valid_at, created_at
            ) VALUES ('00000000-0000-0000-0000-000000000099', ?, 'daily_life',
                      'owner', 'candidate', 'not confirmed', 0.7, 'candidate',
                      'personal', 'extractor-v1', ?, ?, ?)
            """,
            (account_id, f"{account_id}-owner-source", now, now),
        )
        connection.execute(
            """
            INSERT INTO memory_claims (
                claim_id, account_id, category, subject_key, predicate,
                value, confidence, status, sensitive_domain,
                extractor_version, source_event_id, valid_at, created_at
            ) VALUES ('00000000-0000-0000-0000-000000000098', ?, 'daily_life',
                      'owner', 'companion', 'misclassified companion memory', 0.9,
                      'confirmed', 'personal', 'extractor-v1', ?, ?, ?)
            """,
            (account_id, f"{account_id}-companion-source", now, now),
        )

        snapshot: list[dict[str, object]] = []
        for index, speaker_class in enumerate(("owner", "guest", "uncertain", "assistant")):
            trait_id = f"10000000-0000-0000-0000-00000000000{index}"
            description = f"{speaker_class} trait"
            connection.execute(
                """
                INSERT INTO persona_traits (
                    trait_id, account_id, category, normalized_key, description,
                    context, counterexample, confidence, status, observation_count,
                    created_at, updated_at
                ) VALUES (?, ?, 'verbal_tic', ?, ?, 'conversation', '', 0.9,
                          'confirmed', 3, ?, ?)
                """,
                (trait_id, account_id, speaker_class, description, now, now),
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
        connection.execute(
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason,
                snapshot_json, parent_version_id, created_at
            ) VALUES ('20000000-0000-0000-0000-000000000001', ?, 1, 'active',
                      'test', ?, NULL, ?)
            """,
            (account_id, json.dumps(snapshot, ensure_ascii=False), now),
        )


@pytest.mark.asyncio
async def test_build_rejects_an_empty_owner_source_snapshot(tmp_path: Path) -> None:
    registry = DigitalSelfRegistry.sqlite(tmp_path / "memoria.sqlite3")
    registry.initialize()

    with pytest.raises(EmptyDigitalSelfSourceError):
        await registry.build(account_id="empty-account")


@pytest.mark.asyncio
async def test_build_is_deterministic_and_only_compiles_confirmed_owner_sources(
    tmp_path: Path,
) -> None:
    versions = []
    for name in ("first.sqlite3", "second.sqlite3"):
        path = tmp_path / name
        registry = DigitalSelfRegistry.sqlite(path)
        registry.initialize()
        await _seed_sources(path)
        versions.append(await registry.build(account_id="owner-account"))

    first, second = versions
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
    assert first.manifest.source_summary.memory_claim_count == 1
    assert first.manifest.source_summary.persona_trait_count == 1
    assert (
        first.manifest.source_summary.persona_version_id == "20000000-0000-0000-0000-000000000001"
    )


@pytest.mark.asyncio
async def test_source_correction_creates_a_new_version_without_mutating_old_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)
    first = await registry.build(account_id="owner-account")

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE memory_claims SET value = 'corrected owner memory'
            WHERE account_id = 'owner-account'
              AND claim_id = '00000000-0000-0000-0000-000000000000'
            """
        )
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

    with (
        sqlite3.connect(path) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="immutable",
        ),
    ):
        connection.execute(
            """
            UPDATE digital_self_versions SET manifest_json = '{}'
            WHERE version_id = ?
            """,
            (first.version_id,),
        )


@pytest.mark.asyncio
async def test_negative_owner_evidence_excludes_target_from_the_next_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)
    first = await registry.build(account_id="owner-account")
    archive = LifeArchive.sqlite(path)
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
                "target_id": "00000000-0000-0000-0000-000000000000",
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
async def test_state_machine_preconditions_and_rollback_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)

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

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE memory_claims SET value = 'new source value'
            WHERE account_id = 'owner-account'
              AND claim_id = '00000000-0000-0000-0000-000000000000'
            """
        )
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
    tmp_path: Path,
) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)
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

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER digital_self_manifest_immutable")
        connection.execute(
            """
            UPDATE digital_self_versions SET source_summary_sha256 = ?
            WHERE version_id = ?
            """,
            ("f" * 64, version.version_id),
        )
    with pytest.raises(ManifestIntegrityError):
        await registry.get(account_id="owner-account", version_id=version.version_id)


@pytest.mark.asyncio
async def test_account_governance_exports_and_deletes_digital_self_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)
    version = await registry.build(account_id="owner-account")
    repository = SqliteAccountRepository.archive(path)

    exported = await repository.export_account("owner-account")
    rows = exported["digital_self_versions"]
    assert len(rows) == 1
    assert rows[0]["manifest_sha256"] == version.manifest_sha256
    assert rows[0]["manifest"]["schema_version"] == "digital-self-manifest-v1"
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
async def test_lifecycle_audit_is_account_scoped_immutable_and_complete(tmp_path: Path) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)
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

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT action, actor_account_id, version_id, manifest_sha256,
                   from_status, to_status, target_version_id, new_version_id, occurred_at
            FROM digital_self_lifecycle_audit_events
            WHERE account_id = ? ORDER BY rowid
            """,
            ("owner-account",),
        ).fetchall()
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
        assert rows[0][4:] == (None, "draft", None, version.version_id, rows[0][8])
        assert rows[-1][2:8] == (
            rollback.version_id,
            rollback.manifest_sha256,
            None,
            "draft",
            version.version_id,
            rollback.version_id,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE digital_self_lifecycle_audit_events SET action = 'build'"
            )


@pytest.mark.asyncio
async def test_transitions_require_a_well_formed_manifest_digest(tmp_path: Path) -> None:
    path = tmp_path / "memoria.sqlite3"
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    await _seed_sources(path)
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
    assert (await registry.get(account_id="owner-account", version_id=version.version_id)).status == (
        "draft"
    )
