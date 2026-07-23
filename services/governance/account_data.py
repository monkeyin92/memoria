"""Account-wide export and deletion control plane for SQLite development data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

import asyncpg

from services.archive.object_store import ObjectRef, ObjectStore
from services.control_api.app.database import MemoryStore
from services.legacy.domain import LegacyAccountExport, LegacyRegistryPort
from services.voice_profile.domain import VoiceProfilePort

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    excluded_columns: frozenset[str] = frozenset()
    json_columns: frozenset[str] = frozenset()


_ARCHIVE_EXPORT_TABLES = (
    TableSpec("evidence_events", json_columns=frozenset({"payload_json"})),
    TableSpec("consent_grants"),
    TableSpec(
        "evidence_blobs",
        excluded_columns=frozenset({"object_key", "encryption_key_version"}),
    ),
    TableSpec("transcript_versions"),
    TableSpec("memory_claims"),
    TableSpec("person_entities"),
    TableSpec("person_aliases"),
    TableSpec("relationships"),
    TableSpec("life_episodes"),
    TableSpec("episode_evidence"),
    TableSpec("timeline_entries"),
    TableSpec("knowledge_items"),
    TableSpec("memory_search_documents"),
    TableSpec("persona_traits"),
    TableSpec("persona_evidence"),
    TableSpec("speech_style_stats", json_columns=frozenset({"tic_counts_json"})),
    TableSpec("persona_learning_consents"),
    TableSpec("persona_versions", json_columns=frozenset({"snapshot_json"})),
    TableSpec("digital_self_versions", json_columns=frozenset({"manifest_json"})),
    TableSpec("digital_self_lifecycle_audit_events"),
    TableSpec("self_model_cognitive_claims"),
    TableSpec(
        "self_model_decision_cases",
        json_columns=frozenset(
            {"options_json", "constraints_json", "rejected_options_json"}
        ),
    ),
    TableSpec(
        "self_model_relationship_profiles",
        json_columns=frozenset({"boundaries_json"}),
    ),
    TableSpec("self_model_sources"),
    TableSpec("self_model_audit_events", json_columns=frozenset({"payload_json"})),
    TableSpec("self_model_command_receipts"),
    TableSpec("voice_clone_consents"),
    TableSpec(
        "voice_samples",
        excluded_columns=frozenset(
            {"object_key", "encryption_key_version", "object_backend"}
        ),
    ),
    TableSpec("voice_profiles", excluded_columns=frozenset({"provider_voice_id"})),
    TableSpec(
        "voice_enrollment_operations",
        excluded_columns=frozenset({"provider_voice_id"}),
    ),
    TableSpec("voice_blind_trials", excluded_columns=frozenset({"candidate_slot"})),
    TableSpec("voice_evaluations"),
    TableSpec("voice_quality_measurements"),
)

_ARCHIVE_DELETE_ORDER = (
    "voice_quality_measurements",
    "voice_evaluations",
    "voice_blind_trials",
    "voice_profiles",
    "voice_enrollment_operations",
    "voice_samples",
    "voice_clone_consents",
    "persona_evidence",
    "persona_observation_receipts",
    "speech_style_stats",
    "self_model_cognitive_claim_sources",
    "self_model_decision_case_sources",
    "self_model_relationship_profile_sources",
    "self_model_sources",
    "self_model_command_receipts",
    "self_model_audit_events",
    "self_model_relationship_profiles",
    "self_model_decision_cases",
    "self_model_cognitive_claims",
    "digital_self_lifecycle_audit_events",
    "digital_self_versions",
    "persona_versions",
    "persona_traits",
    "persona_learning_consents",
    "memory_vector_documents",
    "memory_search_documents",
    "timeline_entries",
    "episode_evidence",
    "life_episodes",
    "relationships",
    "person_aliases",
    "person_entities",
    "knowledge_items",
    "memory_claims",
    "memory_compile_receipts",
    "transcript_versions",
    "evidence_blobs",
    "processing_outbox",
    "evidence_events",
    "consent_grants",
)

_SPEAKER_EXPORT_TABLES = (
    TableSpec("speaker_identities"),
    TableSpec("speaker_profiles", excluded_columns=frozenset({"template_ciphertext"})),
    TableSpec("speaker_enrollment_samples"),
)

_SPEAKER_DELETE_ORDER = (
    "speaker_enrollment_samples",
    "speaker_profiles",
    "speaker_identities",
)

_POSTGRES_ARCHIVE_EXPORT_TABLES = (
    TableSpec("archive_evidence_events", json_columns=frozenset({"payload"})),
    TableSpec("archive_consent_grants"),
    TableSpec(
        "archive_evidence_blobs",
        excluded_columns=frozenset({"object_key", "encryption_key_version"}),
    ),
    TableSpec("archive_transcript_versions"),
    TableSpec("memory_claims"),
    TableSpec("person_entities"),
    TableSpec("person_aliases"),
    TableSpec("relationships"),
    TableSpec("life_episodes"),
    TableSpec("timeline_entries"),
    TableSpec("episode_evidence"),
    TableSpec("knowledge_items"),
    TableSpec("memory_search_documents", excluded_columns=frozenset({"search_vector"})),
    TableSpec("persona_traits"),
    TableSpec("persona_evidence"),
    TableSpec("speech_style_stats", json_columns=frozenset({"tic_counts"})),
    TableSpec("persona_learning_consents"),
    TableSpec("persona_versions", json_columns=frozenset({"snapshot"})),
    TableSpec("digital_self_versions", json_columns=frozenset({"manifest_json"})),
    TableSpec("digital_self_lifecycle_audit_events"),
    TableSpec("self_model_cognitive_claims"),
    TableSpec(
        "self_model_cognitive_claim_sources",
    ),
    TableSpec(
        "self_model_decision_cases",
        json_columns=frozenset({"options", "constraints", "rejected_options"}),
    ),
    TableSpec("self_model_decision_case_sources"),
    TableSpec(
        "self_model_relationship_profiles",
        json_columns=frozenset({"boundaries"}),
    ),
    TableSpec("self_model_relationship_profile_sources"),
    TableSpec("self_model_audit_events", json_columns=frozenset({"details"})),
    TableSpec("self_model_command_receipts"),
    TableSpec("voice_clone_consents"),
    TableSpec(
        "voice_samples",
        excluded_columns=frozenset(
            {"object_key", "encryption_key_version", "object_backend"}
        ),
    ),
    TableSpec("voice_profiles", excluded_columns=frozenset({"provider_voice_id"})),
    TableSpec(
        "voice_enrollment_operations",
        excluded_columns=frozenset({"provider_voice_id"}),
    ),
    TableSpec("voice_blind_trials", excluded_columns=frozenset({"candidate_slot"})),
    TableSpec("voice_evaluations"),
    TableSpec("voice_quality_measurements"),
)

_POSTGRES_ARCHIVE_DELETE_ORDER = tuple(
    {
        "evidence_events": "archive_evidence_events",
        "processing_outbox": "archive_processing_outbox",
        "consent_grants": "archive_consent_grants",
        "evidence_blobs": "archive_evidence_blobs",
        "transcript_versions": "archive_transcript_versions",
    }.get(table, table)
    for table in _ARCHIVE_DELETE_ORDER
    if table != "self_model_sources"
)

_DELETION_STEP_RANK = {
    "started": 0,
    "sessions_terminated": 1,
    "legacy_rows_deleted": 2,
    "voice_profiles_deleted": 3,
    "archive_objects_deleted": 4,
    "archive_rows_deleted": 5,
    "speaker_rows_deleted": 6,
    "verified_empty": 7,
    "completed": 8,
}


class AccountRepository(Protocol):
    async def export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]: ...

    async def object_references(self, account_id: str) -> tuple[ObjectRef, ...]: ...

    async def delete_account(self, account_id: str) -> dict[str, int]: ...

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]: ...


class AccountSessionTerminator(Protocol):
    async def terminate_account(self, account_id: str) -> int: ...


class AccountOperationBlocker(Protocol):
    async def block_account(self, account_id: str) -> None: ...


class PendingDeletionRetrier(Protocol):
    async def retry_pending_deletions(self) -> int: ...


class AccountDeletionIncompleteError(RuntimeError):
    """External assets remain, so database erasure must be retried later."""


class AccountDeletionWorker:
    def __init__(
        self,
        governance: PendingDeletionRetrier,
        *,
        interval_s: float = 30.0,
    ) -> None:
        if not 0.1 <= interval_s <= 300:
            raise ValueError("account deletion interval must be between 0.1 and 300 seconds")
        self._governance = governance
        self._interval_s = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="account-deletion-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._governance.retry_pending_deletions()
            except Exception:
                logger.exception("account deletion retry iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass


class SqliteAccountRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        export_tables: tuple[TableSpec, ...],
        delete_order: tuple[str, ...],
        blob_table: str | None = None,
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        self._export_tables = export_tables
        self._delete_order = delete_order
        self._blob_table = blob_table

    @classmethod
    def archive(cls, path: str | Path) -> SqliteAccountRepository:
        return cls(
            path,
            export_tables=_ARCHIVE_EXPORT_TABLES,
            delete_order=_ARCHIVE_DELETE_ORDER,
            blob_table="evidence_blobs",
        )

    @classmethod
    def speaker(cls, path: str | Path) -> SqliteAccountRepository:
        return cls(
            path,
            export_tables=_SPEAKER_EXPORT_TABLES,
            delete_order=_SPEAKER_DELETE_ORDER,
        )

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]:
        return await asyncio.to_thread(self._export_account, account_id)

    def _export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]:
        if not self._path.is_file():
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as connection:
            for spec in self._export_tables:
                if not self._table_exists(connection, spec.name):
                    continue
                columns = [
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({self._quoted(spec.name)})"
                    ).fetchall()
                    if str(row["name"]) not in spec.excluded_columns
                ]
                if "account_id" not in columns:
                    continue
                selected = ", ".join(self._quoted(column) for column in columns)
                rows = connection.execute(
                    f"SELECT {selected} FROM {self._quoted(spec.name)} WHERE account_id = ?",
                    (account_id,),
                ).fetchall()
                values = [self._portable_row(row, spec.json_columns) for row in rows]
                values.sort(key=self._canonical)
                result[spec.name] = values
        return result

    async def object_references(self, account_id: str) -> tuple[ObjectRef, ...]:
        return await asyncio.to_thread(self._object_references, account_id)

    def _object_references(self, account_id: str) -> tuple[ObjectRef, ...]:
        if self._blob_table is None or not self._path.is_file():
            return ()
        with self._connect() as connection:
            if not self._table_exists(connection, self._blob_table):
                return ()
            rows = connection.execute(
                f"""
                SELECT object_key, media_type, byte_count, content_sha256,
                       encryption_key_version
                FROM {self._quoted(self._blob_table)}
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchall()
        return tuple(
            ObjectRef(
                account_id=account_id,
                object_key=str(row["object_key"]),
                media_type=str(row["media_type"]),
                byte_count=int(row["byte_count"]),
                content_sha256=str(row["content_sha256"]),
                encryption_key_version=str(row["encryption_key_version"]),
                backend="archive",
            )
            for row in rows
        )

    async def delete_account(self, account_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._delete_account, account_id)

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._remaining_account_rows, account_id)

    def _remaining_account_rows(self, account_id: str) -> dict[str, int]:
        if not self._path.is_file():
            return {}
        remaining: dict[str, int] = {}
        with self._connect() as connection:
            for table in self._delete_order:
                if not self._table_exists(connection, table):
                    continue
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({self._quoted(table)})"
                    ).fetchall()
                }
                if "account_id" not in columns:
                    continue
                count = int(
                    connection.execute(
                        f"SELECT count(*) FROM {self._quoted(table)} WHERE account_id = ?",
                        (account_id,),
                    ).fetchone()[0]
                )
                if count:
                    remaining[table] = count
        return remaining

    def _delete_account(self, account_id: str) -> dict[str, int]:
        if not self._path.is_file():
            return {}
        counts: dict[str, int] = {}
        with self._connect() as connection:
            connection.execute("PRAGMA defer_foreign_keys=ON")
            for table in self._delete_order:
                if not self._table_exists(connection, table):
                    continue
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA table_info({self._quoted(table)})"
                    ).fetchall()
                }
                if "account_id" not in columns:
                    continue
                cursor = connection.execute(
                    f"DELETE FROM {self._quoted(table)} WHERE account_id = ?",
                    (account_id,),
                )
                counts[table] = max(0, cursor.rowcount)
        return counts

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _quoted(identifier: str) -> str:
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("invalid database identifier")
        return f'"{identifier}"'

    @staticmethod
    def _portable_row(
        row: sqlite3.Row,
        json_columns: frozenset[str],
    ) -> dict[str, Any]:
        result = dict(row)
        for column in json_columns:
            value = result.get(column)
            if isinstance(value, str):
                try:
                    result[column.removesuffix("_json")] = json.loads(value)
                    del result[column]
                except json.JSONDecodeError:
                    pass
        return result

    @staticmethod
    def _canonical(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PostgresAccountRepository:
    def __init__(
        self,
        dsn: str,
        *,
        export_tables: tuple[TableSpec, ...],
        delete_order: tuple[str, ...],
        blob_table: str | None = None,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("account governance DSN must use PostgreSQL")
        self._dsn = dsn
        self._export_tables = export_tables
        self._delete_order = delete_order
        self._blob_table = blob_table

    @classmethod
    def archive(cls, dsn: str) -> PostgresAccountRepository:
        return cls(
            dsn,
            export_tables=_POSTGRES_ARCHIVE_EXPORT_TABLES,
            delete_order=_POSTGRES_ARCHIVE_DELETE_ORDER,
            blob_table="archive_evidence_blobs",
        )

    @classmethod
    def speaker(cls, dsn: str) -> PostgresAccountRepository:
        return cls(
            dsn,
            export_tables=_SPEAKER_EXPORT_TABLES,
            delete_order=_SPEAKER_DELETE_ORDER,
        )

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]:
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction():
                await self._scope(connection, account_id)
                result: dict[str, list[dict[str, Any]]] = {}
                for spec in self._export_tables:
                    columns = await self._columns(connection, spec)
                    if "account_id" not in columns:
                        continue
                    selected = ", ".join(self._quoted(column) for column in columns)
                    rows = await connection.fetch(
                        f"SELECT {selected} FROM {self._quoted(spec.name)} WHERE account_id = $1",
                        account_id,
                    )
                    values = [self._portable_record(row, spec.json_columns) for row in rows]
                    values.sort(key=SqliteAccountRepository._canonical)
                    result[spec.name] = values
                return result
        finally:
            await connection.close()

    async def object_references(self, account_id: str) -> tuple[ObjectRef, ...]:
        if self._blob_table is None:
            return ()
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction():
                await self._scope(connection, account_id)
                columns = await self._column_names(connection, self._blob_table)
                if not columns:
                    return ()
                rows = await connection.fetch(
                    f"""
                    SELECT object_key, media_type, byte_count, content_sha256,
                           encryption_key_version
                    FROM {self._quoted(self._blob_table)}
                    WHERE account_id = $1
                    """,
                    account_id,
                )
            return tuple(
                ObjectRef(
                    account_id=account_id,
                    object_key=str(row["object_key"]),
                    media_type=str(row["media_type"]),
                    byte_count=int(row["byte_count"]),
                    content_sha256=str(row["content_sha256"]),
                    encryption_key_version=str(row["encryption_key_version"]),
                    backend="archive",
                )
                for row in rows
            )
        finally:
            await connection.close()

    async def delete_account(self, account_id: str) -> dict[str, int]:
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction():
                await self._scope(connection, account_id)
                await connection.execute(
                    "SELECT set_config('app.self_model_delete', 'true', true)"
                )
                counts: dict[str, int] = {}
                for table in self._delete_order:
                    columns = await self._column_names(connection, table)
                    if "account_id" not in columns:
                        continue
                    status = await connection.execute(
                        f"DELETE FROM {self._quoted(table)} WHERE account_id = $1",
                        account_id,
                    )
                    counts[table] = int(status.rsplit(" ", 1)[-1])
                return counts
        finally:
            await connection.close()

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]:
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction():
                await self._scope(connection, account_id)
                remaining: dict[str, int] = {}
                for table in self._delete_order:
                    columns = await self._column_names(connection, table)
                    if "account_id" not in columns:
                        continue
                    count = int(
                        await connection.fetchval(
                            f"SELECT count(*) FROM {self._quoted(table)} WHERE account_id = $1",
                            account_id,
                        )
                    )
                    if count:
                        remaining[table] = count
                return remaining
        finally:
            await connection.close()

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    @classmethod
    async def _columns(
        cls,
        connection: asyncpg.Connection,
        spec: TableSpec,
    ) -> list[str]:
        return [
            column
            for column in await cls._column_names(connection, spec.name)
            if column not in spec.excluded_columns
        ]

    @staticmethod
    async def _column_names(
        connection: asyncpg.Connection,
        table: str,
    ) -> list[str]:
        rows = await connection.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
        return [str(row["column_name"]) for row in rows]

    @staticmethod
    def _quoted(identifier: str) -> str:
        return SqliteAccountRepository._quoted(identifier)

    @classmethod
    def _portable_record(
        cls,
        row: asyncpg.Record,
        json_columns: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        result = {key: cls._portable_value(value) for key, value in row.items()}
        for column in json_columns:
            if not column.endswith("_json"):
                continue
            value = result.get(column)
            if isinstance(value, str):
                try:
                    result[column.removesuffix("_json")] = json.loads(value)
                    del result[column]
                except json.JSONDecodeError:
                    pass
        return result

    @classmethod
    def _portable_value(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, bytes):
            return {"binary": "omitted"}
        if isinstance(value, Mapping):
            return {str(key): cls._portable_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._portable_value(item) for item in value]
        return value


class AccountDataGovernance:
    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        archive_repository: AccountRepository,
        speaker_repository: AccountRepository,
        voice_profiles: VoiceProfilePort,
        legacy_registry: LegacyRegistryPort | None = None,
        archive_object_store: ObjectStore | None = None,
        session_terminator: AccountSessionTerminator | None = None,
        operation_blocker: AccountOperationBlocker | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._archive_repository = archive_repository
        self._speaker_repository = speaker_repository
        self._voice_profiles = voice_profiles
        self._legacy_registry = legacy_registry
        self._archive_object_store = archive_object_store
        self._session_terminator = session_terminator
        self._operation_blocker = operation_blocker
        # ponytail: deletion is rare; one lock avoids a per-account lock lifecycle.
        self._deletion_lock = asyncio.Lock()

    async def export_account(self, account_id: str) -> dict[str, Any]:
        control, archive, speaker, legacy = await asyncio.gather(
            asyncio.to_thread(self._memory_store.export_account_data, user_id=account_id),
            self._archive_repository.export_account(account_id),
            self._speaker_repository.export_account(account_id),
            self._export_legacy(account_id),
        )
        body: dict[str, Any] = {
            "format_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "account_id": account_id,
            "sections": {
                "conversation": control,
                "archive": archive,
                "speaker": speaker,
                "legacy": legacy,
            },
        }
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        body["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
        return body

    async def _export_legacy(self, account_id: str) -> dict[str, Any]:
        return _portable_legacy_export(await self._legacy_snapshot(account_id))

    async def _legacy_snapshot(self, account_id: str) -> LegacyAccountExport:
        if self._legacy_registry is None:
            return _empty_legacy_export()
        return await self._legacy_registry.export_for_account(account_id=account_id)

    async def delete_account(self, account_id: str) -> dict[str, Any]:
        async with self._deletion_lock:
            return await self._delete_account(account_id)

    async def retry_pending_deletions(self, *, limit: int = 100) -> int:
        account_ids = await asyncio.to_thread(
            self._memory_store.pending_account_deletions,
            limit=limit,
        )
        completed = 0
        for account_id in account_ids:
            try:
                await self.delete_account(account_id)
            except Exception as exc:
                logger.warning(
                    "account deletion retry remains incomplete account_hash=%s reason=%s",
                    hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16],
                    type(exc).__name__,
                )
            else:
                completed += 1
        return completed

    async def _delete_account(self, account_id: str) -> dict[str, Any]:
        if self._operation_blocker is not None:
            await self._operation_blocker.block_account(account_id)
        started_at = datetime.now(UTC).isoformat()
        deletion = await asyncio.to_thread(
            self._memory_store.begin_account_deletion,
            user_id=account_id,
            started_at=started_at,
        )
        if deletion["status"] == "completed":
            return {
                "request_id": deletion["request_id"],
                "status": "completed",
                "completed_at": deletion["completed_at"],
                "deleted_counts": deletion["deleted_counts"],
                "terminate_sessions": True,
            }
        request_id = str(deletion["request_id"])
        step = str(deletion["step"])
        progress = {
            str(key): int(value) for key, value in dict(deletion["progress"]).items()
        }

        async def checkpoint(next_step: str, *, last_error: str | None = None) -> None:
            nonlocal step
            await asyncio.to_thread(
                self._memory_store.update_account_deletion,
                user_id=account_id,
                request_id=request_id,
                step=next_step,
                updated_at=datetime.now(UTC).isoformat(),
                progress=progress,
                last_error=last_error,
            )
            step = next_step

        async def delete_voice_profiles() -> int:
            profiles = await self._voice_profiles.profiles(account_id=account_id)
            for profile in profiles:
                revoked = await self._voice_profiles.revoke_profile(
                    account_id=account_id,
                    profile_id=profile.profile_id,
                )
                if revoked.deletion_status != "completed":
                    raise AccountDeletionIncompleteError(
                        "voice provider asset deletion is incomplete"
                    )
            return len(profiles)

        async def delete_archive_objects() -> int:
            references = await self._archive_repository.object_references(account_id)
            if references and self._archive_object_store is None:
                raise AccountDeletionIncompleteError("archive object store is not configured")
            if self._archive_object_store is not None:
                for reference in references:
                    await self._archive_object_store.delete(reference)
            return len(references)

        try:
            if _DELETION_STEP_RANK[step] < _DELETION_STEP_RANK["sessions_terminated"]:
                terminated = (
                    await self._session_terminator.terminate_account(account_id)
                    if self._session_terminator is not None
                    else 0
                )
                progress["sessions"] = terminated
                await checkpoint("sessions_terminated")

            if _DELETION_STEP_RANK[step] < _DELETION_STEP_RANK["legacy_rows_deleted"]:
                if self._legacy_registry is not None:
                    legacy = await self._legacy_registry.export_for_account(
                        account_id=account_id
                    )
                    progress["legacy.grants"] = len(legacy.grants)
                    progress["legacy.shells"] = len(legacy.shells)
                    progress["legacy.shell_turns"] = len(legacy.shell_turns)
                    progress["legacy.audit_events"] = len(legacy.audit_events)
                    await self._legacy_registry.delete_for_account(account_id=account_id)
                await checkpoint("legacy_rows_deleted")

            if _DELETION_STEP_RANK[step] < _DELETION_STEP_RANK["voice_profiles_deleted"]:
                progress["voice.profiles"] = await delete_voice_profiles()
                await checkpoint("voice_profiles_deleted")

            if _DELETION_STEP_RANK[step] < _DELETION_STEP_RANK["archive_objects_deleted"]:
                progress["archive.objects"] = await delete_archive_objects()
                await checkpoint("archive_objects_deleted")

            if _DELETION_STEP_RANK[step] < _DELETION_STEP_RANK["archive_rows_deleted"]:
                archive_counts = await self._archive_repository.delete_account(account_id)
                progress.update(
                    {f"archive.{key}": value for key, value in archive_counts.items()}
                )
                await checkpoint("archive_rows_deleted")

            if _DELETION_STEP_RANK[step] < _DELETION_STEP_RANK["speaker_rows_deleted"]:
                speaker_counts = await self._speaker_repository.delete_account(account_id)
                progress.update(
                    {f"speaker.{key}": value for key, value in speaker_counts.items()}
                )
                await checkpoint("speaker_rows_deleted")

            # Catch any asset or projection that arrived just before the deleting fence.
            late_profiles, late_references, late_archive_rows, late_speaker_rows, late_legacy = (
                await asyncio.gather(
                    self._voice_profiles.profiles(account_id=account_id),
                    self._archive_repository.object_references(account_id),
                    self._archive_repository.remaining_account_rows(account_id),
                    self._speaker_repository.remaining_account_rows(account_id),
                    self._legacy_snapshot(account_id),
                )
            )
            if (
                late_profiles
                or late_references
                or late_archive_rows
                or late_speaker_rows
                or _legacy_export_has_rows(late_legacy)
            ):
                progress["voice.profiles"] += await delete_voice_profiles()
                progress["archive.objects"] += await delete_archive_objects()
                if self._legacy_registry is not None:
                    await self._legacy_registry.delete_for_account(account_id=account_id)
                for prefix, counts in (
                    ("archive", await self._archive_repository.delete_account(account_id)),
                    ("speaker", await self._speaker_repository.delete_account(account_id)),
                ):
                    for key, value in counts.items():
                        progress[f"{prefix}.{key}"] = progress.get(f"{prefix}.{key}", 0) + value
            profiles, references, archive_rows, speaker_rows, legacy_rows = await asyncio.gather(
                self._voice_profiles.profiles(account_id=account_id),
                self._archive_repository.object_references(account_id),
                self._archive_repository.remaining_account_rows(account_id),
                self._speaker_repository.remaining_account_rows(account_id),
                self._legacy_snapshot(account_id),
            )
            if (
                profiles
                or references
                or archive_rows
                or speaker_rows
                or _legacy_export_has_rows(legacy_rows)
            ):
                raise AccountDeletionIncompleteError(
                    "assets or projections appeared while account deletion was running"
                )
            await checkpoint("verified_empty")
        except Exception as exc:
            await checkpoint(step, last_error=type(exc).__name__)
            raise

        completed_at = datetime.now(UTC).isoformat()
        finalized = await asyncio.to_thread(
            self._memory_store.finalize_account_deletion,
            user_id=account_id,
            request_id=request_id,
            completed_at=completed_at,
            deleted_counts=progress,
        )
        return {
            "request_id": request_id,
            "status": "completed",
            "completed_at": completed_at,
            "deleted_counts": finalized,
            "terminate_sessions": True,
        }


def _empty_legacy_export() -> LegacyAccountExport:
    return LegacyAccountExport((), (), (), ())


def _legacy_export_has_rows(value: LegacyAccountExport) -> bool:
    return bool(value.grants or value.shells or value.shell_turns or value.audit_events)


def _portable_legacy_export(value: LegacyAccountExport) -> dict[str, Any]:
    def portable(item: Any) -> Any:
        if isinstance(item, datetime):
            return item.isoformat()
        if is_dataclass(item) and not isinstance(item, type):
            return portable(asdict(cast(Any, item)))
        if isinstance(item, Mapping):
            return {str(key): portable(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [portable(child) for child in item]
        return item

    return {
        "grants": portable(value.grants),
        "shells": portable(value.shells),
        "shell_turns": portable(value.shell_turns),
        "audit_events": portable(value.audit_events),
    }
