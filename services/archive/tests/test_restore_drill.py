from __future__ import annotations

import asyncio
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
    ControllerRLSRestoreReport,
    LocalObjectRestorePlan,
    _controller_rls_metadata_failures,
    copy_and_verify_local_objects,
    rebuild_postgres_memory_projections,
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
from services.evolution.domain import (
    CandidateArtifact,
    FenceSnapshot,
    GateResult,
    LayerVerdict,
    LearningSignal,
    SpeakerSnapshot,
    ValidationReport,
)
from services.evolution.postgres_store import PostgresEvolutionStore
from services.governance.lifecycle_tables import (
    POSTGRES_CONTROLLER_ONLY_RLS_TABLES,
    POSTGRES_EVOLUTION_ACCOUNT_TABLES,
)
from services.self_model.domain import SourceInput
from services.self_model.postgres_registry import PostgresSelfModelRegistry


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


class RebuildEmbedderStub:
    model = "rebuild-vector-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "杭州" in text else (0.0, 1.0)


def test_evolution_controller_rls_metadata_contract_includes_control_state() -> None:
    schema = (Path(__file__).resolve().parents[2] / "evolution" / "postgres_schema.sql").read_text(
        encoding="utf-8"
    )
    assert "evolution_control_state" in POSTGRES_CONTROLLER_ONLY_RLS_TABLES
    assert "evolution_control_state" not in POSTGRES_EVOLUTION_ACCOUNT_TABLES
    for table in POSTGRES_CONTROLLER_ONLY_RLS_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in schema
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in schema
    assert schema.count("TO memoria_evolution USING (true) WITH CHECK (true);") == len(
        POSTGRES_CONTROLLER_ONLY_RLS_TABLES
    )

    rows = tuple(
        {
            "table_name": table,
            "table_exists": True,
            "rls_enabled": True,
            "force_rls": True,
            "controller_policy": True,
        }
        for table in POSTGRES_CONTROLLER_ONLY_RLS_TABLES
    )
    assert (
        _controller_rls_metadata_failures(
            rows,
            controller_role={"rolsuper": False, "rolbypassrls": False},
        )
        == ()
    )
    missing_force = tuple(
        {**row, "force_rls": False} if row["table_name"] == "evolution_control_state" else row
        for row in rows
    )
    assert "evolution_control_state:force_rls_disabled" in _controller_rls_metadata_failures(
        missing_force,
        controller_role={"rolsuper": False, "rolbypassrls": False},
    )


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


