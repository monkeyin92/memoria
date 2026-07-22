from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.digital_self.domain import (
    EmptyDigitalSelfSourceError,
    MemoryClaimManifestEntry,
    SourceSnapshotConflictError,
    VersionNotFoundError,
)
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.governance.account_data import PostgresAccountRepository
from services.persona.postgres_engine import PostgresPersonaEngine


def test_postgres_schema_forces_rls_and_manifest_immutability() -> None:
    schema = Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(encoding="utf-8")

    assert "ALTER TABLE digital_self_versions ENABLE ROW LEVEL SECURITY" in schema
    assert "ALTER TABLE digital_self_versions FORCE ROW LEVEL SECURITY" in schema
    assert "CREATE POLICY digital_self_version_account_policy" in schema
    assert "digital_self_manifest_immutable_guard" in schema
    assert "ALTER TABLE digital_self_lifecycle_audit_events FORCE ROW LEVEL SECURITY" in schema
    assert "digital_self_lifecycle_audit_immutable_guard" in schema


def test_postgres_registry_rejects_non_postgres_dsn() -> None:
    try:
        PostgresDigitalSelfRegistry("sqlite:///tmp/memoria.sqlite3")
    except ValueError as exc:
        assert "PostgreSQL" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("non-PostgreSQL DSN was accepted")


def test_postgres_registry_rejects_missing_or_malformed_transition_digests() -> None:
    registry = PostgresDigitalSelfRegistry("postgresql://localhost/memoria")
    version = SimpleNamespace(manifest_sha256="a" * 64)

    for digest in (None, "", "g" * 64, "a" * 63):
        with pytest.raises(ValueError, match="64 hexadecimal"):
            registry._check_digest(version, digest)  # type: ignore[arg-type]


