from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.growth.postgres_reader import PostgresGrowthReader
from services.persona.postgres_engine import PostgresPersonaEngine


def _dsn(dsn: str, *, user: str, password: str, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{quote(user)}:{quote(password)}@{host}", f"/{database}", parsed.query, ""))


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("MEMORIA_TEST_POSTGRES_DSN"), reason="requires PostgreSQL")
async def test_postgres_reader_matches_sqlite_semantics_and_respects_force_rls() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    role = f"memoria_growth_{suffix}"
    database = f"memoria_growth_{suffix}"
    password = f"growth-{suffix}"
    account_a, account_b = f"growth-a-{suffix}", f"growth-b-{suffix}"
    admin = await asyncpg.connect(admin_dsn)
    archive: PostgresLifeArchive | None = None
    catalog: PostgresMemoryCatalog | None = None
    persona: PostgresPersonaEngine | None = None
    registry: PostgresDigitalSelfRegistry | None = None
    reader: PostgresGrowthReader | None = None
    try:
        await admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS")
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{role}"')
        app_dsn = _dsn(admin_dsn, user=role, password=password, database=database)
        archive = PostgresLifeArchive(app_dsn)
        catalog = PostgresMemoryCatalog(app_dsn, extractor=RuleBasedMemoryExtractor())
        persona = PostgresPersonaEngine(app_dsn)
        registry = PostgresDigitalSelfRegistry(app_dsn)
        reader = PostgresGrowthReader(app_dsn)
        await archive.initialize()
        await catalog.initialize()
        await persona.initialize()
        await registry.initialize()
        connection = await asyncpg.connect(app_dsn)
        try:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_a)
                await connection.execute(
                    """INSERT INTO archive_evidence_events (event_id, account_id, event_type, schema_version, occurred_at, speaker_class, source, payload, content_sha256)
                       VALUES ('growth-source', $1, 'speech.utterance_finalized', 1, $2, 'owner', 'test', '{"text":"杭州读书","owner_projection_eligible":true}'::jsonb, $3)""",
                    account_a, datetime(2026, 7, 22, tzinfo=UTC), "a" * 64,
                )
                first_claim_id = uuid.uuid4()
                second_claim_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO memory_claims (claim_id, account_id, category, subject_key, predicate, value, confidence, status, sensitive_domain, extractor_version, source_event_id, valid_at)
                       VALUES ($1, $2, 'life_story', 'self', 'education', '杭州读书', .9, 'confirmed', 'personal', 'test', 'growth-source', $3)""",
                    first_claim_id, account_a, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO memory_claims (claim_id, account_id, category, subject_key, predicate, value, confidence, status, sensitive_domain, extractor_version, source_event_id, valid_at)
                       VALUES ($1, $2, 'life_story', 'self', 'city', '住在杭州', .9, 'confirmed', 'personal', 'test', 'growth-source', $3)""",
                    second_claim_id, account_a, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO archive_evidence_events (event_id, account_id, event_type, schema_version, occurred_at, speaker_class, source, payload, content_sha256)
                       VALUES ('growth-ineligible', $1, 'speech.utterance_finalized', 1, $2, 'owner', 'test', '{"text":"不应采用","owner_projection_eligible":false}'::jsonb, $3)""",
                    account_a, datetime(2026, 7, 22, tzinfo=UTC), "b" * 64,
                )
                person_id = uuid.uuid4()
                episode_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO person_entities (person_id, account_id, canonical_key, display_name, relationship_to_owner, status, source_event_id, created_at)
                       VALUES ($1, $2, 'friend:小林', '小林', 'friend', 'confirmed', 'growth-ineligible', $3)""",
                    person_id, account_a, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO relationships (relationship_id, account_id, person_id, relationship_type, status, source_event_id, valid_at)
                       VALUES ($1, $2, $3, 'friend', 'confirmed', 'growth-ineligible', $4)""",
                    uuid.uuid4(), account_a, person_id, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO life_episodes (episode_id, account_id, title, category, status, event_start, source_event_id)
                       VALUES ($1, $2, '一段经历', 'life_story', 'confirmed', $3, 'growth-ineligible')""",
                    episode_id, account_a, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO timeline_entries (timeline_id, account_id, episode_id, title, category, status, event_start, time_precision, source_event_id)
                       VALUES ($1, $2, $3, '一段经历', 'life_story', 'confirmed', $4, 'day', 'growth-ineligible')""",
                    uuid.uuid4(), account_a, episode_id, datetime(2026, 7, 22, tzinfo=UTC),
                )
            assert await connection.fetchval("SELECT count(*) FROM memory_claims") == 0
        finally:
            await connection.close()
        draft = await registry.build(account_id=account_a)
        testing = await registry.begin_testing(
            account_id=account_a,
            version_id=draft.version_id,
            expected_manifest_sha256=draft.manifest_sha256,
        )
        await registry.approve(
            account_id=account_a,
            version_id=testing.version_id,
            expected_manifest_sha256=testing.manifest_sha256,
        )
        overview_a = await reader.overview(account_id=account_a)
        overview_b = await reader.overview(account_id=account_b)
        life_a = next(item for item in overview_a["dimensions"] if item["key"] == "life_chapters")
        life_b = next(item for item in overview_b["dimensions"] if item["key"] == "life_chapters")
        assert life_a["status"] == "supported"
        assert [source["event_id"] for source in life_a["adopted_sources"]] == [
            "growth-source"
        ]
        assert life_a["rejected_reason_counts"] == {
            "owner_projection_ineligible": 1
        }
        assert life_a["version_readiness"]["status"] == "ready"
        assert set(life_a["adopted_sources"][0]) == {"kind", "target_kind", "target_id", "event_id", "label", "weight", "occurred_at", "event_type"}
        relationship_a = next(
            item
            for item in overview_a["dimensions"]
            if item["key"] == "relationship_models"
        )
        assert relationship_a["rejected_reason_counts"] == {
            "owner_projection_ineligible": 1
        }
        assert life_b["status"] == "empty"

        await archive.record(
            EvidenceEvent(
                event_id=f"growth-hidden-not-me-{suffix}",
                account_id=account_a,
                event_type="owner.action_recorded",
                occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
                speaker_class="owner",
                source="user.growth_feedback",
                payload={
                    "action_type": "not_me",
                    "target_kind": "memory_claim",
                    "target_id": str(second_claim_id),
                    "owner_projection_eligible": True,
                },
            )
        )
        conflicted = await reader.overview(account_id=account_a)
        conflicted_life = next(
            item
            for item in conflicted["dimensions"]
            if item["key"] == "life_chapters"
        )
        assert conflicted_life["status"] == "conflicted"
        assert conflicted_life["conflicts"][0]["target_id"] == str(second_claim_id)
        assert conflicted_life["version_readiness"]["status"] == "stale"

        replacement = await registry.build(account_id=account_a)
        replacement_testing = await registry.begin_testing(
            account_id=account_a,
            version_id=replacement.version_id,
            expected_manifest_sha256=replacement.manifest_sha256,
        )
        await registry.approve(
            account_id=account_a,
            version_id=replacement_testing.version_id,
            expected_manifest_sha256=replacement_testing.manifest_sha256,
        )
        refreshed = await reader.overview(account_id=account_a)
        refreshed_life = next(
            item
            for item in refreshed["dimensions"]
            if item["key"] == "life_chapters"
        )
        assert refreshed_life["status"] == "conflicted"
        assert refreshed_life["version_readiness"]["status"] == "ready"

        connection = await asyncpg.connect(app_dsn)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.account_id', $1, true)",
                    account_a,
                )
                await connection.execute(
                    """
                    UPDATE memory_claims SET status = 'retracted'
                    WHERE account_id = $1 AND claim_id = $2
                    """,
                    account_a,
                    first_claim_id,
                )
        finally:
            await connection.close()
        stale = await reader.overview(account_id=account_a)
        stale_life = next(
            item for item in stale["dimensions"] if item["key"] == "life_chapters"
        )
        assert stale_life["version_readiness"]["status"] == "stale"
    finally:
        if reader is not None:
            await reader.close()
        if registry is not None:
            await registry.close()
        if persona is not None:
            await persona.close()
        if catalog is not None:
            await catalog.close()
        if archive is not None:
            await archive.close()
        await admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", database)
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()
