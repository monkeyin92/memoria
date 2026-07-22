from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.digital_self.registry import DigitalSelfRegistry
from services.growth.reader import GrowthReader
from services.self_model.registry import SelfModelRegistry


@pytest.mark.asyncio
async def test_reader_deduplicates_sources_and_counts_ineligible_confirmed_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "growth.sqlite3"
    account_id = "growth-owner"
    now = datetime(2026, 7, 22, tzinfo=UTC)
    archive = LifeArchive.sqlite(path)
    archive.initialize()
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    catalog.initialize()
    registry = DigitalSelfRegistry.sqlite(path)
    registry.initialize()
    for event_id, eligible in (
        ("shared-claim-source", True),
        ("ineligible-projection-source", False),
    ):
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=now,
                speaker_class="owner",
                source="test",
                payload={
                    "text": event_id,
                    "owner_projection_eligible": eligible,
                },
            )
        )

    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO memory_claims (
                claim_id, account_id, category, subject_key, predicate, value,
                confidence, status, sensitive_domain, extractor_version,
                source_event_id, valid_at, created_at
            ) VALUES (?, ?, 'life_story', 'self', ?, ?, .9, 'confirmed',
                      'personal', 'test', 'shared-claim-source', ?, ?)
            """,
            (
                ("claim-a", account_id, "education", "杭州读书", now.isoformat(), now.isoformat()),
                ("claim-b", account_id, "city", "住在杭州", now.isoformat(), now.isoformat()),
            ),
        )
        connection.execute(
            """
            INSERT INTO life_episodes (
                episode_id, account_id, title, category, status, event_start,
                source_event_id
            ) VALUES ('episode-ineligible', ?, '一段经历', 'life_story',
                      'confirmed', ?, 'ineligible-projection-source')
            """,
            (account_id, now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO timeline_entries (
                timeline_id, account_id, episode_id, title, category, status,
                event_start, time_precision, source_event_id
            ) VALUES ('timeline-ineligible', ?, 'episode-ineligible', '一段经历',
                      'life_story', 'confirmed', ?, 'day',
                      'ineligible-projection-source')
            """,
            (account_id, now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES ('person-ineligible', ?, 'friend:小林', '小林', 'friend',
                      'confirmed', 'ineligible-projection-source', ?)
            """,
            (account_id, now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES ('relationship-ineligible', ?, 'person-ineligible',
                      'friend', 'confirmed', 'ineligible-projection-source', ?)
            """,
            (account_id, now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES ('person-eligible', ?, 'friend:阿青', '阿青', 'friend',
                      'confirmed', 'shared-claim-source', ?)
            """,
            (account_id, now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES ('relationship-eligible', ?, 'person-eligible',
                      'friend', 'confirmed', 'shared-claim-source', ?)
            """,
            (account_id, now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO persona_traits (
                trait_id, account_id, category, normalized_key, description,
                context, counterexample, confidence, status, observation_count,
                created_at, updated_at
            ) VALUES ('legacy-decision-trait', ?, 'decision_habit',
                      'legacy-decision', '旧 Persona 决策标签',
                      'conversation', '也会例外', .9, 'confirmed', 3, ?, ?)
            """,
            (account_id, now.isoformat(), now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO persona_evidence (
                account_id, trait_id, source_event_id, scene, weight, occurred_at
            ) VALUES (?, 'legacy-decision-trait', 'shared-claim-source',
                      'conversation', 1.0, ?)
            """,
            (account_id, now.isoformat()),
        )

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
    reader = GrowthReader.sqlite(str(path))
    ready_overview = await reader.overview(account_id=account_id)
    dimensions = {item["key"]: item for item in ready_overview["dimensions"]}

    assert [
        source["event_id"] for source in dimensions["life_chapters"]["adopted_sources"]
    ] == ["shared-claim-source"]
    assert dimensions["life_chapters"]["adopted_sources"][0]["target_id"] == "claim-a"
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

    await archive.record(
        EvidenceEvent(
            event_id="hidden-claim-not-me",
            account_id=account_id,
            event_type="owner.action_recorded",
            occurred_at=now,
            speaker_class="owner",
            source="user.growth_feedback",
            payload={
                "action_type": "not_me",
                "target_kind": "memory_claim",
                "target_id": "claim-b",
                "owner_projection_eligible": True,
            },
        )
    )
    conflicted_overview = await reader.overview(account_id=account_id)
    conflicted_life = next(
        item
        for item in conflicted_overview["dimensions"]
        if item["key"] == "life_chapters"
    )
    assert conflicted_life["status"] == "conflicted"
    assert conflicted_life["conflicts"][0]["target_id"] == "claim-b"
    assert conflicted_life["version_readiness"]["status"] == "stale"

    replacement = await registry.build(account_id=account_id)
    replacement_testing = await registry.begin_testing(
        account_id=account_id,
        version_id=replacement.version_id,
        expected_manifest_sha256=replacement.manifest_sha256,
    )
    await registry.approve(
        account_id=account_id,
        version_id=replacement_testing.version_id,
        expected_manifest_sha256=replacement_testing.manifest_sha256,
    )
    refreshed = await reader.overview(account_id=account_id)
    refreshed_life = next(
        item for item in refreshed["dimensions"] if item["key"] == "life_chapters"
    )
    assert refreshed_life["status"] == "conflicted"
    assert refreshed_life["version_readiness"]["status"] == "ready"

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_claims SET status = 'retracted' WHERE claim_id = 'claim-a'"
        )
    stale_overview = await reader.overview(account_id=account_id)
    stale_life = next(
        item
        for item in stale_overview["dimensions"]
        if item["key"] == "life_chapters"
    )
    assert stale_life["version_readiness"]["status"] == "stale"


@pytest.mark.asyncio
async def test_reader_only_adopts_effective_self_model_items(tmp_path: Path) -> None:
    path = tmp_path / "growth-self-model.sqlite3"
    account_id = "growth-self-model-owner"
    archive = LifeArchive.sqlite(path)
    self_model = SelfModelRegistry.sqlite(path)
    self_model.initialize()
    for event_id, text in (
        ("claim-source", "我相信先确认事实再判断。"),
        ("decision-source", "我曾先验证需求再决定开发。"),
        ("relationship-source", "我和李梅沟通时会先听完。"),
    ):
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
                speaker_class="owner",
                source="growth-self-model-test",
                payload={
                    "text": text,
                    "interaction_mode": "companion",
                    "owner_projection_eligible": True,
                },
            )
        )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES ('self-model-person', ?, 'friend:李梅', '李梅', 'friend',
                      'confirmed', 'relationship-source', ?)
            """,
            (account_id, datetime(2026, 7, 22, tzinfo=UTC).isoformat()),
        )
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES ('self-model-relationship', ?, 'self-model-person',
                      'friend', 'confirmed', 'relationship-source', ?)
            """,
            (account_id, datetime(2026, 7, 22, tzinfo=UTC).isoformat()),
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
        person_id="self-model-person",
        relationship_id="self-model-relationship",
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

    reader = GrowthReader.sqlite(
        str(path),
        self_model_registry=self_model,
    )
    overview = await reader.overview(account_id=account_id)
    dimensions = {item["key"]: item for item in overview["dimensions"]}

    assert dimensions["decision_cases"]["status"] == "supported"
    assert {
        source["target_kind"]
        for source in dimensions["decision_cases"]["adopted_sources"]
    } == {"cognitive_claim", "decision_case"}
    assert dimensions["relationship_models"]["status"] == "supported"
    assert dimensions["relationship_models"]["adopted_sources"][0][
        "target_kind"
    ] == "relationship_profile"
    assert "relationship_profile_pending_owner_approval" not in dimensions[
        "relationship_models"
    ]["rejected_reason_counts"]
    assert dimensions["decision_cases"]["dependency_blockers"] == []
    assert dimensions["relationship_models"]["dependency_blockers"] == []