async def _seed_evolution_restore_data(dsn: str, suffix: str) -> tuple[str, str]:
    """Seed both account-private and redacted controller evidence for restore."""

    store = PostgresEvolutionStore(dsn)
    store.initialize()
    now = datetime(2026, 7, 19, 12, 3, tzinfo=UTC)
    owner_account = f"restore-evolution-owner-{suffix}"
    owner_signal_id = f"restore-evolution-signal-owner-{suffix}"
    global_signal_id = f"restore-evolution-signal-global-{suffix}"
    owner_candidate_id = f"restore-evolution-candidate-owner-{suffix}"
    global_candidate_id = f"restore-evolution-candidate-global-{suffix}"

    owner_speaker = SpeakerSnapshot(
        classification="owner",
        reason_code="formal_owner",
        history_eligible=True,
        owner_projection_eligible=True,
    )
    global_speaker = SpeakerSnapshot(
        classification="uncertain",
        reason_code="redacted_global",
        history_eligible=False,
        owner_projection_eligible=False,
    )

    def signal(
        signal_id: str,
        *,
        scope: str,
        account_id: str | None,
        speaker: SpeakerSnapshot,
    ) -> LearningSignal:
        event_id = f"restore-evolution-event-{signal_id}"
        return LearningSignal(
            signal_id=signal_id,
            task_family="restore-evolution-contract",
            scope=scope,  # type: ignore[arg-type]
            account_id=account_id,
            fence=FenceSnapshot(f"restore-evolution-session-{signal_id}", 1, 1, 0),
            speaker=speaker,
            source_event_ids=(event_id,),
            result=LayerVerdict("fail", reason_codes=("restore_fixture",)),
            process=LayerVerdict("pass"),
            quality=LayerVerdict("fail", reason_codes=("restore_fixture",)),
            environment_version="restore-evolution-v1",
            failure_code="restore_fixture",
            diagnosis="joint restore fixture",
            created_at=now,
        )

    await asyncio.gather(
        asyncio.to_thread(
            store.append_signal,
            signal(owner_signal_id, scope="owner_private", account_id=owner_account, speaker=owner_speaker),
        ),
        asyncio.to_thread(
            store.append_signal,
            signal(global_signal_id, scope="global_redacted", account_id=None, speaker=global_speaker),
        ),
    )
    await asyncio.to_thread(
        store.mark_signals_processed,
        (owner_signal_id, global_signal_id),
        processed_at=now,
    )

    def candidate(
        candidate_id: str,
        *,
        scope: str,
        account_id: str | None,
        source_signal_id: str,
    ) -> CandidateArtifact:
        return CandidateArtifact(
            candidate_id=candidate_id,
            task_family="restore-evolution-contract",
            kind="prompt",
            scope=scope,  # type: ignore[arg-type]
            account_id=account_id,
            version=1,
            payload={
                "proposal": {
                    "instruction": "restore fixture",
                    "match_terms": ["restore fixture"],
                }
            },
            source_signal_ids=(source_signal_id,),
            expected_behavior="retain evolution evidence after restore",
            regression_guards=("retention", "privacy"),
            risk="low",
            trusted_root_sha256="b" * 64,
            created_at=now,
            updated_at=now,
        )

    for artifact in (
        candidate(
            owner_candidate_id,
            scope="owner_private",
            account_id=owner_account,
            source_signal_id=owner_signal_id,
        ),
        candidate(
            global_candidate_id,
            scope="global_redacted",
            account_id=None,
            source_signal_id=global_signal_id,
        ),
    ):
        await asyncio.to_thread(store.create_candidate, artifact)
        validation = ValidationReport(
            validation_id=f"restore-evolution-validation-{artifact.candidate_id}",
            candidate_id=artifact.candidate_id,
            gates=tuple(
                GateResult(name, True, (f"restore-evolution-{name}",))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
            created_at=now,
        )
        await asyncio.to_thread(store.record_validation, validation)
        await asyncio.to_thread(store.transition_candidate, artifact.candidate_id, "validated")
        await asyncio.to_thread(store.transition_candidate, artifact.candidate_id, "canary")
        await asyncio.to_thread(
            store.record_activation,
            candidate_id=artifact.candidate_id,
            task_id=f"restore-evolution-task-{artifact.candidate_id}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"restore-evolution-activation-{artifact.candidate_id}",
        )
    await asyncio.to_thread(
        store.set_control_state,
        f"restore-evolution-control-{suffix}",
        {"candidate_id": owner_candidate_id},
        updated_at=now,
    )
    return owner_candidate_id, global_candidate_id


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
    evolution_candidate_ids: tuple[str, str] = ("", "")
    try:
        await _database_command(
            admin_dsn,
            """
            DO $evolution_role$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_evolution'
                ) THEN
                    CREATE ROLE memoria_evolution
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
                END IF;
            END
            $evolution_role$
            """,
        )
        await archive.initialize()
        await catalog.initialize()
        await skills.initialize()
        evolution_candidate_ids = await _seed_evolution_restore_data(
            source_dsn,
            suffix,
        )
        for account_id, event_id, text in (
            ("restore-owner-a", "restore-event-a", "我们家的家训是说到做到。"),
            ("restore-owner-b", "restore-event-b", "我做项目时先确认目标。"),
            ("restore-owner-a", "restore-event-person", "我的朋友小林今年30岁。"),
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
        self_model = PostgresSelfModelRegistry(source_dsn)
        await self_model.initialize()
        try:
            identity = await asyncpg.connect(source_dsn)
            try:
                person = await identity.fetchrow(
                    """
                    SELECT person_id
                    FROM person_entities
                    WHERE account_id = $1 AND canonical_key = $2
                    """,
                    "restore-owner-a",
                    "friend:小林",
                )
                relationship = await identity.fetchrow(
                    """
                    SELECT relationship_id
                    FROM relationships
                    WHERE account_id = $1 AND person_id = $2
                    """,
                    "restore-owner-a",
                    person["person_id"] if person is not None else None,
                )
            finally:
                await identity.close()
            assert person is not None
            assert relationship is not None
            await self_model.create_relationship_profile(
                account_id="restore-owner-a",
                idempotency_key="restore-profile-a",
                person_id=str(person["person_id"]),
                relationship_id=str(relationship["relationship_id"]),
                salutation="小林",
                tone="温和",
                advice_style="先倾听再建议",
                boundaries=("不分享私密经历",),
                sources=(SourceInput(source_event_id="restore-event-person"),),
            )
        finally:
            await self_model.close()
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
        restored_self_model = PostgresSelfModelRegistry(restore_dsn)
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
            profiles = await restored_self_model.relationship_profiles(
                account_id="restore-owner-a", effective_only=False
            )
            restored_identity = await asyncpg.connect(restore_dsn)
            try:
                profile_refs = await restored_identity.fetchrow(
                    """
                    SELECT person_id, relationship_id
                    FROM self_model_relationship_profiles
                    WHERE account_id = $1
                    """,
                    "restore-owner-a",
                )
                assert profile_refs is not None
                assert (
                    await restored_identity.fetchval(
                        "SELECT 1 FROM person_entities WHERE person_id = $1",
                        profile_refs["person_id"],
                    )
                    == 1
                )
                assert (
                    await restored_identity.fetchval(
                        "SELECT 1 FROM relationships WHERE relationship_id = $1",
                        profile_refs["relationship_id"],
                    )
                    == 1
                )
            finally:
                await restored_identity.close()
        finally:
            await restored_self_model.close()
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
        assert isinstance(report.rls.controller_only, ControllerRLSRestoreReport)
        assert report.rls.controller_only.passed is True
        assert report.rls.controller_only.tables_checked == len(
            POSTGRES_CONTROLLER_ONLY_RLS_TABLES
        )
        assert report.rls.controller_only.unauthorized_roles_checked == 2
        assert report.rls.controller_only.visibility_probes == 2 * len(
            POSTGRES_CONTROLLER_ONLY_RLS_TABLES
        )
        assert report.rls.controller_only.nonempty_tables_probed > 0
        assert report.rls.controller_only.failures == ()
        assert all(count == 0 for count in report.orphan_counts.values())
        assert all(
            report.source.counts.get(table, 0) == report.restored.counts.get(table, 0) > 0
            for table in POSTGRES_EVOLUTION_ACCOUNT_TABLES
        )
        restored_evolution = await asyncpg.connect(restore_dsn)
        try:
            await restored_evolution.execute("SET ROLE memoria_evolution")
            assert (
                await restored_evolution.fetchval(
                    "SELECT count(*) FROM evolution_control_state"
                )
                >= 1
            )
            await restored_evolution.execute("RESET ROLE")
            assert (
                await restored_evolution.fetchval(
                    "SELECT count(*) FROM evolution_candidates WHERE candidate_id = ANY($1::text[])",
                    list(evolution_candidate_ids),
                )
                == 2
            )
            assert (
                await restored_evolution.fetchval(
                    "SELECT count(*) FROM evolution_control_state WHERE state_key = $1",
                    f"restore-evolution-control-{suffix}",
                )
                == 1
            )
        finally:
            await restored_evolution.close()
        assert [item.title for item in result.items] == ["我们家的家训是答应的事一定做到。"]
        assert [(item.kind, item.status) for item in skill_result.items] == [("skill", "confirmed")]
        assert restored_run.status == "succeeded"
        assert len(profiles) == 1
        assert profiles[0].salutation == "小林"
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


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the pgvector projection rebuild contract",
)
async def test_projection_rebuild_requires_and_recreates_pgvector_documents() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_rebuild_vector_{uuid.uuid4().hex[:12]}"
    dsn = _database_dsn(admin_dsn, database)
    account_id = "rebuild-vector-owner"
    archive: PostgresLifeArchive | None = None
    catalog: PostgresMemoryCatalog | None = None
    await _database_command(admin_dsn, f'CREATE DATABASE "{database}"')
    try:
        archive = PostgresLifeArchive(dsn)
        catalog = PostgresMemoryCatalog(
            dsn,
            extractor=RuleBasedMemoryExtractor(),
            embedder=RebuildEmbedderStub(),
            require_vector=True,
        )
        await archive.initialize()
        await catalog.initialize()
        await archive.record(
            EvidenceEvent(
                event_id="rebuild-vector-event",
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime(2026, 8, 7, tzinfo=UTC),
                speaker_class="owner",
                source="rebuild-vector-test",
                payload={
                    "text": "我在杭州读过书。",
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": True,
                },
            )
        )
        await catalog.compile_pending(limit=100)
        connection = await asyncpg.connect(dsn)
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM memory_vector_documents WHERE account_id = $1",
                    account_id,
                )
                > 0
            )
            with pytest.raises(ValueError, match="requires an embedder"):
                await rebuild_postgres_memory_projections(dsn, require_vector=True)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM memory_vector_documents WHERE account_id = $1",
                    account_id,
                )
                > 0
            )
        finally:
            await connection.close()

        report = await rebuild_postgres_memory_projections(
            dsn,
            extractor=RuleBasedMemoryExtractor(),
            embedder=RebuildEmbedderStub(),
            require_vector=True,
        )
        connection = await asyncpg.connect(dsn)
        try:
            models = await connection.fetch(
                """
                SELECT DISTINCT embedding_model, embedding_dimensions
                FROM memory_vector_documents WHERE account_id = $1
                """,
                account_id,
            )
        finally:
            await connection.close()

        assert report.failed_events == 0
        assert {
            (str(row["embedding_model"]), int(row["embedding_dimensions"])) for row in models
        } == {("rebuild-vector-test-v1", 2)}
    finally:
        if catalog is not None:
            await catalog.close()
        if archive is not None:
            await archive.close()
        await _database_command(admin_dsn, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
