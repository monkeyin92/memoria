"""SQLite persistence for approved, executable procedural memories."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from services.archive.life_archive import _SCHEMA as _LEDGER_SCHEMA
from services.archive.memory_catalog import _SCHEMA as _MEMORY_SCHEMA
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_definitions (
    skill_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'approved', 'retired')),
    latest_version INTEGER NOT NULL CHECK (latest_version >= 1),
    approved_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'approved', 'superseded', 'retired')),
    description TEXT NOT NULL,
    trigger_phrases_json TEXT NOT NULL,
    input_schema_json TEXT NOT NULL,
    output_schema_json TEXT NOT NULL,
    output_template_json TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('explicit_instruction', 'repeated_tool_success')),
    domain_category TEXT NOT NULL,
    sensitivity TEXT NOT NULL
        CHECK (sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')),
    salience REAL NOT NULL CHECK (salience >= 0 AND salience <= 1),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approval_event_id TEXT,
    PRIMARY KEY (skill_id, version),
    FOREIGN KEY (skill_id) REFERENCES skill_definitions(skill_id) ON DELETE CASCADE,
    FOREIGN KEY (approval_event_id) REFERENCES evidence_events(event_id)
);

CREATE TABLE IF NOT EXISTS skill_version_evidence (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (skill_id, version, source_event_id),
    FOREIGN KEY (skill_id, version)
        REFERENCES skill_versions(skill_id, version) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id)
        REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS skill_runs (
    run_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    rollback_status TEXT NOT NULL
        CHECK (rollback_status IN ('not_required', 'completed', 'partial')),
    confirmation_event_id TEXT NOT NULL UNIQUE,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error_code TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (skill_id, skill_version)
        REFERENCES skill_versions(skill_id, version) ON DELETE CASCADE,
    FOREIGN KEY (confirmation_event_id)
        REFERENCES evidence_events(event_id)
);

CREATE TABLE IF NOT EXISTS skill_run_steps (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    account_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('forward', 'compensation')),
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    output_json TEXT,
    error_code TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES skill_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_skill_versions_account_status
ON skill_versions(account_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_skill_runs_account_started
ON skill_runs(account_id, started_at DESC);
"""