def _postgres_dsn(dsn: str, *, user: str, password: str, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{quote(user)}:{quote(password)}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL Digital Self contract",
)
async def test_postgres_registry_enforces_rls_crud_immutability_and_governance() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    database = f"memoria_digital_self_{suffix}"
    app_role = f"memoria_digital_self_{suffix}"
    app_password = f"digital-self-{suffix}-password"
    account_a = f"digital-self-a-{suffix}"
    account_b = f"digital-self-b-{suffix}"
    admin = await asyncpg.connect(admin_dsn)
    archive: PostgresLifeArchive | None = None
    catalog: PostgresMemoryCatalog | None = None
    persona: PostgresPersonaEngine | None = None
    registry: PostgresDigitalSelfRegistry | None = None
    try:
        await admin.execute(
            f"CREATE ROLE \"{app_role}\" LOGIN PASSWORD '{app_password}' NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{app_role}"')
        app_dsn = _postgres_dsn(
            admin_dsn,
            user=app_role,
            password=app_password,
            database=database,
        )
        archive = PostgresLifeArchive(app_dsn)
        catalog = PostgresMemoryCatalog(app_dsn, extractor=RuleBasedMemoryExtractor())
        persona = PostgresPersonaEngine(app_dsn)
        registry = PostgresDigitalSelfRegistry(app_dsn)
        await archive.initialize()
        await catalog.initialize()
        await persona.initialize()
        await registry.initialize()

        connection = await asyncpg.connect(app_dsn)
        try:
            for account_id in (account_a, account_b):
                event_id = f"source-{account_id}"
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.account_id', $1, true)", account_id
                    )
                    await connection.execute(
                        """
                        INSERT INTO archive_evidence_events (
                            event_id, account_id, event_type, schema_version, occurred_at,
                            speaker_class, source, payload, content_sha256
                        ) VALUES ($1, $2, 'speech.utterance_finalized', 1, $3,
                                  'owner', 'digital-self-test', $4::jsonb, $5)
                        """,
                        event_id,
                        account_id,
                        datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
                        '{"text":"confirmed owner source","interaction_mode":"companion","prompt_kind":"spontaneous","owner_projection_eligible":true}',
                        "a" * 64,
                    )
                    await connection.execute(
                        """
                        INSERT INTO memory_claims (
                            claim_id, account_id, category, subject_key, predicate,
                            value, confidence, status, sensitive_domain,
                            extractor_version, source_event_id, valid_at
                        ) VALUES ($1, $2, 'life_story', 'owner', 'preference', $3,
                                  0.9, 'confirmed', 'personal', 'extractor-v1', $4, $5)
                        """,
                        uuid.uuid4(),
                        account_id,
                        f"owner memory {account_id}",
                        event_id,
                        datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
                    )

            assert await connection.fetchval("SELECT count(*) FROM memory_claims") == 0
            assert await connection.fetchval("SELECT count(*) FROM digital_self_versions") == 0
        finally:
            await connection.close()

        version_a = await registry.build(account_id=account_a)
        version_b = await registry.build(account_id=account_b)
        claim_a_id = next(
            entry.claim_id
            for entry in version_a.manifest.entries
            if isinstance(entry, MemoryClaimManifestEntry)
        )
        await archive.record(
            EvidenceEvent(
                event_id=f"negative-{account_a}",
                account_id=account_a,
                event_type="owner.action_recorded",
                occurred_at=datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
                speaker_class="owner",
                source="user.growth_feedback",
                payload={
                    "action_type": "not_me",
                    "target_kind": "memory_claim",
                    "target_id": claim_a_id,
                    "owner_projection_eligible": True,
                },
            )
        )
        with pytest.raises(EmptyDigitalSelfSourceError):
            await registry.build(account_id=account_a)
        with pytest.raises(VersionNotFoundError):
            await registry.get(account_id=account_b, version_id=version_a.version_id)
        with pytest.raises(SourceSnapshotConflictError):
            await registry.begin_testing(
                account_id=account_a,
                version_id=version_a.version_id,
                expected_manifest_sha256="0" * 64,
            )
        assert (
            await registry.get(account_id=account_a, version_id=version_a.version_id)
        ).status == "draft"
        await registry.begin_testing(
            account_id=account_a,
            version_id=version_a.version_id,
            expected_manifest_sha256=version_a.manifest_sha256,
        )
        await registry.approve(
            account_id=account_a,
            version_id=version_a.version_id,
            expected_manifest_sha256=version_a.manifest_sha256,
        )
        await registry.freeze(
            account_id=account_a,
            version_id=version_a.version_id,
            expected_manifest_sha256=version_a.manifest_sha256,
        )
        await registry.revoke(
            account_id=account_a,
            version_id=version_a.version_id,
            expected_manifest_sha256=version_a.manifest_sha256,
        )
        rolled_back = await registry.rollback(
            account_id=account_a,
            target_version_id=version_a.version_id,
            expected_manifest_sha256=version_a.manifest_sha256,
        )
        assert rolled_back.status == "draft"
        assert rolled_back.manifest.parent_version_id == version_a.version_id
        assert rolled_back.manifest.rollback_target_version_id == version_a.version_id

        connection = await asyncpg.connect(app_dsn)
        try:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_a)
                assert await connection.fetchval("SELECT count(*) FROM memory_claims") == 1
                assert await connection.fetchval("SELECT count(*) FROM digital_self_versions") == 2
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM digital_self_lifecycle_audit_events"
                    )
                    == 6
                )
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM digital_self_versions WHERE account_id = $1",
                        account_b,
                    )
                    == 0
                )
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.account_id', $1, true)", account_a
                    )
                    await connection.execute(
                        """
                        UPDATE digital_self_versions SET manifest_json = '{}'
                        WHERE version_id = $1
                        """,
                        uuid.UUID(version_a.version_id),
                    )
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                async with connection.transaction():
                    await connection.execute("SELECT set_config('app.account_id', $1, true)", account_a)
                    await connection.execute(
                        "UPDATE digital_self_lifecycle_audit_events SET action = 'build'"
                    )
            rls = await connection.fetchrow(
                """
                SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                WHERE relname = 'digital_self_versions'
                """
            )
            assert rls is not None
            assert rls["relrowsecurity"] and rls["relforcerowsecurity"]
            audit_rls = await connection.fetchrow(
                """
                SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                WHERE relname = 'digital_self_lifecycle_audit_events'
                """
            )
            assert audit_rls is not None
            assert audit_rls["relrowsecurity"] and audit_rls["relforcerowsecurity"]
        finally:
            await connection.close()

        governance = PostgresAccountRepository.archive(app_dsn)
        exported = await governance.export_account(account_a)
        assert len(exported["digital_self_versions"]) == 2
        assert version_a.manifest_sha256 in str(exported["digital_self_versions"])
        assert exported["digital_self_versions"][0]["manifest"]["schema_version"] == (
            "digital-self-manifest-v1"
        )
        assert len(exported["digital_self_lifecycle_audit_events"]) == 6
        deleted = await governance.delete_account(account_a)
        assert deleted["digital_self_lifecycle_audit_events"] == 6
        assert deleted["digital_self_versions"] == 2
        assert await governance.remaining_account_rows(account_a) == {}
        assert (
            await registry.get(account_id=account_b, version_id=version_b.version_id)
        ).account_id == (account_b)
    finally:
        if registry is not None:
            await registry.close()
        if persona is not None:
            await persona.close()
        if catalog is not None:
            await catalog.close()
        if archive is not None:
            await archive.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{app_role}"')
        await admin.close()
