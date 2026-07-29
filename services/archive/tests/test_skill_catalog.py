from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import MemorySearchQuery
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.skill_catalog import SkillCatalog
from services.archive.skill_domain import (
    SkillApproval,
    SkillApprovalRequiredError,
    SkillExecutionError,
    SkillNotFoundError,
    SkillProposal,
    SkillRunRequest,
    SkillSchemaValidationError,
    SkillStepDefinition,
    skill_input_sha256,
)
from services.archive.skill_executor import SkillExecutor


class FakeTools:
    def __init__(self, *, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.invocations: list[tuple[str, bool, Mapping[str, object]]] = []

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
        del account_id, run_id, step_id
        self.invocations.append((tool_name, compensation, arguments))
        if tool_name == self.fail_on and not compensation:
            raise RuntimeError("tool failed")
        if tool_name == "set_light" and not compensation:
            return {"previous_brightness": 80, "brightness": arguments["brightness"]}
        if tool_name == "play_story":
            return {"story_id": "story-001"}
        return {"ok": True}


class NonJsonToolResult(FakeTools):
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
        self.invocations.append((tool_name, compensation, arguments))
        del account_id, run_id, step_id
        if not compensation:
            return {"unsupported": {1, 2}}
        return {"ok": True}


async def _record(
    archive: LifeArchive,
    *,
    event_id: str,
    account_id: str,
    event_type: str,
    payload: Mapping[str, object],
    speaker_class: str = "owner",
    minute: int = 0,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            event_type=event_type,
            occurred_at=datetime(2026, 7, 28, 9, minute, tzinfo=UTC),
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="skill-test",
            payload=payload,
        )
    )


