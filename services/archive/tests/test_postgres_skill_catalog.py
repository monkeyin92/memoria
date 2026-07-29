from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.archive.skill_domain import (
    SkillApproval,
    SkillConfirmationRequiredError,
    SkillNotFoundError,
    SkillProposal,
    SkillRunRequest,
    SkillStepDefinition,
    skill_input_sha256,
)
from services.archive.skill_executor import SkillExecutor


class PostgresSkillTools:
    async def invoke(
        self,
        *,
        account_id: str,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        compensation: bool,
    ) -> object:
        del account_id, run_id, step_id, compensation
        if tool_name == "set_light":
            return {"brightness": arguments["brightness"], "previous_brightness": 90}
        return {"story_id": "postgres-story"}


def _proposal(
    account_id: str,
    event_id: str,
    *,
    name: str = "睡前流程",
) -> SkillProposal:
    return SkillProposal(
        account_id=account_id,
        name=name,
        description="先调暗灯光，再播放故事。",
        trigger_phrases=("开始睡前流程",),
        input_schema={
            "type": "object",
            "properties": {"device": {"type": "string"}},
            "required": ["device"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"story_id": {"type": "string"}},
            "required": ["story_id"],
            "additionalProperties": False,
        },
        output_template={"story_id": "$steps.story.output.story_id"},
        allowed_tools=("set_light", "play_story"),
        steps=(
            SkillStepDefinition(
                step_id="dim",
                tool_name="set_light",
                arguments={"device": "$input.device", "brightness": 20},
            ),
            SkillStepDefinition(
                step_id="story",
                tool_name="play_story",
                arguments={"minutes": 5},
            ),
        ),
        source_kind="explicit_instruction",
        source_event_ids=(event_id,),
    )


@pytest.mark.asyncio
async def test_postgres_skill_catalog_rejects_invalid_ids_without_opening_a_connection() -> None:
    catalog = PostgresSkillCatalog("postgresql://unused")

    with pytest.raises(SkillNotFoundError):
        await catalog.get_version(
            account_id="account",
            skill_id="not-a-uuid",
            version=1,
        )
    with pytest.raises(SkillNotFoundError):
        await catalog.get_run(account_id="account", run_id="not-a-uuid")


async def _record(
    archive: PostgresLifeArchive,
    *,
    event_id: str,
    account_id: str,
    event_type: str,
    payload: Mapping[str, object],
    minute: int,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            event_type=event_type,
            occurred_at=datetime(2026, 7, 28, 11, minute, tzinfo=UTC),
            speaker_class="owner",
            source="postgres-skill-test",
            payload=payload,
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL skill contract",
)
async def test_postgres_skill_catalog_matches_sqlite_execution_and_rls_contract() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"postgres-skill-{uuid.uuid4()}"
    instruction = f"instruction-{uuid.uuid4()}"
    instruction_v2 = f"instruction-{uuid.uuid4()}"
    approval = f"approval-{uuid.uuid4()}"
    confirmation = f"confirmation-{uuid.uuid4()}"
    archive = PostgresLifeArchive(dsn)
    memory = PostgresMemoryCatalog(dsn, extractor=RuleBasedMemoryExtractor())
    skill = PostgresSkillCatalog(dsn)
    await archive.initialize()
    await memory.initialize()
    await skill.initialize()
    try:
        await _record(
            archive,
            event_id=instruction,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            payload={"text": "以后我说开始睡前流程，就先调暗灯光再讲故事。"},
            minute=0,
        )
        first_candidate = await skill.propose(
            _proposal(account_id, instruction, name="Bedtime Routine")
        )
        await _record(
            archive,
            event_id=instruction_v2,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            payload={"text": "Teach the updated bedtime routine."},
            minute=1,
        )
        candidate = await skill.propose(
            _proposal(account_id, instruction_v2, name="BEDTIME ROUTINE")
        )
        assert candidate.skill_id == first_candidate.skill_id
        assert candidate.version == 2
        assert candidate.name == "Bedtime Routine"
        with pytest.raises(SkillNotFoundError):
            await skill.get_version(
                account_id="another-account",
                skill_id=candidate.skill_id,
                version=candidate.version,
            )
        await _record(
            archive,
            event_id=approval,
            account_id=account_id,
            event_type="skill.approved",
            payload={
                "skill_id": candidate.skill_id,
                "version": candidate.version,
            },
            minute=2,
        )
        approved = await skill.approve(
            SkillApproval(
                account_id=account_id,
                skill_id=candidate.skill_id,
                version=candidate.version,
                approval_event_id=approval,
            )
        )
        await _record(
            archive,
            event_id=confirmation,
            account_id=account_id,
            event_type="skill.run_confirmed",
            payload={
                "skill_id": candidate.skill_id,
                "version": candidate.version,
                "input_sha256": skill_input_sha256({"device": "bedroom"}),
            },
            minute=3,
        )
        request = SkillRunRequest(
            account_id=account_id,
            skill_id=candidate.skill_id,
            version=candidate.version,
            confirmation_event_id=confirmation,
            inputs={"device": "bedroom"},
        )

        run = await SkillExecutor(
            catalog=skill,
            tools=PostgresSkillTools(),
        ).execute(request)

        assert (approved.status, run.status, run.output) == (
            "approved",
            "succeeded",
            {"story_id": "postgres-story"},
        )
        assert [(step.step_id, step.status) for step in run.steps] == [
            ("dim", "succeeded"),
            ("story", "succeeded"),
        ]
        with pytest.raises(SkillConfirmationRequiredError):
            await skill.start_run(request)

        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "DELETE FROM memory_search_documents "
                "WHERE account_id = $1 AND kind = 'skill'",
                account_id,
            )
            assert (
                await skill.rebuild_search_projections(account_id=account_id)
                == 1
            )
            rebuilt_projection = await connection.fetchrow(
                """
                SELECT status, memory_kind
                FROM memory_search_documents
                WHERE account_id = $1 AND kind = 'skill'
                """,
                account_id,
            )
            rls = await connection.fetch(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = ANY($1::text[])
                ORDER BY relname
                """,
                [
                    "skill_definitions",
                    "skill_versions",
                    "skill_version_evidence",
                    "skill_runs",
                    "skill_run_steps",
                ],
            )
            assert len(rls) == 5
            assert all(
                row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls
            )
            assert rebuilt_projection is not None
            assert tuple(rebuilt_projection.values()) == ("confirmed", "procedural")
        finally:
            await connection.close()
    finally:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "DELETE FROM skill_definitions WHERE account_id = $1",
                account_id,
            )
            await connection.execute(
                "DELETE FROM archive_evidence_events WHERE account_id = $1",
                account_id,
            )
        finally:
            await connection.close()
        await skill.close()
        await memory.close()
        await archive.close()
