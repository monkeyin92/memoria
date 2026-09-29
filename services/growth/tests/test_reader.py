"""Growth reader behaviour on PostgreSQL, as the production archive role."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from services.archive.domain import EvidenceEvent
from services.archive.postgres_archive import PostgresLifeArchive
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.growth.postgres_reader import PostgresGrowthReader
from services.self_model.postgres_registry import PostgresSelfModelRegistry

NOW = datetime(2026, 7, 22, tzinfo=UTC)
# Claims share a source event and an occurred_at, so the reader orders them by
# claim_id; fixed UUIDs keep "the first claim wins the deduplicated source" exact.
CLAIM_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
CLAIM_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")


@dataclass
class _Stores:
    database: Any
    archive: PostgresLifeArchive
    digital_self: PostgresDigitalSelfRegistry
    self_model: PostgresSelfModelRegistry


@pytest_asyncio.fixture
async def stores(postgres_database: Any) -> AsyncIterator[_Stores]:
    dsn = postgres_database.role_dsn("memoria_app")
    archive = PostgresLifeArchive(dsn)
    digital_self = PostgresDigitalSelfRegistry(dsn)
    self_model = PostgresSelfModelRegistry(dsn)
    await archive.initialize()
    await digital_self.initialize()
    await self_model.initialize()
    try:
        yield _Stores(postgres_database, archive, digital_self, self_model)
    finally:
        await self_model.close()
        await digital_self.close()
        await archive.close()


async def _seed(database: Any, statements: list[tuple[str, tuple[object, ...]]]) -> None:
    """Rows no store API writes (catalog projections), as the clone admin."""

    connection = await asyncpg.connect(database.owner_dsn())
    try:
        async with connection.transaction():
            for sql, args in statements:
                await connection.execute(sql, *args)
    finally:
        await connection.close()


async def _approve_new_version(registry: PostgresDigitalSelfRegistry, account_id: str) -> None:
    draft = await registry.build(account_id=account_id)
    testing = await registry.begin_testing(
        account_id=account_id,
        version_id=draft.version_id,
        expected_manifest_sha256=draft.manifest_sha256,
    )
    await registry.approve(
        account_id=account_id,
        version_id=testing.version_id,
        expected_manifest_sha256=testing.manifest_sha256,
    )


def _dimension(overview: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in overview["dimensions"] if item["key"] == key)


@pytest.mark.asyncio
async def test_reader_deduplicates_sources_and_counts_ineligible_confirmed_rows(
    stores: _Stores,
) -> None:
    account_id = "growth-owner"
    for event_id, eligible in (
        ("shared-claim-source", True),
        ("ineligible-projection-source", False),
    ):
        await stores.archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=NOW,
                speaker_class="owner",
                source="test",
                payload={
                    "text": event_id,
                    "owner_projection_eligible": eligible,
                },
            )
        )

    episode_id = uuid.uuid4()
    ineligible_person, eligible_person = uuid.uuid4(), uuid.uuid4()
    legacy_trait = uuid.uuid4()
    claim_sql = """
        INSERT INTO memory_claims (
            claim_id, account_id, category, domain_category,
            subject_key, predicate, value, confidence, status,
            sensitive_domain, extractor_version, source_event_id,
            valid_at, observed_at
        ) VALUES ($1, $2, 'life_story', 'life_story', 'self', $3, $4, .9,
                  'confirmed', 'personal', 'test', 'shared-claim-source', $5, $5)
    """
    person_sql = """
        INSERT INTO person_entities (
            person_id, account_id, canonical_key, display_name,
            relationship_to_owner, status, source_event_id, created_at
        ) VALUES ($1, $2, $3, $4, 'friend', 'confirmed', $5, $6)
    """
    relationship_sql = """
        INSERT INTO relationships (
            relationship_id, account_id, person_id, relationship_type,
            status, source_event_id, valid_at
        ) VALUES ($1, $2, $3, 'friend', 'confirmed', $4, $5)
    """
    await _seed(
        stores.database,
        [
            (claim_sql, (CLAIM_A, account_id, "education", "杭州读书", NOW)),
            (claim_sql, (CLAIM_B, account_id, "city", "住在杭州", NOW)),
            (
                """
                INSERT INTO life_episodes (
                    episode_id, account_id, title, category, domain_category,
                    consolidation_key, status, event_start, observed_at,
                    source_event_id
                ) VALUES ($1, $2, '一段经历', 'life_story', 'life_story',
                          'test:episode-ineligible', 'confirmed', $3, $3,
                          'ineligible-projection-source')
                """,
                (episode_id, account_id, NOW),
            ),
            (
                """
                INSERT INTO timeline_entries (
                    timeline_id, account_id, episode_id, title, category,
                    domain_category, status, event_start, time_precision,
                    observed_at, source_event_id
                ) VALUES ($1, $2, $3, '一段经历', 'life_story', 'life_story',
                          'confirmed', $4, 'day', $4, 'ineligible-projection-source')
                """,
                (uuid.uuid4(), account_id, episode_id, NOW),
            ),
            (
                person_sql,
                (ineligible_person, account_id, "friend:小林", "小林", "ineligible-projection-source", NOW),
            ),
            (
                relationship_sql,
                (uuid.uuid4(), account_id, ineligible_person, "ineligible-projection-source", NOW),
            ),
            (
                person_sql,
                (eligible_person, account_id, "friend:阿青", "阿青", "shared-claim-source", NOW),
            ),
            (
                relationship_sql,
                (uuid.uuid4(), account_id, eligible_person, "shared-claim-source", NOW),
            ),
            (
                """
                INSERT INTO persona_traits (
                    trait_id, account_id, category, normalized_key, description,
                    context, counterexample, confidence, status, observation_count
                ) VALUES ($1, $2, 'decision_habit', 'legacy-decision',
                          '旧 Persona 决策标签', 'conversation', '也会例外', .9,
                          'confirmed', 3)
                """,
                (legacy_trait, account_id),
            ),
            (
                """
                INSERT INTO persona_evidence (
                    trait_id, account_id, source_event_id, scene, weight, occurred_at
                ) VALUES ($1, $2, 'shared-claim-source', 'conversation', 1.0, $3)
                """,
                (legacy_trait, account_id, NOW),
            ),
        ],
    )

    await _approve_new_version(stores.digital_self, account_id)
    # No Self Model registry: an eligible confirmed relationship still waits
    # for an owner-approved relationship profile.
    reader = PostgresGrowthReader(stores.database.role_dsn("memoria_app"))
    try:
        ready_overview = await reader.overview(account_id=account_id)
        dimensions = {item["key"]: item for item in ready_overview["dimensions"]}

        assert [
            source["event_id"] for source in dimensions["life_chapters"]["adopted_sources"]
        ] == ["shared-claim-source"]
        assert dimensions["life_chapters"]["adopted_sources"][0]["target_id"] == str(CLAIM_A)
        assert dimensions["life_chapters"]["version_readiness"]["status"] == "ready"
        assert dimensions["life_chapters"]["rejected_reason_counts"] == {
            "owner_projection_ineligible": 1
        }
        assert dimensions["relationship_models"]["rejected_reason_counts"] == {
            "owner_projection_ineligible": 1,
            "relationship_profile_pending_owner_approval": 1,
        }
        assert dimensions["relationship_models"]["adopted_sources"] == []
        assert dimensions["decision_cases"]["rejected_reason_counts"] == {
            "legacy_persona_candidate": 1
        }
        assert dimensions["decision_cases"]["adopted_sources"] == []

        await stores.archive.record(
            EvidenceEvent(
                event_id="hidden-claim-not-me",
                account_id=account_id,
                event_type="owner.action_recorded",
                occurred_at=NOW,
                speaker_class="owner",
                source="user.growth_feedback",
                payload={
                    "action_type": "not_me",
                    "target_kind": "memory_claim",
                    "target_id": str(CLAIM_B),
                    "owner_projection_eligible": True,
                },
            )
        )
        conflicted_life = _dimension(await reader.overview(account_id=account_id), "life_chapters")
        assert conflicted_life["status"] == "conflicted"
        assert conflicted_life["conflicts"][0]["target_id"] == str(CLAIM_B)
        assert conflicted_life["version_readiness"]["status"] == "stale"

        await _approve_new_version(stores.digital_self, account_id)
        refreshed_life = _dimension(await reader.overview(account_id=account_id), "life_chapters")
        assert refreshed_life["status"] == "conflicted"
        assert refreshed_life["version_readiness"]["status"] == "ready"

        await _seed(
            stores.database,
            [("UPDATE memory_claims SET status = 'retracted' WHERE claim_id = $1", (CLAIM_A,))],
        )
        stale_life = _dimension(await reader.overview(account_id=account_id), "life_chapters")
        assert stale_life["version_readiness"]["status"] == "stale"
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_reader_only_adopts_effective_self_model_items(stores: _Stores) -> None:
    account_id = "growth-self-model-owner"
    self_model = stores.self_model
    for event_id, text in (
        ("claim-source", "我相信先确认事实再判断。"),
        ("decision-source", "我曾先验证需求再决定开发。"),
        ("relationship-source", "我和李梅沟通时会先听完。"),
    ):
        await stores.archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=NOW,
                speaker_class="owner",
                source="growth-self-model-test",
                payload={
                    "text": text,
                    "interaction_mode": "companion",
                    "owner_projection_eligible": True,
                },
            )
        )
    person_id, relationship_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        stores.database,
        [
            (
                """
                INSERT INTO person_entities (
                    person_id, account_id, canonical_key, display_name,
                    relationship_to_owner, status, source_event_id, created_at
                ) VALUES ($1, $2, 'friend:李梅', '李梅', 'friend',
                          'confirmed', 'relationship-source', $3)
                """,
                (person_id, account_id, NOW),
            ),
            (
                """
                INSERT INTO relationships (
                    relationship_id, account_id, person_id, relationship_type,
                    status, source_event_id, valid_at
                ) VALUES ($1, $2, $3, 'friend', 'confirmed', 'relationship-source', $4)
                """,
                (relationship_id, account_id, person_id, NOW),
            ),
        ],
    )

    claim = await self_model.create_cognitive_claim(
        account_id=account_id,
        claim_type="belief",
        statement="我相信先确认事实再判断。",
        confidence=0.9,
        idempotency_key="create-claim",
    )
    claim = await self_model.add_source(
        account_id=account_id,
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="claim-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=claim.version,
        idempotency_key="source-claim",
    )
    await self_model.review_cognitive_claim(
        account_id=account_id,
        claim_id=claim.claim_id,
        status="confirmed",
        expected_version=claim.version,
        step_up_verified=False,
        idempotency_key="confirm-claim",
    )

    decision = await self_model.create_decision_case(
        account_id=account_id,
        kind="real",
        context="决定先验证需求",
        options=("直接开发", "先验证"),
        constraints=("时间有限",),
        chosen_option="先验证",
        rejected_options=("直接开发",),
        outcome="避免返工",
        reflection="仍然认同",
        still_endorsed=True,
        idempotency_key="create-decision",
    )
    decision = await self_model.add_source(
        account_id=account_id,
        item_kind="decision_case",
        item_id=decision.case_id,
        source_event_id="decision-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=decision.version,
        idempotency_key="source-decision",
    )
    await self_model.review_decision_case(
        account_id=account_id,
        case_id=decision.case_id,
        status="confirmed",
        expected_version=decision.version,
        step_up_verified=False,
        idempotency_key="confirm-decision",
    )

    profile = await self_model.create_relationship_profile(
        account_id=account_id,
        person_id=str(person_id),
        relationship_id=str(relationship_id),
        salutation="李梅",
        tone="温和",
        advice_style="先听再建议",
        boundaries=("不谈财务细节",),
        idempotency_key="create-profile",
    )
    profile = await self_model.add_source(
        account_id=account_id,
        item_kind="relationship_profile",
        item_id=profile.profile_id,
        source_event_id="relationship-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=profile.version_number,
        idempotency_key="source-profile",
    )
    await self_model.review_relationship_profile(
        account_id=account_id,
        profile_id=profile.profile_id,
        version_number=profile.version_number,
        status="approved",
        expected_status="candidate",
        step_up_verified=True,
        idempotency_key="approve-profile",
    )

    reader = PostgresGrowthReader(
        stores.database.role_dsn("memoria_app"),
        self_model_registry=self_model,
    )
    try:
        overview = await reader.overview(account_id=account_id)
    finally:
        await reader.close()
    dimensions = {item["key"]: item for item in overview["dimensions"]}

    assert dimensions["decision_cases"]["status"] == "supported"
    assert {
        source["target_kind"] for source in dimensions["decision_cases"]["adopted_sources"]
    } == {"cognitive_claim", "decision_case"}
    assert dimensions["relationship_models"]["status"] == "supported"
    assert dimensions["relationship_models"]["adopted_sources"][0]["target_kind"] == (
        "relationship_profile"
    )
    assert "relationship_profile_pending_owner_approval" not in dimensions[
        "relationship_models"
    ]["rejected_reason_counts"]
    assert dimensions["decision_cases"]["dependency_blockers"] == []
    assert dimensions["relationship_models"]["dependency_blockers"] == []
