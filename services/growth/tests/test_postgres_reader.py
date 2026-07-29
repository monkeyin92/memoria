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
from services.self_model.postgres_registry import PostgresSelfModelRegistry


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
    self_model: PostgresSelfModelRegistry | None = None
    reader: PostgresGrowthReader | None = None
    try:
        await admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS")
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{role}"')
        app_dsn = _dsn(admin_dsn, user=role, password=password, database=database)
        archive = PostgresLifeArchive(app_dsn)
        catalog = PostgresMemoryCatalog(app_dsn, extractor=RuleBasedMemoryExtractor())
        persona = PostgresPersonaEngine(app_dsn)
        registry = PostgresDigitalSelfRegistry(app_dsn)
        self_model = PostgresSelfModelRegistry(app_dsn)
        await archive.initialize()
        await catalog.initialize()
        await persona.initialize()
        await registry.initialize()
        await self_model.initialize()
        reader = PostgresGrowthReader(
            app_dsn,
            self_model_registry=self_model,
        )
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
                    """INSERT INTO memory_claims (
                           claim_id, account_id, category, domain_category,
                           subject_key, predicate, value, confidence, status,
                           sensitive_domain, extractor_version, source_event_id,
                           valid_at, observed_at
                       ) VALUES (
                           $1, $2, 'life_story', 'life_story', 'self',
                           'education', '杭州读书', .9, 'confirmed', 'personal',
                           'test', 'growth-source', $3, $3
                       )""",
                    first_claim_id, account_a, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO memory_claims (
                           claim_id, account_id, category, domain_category,
                           subject_key, predicate, value, confidence, status,
                           sensitive_domain, extractor_version, source_event_id,
                           valid_at, observed_at
                       ) VALUES (
                           $1, $2, 'life_story', 'life_story', 'self', 'city',
                           '住在杭州', .9, 'confirmed', 'personal', 'test',
                           'growth-source', $3, $3
                       )""",
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
                eligible_person_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO person_entities (person_id, account_id, canonical_key, display_name, relationship_to_owner, status, source_event_id, created_at)
                       VALUES ($1, $2, 'friend:阿青', '阿青', 'friend', 'confirmed', 'growth-source', $3)""",
                    eligible_person_id,
                    account_a,
                    datetime(2026, 7, 22, tzinfo=UTC),
                )
                eligible_relationship_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO relationships (relationship_id, account_id, person_id, relationship_type, status, source_event_id, valid_at)
                       VALUES ($1, $2, $3, 'friend', 'confirmed', 'growth-source', $4)""",
                    eligible_relationship_id,
                    account_a,
                    eligible_person_id,
                    datetime(2026, 7, 22, tzinfo=UTC),
                )
                legacy_trait_id = uuid.uuid4()
                await connection.execute(
                    """INSERT INTO persona_traits (
                           trait_id, account_id, category, normalized_key,
                           description, context, counterexample, confidence,
                           status, observation_count
                       ) VALUES ($1, $2, 'decision_habit', 'legacy-decision',
                                 '旧 Persona 决策标签', 'conversation',
                                 '也会例外', .9, 'confirmed', 3)""",
                    legacy_trait_id,
                    account_a,
                )
                await connection.execute(
                    """INSERT INTO persona_evidence (
                           trait_id, account_id, source_event_id, scene,
                           weight, occurred_at
                       ) VALUES ($1, $2, 'growth-source', 'conversation', 1.0, $3)""",
                    legacy_trait_id,
                    account_a,
                    datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO life_episodes (
                           episode_id, account_id, title, category,
                           domain_category, consolidation_key, status,
                           event_start, observed_at, source_event_id
                       ) VALUES (
                           $1, $2, '一段经历', 'life_story', 'life_story',
                           'test:growth-ineligible', 'confirmed', $3, $3,
                           'growth-ineligible'
                       )""",
                    episode_id, account_a, datetime(2026, 7, 22, tzinfo=UTC),
                )
                await connection.execute(
                    """INSERT INTO timeline_entries (
                           timeline_id, account_id, episode_id, title, category,
                           domain_category, status, event_start, time_precision,
                           observed_at, source_event_id
                       ) VALUES (
                           $1, $2, $3, '一段经历', 'life_story', 'life_story',
                           'confirmed', $4, 'day', $4, 'growth-ineligible'
                       )""",
                    uuid.uuid4(), account_a, episode_id, datetime(2026, 7, 22, tzinfo=UTC),
                )
            assert await connection.fetchval("SELECT count(*) FROM memory_claims") == 0
        finally:
            await connection.close()
        claim = await self_model.create_cognitive_claim(
            account_id=account_a,
            claim_type="belief",
            statement="我相信先确认事实再判断。",
            confidence=0.9,
            idempotency_key="growth-pg-create-claim",
        )
        claim = await self_model.add_source(
            account_id=account_a,
            item_kind="cognitive_claim",
            item_id=claim.claim_id,
            source_event_id="growth-source",
            relation="support",
            adopted=True,
            negative=False,
            expected_version=claim.version,
            idempotency_key="growth-pg-source-claim",
        )
        await self_model.review_cognitive_claim(
            account_id=account_a,
            claim_id=claim.claim_id,
            status="confirmed",
            expected_version=claim.version,
            step_up_verified=False,
            idempotency_key="growth-pg-confirm-claim",
        )
        decision = await self_model.create_decision_case(
            account_id=account_a,
            kind="real",
            context="决定先确认事实",
            options=("直接决定", "先确认"),
            constraints=("时间有限",),
            chosen_option="先确认",
            rejected_options=("直接决定",),
            outcome="减少误判",
            reflection="仍然认同",
            still_endorsed=True,
            idempotency_key="growth-pg-create-decision",
        )
        decision = await self_model.add_source(
            account_id=account_a,
            item_kind="decision_case",
            item_id=decision.case_id,
            source_event_id="growth-source",
            relation="support",
            adopted=True,
            negative=False,
            expected_version=decision.version,
            idempotency_key="growth-pg-source-decision",
        )
        await self_model.review_decision_case(
            account_id=account_a,
            case_id=decision.case_id,
            status="confirmed",
            expected_version=decision.version,
            step_up_verified=False,
            idempotency_key="growth-pg-confirm-decision",
        )
        profile = await self_model.create_relationship_profile(
            account_id=account_a,
            person_id=str(eligible_person_id),
            relationship_id=str(eligible_relationship_id),
            salutation="阿青",
            tone="温和",
            advice_style="先听再建议",
            boundaries=("不谈财务细节",),
            idempotency_key="growth-pg-create-profile",
        )
        profile = await self_model.add_source(
            account_id=account_a,
            item_kind="relationship_profile",
            item_id=profile.profile_id,
            source_event_id="growth-source",
            relation="support",
            adopted=True,
            negative=False,
            expected_version=profile.version_number,
            idempotency_key="growth-pg-source-profile",
        )
        await self_model.review_relationship_profile(
            account_id=account_a,
            profile_id=profile.profile_id,
            version_number=profile.version_number,
            status="approved",
            expected_status="candidate",
            step_up_verified=True,
            idempotency_key="growth-pg-approve-profile",
        )
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
            "owner_projection_ineligible": 1,
        }
        assert relationship_a["adopted_sources"][0]["target_kind"] == (
            "relationship_profile"
        )
        decision_a = next(
            item for item in overview_a["dimensions"] if item["key"] == "decision_cases"
        )
        assert decision_a["rejected_reason_counts"] == {
            "legacy_persona_candidate": 1
        }
        assert {
            source["target_kind"] for source in decision_a["adopted_sources"]
        } == {"cognitive_claim", "decision_case"}
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
        if self_model is not None:
            await self_model.close()
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
