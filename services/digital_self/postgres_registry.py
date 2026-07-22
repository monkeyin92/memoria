"""PostgreSQL adapter for the immutable Digital Self registry."""

from __future__ import annotations

import builtins
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import asyncpg

from services.digital_self.compiler import (
    DEFAULT_COMPILER_VERSION,
    DEFAULT_POLICY_VERSION,
    build_manifest,
    decode_manifest,
    memory_entry,
    persona_entry,
)
from services.digital_self.domain import (
    DigitalSelfVersion,
    InvalidVersionTransitionError,
    ManifestEntry,
    SourceSnapshotConflictError,
    VersionNotFoundError,
    VersionStatus,
)

_TRANSITIONS: dict[str, tuple[VersionStatus, VersionStatus]] = {
    "begin_testing": ("draft", "testing"),
    "approve": ("testing", "approved"),
    "freeze": ("approved", "frozen"),
    "revoke": ("frozen", "revoked"),
}
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class PostgresDigitalSelfRegistry:
    def __init__(
        self,
        dsn: str,
        *,
        compiler_version: str = DEFAULT_COMPILER_VERSION,
        policy_version: str = DEFAULT_POLICY_VERSION,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("digital self DSN must use PostgreSQL")
        if not compiler_version.strip() or not policy_version.strip():
            raise ValueError("digital self compiler and policy versions are required")
        self._dsn = dsn
        self._compiler_version = compiler_version
        self._policy_version = policy_version
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=15)
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL digital self pool")
        schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        try:
            async with pool.acquire() as connection:
                await connection.execute(schema)
        except Exception:
            await pool.close()
            raise
        self._pool = pool

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL digital self registry is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    @staticmethod
    async def _lock_account(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"memoria-digital-self:{account_id}",
        )

    async def build(
        self,
        *,
        account_id: str,
        parent_version_id: str | None = None,
        expected_source_summary_sha256: str | None = None,
    ) -> DigitalSelfVersion:
        self._require_account(account_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read"):
                await self._scope(connection, account_id)
                await self._lock_account(connection, account_id)
                parent = await self._parent_row(connection, account_id, parent_version_id)
                entries, persona_version_id = await self._source_entries(connection, account_id)
                manifest, manifest_bytes, manifest_sha256 = build_manifest(
                    entries,
                    compiler_version=self._compiler_version,
                    policy_version=self._policy_version,
                    persona_version_id=persona_version_id,
                    parent_version_id=(str(parent["version_id"]) if parent is not None else None),
                    expected_source_summary_sha256=expected_source_summary_sha256,
                )
                version = await self._insert(
                    connection,
                    account_id=account_id,
                    manifest_bytes=manifest_bytes,
                    manifest_sha256=manifest_sha256,
                    source_summary_sha256=manifest.source_summary.source_summary_sha256,
                    parent_version_id=manifest.parent_version_id,
                    rollback_target_version_id=None,
                )
                await self._append_lifecycle_audit(
                    connection,
                    account_id=account_id,
                    action="build",
                    version=version,
                    from_status=None,
                    to_status="draft",
                    new_version_id=version.version_id,
                )
                return version

    async def get(self, *, account_id: str, version_id: str) -> DigitalSelfVersion:
        self._require_account(account_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            return self._version_from_row(
                await self._required_row(connection, account_id, version_id)
            )

    async def list(self, *, account_id: str) -> tuple[DigitalSelfVersion, ...]:
        self._require_account(account_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM digital_self_versions
                WHERE account_id = $1 ORDER BY version_number DESC
                """,
                account_id,
            )
            return tuple(self._version_from_row(row) for row in rows)

    async def begin_testing(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return await self._transition(
            account_id=account_id,
            version_id=version_id,
            action="begin_testing",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def approve(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return await self._transition(
            account_id=account_id,
            version_id=version_id,
            action="approve",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def freeze(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return await self._transition(
            account_id=account_id,
            version_id=version_id,
            action="freeze",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def revoke(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return await self._transition(
            account_id=account_id,
            version_id=version_id,
            action="revoke",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def rollback(
        self,
        *,
        account_id: str,
        target_version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        self._require_account(account_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            target = self._version_from_row(
                await self._required_row(
                    connection,
                    account_id,
                    target_version_id,
                    for_update=True,
                )
            )
            self._check_digest(target, expected_manifest_sha256)
            parent_row = await self._parent_row(connection, account_id, None)
            parent_id = str(parent_row["version_id"]) if parent_row is not None else None
            manifest, manifest_bytes, manifest_sha256 = build_manifest(
                target.manifest.entries,
                compiler_version=self._compiler_version,
                policy_version=self._policy_version,
                persona_version_id=target.manifest.source_summary.persona_version_id,
                parent_version_id=parent_id,
                rollback_target_version_id=target.version_id,
            )
            version = await self._insert(
                connection,
                account_id=account_id,
                manifest_bytes=manifest_bytes,
                manifest_sha256=manifest_sha256,
                source_summary_sha256=manifest.source_summary.source_summary_sha256,
                parent_version_id=parent_id,
                rollback_target_version_id=target.version_id,
            )
            await self._append_lifecycle_audit(
                connection,
                account_id=account_id,
                action="rollback",
                version=version,
                from_status=None,
                to_status=version.status,
                target_version_id=target.version_id,
                new_version_id=version.version_id,
            )
            return version

    async def _transition(
        self,
        *,
        account_id: str,
        version_id: str,
        action: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        self._require_account(account_id)
        source_status, target_status = _TRANSITIONS[action]
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            current = self._version_from_row(
                await self._required_row(
                    connection,
                    account_id,
                    version_id,
                    for_update=True,
                )
            )
            self._check_digest(current, expected_manifest_sha256)
            if current.status != source_status:
                raise InvalidVersionTransitionError(
                    f"cannot {action} a digital self version in {current.status}"
                )
            row = await connection.fetchrow(
                """
                UPDATE digital_self_versions SET status = $1
                WHERE account_id = $2 AND version_id = $3
                RETURNING *
                """,
                target_status,
                account_id,
                uuid.UUID(version_id),
            )
            if row is None:  # pragma: no cover - locked row cannot disappear
                raise VersionNotFoundError(version_id)
            version = self._version_from_row(row)
            await self._append_lifecycle_audit(
                connection,
                account_id=account_id,
                action=action,
                version=version,
                from_status=source_status,
                to_status=target_status,
            )
            return version

    async def _source_entries(
        self,
        connection: asyncpg.Connection,
        account_id: str,
    ) -> tuple[builtins.list[ManifestEntry], str | None]:
        memory_rows = await connection.fetch(
            """
            SELECT claim.*
            FROM memory_claims AS claim
            JOIN archive_evidence_events AS source
              ON source.event_id = claim.source_event_id
             AND source.account_id = claim.account_id
            WHERE claim.account_id = $1
              AND claim.status = 'confirmed'
              AND source.speaker_class = 'owner'
              AND source.event_type = 'speech.utterance_finalized'
            ORDER BY claim.claim_id
            """,
            account_id,
        )
        entries: builtins.list[ManifestEntry] = [memory_entry(dict(row)) for row in memory_rows]
        persona_row = await connection.fetchrow(
            """
            SELECT version_id, snapshot
            FROM persona_versions
            WHERE account_id = $1 AND status = 'active'
            """,
            account_id,
        )
        if persona_row is None:
            return entries, None
        persona_version_id = str(persona_row["version_id"])
        snapshot = persona_row["snapshot"]
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except json.JSONDecodeError as exc:
                raise SourceSnapshotConflictError("active persona snapshot is invalid") from exc
        if not isinstance(snapshot, list):
            raise SourceSnapshotConflictError("active persona snapshot is invalid")
        seen: set[str] = set()
        for raw_item in snapshot:
            if not isinstance(raw_item, dict):
                raise SourceSnapshotConflictError("active persona snapshot is invalid")
            item = cast(dict[str, object], raw_item)
            trait_id = str(item.get("trait_id") or "")
            if not trait_id or trait_id in seen:
                raise SourceSnapshotConflictError("active persona snapshot has duplicate traits")
            seen.add(trait_id)
            try:
                trait_uuid = uuid.UUID(trait_id)
            except ValueError as exc:
                raise SourceSnapshotConflictError("active persona trait id is invalid") from exc
            trait = await connection.fetchrow(
                """
                SELECT category, description, counterexample
                FROM persona_traits
                WHERE account_id = $1 AND trait_id = $2 AND status = 'confirmed'
                """,
                account_id,
                trait_uuid,
            )
            if trait is None:
                continue
            if (
                str(trait["category"]) != str(item.get("category") or "")
                or str(trait["description"]) != str(item.get("description") or "")
                or str(trait["counterexample"]) != str(item.get("counterexample") or "")
            ):
                raise SourceSnapshotConflictError(
                    "active persona trait conflicts with its snapshot"
                )
            entry = persona_entry(item, persona_version_id=persona_version_id)
            if not entry.source_event_ids:
                continue
            evidence = await connection.fetch(
                """
                SELECT event_id, speaker_class, event_type FROM archive_evidence_events
                WHERE account_id = $1 AND event_id = ANY($2::text[])
                """,
                account_id,
                list(entry.source_event_ids),
            )
            if len(evidence) != len(entry.source_event_ids) or any(
                row["speaker_class"] != "owner"
                or row["event_type"] != "speech.utterance_finalized"
                for row in evidence
            ):
                continue
            entries.append(entry)
        return entries, persona_version_id

    async def _parent_row(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        parent_version_id: str | None,
    ) -> asyncpg.Record | None:
        if parent_version_id is not None:
            row = await self._required_row(
                connection,
                account_id,
                parent_version_id,
                for_update=True,
            )
            self._version_from_row(row)
            return row
        row = await connection.fetchrow(
            """
            SELECT * FROM digital_self_versions
            WHERE account_id = $1 ORDER BY version_number DESC LIMIT 1
            FOR UPDATE
            """,
            account_id,
        )
        if row is not None:
            self._version_from_row(row)
        return row

    async def _insert(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        manifest_bytes: bytes,
        manifest_sha256: str,
        source_summary_sha256: str,
        parent_version_id: str | None,
        rollback_target_version_id: str | None,
    ) -> DigitalSelfVersion:
        version_id = uuid.uuid4()
        version_number = int(
            await connection.fetchval(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM digital_self_versions WHERE account_id = $1
                """,
                account_id,
            )
        )
        created_at = datetime.now(UTC)
        row = await connection.fetchrow(
            """
            INSERT INTO digital_self_versions (
                version_id, account_id, version_number, status, manifest_json,
                manifest_sha256, source_summary_sha256, parent_version_id,
                rollback_target_version_id, created_at
            ) VALUES ($1, $2, $3, 'draft', $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            version_id,
            account_id,
            version_number,
            manifest_bytes.decode("utf-8"),
            manifest_sha256,
            source_summary_sha256,
            uuid.UUID(parent_version_id) if parent_version_id is not None else None,
            (
                uuid.UUID(rollback_target_version_id)
                if rollback_target_version_id is not None
                else None
            ),
            created_at,
        )
        if row is None:  # pragma: no cover
            raise RuntimeError("digital self version insert returned no row")
        return self._version_from_row(row)

    @staticmethod
    async def _append_lifecycle_audit(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        action: str,
        version: DigitalSelfVersion,
        from_status: VersionStatus | None,
        to_status: VersionStatus,
        target_version_id: str | None = None,
        new_version_id: str | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO digital_self_lifecycle_audit_events (
                event_id, account_id, actor_account_id, action, version_id,
                manifest_sha256, from_status, to_status, target_version_id,
                new_version_id, occurred_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            uuid.uuid4(),
            account_id,
            account_id,
            action,
            uuid.UUID(version.version_id),
            version.manifest_sha256,
            from_status,
            to_status,
            uuid.UUID(target_version_id) if target_version_id is not None else None,
            uuid.UUID(new_version_id) if new_version_id is not None else None,
            datetime.now(UTC),
        )

    @staticmethod
    async def _required_row(
        connection: asyncpg.Connection,
        account_id: str,
        version_id: str,
        *,
        for_update: bool = False,
    ) -> asyncpg.Record:
        try:
            parsed_id = uuid.UUID(version_id)
        except ValueError as exc:
            raise VersionNotFoundError(version_id) from exc
        suffix = " FOR UPDATE" if for_update else ""
        row = await connection.fetchrow(
            """
            SELECT * FROM digital_self_versions
            WHERE account_id = $1 AND version_id = $2
            """
            + suffix,
            account_id,
            parsed_id,
        )
        if row is None:
            raise VersionNotFoundError(version_id)
        return row

    @staticmethod
    def _version_from_row(row: asyncpg.Record) -> DigitalSelfVersion:
        parent_id = str(row["parent_version_id"]) if row["parent_version_id"] else None
        rollback_id = (
            str(row["rollback_target_version_id"]) if row["rollback_target_version_id"] else None
        )
        manifest = decode_manifest(
            str(row["manifest_json"]).encode("utf-8"),
            expected_manifest_sha256=str(row["manifest_sha256"]),
            expected_source_summary_sha256=str(row["source_summary_sha256"]),
            expected_parent_version_id=parent_id,
            expected_rollback_target_version_id=rollback_id,
        )
        return DigitalSelfVersion(
            version_id=str(row["version_id"]),
            account_id=str(row["account_id"]),
            version_number=int(row["version_number"]),
            status=cast(VersionStatus, str(row["status"])),
            manifest=manifest,
            manifest_sha256=str(row["manifest_sha256"]),
            created_at=cast(datetime, row["created_at"]),
        )

    @staticmethod
    def _check_digest(
        version: DigitalSelfVersion,
        expected_manifest_sha256: str,
    ) -> None:
        if not isinstance(expected_manifest_sha256, str) or not _SHA256_HEX.fullmatch(
            expected_manifest_sha256
        ):
            raise ValueError("expected_manifest_sha256 must be exactly 64 hexadecimal characters")
        if expected_manifest_sha256 != version.manifest_sha256:
            raise SourceSnapshotConflictError("manifest changed before transition")

    @staticmethod
    def _require_account(account_id: str) -> None:
        if not account_id.strip():
            raise ValueError("digital self account_id is required")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
