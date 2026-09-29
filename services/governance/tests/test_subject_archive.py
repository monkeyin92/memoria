"""Subject-scoped archive deletion contract on PostgreSQL.

One owner account stores its own turns, a bound child's turns (``subject_id``)
and an elder's turns.  Deleting the child removes the child's evidence, every
projection that cites it -- including an episode, a person search document and
a persona trait merged with the owner's turns -- and leaves the owner's and the
elder's single-source rows in place.  The child's crisis evidence (stored under
the child's own id) goes; the person-consent ledger row stays as audit.

Everything runs on ``postgres_database``, a production-shaped clone: the
product seams as the archive role ``memoria_app`` (the memory compiler as
``memoria_compiler``), the port under test as ``memoria_app`` under FORCE RLS,
and assertions and the few seeded rows as the clone owner.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemoryExtraction
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.archive.skill_domain import (
    SkillApproval,
    SkillProposal,
    SkillRunRequest,
    SkillStepDefinition,
    skill_input_sha256,
)
from services.archive.skill_executor import SkillExecutor
from services.governance.subject_archive import PostgresSubjectArchive
from services.governance.subject_ports import SubjectArchivePort, SubjectScope
from services.governance.tests.test_account_data import SkillToolStub
from services.self_model.postgres_registry import PostgresSelfModelRegistry
from testing.postgres_harness import TestDatabase

_OWNER = "owner-account"
_CHILD = "person-child"
_ELDER = "person-elder"
_WORK_A = "做项目复盘时，我习惯先找事实，再讨论责任。"
_WORK_B = "这个项目收尾阶段，客户要求重新评估范围。"
_MOTHER_A = "我妈妈叫李梅，今年60岁。"
_MOTHER_B = "我母亲李梅今年61岁了。"
_ELDER_TEXT = "我年轻时在杭州读过书。"
_FATHER = "我爸爸叫王强，今年65岁。"

#: The child's device turns inside the owner account (plus one superseding row).
_CHILD_EVENTS = frozenset(
    {
        "child-work",
        "child-mother",
        "child-father",
        "child-withheld",
        "late-correction",
        "child-skill",
        "child-approval",
        "child-confirm",
    }
)
_KEPT_EVENTS = frozenset(
    {"owner-work", "owner-mother", "elder-turn", "owner-skill", "owner-confirm"}
)


class _SharedWorkEpisode:
    """Rule-based extraction; both work turns share one episode identity."""

    version = "subject-archive-episode-v1"

    def __init__(self) -> None:
        self._delegate = RuleBasedMemoryExtractor()

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        extraction = await self._delegate.extract(event)
        if event.payload.get("text") not in {_WORK_A, _WORK_B}:
            return replace(extraction, extractor_version=self.version)
        return replace(
            extraction,
            timeline=tuple(
                replace(item, canonical_key="shared-work-episode") for item in extraction.timeline
            ),
            extractor_version=self.version,
        )


class _EmbedderStub:
    model = "subject-archive-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "项目" in text else (0.0, 1.0)


def _event(
    event_id: str,
    *,
    subject_id: str | None,
    minute: int,
    text: str = "",
    account_id: str = _OWNER,
    event_type: str = "speech.utterance_finalized",
    payload: dict[str, object] | None = None,
    supersedes: str | None = None,
) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=event_id,
        account_id=account_id,
        session_id="session-device",
        turn_id=minute + 1,
        generation_id=minute + 1,
        event_type=event_type,
        occurred_at=datetime(2026, 9, 1, 10, minute, tzinfo=UTC),
        speaker_class="system" if event_type.startswith("guardian.") else "owner",
        source="subject-archive-test",
        subject_id=subject_id,
        supersedes_event_id=supersedes,
        payload=payload
        or {
            "text": text,
            "interaction_mode": "companion",
            "prompt_kind": "spontaneous",
            "owner_projection_eligible": True,
        },
    )


def _skill(name: str, source_event_id: str) -> SkillProposal:
    return SkillProposal(
        account_id=_OWNER,
        name=name,
        description="关闭床头灯。",
        trigger_phrases=(name,),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        output_template={"ok": "$steps.light.output.ok"},
        allowed_tools=("set_light",),
        steps=(SkillStepDefinition(step_id="light", tool_name="set_light", arguments={}),),
        source_kind="explicit_instruction",
        source_event_ids=(source_event_id,),
    )


@dataclass(frozen=True, slots=True)
class _Seeded:
    child_skill_id: str
    owner_skill_id: str
    child_claim_id: str
    owner_claim_id: str
    profile_id: str


#: ``values(table, column, where_column, where_value)`` -> CAST(column AS TEXT) values.
_Values = Callable[[str, str, str, str], Awaitable[set[str]]]


async def _seed(
    archive: Any, catalog: Any, skills: Any, self_model: Any, values: _Values
) -> _Seeded:
    """Write every row through the product seams (archive, catalog, skills, self model)."""

    for event in (
        _event("child-work", subject_id=_CHILD, minute=0, text=_WORK_A),
        _event("owner-work", subject_id=_OWNER, minute=1, text=_WORK_B),
        _event("owner-mother", subject_id=None, minute=2, text=_MOTHER_A),
        _event("child-mother", subject_id=_CHILD, minute=3, text=_MOTHER_B),
        _event("child-father", subject_id=_CHILD, minute=15, text=_FATHER),
        _event("elder-turn", subject_id=_ELDER, minute=4, text=_ELDER_TEXT),
        # Withheld from projection and export, but still the child's evidence.
        _event(
            "child-withheld",
            subject_id=_CHILD,
            minute=5,
            payload={"text": "不要记住这句话。", "memory_retention": "ephemeral_only"},
        ),
        # An unattributed correction of the child's turn joins through supersedes.
        _event(
            "late-correction",
            subject_id=None,
            minute=6,
            event_type="speech.transcript_revised",
            payload={"target_id": "child-work", "corrected_text": _WORK_A},
            supersedes="child-work",
        ),
        _event("child-skill", subject_id=_CHILD, minute=7, text="以后我说晚安就关灯。"),
        _event("owner-skill", subject_id=_OWNER, minute=8, text="以后我说睡觉就关灯。"),
        # Crisis evidence lives under the subject's own id; consent is its audit.
        _event(
            "child-crisis",
            account_id=_CHILD,
            subject_id=None,
            minute=9,
            event_type="guardian.crisis_event",
            payload={"script_version": "v1", "contains_transcript": False},
        ),
        _event(
            "child-consent",
            account_id=_CHILD,
            subject_id=None,
            minute=10,
            event_type="guardian.person_consent_granted",
            payload={"subject_person_id": _CHILD, "consent_kind": "memory"},
        ),
    ):
        await archive.record(event)
    await catalog.compile_pending(limit=1000)

    child_skill = await skills.propose(_skill("晚安", "child-skill"))
    owner_skill = await skills.propose(_skill("睡觉", "owner-skill"))
    # The owner's skill was approved, and once run, on the child's word.
    await archive.record(
        _event(
            "child-approval",
            subject_id=_CHILD,
            minute=11,
            event_type="skill.approved",
            payload={"skill_id": owner_skill.skill_id, "version": owner_skill.version},
        )
    )
    await skills.approve(
        SkillApproval(
            account_id=_OWNER,
            skill_id=owner_skill.skill_id,
            version=owner_skill.version,
            approval_event_id="child-approval",
        )
    )
    executor = SkillExecutor(catalog=skills, tools=SkillToolStub())
    for minute, (confirmation, subject_id) in enumerate(
        (("child-confirm", _CHILD), ("owner-confirm", _OWNER)), start=12
    ):
        await archive.record(
            _event(
                confirmation,
                subject_id=subject_id,
                minute=minute,
                event_type="skill.run_confirmed",
                payload={
                    "skill_id": owner_skill.skill_id,
                    "version": owner_skill.version,
                    "input_sha256": skill_input_sha256({}),
                },
            )
        )
        await executor.execute(
            SkillRunRequest(
                account_id=_OWNER,
                skill_id=owner_skill.skill_id,
                version=owner_skill.version,
                confirmation_event_id=confirmation,
                inputs={},
            )
        )

    claim_ids: list[str] = []
    for source_event_id in ("child-work", "owner-work"):
        claim = await self_model.create_cognitive_claim(
            account_id=_OWNER,
            claim_type="belief",
            statement=f"复盘先看事实（{source_event_id}）。",
            confidence=0.8,
            idempotency_key=f"claim-{source_event_id}",
        )
        await self_model.add_source(
            account_id=_OWNER,
            item_kind="cognitive_claim",
            item_id=claim.claim_id,
            source_event_id=source_event_id,
            relation="support",
            adopted=True,
            negative=False,
            expected_version=claim.version,
            idempotency_key=f"claim-source-{source_event_id}",
        )
        claim_ids.append(str(claim.claim_id))

    # An approved relationship profile about a person the child introduced; it
    # references the person without a cascade.
    (father_id,) = await values("person_entities", "person_id", "source_event_id", "child-father")
    (relationship_id,) = await values("relationships", "relationship_id", "person_id", father_id)
    profile = await self_model.create_relationship_profile(
        account_id=_OWNER,
        person_id=father_id,
        relationship_id=relationship_id,
        salutation="爸",
        tone="温和",
        advice_style="先听再说",
        boundaries=("不谈钱",),
        idempotency_key="profile-father",
    )
    await self_model.approve_relationship_profile(
        account_id=_OWNER, profile_id=profile.profile_id, step_up_verified=True
    )
    return _Seeded(
        child_skill_id=str(child_skill.skill_id),
        owner_skill_id=str(owner_skill.skill_id),
        child_claim_id=claim_ids[0],
        owner_claim_id=claim_ids[1],
        profile_id=str(profile.profile_id),
    )


async def _assert_subject_deletion(
    port: SubjectArchivePort,
    values: _Values,
    seeded: _Seeded,
) -> None:
    evidence = "archive_evidence_events"
    transcripts = "archive_transcript_versions"
    scope = SubjectScope(account_id=_OWNER, subject_id=_CHILD)
    event_ids = await port.subject_event_ids(scope)
    assert set(event_ids) == _CHILD_EVENTS
    assert await port.subject_account_event_ids(_CHILD) == ("child-crisis",)

    references = await port.object_references_for(account_id=_OWNER, event_ids=event_ids)
    assert [reference.object_key for reference in references] == ["blob/child-work"]

    # The merged episode cites the child's and the owner's work turns.
    episode_ids = await values("episode_evidence", "episode_id", "source_event_id", "owner-work")
    assert len(episode_ids) == 1
    (episode_id,) = episode_ids
    assert await values("episode_evidence", "source_event_id", "episode_id", episode_id) >= {
        "child-work",
        "owner-work",
    }
    owner_claims = await values("memory_claims", "claim_id", "source_event_id", "owner-work")
    person_ids = await values("person_entities", "person_id", "source_event_id", "owner-mother")
    elder_claims = await values("memory_claims", "claim_id", "source_event_id", "elder-turn")
    assert owner_claims and person_ids and elder_claims
    assert await values("person_aliases", "alias", "source_event_id", "child-mother")
    before = await port.remaining_rows_for(
        account_id=_OWNER, event_ids=event_ids, subject_id=_CHILD
    )
    assert before[evidence] == len(_CHILD_EVENTS)

    counts = await port.delete_events(account_id=_OWNER, event_ids=event_ids)

    assert counts[evidence] == len(_CHILD_EVENTS)
    assert counts["life_episodes"] >= 1 and counts["memory_search_documents"] >= 1
    assert counts["skill_definitions"] == 1 and counts["skill_runs"] == 1
    assert counts["skill_versions.approval_event_id"] == 1
    assert counts[transcripts] == 1
    assert counts[f"{evidence}.supersedes_event_id"] == 0
    # Only the child's evidence went; the owner's and the elder's stayed.
    assert await values(evidence, "event_id", "account_id", _OWNER) == set(_KEPT_EVENTS)
    assert await values(evidence, "event_id", "account_id", _CHILD) == {
        "child-crisis",
        "child-consent",
    }
    # Merged projections went; the owner's and the elder's single-source rows stay.
    assert await values("life_episodes", "episode_id", "episode_id", episode_id) == set()
    assert await values("timeline_entries", "timeline_id", "episode_id", episode_id) == set()
    assert await values("episode_evidence", "episode_id", "source_event_id", "owner-work") == set()
    assert await values("memory_claims", "claim_id", "source_event_id", "owner-work") == (
        owner_claims
    )
    assert await values("memory_claims", "claim_id", "source_event_id", "elder-turn") == (
        elder_claims
    )
    for claim_id in owner_claims:
        assert await values("memory_search_documents", "document_id", "item_id", claim_id)
    # The owner's person stays; the child's alias and the merged person document go.
    assert await values("person_entities", "person_id", "source_event_id", "owner-mother") == (
        person_ids
    )
    assert await values("person_aliases", "alias", "source_event_id", "child-mother") == set()
    for person_id in person_ids:
        assert await values("memory_search_documents", "document_id", "item_id", person_id) == (
            set()
        )
    assert await values("memory_vector_documents", "item_id", "item_id", episode_id) == set()
    for claim_id in owner_claims:
        assert await values("memory_vector_documents", "item_id", "item_id", claim_id)
    # Transcripts and blobs: the child's go, the owner's stay.
    assert await values(transcripts, "evidence_event_id", "account_id", _OWNER) == {"owner-work"}
    assert await values(
        "archive_evidence_blobs", "evidence_event_id", "account_id", _OWNER
    ) == {"owner-work"}
    # Skills: the child-taught skill goes; the owner's stays without the child's
    # approval pointer or the run the child confirmed.
    assert await values("skill_definitions", "skill_id", "account_id", _OWNER) == {
        seeded.owner_skill_id
    }
    assert (
        await values("skill_versions", "approval_event_id", "skill_id", seeded.owner_skill_id)
        == set()
    )
    assert await values("skill_runs", "confirmation_event_id", "account_id", _OWNER) == {
        "owner-confirm"
    }
    assert len(await values("skill_run_steps", "run_id", "account_id", _OWNER)) == 1
    # Persona trait merged from both speakers goes; the owner's own trait stays.
    assert await values("persona_traits", "normalized_key", "account_id", _OWNER) == {"owner"}
    assert await values("self_model_cognitive_claims", "claim_id", "account_id", _OWNER) == {
        seeded.owner_claim_id
    }
    # A person the child introduced goes with its relationship and profile.
    assert await values("person_entities", "person_id", "source_event_id", "child-father") == set()
    assert (
        await values("self_model_relationship_profiles", "profile_id", "account_id", _OWNER)
        == set()
    )

    assert (
        await port.remaining_rows_for(account_id=_OWNER, event_ids=event_ids, subject_id=_CHILD)
        == {}
    )
    # Idempotent: a retry finds nothing left to delete.
    retry = await port.delete_events(account_id=_OWNER, event_ids=event_ids)
    assert set(retry.values()) == {0}

    # Crisis evidence under the subject's own id goes; consent stays as audit.
    crisis_ids = await port.subject_account_event_ids(_CHILD)
    crisis_counts = await port.delete_events(account_id=_CHILD, event_ids=crisis_ids)
    assert crisis_counts[evidence] == 1
    assert await values(evidence, "event_id", "account_id", _CHILD) == {"child-consent"}
    assert (
        await port.remaining_rows_for(account_id=_CHILD, event_ids=crisis_ids, subject_id=None)
        == {}
    )
    assert await port.subject_account_event_ids(_CHILD) == ()
    assert await port.subject_event_ids(scope) == ()


async def _seed_postgres_rows(connection: asyncpg.Connection) -> None:
    """Rows no product seam writes here: transcripts, blobs, persona traits."""

    for event_id in ("child-work", "owner-work"):
        await connection.execute(
            """
            INSERT INTO archive_transcript_versions (
                transcript_version_id, account_id, session_id, turn_id,
                evidence_event_id, text, source
            ) VALUES ($1, $2, 'session-device', 1, $3, '转写', 'asr')
            """,
            uuid.uuid4(),
            _OWNER,
            event_id,
        )
        await connection.execute(
            """
            INSERT INTO archive_evidence_blobs (
                blob_id, account_id, evidence_event_id, object_key, media_type,
                byte_count, content_sha256, encryption_key_version, retention_policy
            ) VALUES ($1, $2, $3, $4, 'audio/wav', 4, $5, 'v1', 'account_lifetime')
            """,
            uuid.uuid4(),
            _OWNER,
            event_id,
            f"blob/{event_id}",
            "b" * 64,
        )
    for key, sources in (("merged", ("child-work", "owner-work")), ("owner", ("owner-work",))):
        trait_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO persona_traits (
                trait_id, account_id, category, normalized_key, description, context, confidence
            ) VALUES ($1, $2, 'style', $3, '先讲事实', '复盘', 0.7)
            """,
            trait_id,
            _OWNER,
            key,
        )
        for source in sources:
            await connection.execute(
                """
                INSERT INTO persona_evidence (
                    trait_id, account_id, source_event_id, scene, weight, occurred_at
                ) VALUES ($1, $2, $3, 'work', 0.5, now())
                """,
                trait_id,
                _OWNER,
                source,
            )