def _proposal(
    *,
    account_id: str = "skill-account",
    name: str = "睡前流程",
    source_event_ids: tuple[str, ...] = ("skill-instruction",),
    source_kind: str = "explicit_instruction",
    description: str = "先调暗灯光，再播放一个睡前故事。",
) -> SkillProposal:
    return SkillProposal(
        account_id=account_id,
        name=name,
        description=description,
        trigger_phrases=("开始睡前流程",),
        input_schema={
            "type": "object",
            "properties": {
                "device": {"type": "string", "minLength": 1},
            },
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
        allowed_tools=("set_light", "play_story", "stop_story"),
        steps=(
            SkillStepDefinition(
                step_id="dim",
                tool_name="set_light",
                arguments={"device": "$input.device", "brightness": 20},
                compensation_tool_name="set_light",
                compensation_arguments={
                    "device": "$input.device",
                    "brightness": "$steps.dim.output.previous_brightness",
                },
            ),
            SkillStepDefinition(
                step_id="story",
                tool_name="play_story",
                arguments={"duration_minutes": 5},
                compensation_tool_name="stop_story",
                compensation_arguments={
                    "story_id": "$steps.story.output.story_id",
                },
            ),
        ),
        source_kind=source_kind,  # type: ignore[arg-type]
        source_event_ids=source_event_ids,
        domain_category="daily_life",
        sensitivity="personal",
        salience=0.8,
    )


async def _approved(
    path: Path,
) -> tuple[LifeArchive, SkillCatalog, str]:
    archive = LifeArchive.sqlite(path)
    catalog = SkillCatalog.sqlite(path)
    await _record(
        archive,
        event_id="skill-instruction",
        account_id="skill-account",
        event_type="speech.utterance_finalized",
        payload={"text": "以后我说开始睡前流程，就先调暗灯光再讲故事。"},
    )
    candidate = await catalog.propose(_proposal())
    await _record(
        archive,
        event_id="skill-approval",
        account_id="skill-account",
        event_type="skill.approved",
        payload={"skill_id": candidate.skill_id, "version": candidate.version},
        minute=1,
    )
    approved = await catalog.approve(
        SkillApproval(
            account_id="skill-account",
            skill_id=candidate.skill_id,
            version=candidate.version,
            approval_event_id="skill-approval",
        )
    )
    return archive, catalog, approved.skill_id


@pytest.mark.asyncio
async def test_candidate_is_not_executable_and_account_scope_is_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    catalog = SkillCatalog.sqlite(path)
    await _record(
        archive,
        event_id="skill-instruction",
        account_id="skill-account",
        event_type="speech.utterance_finalized",
        payload={"text": "以后我说开始睡前流程，就先调暗灯光再讲故事。"},
    )

    candidate = await catalog.propose(_proposal())

    with pytest.raises(SkillNotFoundError):
        await catalog.get_version(
            account_id="another-account",
            skill_id=candidate.skill_id,
            version=1,
        )
    with pytest.raises(SkillApprovalRequiredError):
        await catalog.start_run(
            SkillRunRequest(
                account_id="skill-account",
                skill_id=candidate.skill_id,
                version=1,
                confirmation_event_id="not-recorded",
                inputs={"device": "bedroom"},
            )
        )


@pytest.mark.asyncio
async def test_approved_skill_executes_with_schema_and_complete_step_audit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive, catalog, skill_id = await _approved(path)
    await _record(
        archive,
        event_id="skill-run-confirmation",
        account_id="skill-account",
        event_type="skill.run_confirmed",
        payload={
            "skill_id": skill_id,
            "version": 1,
            "input_sha256": skill_input_sha256({"device": "bedroom"}),
        },
        minute=2,
    )
    tools = FakeTools()

    run = await SkillExecutor(catalog=catalog, tools=tools).execute(
        SkillRunRequest(
            account_id="skill-account",
            skill_id=skill_id,
            version=1,
            confirmation_event_id="skill-run-confirmation",
            inputs={"device": "bedroom"},
        )
    )

    assert run.status == "succeeded"
    assert run.output == {"story_id": "story-001"}
    assert [(step.step_id, step.phase, step.status) for step in run.steps] == [
        ("dim", "forward", "succeeded"),
        ("story", "forward", "succeeded"),
    ]
    assert [call[:2] for call in tools.invocations] == [
        ("set_light", False),
        ("play_story", False),
    ]

    memory_catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    search = await memory_catalog.context(
        MemorySearchQuery(
            account_id="skill-account",
            speaker_class="owner",
            text="睡前流程",
        )
    )
    assert [(item.kind, item.memory_kind, item.status) for item in search.items] == [
        ("skill", "procedural", "confirmed")
    ]
    assert set(search.items[0].source_event_ids) == {
        "skill-instruction",
        "skill-approval",
    }


@pytest.mark.asyncio
async def test_skill_search_projection_rebuilds_from_durable_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    _archive, catalog, _skill_id = await _approved(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM memory_search_documents WHERE kind = 'skill'")

    rebuilt = await catalog.rebuild_search_projections(account_id="skill-account")
    search = await MemoryCatalog.sqlite(
        path,
        extractor=RuleBasedMemoryExtractor(),
    ).context(
        MemorySearchQuery(
            account_id="skill-account",
            speaker_class="owner",
            text="睡前流程",
        )
    )

    assert rebuilt == 1
    assert [(item.kind, item.status) for item in search.items] == [
        ("skill", "confirmed")
    ]


@pytest.mark.asyncio
async def test_skill_input_schema_rejects_before_any_tool_runs(tmp_path: Path) -> None:
    path = tmp_path / "archive.sqlite3"
    archive, catalog, skill_id = await _approved(path)
    await _record(
        archive,
        event_id="invalid-run-confirmation",
        account_id="skill-account",
        event_type="skill.run_confirmed",
        payload={
            "skill_id": skill_id,
            "version": 1,
            "input_sha256": skill_input_sha256(
                {"device": "bedroom", "unexpected": True}
            ),
        },
        minute=2,
    )
    tools = FakeTools()

    with pytest.raises(SkillSchemaValidationError):
        await SkillExecutor(catalog=catalog, tools=tools).execute(
            SkillRunRequest(
                account_id="skill-account",
                skill_id=skill_id,
                version=1,
                confirmation_event_id="invalid-run-confirmation",
                inputs={"device": "bedroom", "unexpected": True},
            )
        )

    assert tools.invocations == []


def test_skill_schema_rejects_constraints_the_executor_does_not_enforce() -> None:
    proposal = _proposal()

    with pytest.raises(
        SkillSchemaValidationError,
        match="unsupported schema fields",
    ):
        replace(
            proposal,
            input_schema={
                "type": "object",
                "properties": {
                    "device": {"type": "string", "pattern": "^bedroom$"},
                },
                "required": ["device"],
                "additionalProperties": False,
            },
        )


@pytest.mark.asyncio
async def test_failed_step_compensates_completed_steps_in_reverse_and_keeps_audit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive, catalog, skill_id = await _approved(path)
    await _record(
        archive,
        event_id="failed-run-confirmation",
        account_id="skill-account",
        event_type="skill.run_confirmed",
        payload={
            "skill_id": skill_id,
            "version": 1,
            "input_sha256": skill_input_sha256({"device": "bedroom"}),
        },
        minute=2,
    )
    tools = FakeTools(fail_on="play_story")
    executor = SkillExecutor(catalog=catalog, tools=tools)

    with pytest.raises(SkillExecutionError) as raised:
        await executor.execute(
            SkillRunRequest(
                account_id="skill-account",
                skill_id=skill_id,
                version=1,
                confirmation_event_id="failed-run-confirmation",
                inputs={"device": "bedroom"},
            )
        )

    run = await catalog.get_run(
        account_id="skill-account",
        run_id=raised.value.run_id,
    )
    assert (run.status, run.rollback_status, run.error_code) == (
        "failed",
        "completed",
        "RuntimeError",
    )
    assert [(step.step_id, step.phase, step.status) for step in run.steps] == [
        ("dim", "forward", "succeeded"),
        ("story", "forward", "failed"),
        ("dim", "compensation", "succeeded"),
    ]
    assert [call[:2] for call in tools.invocations] == [
        ("set_light", False),
        ("play_story", False),
        ("set_light", True),
    ]
    assert tools.invocations[-1][2]["brightness"] == 80


@pytest.mark.asyncio
async def test_failed_compensation_template_does_not_reuse_previous_step_arguments(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    catalog = SkillCatalog.sqlite(path)
    await _record(
        archive,
        event_id="rollback-template-instruction",
        account_id="skill-account",
        event_type="speech.utterance_finalized",
        payload={"text": "先执行两步，失败时按相反顺序撤销。"},
    )
    proposal = replace(
        _proposal(source_event_ids=("rollback-template-instruction",)),
        steps=(
            SkillStepDefinition(
                step_id="first",
                tool_name="set_light",
                arguments={"brightness": 20},
                compensation_tool_name="set_light",
                compensation_arguments={
                    "brightness": "$steps.missing.output.brightness"
                },
            ),
            SkillStepDefinition(
                step_id="second",
                tool_name="set_light",
                arguments={"brightness": 30},
                compensation_tool_name="set_light",
                compensation_arguments={"brightness": 50},
            ),
            SkillStepDefinition(
                step_id="fail",
                tool_name="play_story",
                arguments={"duration_minutes": 5},
            ),
        ),
    )
    candidate = await catalog.propose(proposal)
    await _record(
        archive,
        event_id="rollback-template-approval",
        account_id="skill-account",
        event_type="skill.approved",
        payload={"skill_id": candidate.skill_id, "version": candidate.version},
        minute=1,
    )
    await catalog.approve(
        SkillApproval(
            account_id="skill-account",
            skill_id=candidate.skill_id,
            version=candidate.version,
            approval_event_id="rollback-template-approval",
        )
    )
    inputs = {"device": "bedroom"}
    await _record(
        archive,
        event_id="rollback-template-confirmation",
        account_id="skill-account",
        event_type="skill.run_confirmed",
        payload={
            "skill_id": candidate.skill_id,
            "version": candidate.version,
            "input_sha256": skill_input_sha256(inputs),
        },
        minute=2,
    )

    with pytest.raises(SkillExecutionError) as raised:
        await SkillExecutor(
            catalog=catalog,
            tools=FakeTools(fail_on="play_story"),
        ).execute(
            SkillRunRequest(
                account_id="skill-account",
                skill_id=candidate.skill_id,
                version=candidate.version,
                confirmation_event_id="rollback-template-confirmation",
                inputs=inputs,
            )
        )

    run = await catalog.get_run(
        account_id="skill-account",
        run_id=raised.value.run_id,
    )
    failed_compensation = next(
        step
        for step in run.steps
        if step.step_id == "first" and step.phase == "compensation"
    )
    assert failed_compensation.status == "failed"
    assert failed_compensation.arguments == {}


@pytest.mark.asyncio
async def test_non_json_tool_result_still_compensates_the_completed_side_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    catalog = SkillCatalog.sqlite(path)
    await _record(
        archive,
        event_id="non-json-instruction",
        account_id="skill-account",
        event_type="speech.utterance_finalized",
        payload={"text": "把灯调暗，失败时恢复亮度。"},
    )
    proposal = replace(
        _proposal(source_event_ids=("non-json-instruction",)),
        steps=(
            SkillStepDefinition(
                step_id="dim",
                tool_name="set_light",
                arguments={"brightness": 20},
                compensation_tool_name="set_light",
                compensation_arguments={"brightness": 80},
            ),
        ),
    )
    candidate = await catalog.propose(proposal)
    await _record(
        archive,
        event_id="non-json-approval",
        account_id="skill-account",
        event_type="skill.approved",
        payload={"skill_id": candidate.skill_id, "version": candidate.version},
        minute=1,
    )
    await catalog.approve(
        SkillApproval(
            account_id="skill-account",
            skill_id=candidate.skill_id,
            version=candidate.version,
            approval_event_id="non-json-approval",
        )
    )
    inputs = {"device": "bedroom"}
    await _record(
        archive,
        event_id="non-json-confirmation",
        account_id="skill-account",
        event_type="skill.run_confirmed",
        payload={
            "skill_id": candidate.skill_id,
            "version": candidate.version,
            "input_sha256": skill_input_sha256(inputs),
        },
        minute=2,
    )
    tools = NonJsonToolResult()

    with pytest.raises(SkillExecutionError) as raised:
        await SkillExecutor(catalog=catalog, tools=tools).execute(
            SkillRunRequest(
                account_id="skill-account",
                skill_id=candidate.skill_id,
                version=candidate.version,
                confirmation_event_id="non-json-confirmation",
                inputs=inputs,
            )
        )

    run = await catalog.get_run(
        account_id="skill-account",
        run_id=raised.value.run_id,
    )
    assert [call[:2] for call in tools.invocations] == [
        ("set_light", False),
        ("set_light", True),
    ]
    assert (run.status, run.rollback_status) == ("failed", "completed")


@pytest.mark.asyncio
async def test_new_approved_version_supersedes_content_without_mutating_old_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive, catalog, skill_id = await _approved(path)
    await _record(
        archive,
        event_id="skill-instruction-v2",
        account_id="skill-account",
        event_type="speech.utterance_finalized",
        payload={"text": "睡前流程改为先调暗灯光，再讲十分钟故事。"},
        minute=2,
    )
    second = await catalog.propose(
        _proposal(
            source_event_ids=("skill-instruction-v2",),
            description="先调暗灯光，再播放十分钟睡前故事。",
        )
    )
    await _record(
        archive,
        event_id="skill-approval-v2",
        account_id="skill-account",
        event_type="skill.approved",
        payload={"skill_id": skill_id, "version": 2},
        minute=3,
    )

    approved = await catalog.approve(
        SkillApproval(
            account_id="skill-account",
            skill_id=skill_id,
            version=second.version,
            approval_event_id="skill-approval-v2",
        )
    )
    first = await catalog.get_version(
        account_id="skill-account",
        skill_id=skill_id,
        version=1,
    )

    assert (first.status, approved.status) == ("superseded", "approved")
    assert first.description == "先调暗灯光，再播放一个睡前故事。"
    assert approved.description == "先调暗灯光，再播放十分钟睡前故事。"


@pytest.mark.asyncio
async def test_skill_name_case_variants_create_versions_of_the_same_definition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    catalog = SkillCatalog.sqlite(path)
    for event_id, minute in (("case-name-v1", 0), ("case-name-v2", 1)):
        await _record(
            archive,
            event_id=event_id,
            account_id="skill-account",
            event_type="speech.utterance_finalized",
            payload={"text": "Teach the bedtime routine."},
            minute=minute,
        )

    first = await catalog.propose(
        _proposal(
            name="Bedtime Routine",
            source_event_ids=("case-name-v1",),
        )
    )
    second = await catalog.propose(
        _proposal(
            name="BEDTIME ROUTINE",
            source_event_ids=("case-name-v2",),
        )
    )

    assert second.skill_id == first.skill_id
    assert second.version == 2
    assert second.name == "Bedtime Routine"


@pytest.mark.asyncio
async def test_repeated_success_skill_requires_three_success_evidence_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    catalog = SkillCatalog.sqlite(path)
    for index in range(3):
        await _record(
            archive,
            event_id=f"tool-success-{index}",
            account_id="skill-account",
            event_type="tool.execution_succeeded",
            payload={"tool": "set_light", "result": "ok"},
            speaker_class="system",
            minute=index,
        )

    proposed = await catalog.propose(
        _proposal(
            source_event_ids=(
                "tool-success-0",
                "tool-success-1",
                "tool-success-2",
            ),
            source_kind="repeated_tool_success",
        )
    )

    assert proposed.source_kind == "repeated_tool_success"
    assert proposed.source_event_ids == (
        "tool-success-0",
        "tool-success-1",
        "tool-success-2",
    )
