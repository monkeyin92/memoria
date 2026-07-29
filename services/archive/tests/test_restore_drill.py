from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemoryClaimReview, MemorySearchQuery
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.object_store import EncryptedLocalObjectStore
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.archive.restore_drill import (
    LocalObjectRestorePlan,
    copy_and_verify_local_objects,
    run_postgres_restore_drill,
)
from services.archive.skill_domain import (
    SkillApproval,
    SkillProposal,
    SkillRunRequest,
    SkillStepDefinition,
    skill_input_sha256,
)
from services.archive.skill_executor import SkillExecutor


class RestoreSkillTools:
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
        del account_id, run_id, step_id, tool_name, arguments, compensation
        return {"ok": True}


@pytest.mark.asyncio
async def test_restore_drill_copies_ciphertext_and_verifies_plaintext_manifest(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    source_root = tmp_path / "source-objects"
    restore_root = tmp_path / "restore-objects"
    source = EncryptedLocalObjectStore(
        root=source_root,
        key=key,
        key_version="archive-v1",
    )
    references = (
        await source.put(
            account_id="account-a",
            purpose="source-audio",
            data=b"first object",
            media_type="audio/wav",
        ),
        await source.put(
            account_id="account-b",
            purpose="life-photo",
            data=b"second object",
            media_type="image/jpeg",
        ),
    )

    report = await copy_and_verify_local_objects(
        source_root=source_root,
        restore_root=restore_root,
        references=references,
        key=key,
        key_version="archive-v1",
    )

    assert report.object_count == 2
    assert report.failed_count == 0
    assert report.key_versions == ("archive-v1",)
    assert len(report.manifest_sha256) == 64
    for reference in references:
        assert (restore_root / reference.object_key).read_bytes() == (
            source_root / reference.object_key
        ).read_bytes()


def _database_dsn(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(parsed._replace(path=f"/{database}"))


async def _database_command(admin_dsn: str, sql: str) -> None:
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN") or not os.getenv("MEMORIA_TEST_POSTGRES_CONTAINER"),
    reason="set PostgreSQL DSN and container for the joint restore drill",
)
async def test_postgres_restore_drill_rebuilds_reviewed_projection_and_rls(
    tmp_path: Path,
) -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    container = os.environ["MEMORIA_TEST_POSTGRES_CONTAINER"]
    suffix = uuid.uuid4().hex[:12]
    source_database = f"memoria_drill_source_{suffix}"
    restore_database = f"memoria_drill_restore_{suffix}"
    source_dsn = _database_dsn(admin_dsn, source_database)
    restore_dsn = _database_dsn(admin_dsn, restore_database)
    archive_key = Fernet.generate_key().decode("ascii")
    archive_source_root = tmp_path / "archive-source"
    archive_restore_root = tmp_path / "archive-restored"
    await _database_command(admin_dsn, f'CREATE DATABASE "{source_database}"')
    archive = PostgresLifeArchive(source_dsn)
    catalog = PostgresMemoryCatalog(source_dsn, extractor=RuleBasedMemoryExtractor())
    skills = PostgresSkillCatalog(source_dsn)
    skill_run_id = ""
    try:
        await archive.initialize()
        await catalog.initialize()
        await skills.initialize()
        for account_id, event_id, text in (
            ("restore-owner-a", "restore-event-a", "我们家的家训是说到做到。"),
            ("restore-owner-b", "restore-event-b", "我做项目时先确认目标。"),
        ):
            await archive.record(
                EvidenceEvent(
                    event_id=event_id,
                    account_id=account_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
                    speaker_class="owner",
                    source="restore-test",
                    payload={
                        "text": text,
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                    },
                )
            )
        await catalog.compile_pending(limit=100)
        candidate = await skills.propose(
            SkillProposal(
                account_id="restore-owner-a",
                name="睡前流程",
                description="关闭床头灯。",
                trigger_phrases=("晚安",),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                output_template={"ok": "$steps.light.output.ok"},
                allowed_tools=("set_light",),
                steps=(
                    SkillStepDefinition(
                        step_id="light",
                        tool_name="set_light",
                        arguments={"brightness": 0},
                    ),
                ),
                source_kind="explicit_instruction",
                source_event_ids=("restore-event-a",),
            )
        )
        await archive.record(
            EvidenceEvent(
                event_id="restore-skill-approval",
                account_id="restore-owner-a",
                event_type="skill.approved",
                occurred_at=datetime(2026, 7, 19, 12, 1, tzinfo=UTC),
                speaker_class="owner",
                source="restore-test",
                payload={
                    "skill_id": candidate.skill_id,
                    "version": candidate.version,
                },
            )
        )
        await skills.approve(
            SkillApproval(
                account_id="restore-owner-a",
                skill_id=candidate.skill_id,
                version=candidate.version,
                approval_event_id="restore-skill-approval",
            )
        )
        skill_inputs: dict[str, object] = {}
        await archive.record(
            EvidenceEvent(
                event_id="restore-skill-confirmation",
                account_id="restore-owner-a",
                event_type="skill.run_confirmed",
                occurred_at=datetime(2026, 7, 19, 12, 2, tzinfo=UTC),
                speaker_class="owner",
                source="restore-test",
                payload={
                    "skill_id": candidate.skill_id,
                    "version": candidate.version,
                    "input_sha256": skill_input_sha256(skill_inputs),
                },
            )
        )
        skill_run = await SkillExecutor(
            catalog=skills,
            tools=RestoreSkillTools(),
        ).execute(
            SkillRunRequest(
                account_id="restore-owner-a",
                skill_id=candidate.skill_id,
                version=candidate.version,
                confirmation_event_id="restore-skill-confirmation",
                inputs=skill_inputs,
            )
        )
        skill_run_id = skill_run.run_id
        object_reference = await EncryptedLocalObjectStore(
            root=archive_source_root,
            key=archive_key,
            key_version="archive-v1",
        ).put(
            account_id="restore-owner-a",
            purpose="source-audio",
            data=b"encrypted life archive sample",
            media_type="audio/wav",
        )
        connection = await asyncpg.connect(source_dsn)
        try:
            await connection.execute(
                """
                INSERT INTO archive_evidence_blobs (
                    blob_id, account_id, evidence_event_id, object_key,
                    media_type, byte_count, content_sha256,
                    encryption_key_version, retention_policy
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'owner-controlled')
                """,
                uuid.uuid4(),
                object_reference.account_id,
                "restore-event-a",
                object_reference.object_key,
                object_reference.media_type,
                object_reference.byte_count,
                object_reference.content_sha256,
                object_reference.encryption_key_version,
            )
        finally:
            await connection.close()
        claim = (await catalog.review_queue(account_id="restore-owner-a"))[0]
        await catalog.review(
            MemoryClaimReview(
                account_id="restore-owner-a",
                claim_id=claim.item_id,
                action="correct",
                corrected_value="我们家的家训是答应的事一定做到。",
            )
        )
        await catalog.compile_pending(limit=100)
    finally:
        await skills.close()
        await catalog.close()
        await archive.close()

    try:
        report = await run_postgres_restore_drill(
            source_dsn=source_dsn,
            admin_dsn=admin_dsn,
            restore_database=restore_database,
            dump_path=tmp_path / "archive.dump",
            postgres_container=container,
            local_objects=(
                LocalObjectRestorePlan(
                    domain="archive",
                    source_root=archive_source_root,
                    restore_root=archive_restore_root,
                    keys={"archive-v1": archive_key},
                ),
            ),
        )
        restored = PostgresMemoryCatalog(
            restore_dsn,
            extractor=RuleBasedMemoryExtractor(),
        )
        restored_skills = PostgresSkillCatalog(restore_dsn)
        try:
            result = await restored.context(
                MemorySearchQuery(
                    account_id="restore-owner-a",
                    speaker_class="owner",
                    text="答应",
                )
            )
            skill_result = await restored.context(
                MemorySearchQuery(
                    account_id="restore-owner-a",
                    speaker_class="owner",
                    text="睡前流程",
                )
            )
            restored_run = await restored_skills.get_run(
                account_id="restore-owner-a",
                run_id=skill_run_id,
            )
        finally:
            await restored_skills.close()
            await restored.close()

        assert report.passed is True
        assert report.source.event_manifest_sha256 == report.restored.event_manifest_sha256
        assert report.projection_rebuild.failed_events == 0
        assert report.projection_rebuild.skill_documents_rebuilt == 1
        assert report.objects["archive"].failed_count == 0
        assert report.objects["archive"].object_count == 1
        assert archive_key not in json.dumps(report.as_dict(), ensure_ascii=False)
        assert report.rls.passed is True
        assert all(count == 0 for count in report.orphan_counts.values())
        assert [item.title for item in result.items] == ["我们家的家训是答应的事一定做到。"]
        assert [(item.kind, item.status) for item in skill_result.items] == [
            ("skill", "confirmed")
        ]
        assert restored_run.status == "succeeded"
        assert (archive_restore_root / object_reference.object_key).read_bytes() == (
            archive_source_root / object_reference.object_key
        ).read_bytes()
    finally:
        await _database_command(
            admin_dsn,
            f'DROP DATABASE IF EXISTS "{restore_database}" WITH (FORCE)',
        )
        await _database_command(
            admin_dsn,
            f'DROP DATABASE IF EXISTS "{source_database}" WITH (FORCE)',
        )