class SkillCatalog:
    """Store immutable skill versions beside, but never instead of, ledger evidence."""

    def __init__(self, sqlite_path: Path) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(cls, path: str | Path) -> SkillCatalog:
        return cls(Path(path))

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path, timeout=5) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_LEDGER_SCHEMA)
                connection.executescript(_MEMORY_SCHEMA)
                connection.executescript(_SCHEMA)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def propose(self, proposal: SkillProposal) -> SkillVersion:
        now = datetime.now(UTC)
        skill_id = _stable_id("skill", proposal.account_id, proposal.name.casefold())
        with self._connect() as connection:
            self._validate_proposal_evidence(connection, proposal)
            definition = connection.execute(
                """
                SELECT * FROM skill_definitions
                WHERE account_id = ? AND skill_id = ?
                """,
                (proposal.account_id, skill_id),
            ).fetchone()
            if definition is None:
                version = 1
                connection.execute(
                    """
                    INSERT INTO skill_definitions (
                        skill_id, account_id, name, status, latest_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'candidate', 1, ?, ?)
                    """,
                    (
                        skill_id,
                        proposal.account_id,
                        proposal.name,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                approved_version: int | None = None
            else:
                skill_id = str(definition["skill_id"])
                version = int(definition["latest_version"]) + 1
                approved_version = (
                    int(definition["approved_version"])
                    if definition["approved_version"] is not None
                    else None
                )
                connection.execute(
                    """
                    UPDATE skill_definitions
                    SET latest_version = ?, updated_at = ?
                    WHERE skill_id = ? AND account_id = ?
                    """,
                    (version, now.isoformat(), skill_id, proposal.account_id),
                )
            connection.execute(
                """
                INSERT INTO skill_versions (
                    skill_id, version, account_id, status, description,
                    trigger_phrases_json, input_schema_json, output_schema_json,
                    output_template_json, allowed_tools_json, steps_json,
                    source_kind, domain_category, sensitivity, salience, created_at
                ) VALUES (
                    ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
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
                    now.isoformat(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO skill_version_evidence (
                    skill_id, version, account_id, source_event_id
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (skill_id, version, proposal.account_id, source_event_id)
                    for source_event_id in proposal.source_event_ids
                ],
            )
            if approved_version is None:
                self._write_search_projection(
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
            return self._load_version(
                connection,
                account_id=proposal.account_id,
                skill_id=skill_id,
                version=version,
            )

    async def approve(self, command: SkillApproval) -> SkillVersion:
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = self._version_row(
                connection,
                account_id=command.account_id,
                skill_id=command.skill_id,
                version=command.version,
            )
            if str(row["status"]) != "candidate":
                raise SkillApprovalRequiredError("only candidate skill versions can be approved")
            self._validate_owner_event(
                connection,
                account_id=command.account_id,
                event_id=command.approval_event_id,
                event_type="skill.approved",
                skill_id=command.skill_id,
                version=command.version,
                input_sha256=None,
            )
            connection.execute(
                """
                UPDATE skill_versions
                SET status = 'superseded'
                WHERE account_id = ? AND skill_id = ? AND status = 'approved'
                """,
                (command.account_id, command.skill_id),
            )
            connection.execute(
                """
                UPDATE skill_versions
                SET status = 'approved', approved_at = ?, approval_event_id = ?
                WHERE account_id = ? AND skill_id = ? AND version = ?
                """,
                (
                    now.isoformat(),
                    command.approval_event_id,
                    command.account_id,
                    command.skill_id,
                    command.version,
                ),
            )
            connection.execute(
                """
                UPDATE skill_definitions
                SET status = 'approved', approved_version = ?, updated_at = ?
                WHERE account_id = ? AND skill_id = ?
                """,
                (
                    command.version,
                    now.isoformat(),
                    command.account_id,
                    command.skill_id,
                ),
            )
            version = self._load_version(
                connection,
                account_id=command.account_id,
                skill_id=command.skill_id,
                version=command.version,
            )
            self._write_search_projection(
                connection,
                skill_id=version.skill_id,
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
        with self._connect() as connection:
            return self._load_version(
                connection,
                account_id=account_id,
                skill_id=skill_id,
                version=version,
            )

    async def list_versions(
        self,
        *,
        account_id: str,
    ) -> tuple[SkillVersion, ...]:
        if not account_id.strip():
            raise ValueError("skill list requires account_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT version.skill_id, version.version
                FROM skill_versions version
                WHERE version.account_id = ?
                ORDER BY version.created_at DESC, version.skill_id, version.version DESC
                """,
                (account_id,),
            ).fetchall()
            return tuple(
                self._load_version(
                    connection,
                    account_id=account_id,
                    skill_id=str(row["skill_id"]),
                    version=int(row["version"]),
                )
                for row in rows
            )

    async def rebuild_search_projections(self, *, account_id: str) -> int:
        if not account_id.strip():
            raise ValueError("skill projection rebuild requires account_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT skill_id, COALESCE(approved_version, latest_version) AS version
                FROM skill_definitions
                WHERE account_id = ? AND status != 'retired'
                ORDER BY skill_id
                """,
                (account_id,),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM memory_search_documents
                WHERE account_id = ? AND kind = 'skill'
                """,
                (account_id,),
            )
            for row in rows:
                version = self._load_version(
                    connection,
                    account_id=account_id,
                    skill_id=str(row["skill_id"]),
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
                self._write_search_projection(
                    connection,
                    skill_id=version.skill_id,
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
        now = datetime.now(UTC)
        run_id = str(uuid.uuid4())
        with self._connect() as connection:
            version = self._load_version(
                connection,
                account_id=request.account_id,
                skill_id=request.skill_id,
                version=request.version,
            )
            if version.status != "approved":
                raise SkillApprovalRequiredError(
                    "only the currently approved skill version can execute"
                )
            self._validate_owner_event(
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
                connection.execute(
                    """
                    INSERT INTO skill_runs (
                        run_id, account_id, skill_id, skill_version, status,
                        rollback_status, confirmation_event_id, input_json, started_at
                    ) VALUES (?, ?, ?, ?, 'running', 'not_required', ?, ?, ?)
                    """,
                    (
                        run_id,
                        request.account_id,
                        request.skill_id,
                        request.version,
                        request.confirmation_event_id,
                        canonical_json(dict(request.inputs)),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SkillConfirmationRequiredError(
                    "skill run confirmation evidence is missing or was already consumed"
                ) from exc
            return self._load_run(
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
        stored_arguments = dict(arguments)
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT status FROM skill_runs
                WHERE run_id = ? AND account_id = ?
                """,
                (run_id, account_id),
            ).fetchone()
            if run is None:
                raise SkillNotFoundError(run_id)
            if str(run["status"]) != "running":
                raise SkillExecutionError(run_id, "cannot append audit to a completed run")
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM skill_run_steps WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO skill_run_steps (
                    run_id, sequence, account_id, step_id, phase, tool_name,
                    arguments_json, status, output_json, error_code,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    account_id,
                    step_id,
                    phase,
                    tool_name,
                    canonical_json(stored_arguments),
                    status,
                    canonical_json(output) if output is not None else None,
                    error_code,
                    started_at.astimezone(UTC).isoformat(),
                    completed_at.astimezone(UTC).isoformat(),
                ),
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
        if status not in ("succeeded", "failed"):
            raise ValueError("skill run can only finish succeeded or failed")
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT status FROM skill_runs
                WHERE run_id = ? AND account_id = ?
                """,
                (run_id, account_id),
            ).fetchone()
            if current is None:
                raise SkillNotFoundError(run_id)
            if str(current["status"]) != "running":
                raise SkillExecutionError(run_id, "skill run is already complete")
            connection.execute(
                """
                UPDATE skill_runs
                SET status = ?, rollback_status = ?, output_json = ?,
                    error_code = ?, completed_at = ?
                WHERE run_id = ? AND account_id = ?
                """,
                (
                    status,
                    rollback_status,
                    canonical_json(output) if output is not None else None,
                    error_code,
                    completed_at.astimezone(UTC).isoformat(),
                    run_id,
                    account_id,
                ),
            )
            return self._load_run(
                connection,
                account_id=account_id,
                run_id=run_id,
            )

    async def get_run(self, *, account_id: str, run_id: str) -> SkillRun:
        with self._connect() as connection:
            return self._load_run(
                connection,
                account_id=account_id,
                run_id=run_id,
            )

    @staticmethod
    def _validate_proposal_evidence(
        connection: sqlite3.Connection,
        proposal: SkillProposal,
    ) -> None:
        placeholders = ",".join("?" for _ in proposal.source_event_ids)
        rows = connection.execute(
            f"""
            SELECT event_id, event_type, speaker_class
            FROM evidence_events
            WHERE account_id = ? AND event_id IN ({placeholders})
            """,
            (proposal.account_id, *proposal.source_event_ids),
        ).fetchall()
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
    def _validate_owner_event(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        event_id: str,
        event_type: str,
        skill_id: str,
        version: int,
        input_sha256: str | None,
    ) -> None:
        row = connection.execute(
            """
            SELECT event_type, speaker_class, payload_json
            FROM evidence_events
            WHERE account_id = ? AND event_id = ?
            """,
            (account_id, event_id),
        ).fetchone()
        if row is None:
            raise SkillConfirmationRequiredError(
                "owner confirmation evidence is missing or cross-account"
            )
        payload = json.loads(str(row["payload_json"]))
        if (
            str(row["event_type"]) != event_type
            or str(row["speaker_class"]) != "owner"
            or str(payload.get("skill_id", "")) != skill_id
            or int(payload.get("version", 0)) != version
            or (
                input_sha256 is not None
                and str(payload.get("input_sha256", "")) != input_sha256
            )
        ):
            raise SkillConfirmationRequiredError(
                "owner confirmation evidence does not match this skill version"
            )

    def _load_version(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        skill_id: str,
        version: int,
    ) -> SkillVersion:
        row = self._version_row(
            connection,
            account_id=account_id,
            skill_id=skill_id,
            version=version,
        )
        sources = connection.execute(
            """
            SELECT source_event_id FROM skill_version_evidence
            WHERE account_id = ? AND skill_id = ? AND version = ?
            ORDER BY source_event_id
            """,
            (account_id, skill_id, version),
        ).fetchall()
        return _version_from_row(
            row,
            tuple(str(source["source_event_id"]) for source in sources),
        )

    @staticmethod
    def _version_row(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        skill_id: str,
        version: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT version.*, definition.name
            FROM skill_versions version
            JOIN skill_definitions definition ON definition.skill_id = version.skill_id
            WHERE version.account_id = ? AND version.skill_id = ? AND version.version = ?
            """,
            (account_id, skill_id, version),
        ).fetchone()
        if row is None:
            raise SkillNotFoundError(f"{skill_id}:{version}")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _load_run(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        run_id: str,
    ) -> SkillRun:
        row = connection.execute(
            """
            SELECT * FROM skill_runs
            WHERE account_id = ? AND run_id = ?
            """,
            (account_id, run_id),
        ).fetchone()
        if row is None:
            raise SkillNotFoundError(run_id)
        step_rows = connection.execute(
            """
            SELECT * FROM skill_run_steps
            WHERE account_id = ? AND run_id = ?
            ORDER BY sequence
            """,
            (account_id, run_id),
        ).fetchall()
        return SkillRun(
            run_id=str(row["run_id"]),
            account_id=str(row["account_id"]),
            skill_id=str(row["skill_id"]),
            version=int(row["skill_version"]),
            status=cast(SkillRunStatus, row["status"]),
            rollback_status=cast(SkillRollbackStatus, row["rollback_status"]),
            confirmation_event_id=str(row["confirmation_event_id"]),
            inputs=cast(dict[str, object], json.loads(str(row["input_json"]))),
            output=(
                cast(dict[str, object], json.loads(str(row["output_json"])))
                if row["output_json"] is not None
                else None
            ),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            started_at=datetime.fromisoformat(str(row["started_at"])),
            completed_at=(
                datetime.fromisoformat(str(row["completed_at"]))
                if row["completed_at"] is not None
                else None
            ),
            steps=tuple(_run_step_from_row(step) for step in step_rows),
        )

    @staticmethod
    def _write_search_projection(
        connection: sqlite3.Connection,
        *,
        skill_id: str,
        account_id: str,
        name: str,
        version: int,
        status: str,
        description: str,
        trigger_phrases: tuple[str, ...],
        steps: tuple[SkillStepDefinition, ...],
        domain_category: DomainCategory,
        sensitivity: MemorySensitivity,
        salience: float,
        source_event_ids: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        document_id = _stable_id("skill-document", skill_id)
        source_event_id = source_event_ids[0]
        body = " ".join(
            (
                description,
                *trigger_phrases,
                *(f"{step.step_id}:{step.tool_name}" for step in steps),
            )
        )
        connection.execute(
            """
            INSERT INTO memory_search_documents (
                document_id, account_id, item_id, kind, memory_kind, title, body,
                category, domain_category, entity_ids_json, status, source_event_id,
                occurred_at, valid_from, valid_to, observed_at, stability, salience,
                sensitivity, conflict_state
            ) VALUES (
                ?, ?, ?, 'skill', 'procedural', ?, ?, ?, ?, '[]', ?, ?, ?, ?, NULL,
                ?, ?, ?, ?, 'none'
            )
            ON CONFLICT(kind, item_id) DO UPDATE SET
                title = excluded.title,
                body = excluded.body,
                category = excluded.category,
                domain_category = excluded.domain_category,
                status = excluded.status,
                source_event_id = excluded.source_event_id,
                observed_at = excluded.observed_at,
                stability = excluded.stability,
                salience = excluded.salience,
                sensitivity = excluded.sensitivity
            """,
            (
                document_id,
                account_id,
                skill_id,
                f"{name} v{version}",
                body,
                domain_category,
                domain_category,
                status,
                source_event_id,
                observed_at.isoformat(),
                observed_at.isoformat(),
                observed_at.isoformat(),
                0.95 if status == "confirmed" else 0.5,
                salience,
                sensitivity,
            ),
        )
        connection.execute(
            """
            DELETE FROM memory_search_document_sources
            WHERE document_id = ?
            """,
            (document_id,),
        )
        connection.executemany(
            """
            INSERT INTO memory_search_document_sources (
                document_id, account_id, source_event_id
            ) VALUES (?, ?, ?)
            """,
            [
                (document_id, account_id, source)
                for source in tuple(dict.fromkeys(source_event_ids))
            ],
        )


def _version_from_row(
    row: sqlite3.Row,
    source_event_ids: tuple[str, ...],
) -> SkillVersion:
    return SkillVersion(
        skill_id=str(row["skill_id"]),
        account_id=str(row["account_id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        version=int(row["version"]),
        status=cast(SkillVersionStatus, row["status"]),
        trigger_phrases=tuple(
            str(value) for value in json.loads(str(row["trigger_phrases_json"]))
        ),
        input_schema=cast(
            dict[str, object],
            json.loads(str(row["input_schema_json"])),
        ),
        output_schema=cast(
            dict[str, object],
            json.loads(str(row["output_schema_json"])),
        ),
        output_template=cast(
            dict[str, object],
            json.loads(str(row["output_template_json"])),
        ),
        allowed_tools=tuple(
            str(value) for value in json.loads(str(row["allowed_tools_json"]))
        ),
        steps=_steps_from_json(str(row["steps_json"])),
        source_kind=cast(SkillSourceKind, row["source_kind"]),
        source_event_ids=source_event_ids,
        domain_category=cast(DomainCategory, row["domain_category"]),
        sensitivity=cast(MemorySensitivity, row["sensitivity"]),
        salience=float(row["salience"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        approved_at=(
            datetime.fromisoformat(str(row["approved_at"]))
            if row["approved_at"] is not None
            else None
        ),
        approval_event_id=(
            str(row["approval_event_id"])
            if row["approval_event_id"] is not None
            else None
        ),
    )


def _run_step_from_row(row: sqlite3.Row) -> SkillRunStep:
    return SkillRunStep(
        sequence=int(row["sequence"]),
        step_id=str(row["step_id"]),
        phase=cast(SkillStepPhase, row["phase"]),
        tool_name=str(row["tool_name"]),
        arguments=cast(dict[str, object], json.loads(str(row["arguments_json"]))),
        status=cast(SkillStepStatus, row["status"]),
        output=json.loads(str(row["output_json"])) if row["output_json"] is not None else None,
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        started_at=datetime.fromisoformat(str(row["started_at"])),
        completed_at=datetime.fromisoformat(str(row["completed_at"])),
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


def _steps_from_json(value: str) -> tuple[SkillStepDefinition, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise ValueError("stored skill steps must be an array")
    result: list[SkillStepDefinition] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("stored skill step must be an object")
        arguments = item.get("arguments")
        compensation_arguments = item.get("compensation_arguments")
        if not isinstance(arguments, dict):
            raise ValueError("stored skill arguments must be an object")
        if compensation_arguments is not None and not isinstance(
            compensation_arguments,
            dict,
        ):
            raise ValueError("stored skill compensation arguments must be an object")
        result.append(
            SkillStepDefinition(
                step_id=str(item["step_id"]),
                tool_name=str(item["tool_name"]),
                arguments=cast(dict[str, object], arguments),
                compensation_tool_name=(
                    str(item["compensation_tool_name"])
                    if item.get("compensation_tool_name") is not None
                    else None
                ),
                compensation_arguments=cast(
                    dict[str, object] | None,
                    compensation_arguments,
                ),
            )
        )
    return tuple(result)


def _stable_id(kind: str, *parts: object) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:{kind}:{':'.join(str(part) for part in parts)}",
        )
    )