def _owner_values(admin: asyncpg.Connection) -> _Values:
    async def values(table: str, column: str, where_column: str, where_value: str) -> set[str]:
        rows = await admin.fetch(
            f"SELECT CAST({column} AS TEXT) FROM {table}"
            f" WHERE CAST({where_column} AS TEXT) = $1",
            where_value,
        )
        return {str(row[0]) for row in rows if row[0] is not None}

    return values


@pytest.mark.asyncio
async def test_postgres_subject_deletion_removes_only_the_subjects_lineage(
    postgres_database: TestDatabase,
) -> None:
    app_dsn = postgres_database.role_dsn("memoria_app")
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    archive = PostgresLifeArchive(app_dsn)
    catalog = PostgresMemoryCatalog(
        app_dsn,
        extractor=_SharedWorkEpisode(),
        compiler_dsn=postgres_database.role_dsn("memoria_compiler"),
        compiler_role="memoria_compiler",
        embedder=_EmbedderStub(),
        require_vector=True,
    )
    skills = PostgresSkillCatalog(app_dsn)
    self_model = PostgresSelfModelRegistry(app_dsn)
    try:
        values = _owner_values(admin)
        seeded = await _seed(archive, catalog, skills, self_model, values)
        await _seed_postgres_rows(admin)

        # The port runs as the RLS-bound archive role, never as the owner.
        await _assert_subject_deletion(PostgresSubjectArchive(app_dsn), values, seeded)
    finally:
        await admin.close()
        for closable in (catalog, archive, skills, self_model):
            await closable.close()


