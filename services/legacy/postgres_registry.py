"""PostgreSQL 17 adapter for Legacy grants and relationship shells."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Literal, cast

import asyncpg

from services.digital_self.domain import DigitalSelfVersion
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessPurpose,
    LegacyAccessSnapshot,
    LegacyAccountExport,
    LegacyActorRole,
    LegacyAuditAction,
    LegacyAuditDecision,
    LegacyAuditEvent,
    LegacyAuditReason,
    LegacyAuditTarget,
    LegacyAuditTargetKind,
    LegacyFence,
    LegacyGrant,
    LegacyGrantConflictError,
    LegacyIdempotencyConflictError,
    LegacyManifestItemKind,
    LegacyManifestItemRef,
    LegacyNotFoundError,
    LegacyRelationshipShell,
    LegacyRelationshipSnapshot,
    LegacyRuntimeAuditAction,
    LegacyShellActorRole,
    LegacyShellTurn,
    LegacyVisibility,
    RegisteredGranteeSnapshot,
)
from services.legacy.postgres_schema import read_postgres_schema
from services.legacy.registry import (
    _DEFAULT_PREFERENCES,
    _canonical_items,
    _digest,
    _grant_digest,
    _item_dict,
    _runtime_audit_event_id,
    _validate_issue,
    _validate_preferences,
    _validate_runtime_audit_status,
    _validate_turn,
)
from services.self_model.domain import RelationshipProfile


class PostgresLegacyRegistry:
    def __init__(self, dsn: str) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("Legacy DSN must use PostgreSQL")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=15)
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL Legacy pool")
        schema = read_postgres_schema()
        try:
            async with pool.acquire() as connection:
                await connection.execute(schema)
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
            raise RuntimeError("PostgreSQL Legacy registry is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, actor_account_id: str) -> None:
        await connection.execute(
            "SELECT set_config('app.actor_account_id', $1, true)", actor_account_id
        )

    @staticmethod
    async def _lock_grant(connection: asyncpg.Connection, grant_id: str) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"memoria-legacy:{grant_id}"
        )

    async def issue(
        self,
        *,
        owner_account_id: str,
        grantee: RegisteredGranteeSnapshot,
        version: DigitalSelfVersion,
        relationship_profile: RelationshipProfile,
        allowed_items: tuple[LegacyManifestItemRef, ...],
        visibility: LegacyVisibility,
        voice_allowed: bool,
        expires_at: datetime,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyGrant:
        _validate_issue(
            owner_account_id=owner_account_id,
            grantee=grantee,
            version=version,
            relationship_profile=relationship_profile,
            allowed_items=allowed_items,
            visibility=visibility,
            expires_at=expires_at,
            now=now,
        )
        items = _canonical_items(allowed_items)
        relationship = LegacyRelationshipSnapshot(
            profile_id=relationship_profile.profile_id,
            version_number=relationship_profile.version_number,
            relationship_id=relationship_profile.relationship_id,
            salutation=relationship_profile.salutation,
            tone=relationship_profile.tone,
            advice_style=relationship_profile.advice_style,
            sharing_scope=relationship_profile.sharing_scope,
            boundaries=relationship_profile.boundaries,
        )
        scope_sha256 = _digest(
            {"visibility": visibility, "allowed_items": [_item_dict(item) for item in items]}
        )
        command = {
            "owner_account_id": owner_account_id,
            "grantee_account_id": grantee.account_id,
            "version_id": version.version_id,
            "manifest_sha256": version.manifest_sha256,
            "relationship_profile_id": relationship_profile.profile_id,
            "relationship_profile_version": relationship_profile.version_number,
            "scope_sha256": scope_sha256,
            "voice_allowed": voice_allowed,
            "expires_at": expires_at.isoformat(),
        }
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, owner_account_id)
            duplicate = await self._receipt_result(
                connection, owner_account_id, idempotency_key, "issue", command
            )
            if duplicate is not None:
                return self._grant_from_record(await self._grant_record(connection, duplicate))
            grant_id = str(uuid.uuid4())
            grant_snapshot_sha256 = _grant_digest(
                grant_id=grant_id,
                owner_account_id=owner_account_id,
                grantee_account_id=grantee.account_id,
                version_id=version.version_id,
                version_number=version.version_number,
                manifest_sha256=version.manifest_sha256,
                relationship_profile_id=relationship_profile.profile_id,
                relationship_profile_version=relationship_profile.version_number,
                relationship=relationship,
                scope_sha256=scope_sha256,
                voice_allowed=voice_allowed,
                expires_at=expires_at,
                activated_at=None,
                revoked_at=None,
                revision=1,
                created_at=now,
            )
            await connection.execute(
                """
                INSERT INTO legacy_grants (
                    grant_id, owner_account_id, grantee_account_id, version_id,
                    version_number, manifest_sha256, relationship_profile_id,
                    relationship_profile_version, relationship_id,
                    relationship_salutation, relationship_tone,
                    relationship_advice_style, relationship_sharing_scope,
                    relationship_boundaries_json, allowed_items_json, visibility,
                    scope_sha256, voice_allowed, expires_at, activated_at, revoked_at,
                    grant_snapshot_sha256, revision, created_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,
                    $15::jsonb,$16,$17,$18,$19,NULL,NULL,$20,1,$21
                )
                """,
                uuid.UUID(grant_id),
                owner_account_id,
                grantee.account_id,
                uuid.UUID(version.version_id),
                version.version_number,
                version.manifest_sha256,
                uuid.UUID(relationship_profile.profile_id),
                relationship_profile.version_number,
                uuid.UUID(relationship_profile.relationship_id),
                relationship_profile.salutation,
                relationship_profile.tone,
                relationship_profile.advice_style,
                relationship_profile.sharing_scope,
                json.dumps(relationship_profile.boundaries, ensure_ascii=False),
                json.dumps([_item_dict(item) for item in items], ensure_ascii=False),
                visibility,
                scope_sha256,
                voice_allowed,
                expires_at,
                grant_snapshot_sha256,
                now,
            )
            await self._audit(
                connection,
                grant_id=grant_id,
                owner_account_id=owner_account_id,
                grantee_account_id=grantee.account_id,
                actor_account_id=owner_account_id,
                action="issue",
                reason="issued",
                now=now,
            )
            await self._save_receipt(
                connection,
                actor_account_id=owner_account_id,
                idempotency_key=idempotency_key,
                command_type="issue",
                command=command,
                result_kind="grant",
                result_id=grant_id,
                now=now,
            )
            return self._grant_from_record(await self._grant_record(connection, grant_id))

    async def activate(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        expected_grant_snapshot_sha256: str,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyGrant:
        return await self._transition(
            actor_account_id=actor_account_id,
            grant_id=grant_id,
            expected_grant_snapshot_sha256=expected_grant_snapshot_sha256,
            idempotency_key=idempotency_key,
            action="activate",
            now=now,
        )

    async def revoke(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        expected_grant_snapshot_sha256: str,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyGrant:
        return await self._transition(
            actor_account_id=actor_account_id,
            grant_id=grant_id,
            expected_grant_snapshot_sha256=expected_grant_snapshot_sha256,
            idempotency_key=idempotency_key,
            action="revoke",
            now=now,
        )

    async def _transition(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        expected_grant_snapshot_sha256: str,
        idempotency_key: str,
        action: Literal["activate", "revoke"],
        now: datetime,
    ) -> LegacyGrant:
        command = {
            "grant_id": grant_id,
            "expected_grant_snapshot_sha256": expected_grant_snapshot_sha256,
        }
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            await self._lock_grant(connection, grant_id)
            duplicate = await self._receipt_result(
                connection, actor_account_id, idempotency_key, action, command
            )
            if duplicate is not None:
                return self._grant_from_record(await self._grant_record(connection, duplicate))
            grant = self._grant_from_record(await self._grant_record(connection, grant_id))
            if actor_account_id != grant.owner_account_id:
                raise LegacyNotFoundError(grant_id)
            if grant.grant_snapshot_sha256 != expected_grant_snapshot_sha256:
                raise LegacyGrantConflictError("legacy grant snapshot changed")
            status = grant.status_at(now)
            if action == "activate" and status != "pending":
                raise LegacyGrantConflictError(f"cannot activate a {status} legacy grant")
            if action == "revoke" and status == "revoked":
                raise LegacyGrantConflictError("legacy grant is already revoked")
            activated_at = now if action == "activate" else grant.activated_at
            revoked_at = now if action == "revoke" else None
            revision = grant.revision + 1
            snapshot_sha256 = _grant_digest(
                grant_id=grant.grant_id,
                owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                version_id=grant.version_id,
                version_number=grant.version_number,
                manifest_sha256=grant.manifest_sha256,
                relationship_profile_id=grant.relationship.profile_id,
                relationship_profile_version=grant.relationship.version_number,
                relationship=grant.relationship,
                scope_sha256=grant.scope_sha256,
                voice_allowed=grant.voice_allowed,
                expires_at=grant.expires_at,
                activated_at=activated_at,
                revoked_at=revoked_at,
                revision=revision,
                created_at=grant.created_at,
            )
            await connection.execute(
                """
                UPDATE legacy_grants SET activated_at=$1, revoked_at=$2,
                    grant_snapshot_sha256=$3, revision=$4 WHERE grant_id=$5
                """,
                activated_at,
                revoked_at,
                snapshot_sha256,
                revision,
                uuid.UUID(grant_id),
            )
            await self._audit(
                connection,
                grant_id=grant_id,
                owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                actor_account_id=actor_account_id,
                action=action,
                reason="activated" if action == "activate" else "revoked",
                now=now,
            )
            await self._save_receipt(
                connection,
                actor_account_id=actor_account_id,
                idempotency_key=idempotency_key,
                command_type=action,
                command=command,
                result_kind="grant",
                result_id=grant_id,
                now=now,
            )
            return self._grant_from_record(await self._grant_record(connection, grant_id))

    async def resolve_access(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        purpose: LegacyAccessPurpose,
        now: datetime,
    ) -> LegacyAccessSnapshot:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            grant = self._grant_from_record(await self._grant_record(connection, grant_id))
            if grant.status_at(now) in {"expired", "revoked"}:
                raise LegacyAccessDeniedError("legacy grant is not available")
            shell_id: str | None = None
            actor_role: LegacyActorRole
            if purpose == "owner_preview" and actor_account_id == grant.owner_account_id:
                actor_role = "owner_preview"
            elif purpose == "grantee_session" and actor_account_id == grant.grantee_account_id:
                if grant.status_at(now) != "active":
                    raise LegacyAccessDeniedError("legacy grant is not active")
                actor_role = "grantee"
                shell_id = (await self._ensure_shell(connection, grant, now)).shell_id
            else:
                raise LegacyNotFoundError(grant_id)
            await self._audit(
                connection,
                grant_id=grant_id,
                owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                actor_account_id=actor_account_id,
                shell_id=shell_id,
                action="resolve_access",
                reason=purpose,
                now=now,
            )
            return LegacyAccessSnapshot(
                actor_role=actor_role,
                resource_owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                grant_id=grant.grant_id,
                shell_id=shell_id,
                version_id=grant.version_id,
                version_number=grant.version_number,
                manifest_sha256=grant.manifest_sha256,
                grant_snapshot_sha256=grant.grant_snapshot_sha256,
                scope_sha256=grant.scope_sha256,
                allowed_items=grant.allowed_items,
                relationship_profile_id=grant.relationship.profile_id,
                relationship_profile_version=grant.relationship.version_number,
                voice_allowed=grant.voice_allowed,
                expires_at=grant.expires_at,
            )

    async def append_shell_turn(
        self,
        *,
        actor_account_id: str,
        shell_id: str,
        actor_role: LegacyShellActorRole,
        actual_heard_text: str,
        fence: LegacyFence,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyShellTurn:
        _validate_turn(actual_heard_text, fence)
        command = {
            "shell_id": shell_id,
            "actor_role": actor_role,
            "actual_heard_text": actual_heard_text,
            "fence": {
                "session_id": fence.session_id,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
            },
        }
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            duplicate = await self._receipt_result(
                connection, actor_account_id, idempotency_key, "append_shell_turn", command
            )
            if duplicate is not None:
                return self._turn_from_record(await self._turn_record(connection, duplicate))
            shell = self._shell_from_record(await self._shell_record(connection, shell_id))
            if actor_account_id != shell.grantee_account_id:
                raise LegacyNotFoundError(shell_id)
            grant = self._grant_from_record(await self._grant_record(connection, shell.grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            shell_turn_id = str(uuid.uuid4())
            await connection.execute(
                """
                INSERT INTO legacy_shell_turns (
                    shell_turn_id,shell_id,grant_id,actor_role,actual_heard_text,
                    session_id,turn_id,generation_id,tool_epoch,occurred_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                uuid.UUID(shell_turn_id),
                uuid.UUID(shell_id),
                uuid.UUID(shell.grant_id),
                actor_role,
                actual_heard_text,
                fence.session_id,
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
                now,
            )
            await self._audit(
                connection,
                grant_id=grant.grant_id,
                owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                actor_account_id=actor_account_id,
                shell_id=shell_id,
                shell_turn_id=shell_turn_id,
                action="append_shell_turn",
                reason="shell_turn_appended",
                now=now,
            )
            await self._save_receipt(
                connection,
                actor_account_id=actor_account_id,
                idempotency_key=idempotency_key,
                command_type="append_shell_turn",
                command=command,
                result_kind="shell_turn",
                result_id=shell_turn_id,
                now=now,
            )
            return self._turn_from_record(await self._turn_record(connection, shell_turn_id))

    async def update_shell_preferences(
        self,
        *,
        actor_account_id: str,
        shell_id: str,
        preferences: tuple[tuple[str, str], ...],
        expected_shell_revision: int,
        idempotency_key: str,
        now: datetime,
    ) -> LegacyRelationshipShell:
        canonical = _validate_preferences(preferences)
        command = {
            "shell_id": shell_id,
            "preferences": list(canonical),
            "expected_shell_revision": expected_shell_revision,
        }
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            shell = self._shell_from_record(await self._shell_record(connection, shell_id))
            if actor_account_id != shell.grantee_account_id:
                raise LegacyNotFoundError(shell_id)
            await self._lock_grant(connection, shell.grant_id)
            grant = self._grant_from_record(await self._grant_record(connection, shell.grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            duplicate = await self._receipt_result(
                connection,
                actor_account_id,
                idempotency_key,
                "update_shell_preferences",
                command,
            )
            if duplicate is not None:
                return self._shell_from_record(await self._shell_record(connection, duplicate))
            if shell.revision != expected_shell_revision:
                raise LegacyGrantConflictError("legacy shell revision changed")
            await connection.execute(
                """
                UPDATE legacy_relationship_shells
                SET preferences_json=$1::jsonb, revision=revision+1, updated_at=$2
                WHERE shell_id=$3
                """,
                json.dumps(canonical, ensure_ascii=False),
                now,
                uuid.UUID(shell_id),
            )
            await self._audit(
                connection,
                grant_id=grant.grant_id,
                owner_account_id=grant.owner_account_id,
                grantee_account_id=grant.grantee_account_id,
                actor_account_id=actor_account_id,
                shell_id=shell_id,
                action="update_shell_preferences",
                reason="shell_preferences_updated",
                now=now,
            )
            await self._save_receipt(
                connection,
                actor_account_id=actor_account_id,
                idempotency_key=idempotency_key,
                command_type="update_shell_preferences",
                command=command,
                result_kind="shell",
                result_id=shell_id,
                now=now,
            )
            return self._shell_from_record(await self._shell_record(connection, shell_id))

    async def append_runtime_audit(
        self,
        *,
        actor_account_id: str,
        grant_id: str,
        action: LegacyRuntimeAuditAction,
        decision: LegacyAuditDecision,
        reason: LegacyAuditReason,
        fence: LegacyFence | None,
        target: LegacyAuditTarget | None,
        now: datetime,
    ) -> LegacyAuditEvent:
        event_id = _runtime_audit_event_id(
            actor_account_id=actor_account_id,
            grant_id=grant_id,
            action=action,
            decision=decision,
            reason=reason,
            fence=fence,
            target=target,
        )
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            grant = self._grant_from_record(await self._grant_record(connection, grant_id))
            _validate_runtime_audit_status(
                grant,
                actor_account_id=actor_account_id,
                now=now,
            )
            shell_id = await connection.fetchval(
                "SELECT shell_id FROM legacy_relationship_shells WHERE grant_id=$1",
                uuid.UUID(grant_id),
            )
            await connection.execute(
                """
                INSERT INTO legacy_audit_events (
                    event_id,grant_id,owner_account_id,grantee_account_id,
                    actor_account_id,shell_id,shell_turn_id,session_id,turn_id,
                    generation_id,tool_epoch,target_kind,target_id,action,decision,
                    reason,occurred_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,NULL,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16
                ) ON CONFLICT (event_id) DO NOTHING
                """,
                uuid.UUID(event_id),
                uuid.UUID(grant.grant_id),
                grant.owner_account_id,
                grant.grantee_account_id,
                actor_account_id,
                shell_id,
                fence.session_id if fence is not None else None,
                fence.turn_id if fence is not None else None,
                fence.generation_id if fence is not None else None,
                fence.tool_epoch if fence is not None else None,
                target.kind if target is not None else None,
                target.target_id if target is not None else None,
                action,
                decision,
                reason,
                now,
            )
            row = await connection.fetchrow(
                "SELECT * FROM legacy_audit_events WHERE event_id=$1",
                uuid.UUID(event_id),
            )
            if row is None:  # pragma: no cover
                raise RuntimeError("failed to append Legacy runtime audit event")
            return self._audit_from_record(row)

    async def get_grant(self, *, actor_account_id: str, grant_id: str) -> LegacyGrant:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            return self._grant_from_record(await self._grant_record(connection, grant_id))

    async def list_grants(
        self,
        *,
        actor_account_id: str,
        role: Literal["owner", "grantee"],
    ) -> tuple[LegacyGrant, ...]:
        if role not in {"owner", "grantee"}:
            raise ValueError("unknown Legacy grant role")
        column = "owner_account_id" if role == "owner" else "grantee_account_id"
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            rows = await connection.fetch(
                f"SELECT * FROM legacy_grants WHERE {column}=$1 ORDER BY created_at,grant_id",
                actor_account_id,
            )
            return tuple(self._grant_from_record(row) for row in rows)

    async def get_shell(
        self, *, actor_account_id: str, shell_id: str, now: datetime
    ) -> LegacyRelationshipShell:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            shell = self._shell_from_record(await self._shell_record(connection, shell_id))
            grant = self._grant_from_record(await self._grant_record(connection, shell.grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            return shell

    async def get_shell_for_grant(
        self, *, actor_account_id: str, grant_id: str, now: datetime
    ) -> LegacyRelationshipShell | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            grant = self._grant_from_record(await self._grant_record(connection, grant_id))
            if grant.status_at(now) != "active":
                raise LegacyAccessDeniedError("legacy grant is not active")
            row = await connection.fetchrow(
                "SELECT * FROM legacy_relationship_shells WHERE grant_id=$1",
                uuid.UUID(grant_id),
            )
            return self._shell_from_record(row) if row is not None else None

    async def list_audit_events(
        self, *, actor_account_id: str, grant_id: str
    ) -> tuple[LegacyAuditEvent, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, actor_account_id)
            await self._grant_record(connection, grant_id)
            rows = await connection.fetch(
                "SELECT * FROM legacy_audit_events WHERE grant_id=$1 ORDER BY occurred_at,event_id",
                uuid.UUID(grant_id),
            )
            return tuple(self._audit_from_record(row) for row in rows)

    async def export_for_account(self, *, account_id: str) -> LegacyAccountExport:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            grants = await connection.fetch(
                """
                SELECT * FROM legacy_grants
                WHERE owner_account_id=$1 OR grantee_account_id=$1 ORDER BY created_at,grant_id
                """,
                account_id,
            )
            shells = await connection.fetch("SELECT * FROM legacy_relationship_shells")
            turns = await connection.fetch("SELECT * FROM legacy_shell_turns")
            events = await connection.fetch("SELECT * FROM legacy_audit_events")
            return LegacyAccountExport(
                tuple(self._grant_from_record(row) for row in grants),
                tuple(self._shell_from_record(row) for row in shells),
                tuple(self._turn_from_record(row) for row in turns),
                tuple(self._audit_from_record(row) for row in events),
            )

    async def delete_for_account(self, *, account_id: str) -> None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                """
                DELETE FROM legacy_command_receipts receipt
                WHERE receipt.actor_account_id=$1
                   OR (receipt.result_kind='grant' AND EXISTS (
                        SELECT 1 FROM legacy_grants related_grant
                        WHERE related_grant.grant_id=receipt.result_id
                          AND $1 IN (
                              related_grant.owner_account_id,
                              related_grant.grantee_account_id
                          )
                   ))
                   OR (receipt.result_kind='shell' AND EXISTS (
                        SELECT 1 FROM legacy_relationship_shells shell
                        WHERE shell.shell_id=receipt.result_id
                          AND $1 IN (shell.owner_account_id, shell.grantee_account_id)
                   ))
                   OR (receipt.result_kind='shell_turn' AND EXISTS (
                        SELECT 1 FROM legacy_shell_turns turn
                        JOIN legacy_grants related_grant
                          ON related_grant.grant_id=turn.grant_id
                        WHERE turn.shell_turn_id=receipt.result_id
                          AND $1 IN (
                              related_grant.owner_account_id,
                              related_grant.grantee_account_id
                          )
                   ))
                """,
                account_id,
            )
            await connection.execute(
                "DELETE FROM legacy_grants WHERE owner_account_id=$1 OR grantee_account_id=$1",
                account_id,
            )

    async def _ensure_shell(
        self, connection: asyncpg.Connection, grant: LegacyGrant, now: datetime
    ) -> LegacyRelationshipShell:
        row = await connection.fetchrow(
            "SELECT * FROM legacy_relationship_shells WHERE grant_id=$1",
            uuid.UUID(grant.grant_id),
        )
        if row is None:
            shell_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO legacy_relationship_shells (
                    shell_id,grant_id,owner_account_id,grantee_account_id,
                    preferences_json,revision,created_at,updated_at
                ) VALUES ($1,$2,$3,$4,$5::jsonb,1,$6,$6)
                ON CONFLICT (grant_id) DO NOTHING
                """,
                shell_id,
                uuid.UUID(grant.grant_id),
                grant.owner_account_id,
                grant.grantee_account_id,
                json.dumps(_DEFAULT_PREFERENCES, ensure_ascii=False),
                now,
            )
            row = await connection.fetchrow(
                "SELECT * FROM legacy_relationship_shells WHERE grant_id=$1",
                uuid.UUID(grant.grant_id),
            )
        if row is None:  # pragma: no cover
            raise RuntimeError("failed to create Legacy relationship shell")
        return self._shell_from_record(row)

    @staticmethod
    async def _grant_record(connection: asyncpg.Connection, grant_id: str) -> asyncpg.Record:
        row = await connection.fetchrow(
            "SELECT * FROM legacy_grants WHERE grant_id=$1", uuid.UUID(grant_id)
        )
        if row is None:
            raise LegacyNotFoundError(grant_id)
        return row

    @staticmethod
    async def _shell_record(connection: asyncpg.Connection, shell_id: str) -> asyncpg.Record:
        row = await connection.fetchrow(
            "SELECT * FROM legacy_relationship_shells WHERE shell_id=$1", uuid.UUID(shell_id)
        )
        if row is None:
            raise LegacyNotFoundError(shell_id)
        return row

    @staticmethod
    async def _turn_record(
        connection: asyncpg.Connection, shell_turn_id: str
    ) -> asyncpg.Record:
        row = await connection.fetchrow(
            "SELECT * FROM legacy_shell_turns WHERE shell_turn_id=$1", uuid.UUID(shell_turn_id)
        )
        if row is None:
            raise LegacyNotFoundError(shell_turn_id)
        return row

    @staticmethod
    async def _receipt_result(
        connection: asyncpg.Connection,
        actor_account_id: str,
        idempotency_key: str,
        command_type: str,
        command: object,
    ) -> str | None:
        row = await connection.fetchrow(
            """
            SELECT * FROM legacy_command_receipts
            WHERE actor_account_id=$1 AND idempotency_key=$2
            """,
            actor_account_id,
            idempotency_key,
        )
        if row is None:
            return None
        if str(row["command_type"]) != command_type or str(row["command_sha256"]) != _digest(
            command
        ):
            raise LegacyIdempotencyConflictError("Legacy idempotency key was reused")
        return str(row["result_id"])

    @staticmethod
    async def _save_receipt(
        connection: asyncpg.Connection,
        *,
        actor_account_id: str,
        idempotency_key: str,
        command_type: str,
        command: object,
        result_kind: str,
        result_id: str,
        now: datetime,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO legacy_command_receipts (
                actor_account_id,idempotency_key,command_type,command_sha256,
                result_kind,result_id,created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            actor_account_id,
            idempotency_key,
            command_type,
            _digest(command),
            result_kind,
            uuid.UUID(result_id),
            now,
        )

    @staticmethod
    async def _audit(
        connection: asyncpg.Connection,
        *,
        grant_id: str,
        owner_account_id: str,
        grantee_account_id: str,
        actor_account_id: str,
        action: LegacyAuditAction,
        reason: LegacyAuditReason,
        now: datetime,
        shell_id: str | None = None,
        shell_turn_id: str | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO legacy_audit_events (
                event_id,grant_id,owner_account_id,grantee_account_id,
                actor_account_id,shell_id,shell_turn_id,action,decision,reason,occurred_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'allowed',$9,$10)
            """,
            uuid.uuid4(),
            uuid.UUID(grant_id),
            owner_account_id,
            grantee_account_id,
            actor_account_id,
            uuid.UUID(shell_id) if shell_id else None,
            uuid.UUID(shell_turn_id) if shell_turn_id else None,
            action,
            reason,
            now,
        )

    @staticmethod
    def _grant_from_record(row: asyncpg.Record) -> LegacyGrant:
        items = cast(list[dict[str, object]], _json_value(row["allowed_items_json"]))
        boundaries = cast(list[str], _json_value(row["relationship_boundaries_json"]))
        return LegacyGrant(
            grant_id=str(row["grant_id"]),
            owner_account_id=str(row["owner_account_id"]),
            grantee_account_id=str(row["grantee_account_id"]),
            version_id=str(row["version_id"]),
            version_number=int(row["version_number"]),
            manifest_sha256=str(row["manifest_sha256"]),
            relationship=LegacyRelationshipSnapshot(
                profile_id=str(row["relationship_profile_id"]),
                version_number=int(row["relationship_profile_version"]),
                relationship_id=str(row["relationship_id"]),
                salutation=str(row["relationship_salutation"]),
                tone=str(row["relationship_tone"]),
                advice_style=str(row["relationship_advice_style"]),
                sharing_scope=str(row["relationship_sharing_scope"]),
                boundaries=tuple(str(item) for item in boundaries),
            ),
            allowed_items=tuple(
                LegacyManifestItemRef(
                    kind=cast(LegacyManifestItemKind, str(item["kind"])),
                    item_id=str(item["item_id"]),
                )
                for item in items
            ),
            visibility=cast(LegacyVisibility, str(row["visibility"])),
            scope_sha256=str(row["scope_sha256"]),
            voice_allowed=bool(row["voice_allowed"]),
            expires_at=cast(datetime, row["expires_at"]),
            activated_at=cast(datetime | None, row["activated_at"]),
            revoked_at=cast(datetime | None, row["revoked_at"]),
            grant_snapshot_sha256=str(row["grant_snapshot_sha256"]),
            revision=int(row["revision"]),
            created_at=cast(datetime, row["created_at"]),
        )

    @staticmethod
    def _shell_from_record(row: asyncpg.Record) -> LegacyRelationshipShell:
        values = cast(list[list[str]], _json_value(row["preferences_json"]))
        return LegacyRelationshipShell(
            shell_id=str(row["shell_id"]),
            grant_id=str(row["grant_id"]),
            owner_account_id=str(row["owner_account_id"]),
            grantee_account_id=str(row["grantee_account_id"]),
            preferences=tuple((str(key), str(value)) for key, value in values),
            revision=int(row["revision"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    @staticmethod
    def _turn_from_record(row: asyncpg.Record) -> LegacyShellTurn:
        return LegacyShellTurn(
            shell_turn_id=str(row["shell_turn_id"]),
            shell_id=str(row["shell_id"]),
            grant_id=str(row["grant_id"]),
            actor_role=cast(LegacyShellActorRole, str(row["actor_role"])),
            actual_heard_text=str(row["actual_heard_text"]),
            fence=LegacyFence(
                str(row["session_id"]),
                str(row["turn_id"]),
                str(row["generation_id"]),
                int(row["tool_epoch"]),
            ),
            occurred_at=cast(datetime, row["occurred_at"]),
        )

    @staticmethod
    def _audit_from_record(row: asyncpg.Record) -> LegacyAuditEvent:
        return LegacyAuditEvent(
            event_id=str(row["event_id"]),
            grant_id=str(row["grant_id"]),
            owner_account_id=str(row["owner_account_id"]),
            grantee_account_id=str(row["grantee_account_id"]),
            actor_account_id=str(row["actor_account_id"]),
            shell_id=str(row["shell_id"]) if row["shell_id"] else None,
            shell_turn_id=str(row["shell_turn_id"]) if row["shell_turn_id"] else None,
            session_id=str(row["session_id"]) if row["session_id"] else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] else None,
            generation_id=str(row["generation_id"]) if row["generation_id"] else None,
            tool_epoch=int(row["tool_epoch"]) if row["tool_epoch"] is not None else None,
            target_kind=(
                cast(LegacyAuditTargetKind, str(row["target_kind"]))
                if row["target_kind"]
                else None
            ),
            target_id=str(row["target_id"]) if row["target_id"] else None,
            action=cast(LegacyAuditAction, str(row["action"])),
            decision=cast(LegacyAuditDecision, str(row["decision"])),
            reason=cast(LegacyAuditReason, str(row["reason"])),
            occurred_at=cast(datetime, row["occurred_at"]),
        )


def _json_value(value: object) -> list[object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise ValueError("persisted Legacy JSON must be a list")
    return decoded
