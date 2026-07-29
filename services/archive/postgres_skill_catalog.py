"""PostgreSQL/RLS persistence for executable procedural memories."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import asyncpg

from services.archive.memory_domain import DomainCategory, MemorySensitivity
from services.archive.skill_domain import (
    SkillApproval,
    SkillApprovalRequiredError,
    SkillConfirmationRequiredError,
    SkillExecutionError,
    SkillNotFoundError,
    SkillProposal,
    SkillRollbackStatus,
    SkillRun,
    SkillRunRequest,
    SkillRunStatus,
    SkillRunStep,
    SkillSourceKind,
    SkillStepDefinition,
    SkillStepPhase,
    SkillStepStatus,
    SkillVersion,
    SkillVersionStatus,
    canonical_json,
    skill_input_sha256,
    validate_json_instance,
)

_SCHEMA_PATH = Path(__file__).with_name("postgres_skill_schema.sql")


class PostgresSkillCatalog:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL skill catalog requires a DSN")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        try:
            async with pool.acquire() as connection:
                await connection.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        except Exception:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL skill catalog is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    async def propose(self, proposal: SkillProposal) -> SkillVersion:
        pool = await self._ready_pool()
        now = datetime.now(UTC)
        skill_id = _stable_uuid("skill", proposal.account_id, proposal.name.casefold())
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, proposal.account_id)
            await self._validate_proposal_evidence(connection, proposal)
            definition = await connection.fetchrow(
                """
                SELECT * FROM skill_definitions
                WHERE account_id = $1 AND skill_id = $2
                FOR UPDATE
                """,
                proposal.account_id,
                skill_id,
            )
            if definition is None:
                version = 1
                approved_version: int | None = None
                await connection.execute(
                    """
                    INSERT INTO skill_definitions (
                        skill_id, account_id, name, status, latest_version,
                        created_at, updated_at
                    ) VALUES ($1, $2, $3, 'candidate', 1, $4, $4)
                    """,
                    skill_id,
                    proposal.account_id,
                    proposal.name,
                    now,
                )
            else:
                skill_id = cast(uuid.UUID, definition["skill_id"])
                version = int(definition["latest_version"]) + 1
                approved_version = (
                    int(definition["approved_version"])
                    if definition["approved_version"] is not None
                    else None
                )
                await connection.execute(
                    """
                    UPDATE skill_definitions
                    SET latest_version = $1, updated_at = $2
                    WHERE skill_id = $3 AND account_id = $4
                    """,
                    version,
                    now,
                    skill_id,
                    proposal.account_id,
                )
            await connection.execute(
                """
                INSERT INTO skill_versions (
                    skill_id, version, account_id, status, description,
                    trigger_phrases, input_schema, output_schema, output_template,
                    allowed_tools, steps, source_kind, domain_category,
                    sensitivity, salience, created_at
                ) VALUES (
                    $1, $2, $3, 'candidate', $4, $5::jsonb, $6::jsonb, $7::jsonb,
                    $8::jsonb, $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15
                )
                """,
                skill_id,
                version,
                proposal.account_id,
                proposal.description,
                canonical_json(list(proposal.trigger_phrases)),
                canonical_json(dict(proposal.input_schema)),
                canonical_json(dict(proposal.output_schema)),
                canonical_json(dict(proposal.output_template)),
                canonical_json(list(proposal.allowed_tools)),
                _steps_json(proposal.steps),
                proposal.source_kind,
                proposal.domain_category,
                proposal.sensitivity,
                proposal.salience,
                now,
            )
            await connection.executemany(
                """
                INSERT INTO skill_version_evidence (
                    skill_id, version, account_id, source_event_id
                ) VALUES ($1, $2, $3, $4)
                """,
                [
                    (skill_id, version, proposal.account_id, source_event_id)
                    for source_event_id in proposal.source_event_ids
                ],
            )
            if approved_version is None:
                await self._write_search_projection(
                    connection,
                    skill_id=skill_id,
                    account_id=proposal.account_id,
                    name=proposal.name,
                    version=version,
                    status="candidate",
                    description=proposal.description,
                    trigger_phrases=proposal.trigger_phrases,
                    steps=proposal.steps,
                    domain_category=proposal.domain_category,
                    sensitivity=proposal.sensitivity,
                    salience=proposal.salience,
                    source_event_ids=proposal.source_event_ids,
                    observed_at=now,
                )
            return await self._load_version(
                connection,
                account_id=proposal.account_id,
                skill_id=skill_id,
                version=version,
            )

    async def approve(self, command: SkillApproval) -> SkillVersion:
        pool = await self._ready_pool()
        now = datetime.now(UTC)
        skill_id = _uuid_or_not_found(command.skill_id)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, command.account_id)
            row = await self._version_row(
                connection,
                account_id=command.account_id,
                skill_id=skill_id,
                version=command.version,
                for_update=True,
            )
            if str(row["status"]) != "candidate":
                raise SkillApprovalRequiredError("only candidate skill versions can be approved")
            await self._validate_owner_event(
                connection,
                account_id=command.account_id,
                event_id=command.approval_event_id,
                event_type="skill.approved",
                skill_id=command.skill_id,
                version=command.version,
                input_sha256=None,
            )
            await connection.execute(
                """
                UPDATE skill_versions
                SET status = 'superseded'
                WHERE account_id = $1 AND skill_id = $2 AND status = 'approved'
                """,
                command.account_id,
                skill_id,
            )
            await connection.execute(
                """
                UPDATE skill_versions
                SET status = 'approved', approved_at = $1, approval_event_id = $2
                WHERE account_id = $3 AND skill_id = $4 AND version = $5
                """,
                now,
                command.approval_event_id,
                command.account_id,
                skill_id,
                command.version,
            )
            await connection.execute(
                """
                UPDATE skill_definitions
                SET status = 'approved', approved_version = $1, updated_at = $2
                WHERE account_id = $3 AND skill_id = $4
                """,
                command.version,
                now,
                command.account_id,
                skill_id,
            )
            version = await self._load_version(
                connection,
                account_id=command.account_id,
                skill_id=skill_id,
                version=command.version,
            )
            await self._write_search_projection(
                connection,
                skill_id=skill_id,
                account_id=version.account_id,
                name=version.name,
                version=version.version,
                status="confirmed",
                description=version.description,
                trigger_phrases=version.trigger_phrases,
                steps=version.steps,
                domain_category=version.domain_category,
                sensitivity=version.sensitivity,
                salience=version.salience,
                source_event_ids=(
                    *version.source_event_ids,
                    command.approval_event_id,
                ),
                observed_at=now,
            )
            return version

    async def get_version(
        self,
        *,
        account_id: str,
        skill_id: str,
        version: int,
    ) -> SkillVersion:
        parsed_skill_id = _uuid_or_not_found(skill_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            return await self._load_version(
                connection,
                account_id=account_id,
                skill_id=parsed_skill_id,
                version=version,
            )

    async def list_versions(
        self,
        *,
        account_id: str,
    ) -> tuple[SkillVersion, ...]:
        if not account_id.strip():
            raise ValueError("skill list requires account_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT skill_id, version
                FROM skill_versions
                WHERE account_id = $1
                ORDER BY created_at DESC, skill_id, version DESC
                """,
                account_id,
            )
            return tuple(
                [
                    await self._load_version(
                        connection,
                        account_id=account_id,
                        skill_id=cast(uuid.UUID, row["skill_id"]),
                        version=int(row["version"]),
                    )
                    for row in rows
                ]
            )

    async def rebuild_search_projections(self, *, account_id: str) -> int:
        if not account_id.strip():
            raise ValueError("skill projection rebuild requires account_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT skill_id, COALESCE(approved_version, latest_version) AS version
                FROM skill_definitions
                WHERE account_id = $1 AND status != 'retired'
                ORDER BY skill_id
                """,
                account_id,
            )
            await connection.execute(
                """
                DELETE FROM memory_search_documents
                WHERE account_id = $1 AND kind = 'skill'
                """,
                account_id,
            )
            for row in rows:
                skill_id = cast(uuid.UUID, row["skill_id"])
                version = await self._load_version(
                    connection,
                    account_id=account_id,
                    skill_id=skill_id,
                    version=int(row["version"]),
                )
                source_event_ids = version.source_event_ids
                status: Literal["candidate", "confirmed"] = "candidate"
                observed_at = version.created_at
                if version.status == "approved":
                    status = "confirmed"
                    observed_at = version.approved_at or version.created_at
                    if version.approval_event_id is not None:
                        source_event_ids = (
                            *source_event_ids,
                            version.approval_event_id,
                        )
                await self._write_search_projection(
                    connection,
                    skill_id=skill_id,
                    account_id=version.account_id,
                    name=version.name,
                    version=version.version,
                    status=status,
                    description=version.description,
                    trigger_phrases=version.trigger_phrases,
                    steps=version.steps,
                    domain_category=version.domain_category,
                    sensitivity=version.sensitivity,
                    salience=version.salience,
                    source_event_ids=tuple(dict.fromkeys(source_event_ids)),
                    observed_at=observed_at,
                )
            return len(rows)

    async def start_run(self, request: SkillRunRequest) -> SkillRun:
        pool = await self._ready_pool()
        run_id = uuid.uuid4()
        skill_id = _uuid_or_not_found(request.skill_id)
        now = datetime.now(UTC)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            version = await self._load_version(
                connection,
                account_id=request.account_id,
                skill_id=skill_id,
                version=request.version,
            )
            if version.status != "approved":
                raise SkillApprovalRequiredError(
                    "only the currently approved skill version can execute"
                )
            await self._validate_owner_event(
                connection,
                account_id=request.account_id,
                event_id=request.confirmation_event_id,
                event_type="skill.run_confirmed",
                skill_id=request.skill_id,
                version=request.version,
                input_sha256=skill_input_sha256(request.inputs),
            )
            validate_json_instance(version.input_schema, request.inputs)
            try:
                await connection.execute(
                    """
                    INSERT INTO skill_runs (
                        run_id, account_id, skill_id, skill_version, status,
                        rollback_status, confirmation_event_id, input_json, started_at
                    ) VALUES (
                        $1, $2, $3, $4, 'running', 'not_required', $5, $6::jsonb, $7
                    )
                    """,
                    run_id,
                    request.account_id,
                    skill_id,
                    request.version,
                    request.confirmation_event_id,
                    canonical_json(dict(request.inputs)),
                    now,
                )
            except asyncpg.UniqueViolationError as exc:
                raise SkillConfirmationRequiredError(
                    "skill run confirmation evidence was already consumed"
                ) from exc
            return await self._load_run(
                connection,
                account_id=request.account_id,
                run_id=run_id,
            )

    async def append_run_step(
        self,
        *,
        run_id: str,
        account_id: str,
        step_id: str,
        phase: SkillStepPhase,
        tool_name: str,
        arguments: Mapping[str, object],
        status: SkillStepStatus,
        output: object | None,
        error_code: str | None,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        pool = await self._ready_pool()
        run_uuid = _uuid_or_not_found(run_id)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT status FROM skill_runs
                WHERE run_id = $1 AND account_id = $2
                FOR UPDATE
                """,
                run_uuid,
                account_id,
            )
            if row is None:
                raise SkillNotFoundError(run_id)
            if str(row["status"]) != "running":
                raise SkillExecutionError(run_id, "cannot append audit to a completed run")
            sequence = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM skill_run_steps WHERE run_id = $1
                    """,
                    run_uuid,
                )
            )
            await connection.execute(
                """
                INSERT INTO skill_run_steps (
                    run_id, sequence, account_id, step_id, phase, tool_name,
                    arguments_json, status, output_json, error_code,
                    started_at, completed_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb, $10, $11, $12
                )
                """,
                run_uuid,
                sequence,
                account_id,
                step_id,
                phase,
                tool_name,
                canonical_json(dict(arguments)),
                status,
                canonical_json(output) if output is not None else None,
                error_code,
                started_at.astimezone(UTC),
                completed_at.astimezone(UTC),
            )

    async def finish_run(
        self,
        *,
        run_id: str,
        account_id: str,
        status: Literal["succeeded", "failed"],
        rollback_status: SkillRollbackStatus,
        output: Mapping[str, object] | None,
        error_code: str | None,
        completed_at: datetime,
    ) -> SkillRun:
        pool = await self._ready_pool()
        run_uuid = _uuid_or_not_found(run_id)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            current = await connection.fetchrow(
                """
                SELECT status FROM skill_runs
                WHERE run_id = $1 AND account_id = $2
                FOR UPDATE
                """,
                run_uuid,
                account_id,
            )
            if current is None:
                raise SkillNotFoundError(run_id)
            if str(current["status"]) != "running":
                raise SkillExecutionError(run_id, "skill run is already complete")
            await connection.execute(
                """
                UPDATE skill_runs
                SET status = $1, rollback_status = $2, output_json = $3::jsonb,
                    error_code = $4, completed_at = $5
                WHERE run_id = $6 AND account_id = $7
                """,
                status,
                rollback_status,
                canonical_json(dict(output)) if output is not None else None,
                error_code,
                completed_at.astimezone(UTC),
                run_uuid,
                account_id,
            )
            return await self._load_run(
                connection,
                account_id=account_id,
                run_id=run_uuid,
            )

    async def get_run(self, *, account_id: str, run_id: str) -> SkillRun:
        parsed_run_id = _uuid_or_not_found(run_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            return await self._load_run(
                connection,
                account_id=account_id,
                run_id=parsed_run_id,
            )

    @staticmethod
    async def _validate_proposal_evidence(
        connection: asyncpg.Connection,
        proposal: SkillProposal,
    ) -> None:
        rows = await connection.fetch(
            """
            SELECT event_id, event_type, speaker_class
            FROM archive_evidence_events
            WHERE account_id = $1 AND event_id = ANY($2::text[])
            """,
            proposal.account_id,
            list(proposal.source_event_ids),
        )
        if {str(row["event_id"]) for row in rows} != set(proposal.source_event_ids):
            raise SkillNotFoundError("skill proposal evidence is missing or cross-account")
        if proposal.source_kind == "explicit_instruction":
            if any(str(row["speaker_class"]) != "owner" for row in rows):
                raise SkillApprovalRequiredError(
                    "explicit skill instruction must come from the owner"
                )
        elif any(str(row["event_type"]) != "tool.execution_succeeded" for row in rows):
            raise SkillApprovalRequiredError(
                "repeated tool skill requires successful tool execution evidence"
            )

    @staticmethod
    async def _validate_owner_event(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        event_id: str,
        event_type: str,
        skill_id: str,
        version: int,
        input_sha256: str | None,
    ) -> None:
        row = await connection.fetchrow(
            """
            SELECT event_type, speaker_class, payload
            FROM archive_evidence_events
            WHERE account_id = $1 AND event_id = $2
            """,
            account_id,
            event_id,
        )
        if row is None:
            raise SkillConfirmationRequiredError(
                "owner confirmation evidence is missing or cross-account"
            )
        payload = _json_mapping(row["payload"])
        if (
            str(row["event_type"]) != event_type
            or str(row["speaker_class"]) != "owner"
            or str(payload.get("skill_id", "")) != skill_id
            or int(str(payload.get("version", 0))) != version
            or (
                input_sha256 is not None
                and str(payload.get("input_sha256", "")) != input_sha256
            )
        ):
            raise SkillConfirmationRequiredError(
                "owner confirmation evidence does not match this skill version"
            )

    async def _load_version(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        skill_id: uuid.UUID,
        version: int,
    ) -> SkillVersion:
        row = await self._version_row(
            connection,
            account_id=account_id,
            skill_id=skill_id,
            version=version,
        )
        sources = await connection.fetch(
            """
            SELECT source_event_id FROM skill_version_evidence
            WHERE account_id = $1 AND skill_id = $2 AND version = $3
            ORDER BY source_event_id
            """,
            account_id,
            skill_id,
            version,
        )
        return _version_from_row(
            row,
            tuple(str(source["source_event_id"]) for source in sources),
        )

    @staticmethod
    async def _version_row(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        skill_id: uuid.UUID,
        version: int,
        for_update: bool = False,
    ) -> asyncpg.Record:
        suffix = " FOR UPDATE" if for_update else ""
        row = await connection.fetchrow(
            f"""
            SELECT version.*, definition.name
            FROM skill_versions version
            JOIN skill_definitions definition ON definition.skill_id = version.skill_id
            WHERE version.account_id = $1
              AND version.skill_id = $2
              AND version.version = $3
            {suffix}
            """,
            account_id,
            skill_id,
            version,
        )
        if row is None:
            raise SkillNotFoundError(f"{skill_id}:{version}")
        return row

    @staticmethod
    async def _load_run(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        run_id: uuid.UUID,
    ) -> SkillRun:
        row = await connection.fetchrow(
            """
            SELECT * FROM skill_runs
            WHERE account_id = $1 AND run_id = $2
            """,
            account_id,
            run_id,
        )
        if row is None:
            raise SkillNotFoundError(str(run_id))
        step_rows = await connection.fetch(
            """
            SELECT * FROM skill_run_steps
            WHERE account_id = $1 AND run_id = $2
            ORDER BY sequence
            """,
            account_id,
            run_id,
        )
        output = (
            _json_mapping(row["output_json"])
            if row["output_json"] is not None
            else None
        )
        return SkillRun(
            run_id=str(row["run_id"]),
            account_id=str(row["account_id"]),
            skill_id=str(row["skill_id"]),
            version=int(row["skill_version"]),
            status=cast(SkillRunStatus, row["status"]),
            rollback_status=cast(SkillRollbackStatus, row["rollback_status"]),
            confirmation_event_id=str(row["confirmation_event_id"]),
            inputs=_json_mapping(row["input_json"]),
            output=output,
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            started_at=cast(datetime, row["started_at"]),
            completed_at=cast(datetime | None, row["completed_at"]),
            steps=tuple(_run_step_from_row(step) for step in step_rows),
        )

    @staticmethod
    async def _write_search_projection(
        connection: asyncpg.Connection,
        *,
        skill_id: uuid.UUID,
        account_id: str,
        name: str,
        version: int,
        status: Literal["candidate", "confirmed"],
        description: str,
        trigger_phrases: tuple[str, ...],
        steps: tuple[SkillStepDefinition, ...],
        domain_category: DomainCategory,
        sensitivity: MemorySensitivity,
        salience: float,
        source_event_ids: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        document_id = _stable_uuid("skill-document", skill_id)
        source_event_id = source_event_ids[0]
        body = " ".join(
            (
                description,
                *trigger_phrases,
                *(f"{step.step_id}:{step.tool_name}" for step in steps),
            )
        )
        await connection.execute(
            """
            INSERT INTO memory_search_documents (
                document_id, account_id, item_id, kind, memory_kind, title, body,
                category, domain_category, entity_ids, status, source_event_id,
                occurred_at, valid_from, valid_to, observed_at, stability, salience,
                sensitivity, conflict_state
            ) VALUES (
                $1, $2, $3, 'skill', 'procedural', $4, $5, $6, $6,
                ARRAY[]::UUID[], $7, $8, $9, $9, NULL, $9, $10, $11, $12, 'none'
            )
            ON CONFLICT(kind, item_id) DO UPDATE SET
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                category = EXCLUDED.category,
                domain_category = EXCLUDED.domain_category,
                status = EXCLUDED.status,
                source_event_id = EXCLUDED.source_event_id,
                observed_at = EXCLUDED.observed_at,
                stability = EXCLUDED.stability,
                salience = EXCLUDED.salience,
                sensitivity = EXCLUDED.sensitivity
            """,
            document_id,
            account_id,
            skill_id,
            f"{name} v{version}",
            body,
            domain_category,
            status,
            source_event_id,
            observed_at,
            0.95 if status == "confirmed" else 0.5,
            salience,
            sensitivity,
        )
        await connection.execute(
            """
            DELETE FROM memory_search_document_sources
            WHERE document_id = $1
            """,
            document_id,
        )
        await connection.executemany(
            """
            INSERT INTO memory_search_document_sources (
                document_id, account_id, source_event_id
            ) VALUES ($1, $2, $3)
            """,
            [
                (document_id, account_id, source)
                for source in tuple(dict.fromkeys(source_event_ids))
            ],
        )


def _version_from_row(
    row: Mapping[str, object],
    source_event_ids: tuple[str, ...],
) -> SkillVersion:
    return SkillVersion(
        skill_id=str(row["skill_id"]),
        account_id=str(row["account_id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        version=int(str(row["version"])),
        status=cast(SkillVersionStatus, row["status"]),
        trigger_phrases=tuple(str(value) for value in _json_list(row["trigger_phrases"])),
        input_schema=_json_mapping(row["input_schema"]),
        output_schema=_json_mapping(row["output_schema"]),
        output_template=_json_mapping(row["output_template"]),
        allowed_tools=tuple(str(value) for value in _json_list(row["allowed_tools"])),
        steps=_steps_from_value(row["steps"]),
        source_kind=cast(SkillSourceKind, row["source_kind"]),
        source_event_ids=source_event_ids,
        domain_category=cast(DomainCategory, row["domain_category"]),
        sensitivity=cast(MemorySensitivity, row["sensitivity"]),
        salience=float(str(row["salience"])),
        created_at=cast(datetime, row["created_at"]),
        approved_at=cast(datetime | None, row["approved_at"]),
        approval_event_id=(
            str(row["approval_event_id"])
            if row["approval_event_id"] is not None
            else None
        ),
    )


def _run_step_from_row(row: Mapping[str, object]) -> SkillRunStep:
    return SkillRunStep(
        sequence=int(str(row["sequence"])),
        step_id=str(row["step_id"]),
        phase=cast(SkillStepPhase, row["phase"]),
        tool_name=str(row["tool_name"]),
        arguments=_json_mapping(row["arguments_json"]),
        status=cast(SkillStepStatus, row["status"]),
        output=_json_value(row["output_json"]) if row["output_json"] is not None else None,
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        started_at=cast(datetime, row["started_at"]),
        completed_at=cast(datetime, row["completed_at"]),
    )


def _steps_json(steps: tuple[SkillStepDefinition, ...]) -> str:
    return canonical_json(
        [
            {
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "arguments": dict(step.arguments),
                "compensation_tool_name": step.compensation_tool_name,
                "compensation_arguments": (
                    dict(step.compensation_arguments)
                    if step.compensation_arguments is not None
                    else None
                ),
            }
            for step in steps
        ]
    )


def _steps_from_value(value: object) -> tuple[SkillStepDefinition, ...]:
    raw = _json_list(value)
    result: list[SkillStepDefinition] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("stored PostgreSQL skill step must be an object")
        arguments = item.get("arguments")
        compensation_arguments = item.get("compensation_arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("stored PostgreSQL skill arguments must be an object")
        if compensation_arguments is not None and not isinstance(
            compensation_arguments,
            Mapping,
        ):
            raise ValueError(
                "stored PostgreSQL compensation arguments must be an object"
            )
        result.append(
            SkillStepDefinition(
                step_id=str(item["step_id"]),
                tool_name=str(item["tool_name"]),
                arguments={str(key): child for key, child in arguments.items()},
                compensation_tool_name=(
                    str(item["compensation_tool_name"])
                    if item.get("compensation_tool_name") is not None
                    else None
                ),
                compensation_arguments=(
                    {
                        str(key): child
                        for key, child in compensation_arguments.items()
                    }
                    if isinstance(compensation_arguments, Mapping)
                    else None
                ),
            )
        )
    return tuple(result)


def _json_mapping(value: object) -> dict[str, object]:
    parsed = _json_value(value)
    if not isinstance(parsed, Mapping):
        raise ValueError("stored JSON value must be an object")
    return {str(key): child for key, child in parsed.items()}


def _json_list(value: object) -> list[object]:
    parsed = _json_value(value)
    if not isinstance(parsed, list):
        raise ValueError("stored JSON value must be an array")
    return cast(list[object], parsed)


def _json_value(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _stable_uuid(kind: str, *parts: object) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"memoria:{kind}:{':'.join(str(part) for part in parts)}",
    )


def _uuid_or_not_found(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise SkillNotFoundError(value) from exc