@pytest.mark.asyncio
async def test_partial_deletion_unlinks_a_superseding_row_outside_the_set(
    postgres_database: TestDatabase,
) -> None:
    app_dsn = postgres_database.role_dsn("memoria_app")
    archive = PostgresLifeArchive(app_dsn)
    try:
        await archive.record(_event("child-work", subject_id=_CHILD, minute=0, text=_WORK_A))
        await archive.record(
            _event(
                "late-correction",
                subject_id=None,
                minute=1,
                event_type="speech.transcript_revised",
                payload={"target_id": "child-work", "corrected_text": _WORK_A},
                supersedes="child-work",
            )
        )
    finally:
        await archive.close()
    port = PostgresSubjectArchive(app_dsn)

    counts = await port.delete_events(account_id=_OWNER, event_ids=("child-work",))

    assert counts["archive_evidence_events"] == 1
    assert counts["archive_evidence_events.supersedes_event_id"] == 1
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        rows = await admin.fetch(
            "SELECT event_id, supersedes_event_id FROM archive_evidence_events"
        )
    finally:
        await admin.close()
    assert [tuple(row) for row in rows] == [("late-correction", None)]
    assert (
        await port.remaining_rows_for(
            account_id=_OWNER, event_ids=("child-work",), subject_id=_CHILD
        )
        == {}
    )
